#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""One-time cleanup: delete split stems that don't match their song's audio.

vocals.ogg/instrumental.ogg are separated from a song's full mix, so they
should be the same length as it. A stem that differs by more than
--tolerance seconds came from a different rip (an earlier version of the
song that was later replaced) and plays offset against the chart's timing,
which makes the vocals-toggle feature useless for that song.

For each song folder holding stems, this compares the instrumental against
the folder's own full-mix audio and, when they disagree, deletes both stems
and strips #VOCALS/#INSTRUMENTAL from every chart in the folder.

Deliberately conservative -- a folder is left alone and reported when:
  * it has no full-mix audio file. A video is not used as the reference:
    music videos routinely carry extra footage before or after the song, so
    their duration legitimately differs from the audio and comparing against
    one would condemn perfectly good stems.
  * a chart's #MP3 points at a stem (fix_missing_mp3.py does this for songs
    that never had a full mix). Deleting the stems would leave no audio at
    all.

DELETION IS PERMANENT -- songs/ is gitignored, so removed stems cannot be
recovered from git. Defaults to a dry run; pass --write to apply. Requires
ffprobe (ffmpeg) on PATH.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

STEM_FILENAMES = ("vocals.ogg", "instrumental.ogg", "accompaniment.ogg")
AUDIO_EXTENSIONS = frozenset({".mp3", ".ogg", ".m4a", ".wav", ".flac", ".opus"})
SPLIT_AUDIO_TAGS = ("VOCALS", "INSTRUMENTAL")
# utf-8-sig decodes UTF-8 with or without a leading BOM and drops it. Charts
# are always written back as plain UTF-8, so a BOM never survives an edit.
TAG_ENCODINGS = ("utf-8-sig", "cp1252")


def read_text_preserving_encoding(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in TAG_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        # A stray BOM anywhere breaks header parsing (it is not whitespace,
        # so a line starting with one never looks like a #TAG line).
        return text.replace("﻿", ""), "utf-8" if encoding == "utf-8-sig" else encoding
    return raw.decode("utf-8", errors="replace"), "utf-8"


def audio_duration(path: Path) -> float | None:
    """Length in seconds via ffprobe, or None if it can't be determined."""
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
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def charts_in(directory: Path) -> list[Path]:
    return [p for p in sorted(directory.glob("*.txt")) if not p.name.startswith("._")]


def header_lines(text: str) -> list[str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            return lines[:i]
    return lines


def tag_value(text: str, tag: str) -> str | None:
    for line in header_lines(text):
        key, _, value = line.lstrip()[1:].partition(":")
        if key.strip().upper() == tag:
            return value.strip()
    return None


def stems_in(directory: Path) -> list[Path]:
    found = []
    for name in STEM_FILENAMES:
        for entry in directory.iterdir():
            if entry.is_file() and entry.name.lower() == name:
                found.append(entry)
    return found


def find_full_mix(directory: Path) -> Path | None:
    """The folder's full-mix audio file. Stems never count; neither do
    videos (see module docstring)."""
    stem_names = set(STEM_FILENAMES)
    for entry in sorted(directory.iterdir()):
        if (
            entry.is_file()
            and entry.suffix.lower() in AUDIO_EXTENSIONS
            and entry.name.lower() not in stem_names
        ):
            return entry
    return None


def strip_stem_tags(chart: Path, *, write: bool) -> list[str]:
    text, encoding = read_text_preserving_encoding(chart)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    kept: list[str] = []
    removed: list[str] = []
    header_len = len(header_lines(text))
    for i, line in enumerate(lines):
        if i < header_len and line.lstrip().startswith("#"):
            key = line.lstrip()[1:].partition(":")[0].strip().upper()
            if key in SPLIT_AUDIO_TAGS:
                removed.append(line.strip())
                continue
        kept.append(line)

    if removed and write:
        chart.write_bytes((newline.join(kept) + newline).encode(encoding))
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--songs-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "songs",
        help="Directory containing one song folder per subdirectory (default: ../songs)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1.0,
        help="Allowed difference in seconds between stem and full mix (default: 1.0)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually delete stems and strip tags. Without this flag, only report.",
    )
    args = parser.parse_args()

    songs_dir: Path = args.songs_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    pruned = 0
    matched = 0
    no_reference = 0
    mp3_is_stem = 0
    unmeasurable = 0
    bytes_freed = 0

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        if song_dir.name.startswith("."):
            continue
        stems = stems_in(song_dir)
        if not stems:
            continue

        charts = charts_in(song_dir)
        stem_names = {n.lower() for n in STEM_FILENAMES}
        if any(
            (tag_value(read_text_preserving_encoding(c)[0], "MP3") or "").lower() in stem_names
            for c in charts
        ):
            print(f"skip (#MP3 points at a stem): {song_dir.name}")
            mp3_is_stem += 1
            continue

        full_mix = find_full_mix(song_dir)
        if full_mix is None:
            print(f"skip (no full-mix audio to compare against): {song_dir.name}")
            no_reference += 1
            continue

        # Both stems come out of one separation run, so measuring either
        # settles it; prefer the instrumental.
        probe = next((s for s in stems if s.name.lower() != "vocals.ogg"), stems[0])
        stem_duration = audio_duration(probe)
        mix_duration = audio_duration(full_mix)
        if stem_duration is None or mix_duration is None:
            print(f"skip (could not measure {probe.name} or {full_mix.name}): {song_dir.name}")
            unmeasurable += 1
            continue

        delta = abs(stem_duration - mix_duration)
        if delta <= args.tolerance:
            matched += 1
            continue

        verb = "deleted" if args.write else "would delete"
        print(
            f"{song_dir.name}: stems {stem_duration:.1f}s vs {full_mix.name} "
            f"{mix_duration:.1f}s (off by {delta:.1f}s)"
        )
        for stem in stems:
            bytes_freed += stem.stat().st_size
            print(f"  {verb}: {stem.name}")
            if args.write:
                stem.unlink()
        for chart in charts:
            removed = strip_stem_tags(chart, write=args.write)
            for tag_line in removed:
                print(f"  {'stripped' if args.write else 'would strip'}: {chart.name} -> {tag_line}")
        pruned += 1

    mode = "write" if args.write else "dry-run"
    print(
        f"\n[{mode}] songs pruned: {pruned}, stems already matching: {matched}, "
        f"skipped (no full mix): {no_reference}, skipped (#MP3 is a stem): {mp3_is_stem}, "
        f"skipped (unmeasurable): {unmeasurable}"
    )
    print(f"[{mode}] disk space {'freed' if args.write else 'to free'}: {bytes_freed / 1e9:.2f} GB")
    if not args.write and pruned:
        print("Re-run with --write to apply. DELETION IS PERMANENT.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
