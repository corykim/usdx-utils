"""Per-song metadata sidecar (fix-metadata.json).

Stores information that scripts derive or receive but that would otherwise be
lost between runs: the YouTube video id used for a background video, a
measured ReplayGain value, and the ffprobe summary of the primary audio file.

Individual sections are merged in at the top level rather than the file being
overwritten in full, so adding one piece of information cannot silently erase
another.

Typical structure:

    {
      "video_id": "SXKlJuO07eM",
      "replaygain": {
        "track_gain_db": -9.90,
        "track_peak": 0.997600
      },
      "audio": {
        "filename": "Song Name.ogg",
        "duration_s": 234.567,
        "codec": "vorbis",
        "sample_rate_hz": 44100,
        "channels": 2,
        "bit_rate_kbps": 192.0,
        "size_bytes": 12345678
      }
    }

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


# Sections whose values were derived from the audio file and must be dropped
# when that file is replaced or re-encoded.
_FILE_DEPENDENT_SECTIONS = frozenset({"audio", "replaygain"})


def _drop_stale_sections(song_dir: Path, current: dict) -> tuple[dict, bool]:
    """If the recorded audio file size no longer matches, drop file-dependent sections.

    Returns (possibly-cleaned dict, True if anything was dropped).
    The size check uses audio.filename and audio.size_bytes, which probe_audio
    writes on every successful probe. If either is absent the metadata is left
    alone -- we can't verify what we don't have recorded.
    """
    audio = current.get("audio", {})
    filename = audio.get("filename")
    stored_size = audio.get("size_bytes")
    if not filename or stored_size is None:
        return current, False
    try:
        actual_size = (song_dir / filename).stat().st_size
    except OSError:
        return current, False  # file gone or unreadable; don't invalidate blindly
    if actual_size == stored_size:
        return current, False
    cleaned = {k: v for k, v in current.items() if k not in _FILE_DEPENDENT_SECTIONS}
    return cleaned, True


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
    """Run ffprobe on path and return a dict of relevant fields, or None on failure.

    Returned keys (all present if ffprobe can report them):
        filename, duration_s, codec, sample_rate_hz, sample_rate_khz,
        channels, bits_per_sample, sample_format, bit_rate_kbps, size_bytes

    bits_per_sample is only present for lossless formats (WAV, FLAC, etc.);
    lossy codecs (Vorbis, MP3, AAC) don't have a raw bit depth.
    sample_format is the internal decoded format, e.g. "fltp" (32-bit float
    planar, used by Vorbis/AAC), "s16" (signed 16-bit PCM), "s32".
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "stream=codec_name,codec_type,sample_rate,channels,"
                "bit_rate,bits_per_raw_sample,sample_fmt",
                "-show_entries", "format=duration,size,bit_rate",
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

    result: dict = {"filename": path.name}

    for raw_val, out_key, coerce in [
        (fmt.get("duration"),            "duration_s",     lambda v: round(float(v), 3)),
        (audio_stream.get("codec_name"), "codec",          str),
        (audio_stream.get("sample_rate"),"sample_rate_hz", int),
        (audio_stream.get("channels"),   "channels",       int),
        (fmt.get("size"),                "size_bytes",     int),
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


def set_video_id(song_dir: Path, video_id: str) -> bool:
    """Set the video_id field. Returns True if the file changed."""
    return update(song_dir, {"video_id": video_id})


def set_replaygain(song_dir: Path, gain_db: float, peak: float | None) -> bool:
    """Set the replaygain section. Returns True if the file changed."""
    data: dict = {"track_gain_db": round(gain_db, 2)}
    if peak is not None:
        data["track_peak"] = round(peak, 6)
    return update(song_dir, {"replaygain": data})


def set_audio(song_dir: Path, audio_path: Path) -> bool:
    """Probe audio_path with ffprobe and store the result under 'audio'.

    Returns True if the stored data changed.
    """
    info = probe_audio(audio_path)
    if info is None:
        return False
    return update(song_dir, {"audio": info})


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
