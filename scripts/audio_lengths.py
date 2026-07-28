"""Shared audio-length checks.

Split stems are separated from a song's full mix, so they should be the same
length as it. When they aren't, they came from a different rip -- an earlier
version of the song that was later replaced -- and will play offset against
the chart's timing. Several scripts need that comparison, so it lives here
rather than being written out three times:

  tag_split_audio.py       won't tag stems that disagree with the full mix
  prune_desynced_stems.py  deletes stems that disagree with it
  resolve_duplicate_songs.py
                           won't move stems onto a keeper they don't fit

This is a plain module rather than a `uv run` script. The scripts sit beside
it, so the directory holding them is already on sys.path when any of them
runs and a plain `import audio_lengths` finds it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

AUDIO_EXTENSIONS = frozenset({".mp3", ".ogg", ".m4a", ".wav", ".flac", ".opus"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mkv", ".avi", ".mov", ".mpg", ".mpeg"})
STEM_FILENAMES = frozenset({"vocals.ogg", "instrumental.ogg", "accompaniment.ogg"})

# Stems come out of a full mix, so they should match it near-exactly. A whole
# second of slack absorbs container and codec padding while still catching
# stems that came from a different rip entirely.
DEFAULT_TOLERANCE_S = 1.0

_duration_cache: dict[Path, float | None] = {}


def audio_duration(path: Path) -> float | None:
    """Length in seconds via ffprobe, or None if it can't be determined."""
    if path in _duration_cache:
        return _duration_cache[path]
    result: float | None = None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nokey=1:noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0:
            try:
                result = float(proc.stdout.strip())
            except ValueError:
                result = None
    except (OSError, subprocess.TimeoutExpired):
        result = None
    _duration_cache[path] = result
    return result


def find_full_mix(directory: Path) -> Path | None:
    """The folder's full-mix audio file, or None if it has none.

    Stems never count as the full mix, and neither does a video: a music
    video routinely carries extra footage before or after the song, so its
    duration legitimately differs and comparing against one would condemn
    perfectly good stems.
    """
    for entry in sorted(directory.iterdir()):
        if (
            entry.is_file()
            and entry.suffix.lower() in AUDIO_EXTENSIONS
            and entry.name.lower() not in STEM_FILENAMES
        ):
            return entry
    return None


def stems_in(directory: Path) -> list[Path]:
    """The folder's split stems, in a stable order."""
    return sorted(
        (p for p in directory.iterdir() if p.is_file() and p.name.lower() in STEM_FILENAMES),
        key=lambda p: p.name.lower(),
    )


def lengths_agree(
    first: Path, second: Path, tolerance: float = DEFAULT_TOLERANCE_S
) -> tuple[bool | None, str]:
    """Whether two files are the same length, with a line explaining it.

    Returns None for the verdict when either length can't be measured, so a
    caller can decide for itself whether an unmeasurable file is a reason to
    act -- rather than having that choice made for it by a bare False.
    """
    first_length = audio_duration(first)
    second_length = audio_duration(second)
    if first_length is None or second_length is None:
        unreadable = first.name if first_length is None else second.name
        return None, f"could not measure {unreadable}"
    delta = abs(first_length - second_length)
    summary = f"{first.name} {first_length:.1f}s vs {second.name} {second_length:.1f}s"
    if delta <= tolerance:
        return True, summary
    return False, f"{summary} (off by {delta:.1f}s)"


def stems_match_mix(
    directory: Path, tolerance: float = DEFAULT_TOLERANCE_S
) -> tuple[bool | None, str]:
    """Whether a folder's stems belong to its own full mix.

    Both stems come out of one separation run, so measuring either settles it
    for the pair. Returns None when there is nothing to compare -- no stems,
    or no full mix to compare them against -- which is not the same as a
    mismatch and should not be treated as one.
    """
    stems = stems_in(directory)
    if not stems:
        return None, "no stems"
    mix = find_full_mix(directory)
    if mix is None:
        return None, "no full-mix audio to compare against"
    # Prefer the instrumental: it exists under two different names, and
    # picking it keeps the reported filename stable across both.
    probe = next((s for s in stems if s.name.lower() != "vocals.ogg"), stems[0])
    return lengths_agree(probe, mix, tolerance)
