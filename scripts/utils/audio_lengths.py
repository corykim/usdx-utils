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

This is a plain module rather than a `uv run` script, so it lives in
scripts/utils/ with the other import-only modules. A script run as
`uv run scripts/<name>.py` has scripts/ on sys.path, which makes
`from utils import audio_lengths` resolve without any sys.path fixing.
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
from pathlib import Path

AUDIO_EXTENSIONS = frozenset({".mp3", ".ogg", ".m4a", ".wav", ".flac", ".opus"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mkv", ".avi", ".mov", ".mpg", ".mpeg"})
STEM_FILENAMES = frozenset({"vocals.ogg", "instrumental.ogg", "accompaniment.ogg"})

# Stems come out of a full mix, so they should match it near-exactly. A whole
# second of slack absorbs container and codec padding while still catching
# stems that came from a different rip entirely.
DEFAULT_TOLERANCE_S = 1.0

# Measuring a library's worth of audio takes minutes, and the answer only
# changes when a file does, so it is kept between runs. Set
# AUDIO_LENGTH_CACHE to move it, or to an empty value to measure every time.
CACHE_PATH = Path(
    os.environ.get(
        # Beside this module, so the thing that owns the cache is the thing
        # that sits with it -- and moving the module cannot silently strand
        # the file at a path derived by counting parents.
        "AUDIO_LENGTH_CACHE",
        Path(__file__).resolve().parent / ".audio-lengths.json",
    )
)

_durations: dict[str, float] | None = None
_unsaved = False


def _cache_key(path: Path) -> str | None:
    """Identifies a file by where it is and how big it is, so replacing one
    with a different recording invalidates the entry by itself. Returns None
    when the file cannot be measured for size, leaving it uncacheable."""
    try:
        return f"{path.resolve()}|{path.stat().st_size}"
    except OSError:
        return None


def _load() -> dict[str, float]:
    global _durations
    if _durations is None:
        try:
            stored = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            _durations = {
                key: float(value)
                for key, value in stored.items()
                if isinstance(value, (int, float))
            }
        except (OSError, ValueError, AttributeError):
            # No cache yet, or one written by something else. Start over
            # rather than fail: it is an optimization, not a source of truth.
            _durations = {}
    return _durations


def _still_on_disk(key: str) -> bool:
    """Whether a cached key still describes a file that is there, at that
    size. Replacing a file leaves its old entry behind, so they are dropped
    on the way out rather than accumulating for the life of the library."""
    path, _, size = key.rpartition("|")
    try:
        return path and Path(path).stat().st_size == int(size)
    except (OSError, ValueError):
        return False


def _save(*, prune: bool = True) -> None:
    global _unsaved
    if not _unsaved or not _durations or not str(CACHE_PATH):
        return
    try:
        entries = _durations
        if prune:
            entries = {key: value for key, value in entries.items() if _still_on_disk(key)}
        # Write beside the target and swap, so a run interrupted mid-write
        # cannot leave a truncated cache behind.
        staged = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".partial")
        staged.write_text(json.dumps(entries, indent=0, sort_keys=True), encoding="utf-8")
        staged.replace(CACHE_PATH)
        _unsaved = False
    except OSError:
        pass  # a cache that cannot be written is not worth failing a run over


atexit.register(_save)

# Measuring a whole library takes long enough that someone will interrupt it,
# so the cache is written as it fills rather than only at the end -- otherwise
# a run stopped at minute nineteen would have nothing to show for it. Every
# hundred misses, not every one: rewriting the file per probe would trade one
# cost for another. Stale entries are only weeded out on the way out, to keep
# these interim writes cheap.
FLUSH_EVERY = 100
_since_flush = 0


def audio_duration(path: Path) -> float | None:
    """Length in seconds via ffprobe, or None if it can't be determined."""
    global _unsaved
    cached = _load()
    key = _cache_key(path)
    if key is not None and key in cached:
        return cached[key]

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
            text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if proc.returncode == 0:
            try:
                result = float(proc.stdout.strip())
            except ValueError:
                result = None
    except (OSError, subprocess.TimeoutExpired):
        result = None

    # Only successes are kept. A failure may be a missing codec or a busy
    # machine, and remembering it would make one bad run permanent.
    if result is not None and key is not None:
        global _since_flush
        cached[key] = result
        _unsaved = True
        _since_flush += 1
        if _since_flush >= FLUSH_EVERY:
            _since_flush = 0
            _save(prune=False)
    return result


def cached_duration(path: Path) -> float | None:
    """Return the cached duration for path without running ffprobe.

    Returns None when the file has not been measured yet, or when its current
    size differs from what was measured (implying a replacement).
    """
    key = _cache_key(path)
    if key is None:
        return None
    return _load().get(key)


def declared_audio(directory: Path) -> Path | None:
    """The audio file the folder's charts name in #MP3, if it is there.

    Returns None when no chart names one, or when the name points at a video
    or a stem -- those are #MP3's documented fallbacks for folders with no
    audio, not a full mix.
    """
    for chart in sorted(directory.glob("*.txt")):
        if chart.name.startswith("._"):  # macOS AppleDouble sidecar file
            continue
        try:
            raw = chart.read_bytes()
        except OSError:
            continue
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            continue
        for line in text.replace("﻿", "").splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith("#mp3:"):
                continue
            named = stripped.split(":", 1)[1].strip().lower()
            if not named or named in STEM_FILENAMES:
                break
            if Path(named).suffix.lower() not in AUDIO_EXTENSIONS:
                break
            for entry in directory.iterdir():
                if entry.is_file() and entry.name.lower() == named:
                    return entry
            break
    return None


def has_audio_stream(path: Path) -> bool | None:
    """Whether the file actually carries audio. None if it cannot be probed.

    A container's extension promises nothing. `fix_missing_video.py` fetches
    `-f bestvideo` on purpose -- UltraStar plays a background video muted --
    so the .mp4 it leaves behind holds a video stream and nothing else.
    Anything treating "there is a video file" as "there is audio" points
    #MP3 at silence, which is how 11 songs here ended up unplayable.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
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
    return "audio" in proc.stdout


def forget(path: Path) -> bool:
    """Drop any cached duration for a path. True if there was one.

    Deliberately matches on the path alone rather than the path+size key
    audio_duration() writes: a caller deleting a file cannot read its size
    afterwards, so a key rebuilt at that point would never match and the
    entry would linger until some later run happened to notice the file was
    gone. Removing every size for the path also covers a file replaced more
    than once between cache writes.
    """
    global _unsaved
    cached = _load()
    wanted = str(path.resolve())
    stale = [key for key in cached if key.rpartition("|")[0] == wanted]
    for key in stale:
        del cached[key]
    if stale:
        _unsaved = True
    return bool(stale)


def find_full_mix(directory: Path) -> Path | None:
    """The folder's full-mix audio file, or None if it has none.

    Stems never count as the full mix, and neither does a video: a music
    video routinely carries extra footage before or after the song, so its
    duration legitimately differs and comparing against one would condemn
    perfectly good stems.

    **What the chart names in #MP3 wins**, because that is the audio the game
    actually plays and the audio the notes are timed against; only when no
    chart names one does the first audio file alphabetically stand in. 51
    folders here hold two candidates, usually an .mp3 and an .ogg left over
    from a re-rip, and 35 of those pairs are different recordings -- one is
    92s longer than the other. Picking by filename order meant measuring
    stems against a rip the chart never plays, which quietly inverts the
    safety check every caller here relies on: `prune_desynced_stems` would
    delete stems for disagreeing with the wrong file, and `tag_split_audio`
    would bless stems that disagree with the right one.
    """
    declared = declared_audio(directory)
    if declared is not None:
        return declared
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
