#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Remove UTF-8 byte-order marks from USDX chart files.

A BOM is invisible but not whitespace, so a header line carrying one never
looks like a "#TAG:" line to a parser. That silently truncates the header:
tools conclude the chart declares no tags at all, then re-add tags it already
had, or insert new ones above the BOM line. Charts here are plain UTF-8, so
the mark has no purpose -- strip it.

Marks are removed wherever they appear, not just at the start of the file,
since a mid-file mark is the tell-tale of a tool having inserted a line above
a BOM'd header.

Defaults to a dry run; pass --write to apply.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from utils import fix_metadata

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BOM = b"\xef\xbb\xbf"


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
        "--write",
        action="store_true",
        help="Actually rewrite the charts. Without this flag, only report.",
    )
    args = parser.parse_args()

    songs_dir: Path = args.songs_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    at_start = 0
    mid_file = 0
    modified_dirs: set[Path] = set()

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        if song_dir.name.startswith("."):
            continue
        for chart in sorted(song_dir.glob("*.txt")):
            if chart.name.startswith("._"):
                continue
            raw = chart.read_bytes()
            if BOM not in raw:
                continue
            where = "at start" if raw.startswith(BOM) else "MID-FILE"
            if raw.startswith(BOM):
                at_start += 1
            else:
                mid_file += 1
            verb = "stripped" if args.write else "would strip"
            print(f"{verb} ({where}): {song_dir.name}/{chart.name}")
            if args.write:
                chart.write_bytes(raw.replace(BOM, b""))
                modified_dirs.add(song_dir)

    for sd in modified_dirs:
        fix_metadata.record_basics(sd)

    mode = "write" if args.write else "dry-run"
    total = at_start + mid_file
    print(f"\n[{mode}] charts with a BOM: {total} (at start: {at_start}, mid-file: {mid_file})")
    if not args.write and total:
        print("Re-run with --write to apply changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
