"""Per-song metadata sidecar (fix-metadata.json).

Stores information that scripts derive or receive but that would otherwise be
lost between runs: the YouTube video id used for a background video, a
measured ReplayGain value, and the ffprobe summary of the primary audio file.

Individual sections are merged in at the top level rather than the file being
overwritten in full, so adding one piece of information cannot silently erase
another.

Typical structure:

    {
      "filename": "Song Name.ogg",
      "size_bytes": 12345678,
      "replaygain": {
        "track_gain_db": -9.90,
        "track_peak": 0.997600
      },
      "audio": {
        "duration_s": 234.567,
        "codec": "vorbis",
        "sample_rate_hz": 44100,
        "sample_rate_khz": 44.1,
        "channels": 2,
        "sample_format": "fltp",
        "bit_rate_kbps": 192.0
      },
      "video": {
        "id": "SXKlJuO07eM",
        "filename": "Song Name.mp4",
        "size_bytes": 73456789,
        "duration_s": 252.32,
        "codec": "h264",
        "width": 1920,
        "height": 1080,
        "fps": 25.0,
        "bit_rate_kbps": 2313.7,
        "pixel_format": "yuv420p",
        "major_brand": "mp42"
      }
    }

filename and size_bytes (root-level) are attributes of the primary audio
file. The video section has its own filename and size_bytes. Both use a
staleness check: on every write the recorded size is compared against the
real file; if they differ, file-derived fields are cleared (audio and
replaygain for audio; all video fields except id for video).

To pre-populate the audio section for existing songs, call backfill_audio():

    from utils import fix_metadata
    updated = fix_metadata.backfill_audio(songs_dir)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

FILENAME = "fix-metadata.json"


def read(song_dir: Path) -> dict:
    """Read fix-metadata.json, returning {} if absent or unreadable."""
    try:
        return json.loads((song_dir / FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(song_dir: Path, data: dict) -> None:
    """Write data to fix-metadata.json, staging beside the target to be safe."""
    path = song_dir / FILENAME
    staged = path.with_suffix(".json.partial")
    staged.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    staged.replace(path)


# Keys cleared when the audio file is found to have changed. filename and
# size_bytes are included so a stale check doesn't keep re-firing on every
# subsequent write until set_audio() is called to refresh them.
_FILE_DEPENDENT_KEYS = frozenset({"filename", "size_bytes", "audio", "replaygain"})


def _drop_stale_sections(song_dir: Path, current: dict) -> tuple[dict, bool]:
    """Clear file-derived sections when the recorded file sizes no longer match.

    Checks both audio (root filename/size_bytes) and video (video.filename/
    video.size_bytes). Audio staleness drops filename, size_bytes, audio, and
    replaygain. Video staleness drops all video probe fields but keeps video.id
    so the YouTube id is not lost when the file is replaced.

    Returns (possibly-cleaned dict, True if anything was dropped).
    The check is skipped for a section when the size has not been recorded yet.
    Files that are gone or unreadable are left alone rather than invalidated
    blindly, since a transient error should not wipe measurements.
    """
    invalidated = False

    # Audio staleness
    filename = current.get("filename")
    stored_size = current.get("size_bytes")
    if filename and stored_size is not None:
        try:
            actual_size = (song_dir / filename).stat().st_size
        except OSError:
            pass
        else:
            if actual_size != stored_size:
                current = {k: v for k, v in current.items() if k not in _FILE_DEPENDENT_KEYS}
                invalidated = True

    # Video staleness: clear probe data, preserve id
    video = current.get("video")
    if isinstance(video, dict):
        vfilename = video.get("filename")
        vstored_size = video.get("size_bytes")
        if vfilename and vstored_size is not None:
            try:
                actual_vsize = (song_dir / vfilename).stat().st_size
            except OSError:
                pass
            else:
                if actual_vsize != vstored_size:
                    kept_id = video.get("id")
                    new_video: dict = {"id": kept_id} if kept_id else {}
                    if new_video:
                        current = {**current, "video": new_video}
                    else:
                        current = {k: v for k, v in current.items() if k != "video"}
                    invalidated = True

    return current, invalidated


def update(song_dir: Path, data: dict) -> bool:
    """Shallow-merge data into fix-metadata.json. Returns True if anything changed.

    Each top-level key in `data` replaces (not deep-merges) the same key in
    the file, so callers write whole sections at once:
        update(song_dir, {"replaygain": {"track_gain_db": -9.9, "track_peak": 0.998}})
    Other existing sections are preserved unchanged.

    Before merging, the stored audio file size is compared against the real
    file. If they differ, file-dependent sections (audio, replaygain) are
    cleared first so stale data from a replaced file cannot persist.
    """
    current = read(song_dir)
    current, invalidated = _drop_stale_sections(song_dir, current)
    merged = {**current, **data}
    if not invalidated and merged == current:
        return False
    _write(song_dir, merged)
    return True


def probe_audio(path: Path) -> dict | None:
    """Run ffprobe on path and return audio properties, or None on failure.

    Returns only content derived from decoding the stream -- not filename or
    size, which are filesystem attributes and are stored at the root by
    set_audio() without needing ffprobe.

    Returned keys (present when ffprobe can report them):
        duration_s, codec, sample_rate_hz, sample_rate_khz,
        channels, bits_per_sample, sample_format, bit_rate_kbps

    bits_per_sample is only present for lossless formats (WAV, FLAC, etc.).
    sample_format is the internal decoded format: "fltp" (32-bit float planar,
    used by Vorbis/AAC), "s16" (signed 16-bit PCM), "s32", etc.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "stream=codec_name,codec_type,sample_rate,channels,"
                "bit_rate,bits_per_raw_sample,sample_fmt",
                "-show_entries", "format=duration,bit_rate",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        raw = json.loads(proc.stdout)
    except ValueError:
        return None

    fmt = raw.get("format", {})
    audio_stream = next(
        (s for s in raw.get("streams", []) if s.get("codec_type") == "audio"),
        {},
    )

    result: dict = {}

    for raw_val, out_key, coerce in [
        (fmt.get("duration"),            "duration_s",     lambda v: round(float(v), 3)),
        (audio_stream.get("codec_name"), "codec",          str),
        (audio_stream.get("sample_rate"),"sample_rate_hz", int),
        (audio_stream.get("channels"),   "channels",       int),
    ]:
        if raw_val is not None:
            try:
                result[out_key] = coerce(raw_val)
            except (ValueError, TypeError):
                pass

    # Derive kHz from Hz once we have it.
    if "sample_rate_hz" in result:
        result["sample_rate_khz"] = round(result["sample_rate_hz"] / 1000, 1)

    # bits_per_raw_sample is absent or "0" for lossy codecs; skip those.
    bps_raw = audio_stream.get("bits_per_raw_sample")
    if bps_raw and str(bps_raw) not in ("N/A", "0", ""):
        try:
            result["bits_per_sample"] = int(bps_raw)
        except (ValueError, TypeError):
            pass

    # sample_fmt is always present for audio streams ("fltp", "s16", "s32p", …).
    sample_fmt = audio_stream.get("sample_fmt")
    if sample_fmt and str(sample_fmt) not in ("N/A", ""):
        result["sample_format"] = str(sample_fmt)

    # Stream-level bitrate is often missing (e.g. Vorbis); format-level is reliable.
    for br_raw in (audio_stream.get("bit_rate"), fmt.get("bit_rate")):
        if br_raw and str(br_raw) not in ("N/A", "0", ""):
            try:
                result["bit_rate_kbps"] = round(int(br_raw) / 1000, 1)
                break
            except (ValueError, TypeError):
                pass

    return result


