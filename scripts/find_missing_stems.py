#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""List songs with no usable split-vocal stems (vocals.ogg + instrumental.ogg).

Two separate problems, since they need different fixes:

  none      no stems at all -- a candidate for running Melody Mania
            separation, if there's a full mix to separate. Split further by
            --category has-source|no-source: has-source means a full mix
            exists to run separation on, no-source means there isn't one to
            separate from (nothing this script -- or Melody Mania -- can do
            without audio first; likely already listed by
            find_missing_audio.py). Both are subsets of none, not
            additional songs.
  partial   exactly one of vocals.ogg/instrumental.ogg is present -- the
            other was deleted, never finished separating, or the pair
            partially transferred. Melody Mania always produces both
            together, so this is a data problem, not a normal state.

Deliberately does not flag stems that exist but disagree with their folder's
full mix -- that's prune_desynced_stems.py's job, a different, correctness
question rather than a presence one.

Read-only: there is no automated fix for a missing stem (Melody Mania is a
manual, external separation step), so there is no --write here.

Prints one entry per line, sorted, to stdout with a summary on stderr:

    uv run scripts/find_missing_stems.py > stems-missing.txt

--category picks which list goes to stdout (default: none). Dot-directories
are skipped; they aren't songs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils.audio_lengths import find_full_mix, stems_in  # noqa: E402

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
    parser.add_argument(
        "--category",
        choices=("none", "partial", "all", "has-source", "no-source"),
        default="none",
        help="Which list to print (default: none -- folders with no stems at "
        "all). has-source and no-source are the full-mix breakdown of "
        "'none' above -- both subsets of it, not additional songs.",
    )
    parser.add_argument(
        "--full-paths", action="store_true",
        help="Print the full path to each folder instead of just its name.",
    )
    parser.add_argument(
        "--details", action="store_true",
        help="Append which stem is present (partial) or whether a full mix "
        "exists to separate (none), to each line.",
    )
    args = parser.parse_args()

    songs_dir: Path = args.songs_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    none: list[str] = []
    partial: list[str] = []
    has_source: list[str] = []
    no_source: list[str] = []

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        if song_dir.name.startswith("."):
            continue

        label = str(song_dir) if args.full_paths else song_dir.name
        stems = stems_in(song_dir)

        if len(stems) == 1:
            if args.details:
                label = f"{label}  --  has {stems[0].name}, missing the other"
            partial.append(label)
        elif not stems:
            mix = find_full_mix(song_dir)
            if args.details:
                label = f"{label}  --  {'full mix: ' + mix.name if mix else 'no audio to separate from'}"
            none.append(label)
            (has_source if mix else no_source).append(label)

    listing = {
        "none": none,
        "partial": partial,
        "all": none + partial,
        "has-source": has_source,
        "no-source": no_source,
    }[args.category]
    for entry in listing:
        print(entry)

    print(
        f"\nno stems: {len(none)} | partial (one stem only): {len(partial)}",
        file=sys.stderr,
    )
    print(
        f"  of the {len(none)} with no stems: {len(has_source)} have a full "
        f"mix to separate, {len(no_source)} have no audio to separate from",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
