#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""List songs whose audio never arrived.

A USDB download can fetch the chart and artwork but fail on the media, which
is what happens when the source is geo-restricted or the API refuses it. The
folder is left with a chart, a cover, a background and a .usdb marker, and no
audio at all -- unplayable, and not fixable by re-tagging, since there is
nothing on disk for #MP3 to point at. The marker records the failure, so
--details reports the syncer's own verdict and the source it was reaching for.

Three separate problems get counted, since they need different fixes:

  none      no audio file and no video either -- the media has to be
            re-downloaded
  broken    a chart declares #MP3 but names a file that isn't there
  untagged  audio is present but no chart declares #MP3, so a client has
            nothing to play (fix_missing_mp3.py handles this one)

A folder holding only a video counts as having audio: clients play the
video's own track, and fix_missing_mp3.py points #MP3 at it.

Prints one entry per line, sorted, to stdout with a summary of all three on
stderr, so it can be redirected straight to a file:

    uv run scripts/find_missing_audio.py > audio-missing.txt

--category picks which list goes to stdout (default: none). Dot-directories
are skipped; they aren't songs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

AUDIO_EXTENSIONS = frozenset({".mp3", ".ogg", ".m4a", ".wav", ".flac", ".opus"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mkv", ".avi", ".mov", ".mpg", ".mpeg"})
STEM_FILENAMES = frozenset({"vocals.ogg", "instrumental.ogg", "accompaniment.ogg"})
TAG_ENCODINGS = ("utf-8-sig", "cp1252")


def read_text_preserving_encoding(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in TAG_ENCODINGS:
        try:
            return raw.decode(encoding).replace("﻿", "")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def mp3_tag_value(chart: Path) -> str | None:
    """The chart's #MP3 value, or None if it doesn't declare one."""
    for line in read_text_preserving_encoding(chart).splitlines():
        if not line.lstrip().startswith("#"):
            break  # past the header, into the note lines
        key, _, value = line.lstrip()[1:].partition(":")
        if key.strip().upper() == "MP3":
            return value.strip() or None
    return None


def charts_in(directory: Path) -> list[Path]:
    return [p for p in sorted(directory.glob("*.txt")) if not p.name.startswith("._")]


def usdb_markers(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.usdb"))


def describe_failure(directory: Path) -> str:
    """What the syncer recorded about this song's media, for --details."""
    bits: list[str] = []
    for marker in usdb_markers(directory):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            bits.append(f"{marker.stem}: unreadable marker")
            continue
        statuses = ", ".join(
            f"{part}={payload[part].get('status', '?')}"
            for part in ("audio", "video")
            if isinstance(payload.get(part), dict)
        )
        sources = {
            key: value
            for token in str(payload.get("meta_tags", "")).split(",")
            if "=" in token
            for key, value in [token.split("=", 1)]
            if key in ("a", "v")
        }
        source = " ".join(f"{k}={v}" for k, v in sorted(sources.items()))
        bits.append(
            f"usdb#{payload.get('song_id', '?')} {statuses}" + (f" [{source}]" if source else "")
        )
    return "; ".join(bits) if bits else "no .usdb marker"


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
        help="Which list to print (default: none -- folders with no audio and no video)",
    )
    parser.add_argument(
        "--usdb-only",
        action="store_true",
        help="Only consider folders that have a .usdb marker, i.e. songs the "
        "syncer manages and could be asked to fetch again.",
    )
    parser.add_argument(
        "--full-paths",
        action="store_true",
        help="Print the full path to each folder instead of just its name.",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Append what the .usdb marker recorded: the syncer's status for "
        "audio and video, and the source it was reaching for.",
    )
    args = parser.parse_args()

    songs_dir: Path = args.songs_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    no_audio: list[str] = []
    broken: list[str] = []
    untagged: list[str] = []
    skipped_unmanaged = 0

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        if song_dir.name.startswith("."):
            continue
        if args.usdb_only and not usdb_markers(song_dir):
            skipped_unmanaged += 1
            continue

        files = [p for p in song_dir.iterdir() if p.is_file()]
        # A stem is not a full mix, but it is still something to play, so it
        # counts here -- unlike in the duplicate resolver, which needs the mix
        # itself to compare lengths against.
        audio = [p for p in files if p.suffix.lower() in AUDIO_EXTENSIONS]
        videos = [p for p in files if p.suffix.lower() in VIDEO_EXTENSIONS]
        label = str(song_dir) if args.full_paths else song_dir.name
        if args.details:
            label = f"{label}  --  {describe_failure(song_dir)}"

        if not audio and not videos:
            no_audio.append(label)
            continue

        for chart in charts_in(song_dir):
            declared = mp3_tag_value(chart)
            chart_label = (
                str(chart) if args.full_paths else f"{song_dir.name}/{chart.name}"
            )
            if declared is None:
                untagged.append(chart_label)
            elif not (song_dir / declared).is_file():
                broken.append(f"{chart_label}  ->  #MP3:{declared}")

    listing = {
        "none": no_audio,
        "broken": broken,
        "untagged": untagged,
        "all": no_audio + broken + untagged,
    }[args.category]
    for entry in listing:
        print(entry)

    print(
        f"\nno audio and no video: {len(no_audio)}"
        f" | #MP3 names a missing file: {len(broken)}"
        f" | audio present but untagged: {len(untagged)}",
        file=sys.stderr,
    )
    if args.usdb_only and skipped_unmanaged:
        print(f"skipped {skipped_unmanaged} folder(s) with no .usdb marker", file=sys.stderr)
    if no_audio and not args.details:
        print("Re-run with --details to see what the syncer recorded.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
