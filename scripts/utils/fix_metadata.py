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


def update(song_dir: Path, data: dict) -> bool:
    """Shallow-merge data into fix-metadata.json. Returns True if anything changed.

    Each top-level key in `data` replaces (not deep-merges) the same key in
    the file, so callers write whole sections at once:
        update(song_dir, {"replaygain": {"track_gain_db": -9.9, "track_peak": 0.998}})
    Other existing sections are preserved unchanged.
    """
    current = read(song_dir)
    merged = {**current, **data}
    if merged == current:
        return False
    _write(song_dir, merged)
    return True


def probe_audio(path: Path) -> dict | None:
    """Run ffprobe on path and return a dict of relevant fields, or None on failure.

    Returned keys (all present if ffprobe can report them):
        filename, duration_s, codec, sample_rate_hz, channels,
        bit_rate_kbps, size_bytes
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_name,codec_type,sample_rate,channels,bit_rate",
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


def backfill_audio(songs_dir: Path, *, overwrite: bool = False) -> int:
    """Probe each song's primary audio and write an 'audio' section.

    Skips songs that already have an 'audio' section unless overwrite=True.
    Returns the count of songs whose fix-metadata.json was updated.
    """
    from utils import audio_lengths

    updated = 0
    for song_dir in sorted(
        p for p in songs_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ):
        if not overwrite and "audio" in read(song_dir):
            continue
        mix = audio_lengths.find_full_mix(song_dir)
        if mix is None:
            continue
        if set_audio(song_dir, mix):
            updated += 1
    return updated
