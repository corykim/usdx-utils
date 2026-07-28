#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""List songs with no usable background video.

Three separate problems get counted, since they need different fixes:

  none      the folder has no video file at all -- needs one sourced
  broken    a chart declares #VIDEO but names a file that isn't there --
            the video is gone, or was renamed without updating the chart
  untagged  a video file is sitting in the folder but no chart declares
            #VIDEO, so UltraStar never plays it -- fixable in place

Prints one entry per line, sorted, to stdout with a summary of all three on
stderr, so it can be redirected straight to a file:

    uv run scripts/find_missing_video.py > video-missing.txt

--category picks which list goes to stdout (default: none). Dot-directories
are skipped; they aren't songs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mkv", ".avi", ".mov", ".mpg", ".mpeg"})
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


def header_end_index(lines: list[str]) -> int:
    """Index of the first line that is not a #TAG: header line."""
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            return i
    return len(lines)


def add_video_tag(chart: Path, filename: str) -> None:
    text, encoding = read_text_preserving_encoding(chart)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    lines.insert(header_end_index(lines), f"#VIDEO:{filename}")
    chart.write_bytes((newline.join(lines) + newline).encode(encoding))


def video_tag_value(chart: Path) -> str | None:
    """The chart's #VIDEO value, or None if it doesn't declare one."""
    for line in read_text_preserving_encoding(chart)[0].splitlines():
        if not line.lstrip().startswith("#"):
            break  # past the header, into the note lines
        key, _, value = line.lstrip()[1:].partition(":")
        if key.strip().upper() == "VIDEO":
            stripped = value.strip()
            return stripped or None
    return None


def charts_in(directory: Path) -> list[Path]:
    return [p for p in sorted(directory.glob("*.txt")) if not p.name.startswith("._")]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "songs_dir",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "songs",
        help="Directory containing one song folder per subdirectory (default: ../songs)",
    )
    parser.add_argument(
        "--category",
        choices=("none", "broken", "untagged", "all"),
        default="none",
        help="Which list to print (default: none -- folders with no video file at all)",
    )
    parser.add_argument(
        "--usdb-only",
        action="store_true",
        help="Only consider folders that have a .usdb marker, i.e. songs the "
        "syncer manages and could be asked to fetch again.",
    )
    parser.add_argument(
        "--names-only",
        action="store_true",
        help="Print bare '<Artist> - <Title>' folder names instead of full paths.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Fix the 'untagged' case by adding #VIDEO to charts whose folder "
        "has a video file. Only acts when the folder has exactly one video.",
    )
    args = parser.parse_args()

    songs_dir: Path = args.songs_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    no_video: list[str] = []
    broken: list[str] = []
    untagged: list[str] = []
    tagged: list[str] = []
    ambiguous: list[str] = []
    skipped_unmanaged = 0

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        if song_dir.name.startswith("."):
            continue
        if args.usdb_only and not any(song_dir.glob("*.usdb")):
            skipped_unmanaged += 1
            continue

        videos = {
            entry.name
            for entry in song_dir.iterdir()
            if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS
        }
        label = song_dir.name if args.names_only else str(song_dir)

        if not videos:
            no_video.append(label)
            continue

        for chart in charts_in(song_dir):
            declared = video_tag_value(chart)
            chart_label = (
                f"{song_dir.name}/{chart.name}" if args.names_only else str(chart)
            )
            if declared is None:
                if args.write:
                    if len(videos) == 1:
                        only = next(iter(videos))
                        add_video_tag(chart, only)
                        tagged.append(f"{chart_label}  ->  #VIDEO:{only}")
                    else:
                        ambiguous.append(
                            f"{chart_label}  ({len(videos)} video files, not guessing)"
                        )
                untagged.append(chart_label)
            elif not (song_dir / declared).is_file():
                broken.append(f"{chart_label}  ->  #VIDEO:{declared}")

    listing = {
        "none": no_video,
        "broken": broken,
        "untagged": untagged,
        "all": no_video + broken + untagged,
    }[args.category]
    for entry in listing:
        print(entry)

    if args.write:
        for entry in tagged:
            print(f"tagged: {entry}", file=sys.stderr)
        for entry in ambiguous:
            print(f"skipped: {entry}", file=sys.stderr)

    print(
        f"\nno video file: {len(no_video)}"
        f" | #VIDEO names a missing file: {len(broken)}"
        f" | video present but untagged: {len(untagged)}",
        file=sys.stderr,
    )
    if args.usdb_only and skipped_unmanaged:
        print(f"skipped {skipped_unmanaged} folder(s) with no .usdb marker", file=sys.stderr)
    if args.write:
        print(f"charts tagged: {len(tagged)}, skipped as ambiguous: {len(ambiguous)}", file=sys.stderr)
    elif untagged:
        print("Re-run with --write to add #VIDEO to the untagged charts.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
