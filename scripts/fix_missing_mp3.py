#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Point every USDX chart's #MP3 tag at the best audio its folder actually has.

#MP3 is what UltraStar clients play, so it wants the folder's full mix
whenever there is one. The other options are fallbacks for folders that have
no plain audio file at all, in order:
  * the folder's full-mix audio (.mp3/.ogg/...), else
  * its video file (.mp4/.webm/.mkv/.avi), else
  * instrumental.ogg, else
  * left alone and reported as needing manual attention.

This script originally only ever *added* a missing tag, and picked the video
before it looked for audio -- on the assumption, true of the handful of
folders it was written for, that a folder needing #MP3 had nothing but split
stems. Applied to a folder that did have a full mix, that assumption put the
video in the tag while the real audio sat next to it: `Demi Lovato - Gift Of
A Friend` ended up playing a 203.1s .avi against a chart whose stems came out
of a 205.5s .mp3.

So a tag that names a video or a stem while a full mix sits beside it is now
corrected rather than left alone -- as is one naming a file that isn't there.
A video in #MP3 with no audio in the folder is the documented fallback doing
its job, and is left exactly as it is.

Defaults to a dry run; pass --write to actually modify files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import audio_lengths

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".avi")
INSTRUMENTAL_NAME = "instrumental.ogg"
STEM_NAMES = frozenset({"vocals.ogg", "instrumental.ogg", "accompaniment.ogg"})
# utf-8-sig decodes UTF-8 with or without a leading BOM and drops it. Charts
# are always written back as plain UTF-8, so a BOM never survives an edit.
TAG_ENCODINGS = ("utf-8-sig", "cp1252")


def find_case_insensitive(directory: Path, name: str) -> Path | None:
    lower = name.lower()
    for entry in directory.iterdir():
        if entry.is_file() and entry.name.lower() == lower:
            return entry
    return None


def find_video(song_dir: Path) -> Path | None:
    for entry in sorted(song_dir.iterdir()):
        if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
            return entry
    return None


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


def header_end_index(lines: list[str]) -> int:
    """Index of the first line that is not a #TAG: header line."""
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            return i
    return len(lines)


def determine_mp3_value(song_dir: Path) -> str | None:
    """The best audio reference this folder can offer, best first.

    find_full_mix already knows to skip stems and videos, so sharing it with
    the length checks keeps one definition of what counts as a folder's own
    audio rather than letting a second one drift away from it.
    """
    mix = audio_lengths.find_full_mix(song_dir)
    if mix:
        return mix.name
    video = find_video(song_dir)
    if video:
        return video.name
    instrumental = find_case_insensitive(song_dir, INSTRUMENTAL_NAME)
    if instrumental:
        return instrumental.name
    return None


def wrong_reference(song_dir: Path, current: str) -> bool:
    """Whether an existing #MP3 value should be replaced.

    Only two things are wrong: naming a file that isn't there, and naming a
    video or a stem when the folder has a full mix to name instead. A video
    with no audio beside it is the fallback working as intended.
    """
    if find_case_insensitive(song_dir, current) is None:
        return True
    suffix = Path(current).suffix.lower()
    is_second_best = suffix in VIDEO_EXTENSIONS or current.lower() in STEM_NAMES
    return is_second_best and audio_lengths.find_full_mix(song_dir) is not None


def mp3_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("#mp3:"):
            return i
    return None


def set_mp3_tag(chart: Path, value: str, *, write: bool) -> None:
    """Add #MP3, or rewrite it in place so the tag keeps its position in the
    header rather than being moved to the end by a delete-and-append."""
    text, encoding = read_text_preserving_encoding(chart)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    index = mp3_index(lines)
    if index is None:
        lines.insert(header_end_index(lines), f"#MP3:{value}")
    else:
        lines[index] = f"#MP3:{value}"

    if write:
        chart.write_bytes((newline.join(lines) + newline).encode(encoding))


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
        "--terse",
        action="store_true",
        help="Say nothing about songs it skips; the closing counts still report them.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually modify chart files. Without this flag, only report what would change.",
    )
    args = parser.parse_args()

    songs_dir: Path = args.songs_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    added = corrected = 0
    unresolved: list[Path] = []

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        for chart in sorted(song_dir.glob("*.txt")):
            if chart.name.startswith("._"):
                continue
            text, _ = read_text_preserving_encoding(chart)
            lines = text.splitlines()
            index = mp3_index(lines)
            current = lines[index].split(":", 1)[1].strip() if index is not None else None

            if current is not None and not wrong_reference(song_dir, current):
                continue

            value = determine_mp3_value(song_dir)
            if value is None:
                unresolved.append(chart)
                if not args.terse:
                    print(f"skip (no audio, video or instrumental.ogg): {chart.relative_to(songs_dir)}")
                continue
            if value == current:
                continue

            set_mp3_tag(chart, value, write=args.write)
            if current is None:
                added += 1
                verb = "added" if args.write else "would add"
                print(f"{verb}: {chart.relative_to(songs_dir)} -> #MP3:{value}")
            else:
                corrected += 1
                verb = "corrected" if args.write else "would correct"
                print(f"{verb}: {chart.relative_to(songs_dir)} -> #MP3:{current} -> #MP3:{value}")

    mode = "write" if args.write else "dry-run"
    fixed = added + corrected
    print(
        f"\n[{mode}] charts given a #MP3: {added}, charts pointed at better audio: {corrected}, "
        f"unresolved (nothing to point at): {len(unresolved)}"
    )
    if not args.write and fixed:
        print("Re-run with --write to apply changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
