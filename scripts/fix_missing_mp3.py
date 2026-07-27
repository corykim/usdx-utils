#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Add a missing #MP3 tag to USDX charts that have no combined audio/video
reference.

A handful of song folders only contain split vocals.ogg/instrumental.ogg
stems with no full mix, so #MP3 (the tag UltraStar clients use as the
primary audio reference) was never set. For each chart missing #MP3, this
script points it at:
  * the folder's video file (.mp4/.webm/.mkv/.avi), if one exists, else
  * instrumental.ogg, if that split stem exists, else
  * left alone and reported as needing manual attention.

Defaults to a dry run; pass --write to actually modify files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".avi")
INSTRUMENTAL_NAME = "instrumental.ogg"
TAG_ENCODINGS = ("utf-8", "cp1252")


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
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def has_tag(lines: list[str], tag: str) -> bool:
    prefix = f"#{tag}:".lower()
    return any(line.strip().lower().startswith(prefix) for line in lines)


def header_end_index(lines: list[str]) -> int:
    """Index of the first line that is not a #TAG: header line."""
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            return i
    return len(lines)


def determine_mp3_value(song_dir: Path) -> str | None:
    video = find_video(song_dir)
    if video:
        return video.name
    instrumental = find_case_insensitive(song_dir, INSTRUMENTAL_NAME)
    if instrumental:
        return instrumental.name
    return None


def add_mp3_tag(chart: Path, value: str, *, write: bool) -> bool:
    text, encoding = read_text_preserving_encoding(chart)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    if has_tag(lines, "MP3"):
        return False

    insert_at = header_end_index(lines)
    lines.insert(insert_at, f"#MP3:{value}")

    if write:
        new_text = newline.join(lines) + newline
        chart.write_bytes(new_text.encode(encoding))

    return True


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
        help="Actually modify chart files. Without this flag, only report what would change.",
    )
    args = parser.parse_args()

    songs_dir: Path = args.songs_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    fixed = 0
    unresolved: list[Path] = []

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        for chart in sorted(song_dir.glob("*.txt")):
            if chart.name.startswith("._"):
                continue
            text, _ = read_text_preserving_encoding(chart)
            if has_tag(text.splitlines(), "MP3"):
                continue

            value = determine_mp3_value(song_dir)
            if value is None:
                unresolved.append(chart)
                print(f"skip (no video or instrumental.ogg): {chart.relative_to(songs_dir)}")
                continue

            add_mp3_tag(chart, value, write=args.write)
            fixed += 1
            verb = "updated" if args.write else "would update"
            print(f"{verb}: {chart.relative_to(songs_dir)} -> #MP3:{value}")

    mode = "write" if args.write else "dry-run"
    print(
        f"\n[{mode}] charts {'fixed' if args.write else 'needing #MP3'}: {fixed}, "
        f"unresolved (no video or instrumental.ogg): {len(unresolved)}"
    )
    if not args.write and fixed:
        print("Re-run with --write to apply changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
