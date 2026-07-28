#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""List song folders that have no <youtube-id>.usdb provenance marker, i.e.
songs not yet cross-referenced against USDB (usdb.animux.de).

Prints one bare "<Artist> - <Title>" folder name per line, sorted, to stdout
-- which is how songs are matched against USDB -- so redirecting it refreshes
usdb-missing.txt:

    uv run scripts/find_missing_usdb.py > usdb-missing.txt

Dot-directories (e.g. a stray songs/.claude) are skipped; they aren't songs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


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
    args = parser.parse_args()

    songs_dir: Path = args.songs_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    missing = [
        song_dir
        for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir())
        if not song_dir.name.startswith(".") and not any(song_dir.glob("*.usdb"))
    ]

    for song_dir in missing:
        print(song_dir.name)

    print(f"\n{len(missing)} song folder(s) with no .usdb marker", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