def probe_video(path: Path) -> dict | None:
    """Run ffprobe on a video file and return video stream properties, or None on failure.

    Returned keys (present when ffprobe can report them):
        duration_s, codec, width, height, fps, bit_rate_kbps,
        pixel_format, color_space, major_brand
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "stream=codec_name,codec_type,width,height,r_frame_rate,"
                "bit_rate,pix_fmt,color_space",
                "-show_entries", "format=duration,bit_rate",
                "-show_entries", "format_tags=major_brand",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        raw = json.loads(proc.stdout)
    except ValueError:
        return None

    fmt = raw.get("format", {})
    fmt_tags = fmt.get("tags", {})
    video_stream = next(
        (s for s in raw.get("streams", []) if s.get("codec_type") == "video"),
        {},
    )

    result: dict = {}

    if codec := video_stream.get("codec_name"):
        result["codec"] = str(codec)

    for key in ("width", "height"):
        val = video_stream.get(key)
        if val is not None:
            try:
                result[key] = int(val)
            except (ValueError, TypeError):
                pass

    fps_raw = video_stream.get("r_frame_rate")
    if fps_raw and "/" in str(fps_raw):
        try:
            num, den = str(fps_raw).split("/", 1)
            if int(den):
                result["fps"] = round(int(num) / int(den), 3)
        except (ValueError, ZeroDivisionError):
            pass

    for dur_raw in (fmt.get("duration"), video_stream.get("duration")):
        if dur_raw is not None:
            try:
                result["duration_s"] = round(float(dur_raw), 3)
                break
            except (ValueError, TypeError):
                pass

    for br_raw in (video_stream.get("bit_rate"), fmt.get("bit_rate")):
        if br_raw and str(br_raw) not in ("N/A", "0", ""):
            try:
                result["bit_rate_kbps"] = round(int(br_raw) / 1000, 1)
                break
            except (ValueError, TypeError):
                pass

    pix_fmt = video_stream.get("pix_fmt")
    if pix_fmt and str(pix_fmt) not in ("N/A", ""):
        result["pixel_format"] = str(pix_fmt)

    color_space = video_stream.get("color_space")
    if color_space and str(color_space) not in ("N/A", "unknown", ""):
        result["color_space"] = str(color_space)

    brand = fmt_tags.get("major_brand")
    if brand and str(brand) not in ("N/A", ""):
        result["major_brand"] = str(brand)

    return result or None


def set_video_id(song_dir: Path, video_id: str) -> bool:
    """Set video.id without probing the file. Returns True if the file changed."""
    current = read(song_dir)
    existing_video = current.get("video")
    if isinstance(existing_video, dict):
        updated_video: dict = {**existing_video, "id": video_id}
    else:
        updated_video = {"id": video_id}
    return update(song_dir, {"video": updated_video})


def set_video(song_dir: Path, video_path: Path, video_id: str | None = None) -> bool:
    """Probe video_path and store video properties + file identity + optional id.

    All properties go under the 'video' key. An existing video.id is preserved
    when video_id is not supplied. Returns True if anything changed.
    """
    info = probe_video(video_path) or {}
    data: dict = {"filename": video_path.name}
    try:
        data["size_bytes"] = video_path.stat().st_size
    except OSError:
        pass
    data.update(info)

    if video_id is not None:
        data["id"] = video_id
    else:
        existing_video = read(song_dir).get("video")
        if isinstance(existing_video, dict) and (eid := existing_video.get("id")):
            data["id"] = eid

    return update(song_dir, {"video": data})


def set_replaygain(song_dir: Path, gain_db: float, peak: float | None) -> bool:
    """Set the replaygain section. Returns True if the file changed."""
    data: dict = {"track_gain_db": round(gain_db, 2)}
    if peak is not None:
        data["track_peak"] = round(peak, 6)
    return update(song_dir, {"replaygain": data})


def set_audio(song_dir: Path, audio_path: Path) -> bool:
    """Probe audio_path and store audio properties + file identity.

    filename and size_bytes are read from the filesystem and stored at the
    root (they describe the file, not its audio content). The ffprobe-derived
    properties go under the 'audio' key. Returns True if anything changed.
    """
    info = probe_audio(audio_path)
    if info is None:
        return False
    data: dict = {"audio": info, "filename": audio_path.name}
    try:
        data["size_bytes"] = audio_path.stat().st_size
    except OSError:
        pass
    return update(song_dir, data)


def migrate(songs_dir: Path) -> int:
    """Migrate fix-metadata.json files to the current schema.

    1. Pulls filename and size_bytes out of the audio section (old location)
       and writes them at the top level.
    2. Moves a root-level video_id into video.id.

    Returns the count of files changed.
    """
    updated = 0
    for song_dir in sorted(
        p for p in songs_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ):
        current = read(song_dir)
        if not current:
            continue
        changed = False

        # Migration 1: filename/size_bytes from audio section -> root
        audio = current.get("audio")
        if isinstance(audio, dict):
            for key in ("filename", "size_bytes"):
                if key in audio and key not in current:
                    current[key] = audio.pop(key)
                    changed = True
            if not audio:
                del current["audio"]

        # Migration 2: root video_id -> video.id
        if "video_id" in current:
            root_vid = current.pop("video_id")
            changed = True
            existing_video = current.get("video")
            if isinstance(existing_video, dict):
                if "id" not in existing_video:
                    current["video"] = {"id": root_vid, **existing_video}
            else:
                current["video"] = {"id": root_vid}

        if not changed:
            continue
        _write(song_dir, current)
        updated += 1
    return updated


def _read_replaygain_tags(path: Path) -> tuple[float, float | None] | None:
    """Read ReplayGain track gain and peak from audio tags, or None if absent.

    Handles the three container spellings:
      Vorbis comment   replaygain_track_gain / replaygain_track_peak
      ID3 TXXX frame   TXXX:REPLAYGAIN_TRACK_GAIN / …_PEAK
      MP4 freeform     ----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN / …_PEAK

    The matching rule (last colon-separated segment, case-insensitive) covers
    all three without special-casing each container.
    """
    try:
        import mutagen
        tags = mutagen.File(path)
    except Exception:
        return None
    if tags is None:
        return None
    try:
        items = dict(tags)
    except Exception:
        return None

    def _find(suffix: str) -> float | None:
        for key, value in items.items():
            if str(key).lower().rsplit(":", 1)[-1] != suffix:
                continue
            raw = value
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            if raw is None:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            text = getattr(raw, "text", None)
            if text is not None:
                raw = text[0] if isinstance(text, list) and text else str(text)
            try:
                return float(str(raw).strip().split()[0])
            except (ValueError, IndexError):
                pass
        return None

    gain = _find("replaygain_track_gain")
    if gain is None:
        return None
    peak = _find("replaygain_track_peak")
    return gain, peak


def _find_declared_video(song_dir: Path) -> Path | None:
    """Return the video file named in any chart's #VIDEO tag, or None."""
    for chart in sorted(song_dir.glob("*.txt")):
        if chart.name.startswith("._"):
            continue
        try:
            text = chart.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.lstrip().startswith("#"):
                break
            key, _, value = line.lstrip()[1:].partition(":")
            if key.strip().upper() == "VIDEO":
                named = value.strip()
                if named:
                    candidate = song_dir / named
                    if candidate.is_file():
                        return candidate
                break
    return None


def _usdb_video_id(song_dir: Path) -> str | None:
    """Read video_id from .usdb marker meta_tags (v= preferred, a= fallback)."""
    for marker in sorted(song_dir.glob("*.usdb")):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        sources: dict[str, str] = {}
        for token in str(payload.get("meta_tags", "")).split(","):
            if "=" in token:
                k, _, v = token.partition("=")
                k, v = k.strip(), v.strip()
                if k in ("v", "a") and v:
                    sources[k] = v
        result = sources.get("v") or sources.get("a")
        if result:
            return result
    return None


def record_basics(song_dir: Path, audio_path: Path | None = None) -> bool:
    """Record filename/size, cached duration, and video_id from .usdb — no ffprobe.

    audio_path: the primary audio file; pass None to discover it from the
    chart's #MP3 tag. Returns True if fix-metadata.json changed.
    """
    from utils import audio_lengths as _al

    changed = False

    if audio_path is None:
        audio_path = _al.find_full_mix(song_dir)

    if audio_path is not None and audio_path.is_file():
        try:
            size = audio_path.stat().st_size
        except OSError:
            size = None
        if size is not None:
            data: dict = {"filename": audio_path.name, "size_bytes": size}
            current = read(song_dir)
            if "duration_s" not in current.get("audio", {}):
                dur = _al.cached_duration(audio_path)
                if dur is not None:
                    data["audio"] = {**current.get("audio", {}), "duration_s": dur}
            changed = update(song_dir, data) or changed

    current = read(song_dir)
    if not (isinstance(current.get("video"), dict) and current["video"].get("id")):
        vid = _usdb_video_id(song_dir)
        if vid:
            existing_video = current.get("video")
            if isinstance(existing_video, dict):
                updated_video: dict = {**existing_video, "id": vid}
            else:
                updated_video = {"id": vid}
            changed = update(song_dir, {"video": updated_video}) or changed

    return changed


def backfill_audio(songs_dir: Path, *, overwrite: bool = False) -> int:
    """Probe each song's primary audio and populate audio + replaygain sections.

    For each song folder: probes the full mix with ffprobe (audio section) and
    reads any existing ReplayGain tags from it (replaygain section). Skips
    sections that are already present unless overwrite=True. Returns the count
    of songs whose fix-metadata.json was updated.

    mutagen is used for the ReplayGain read; if it is not installed that
    section is silently skipped and only the audio probe is written.
    """
    from utils import audio_lengths

    updated = 0
    for song_dir in sorted(
        p for p in songs_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ):
        existing = read(song_dir)
        needs_audio = overwrite or "audio" not in existing
        needs_replaygain = overwrite or "replaygain" not in existing
        if not needs_audio and not needs_replaygain:
            continue

        mix = audio_lengths.find_full_mix(song_dir)
        if mix is None:
            continue

        changed = False
        if needs_audio:
            changed = set_audio(song_dir, mix) or changed
        if needs_replaygain:
            rg = _read_replaygain_tags(mix)
            if rg is not None:
                gain_db, peak = rg
                changed = set_replaygain(song_dir, gain_db, peak) or changed

        if changed:
            updated += 1
    return updated
