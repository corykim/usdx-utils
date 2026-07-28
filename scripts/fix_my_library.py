#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Run every routine library remediation, in an order that respects how the
individual scripts interact.

    uv run scripts/fix_my_library.py            # preview everything
    uv run scripts/fix_my_library.py --write    # apply everything

The sequence:

  1. strip_bom            A byte-order mark makes a header line invisible to
                          every other tool here, so clear them before anything
                          parses a chart.
  2. tag_split_audio      Normalizes accompaniment.ogg -> instrumental.ogg and
                          tags the stems, so step 3 can find split audio by its
                          conventional name.
  3. resolve_duplicate_songs
                          Reconciles "<name> (N)" folders against "<name>",
                          moving whole folders around. Everything else works
                          per-folder, so settle which folders exist first.
  4. tag_split_audio      Again: step 3 can merge in a stem that was declared
                          under a non-standard name. Idempotent, so this is
                          usually a no-op.
  5. fix_missing_mp3      Backfills #MP3, which needs the final audio layout.
  6. find_missing_video   Declares #VIDEO for videos already sitting in a
                          folder untagged.

--terse is handed on to the steps that understand it, so they report only
what they actually changed.

Then it reports what still needs a human: songs whose audio never downloaded,
songs with no video at all, and songs not yet cross-referenced against USDB
(regenerating usdb-missing.txt when --write is given).

Deliberately NOT included: prune_desynced_stems.py, which permanently deletes
stem files. songs/ is gitignored, so that cannot be undone -- run it by hand
after reviewing its dry run. Note it skips any folder whose #MP3 points at a
stem, and step 5 above creates exactly that pointer for stems-only folders,
so run it BEFORE fix_my_library if you want it to consider those.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS_DIR = Path(__file__).resolve().parent


@dataclass
class Step:
    script: str
    summary: str
    # find_missing_*.py take the songs dir positionally; the rest use --songs-dir.
    positional_songs_dir: bool = False
    # Some steps are reporting scripts pressed into service as fixers. Their
    # listing on stdout is not what the step is for, and drowns out the run.
    discard_stdout: bool = False
    # Only some steps have anything to hold back, and passing --terse to one
    # that doesn't understand it would abort the run on an unknown argument.
    accepts_terse: bool = False
    extra_args: list[str] = field(default_factory=list)


STEPS = [
    Step("strip_bom.py", "Remove UTF-8 BOMs from charts"),
    Step(
        "tag_split_audio.py",
        "Normalize split-audio filenames and tags",
        accepts_terse=True,
    ),
    Step(
        "resolve_duplicate_songs.py",
        "Reconcile folders holding the same song twice",
        accepts_terse=True,
    ),
    Step(
        "tag_split_audio.py",
        "Re-normalize stems merged in by the previous step",
        accepts_terse=True,
    ),
    Step("fix_missing_mp3.py", "Backfill missing #MP3 tags", accepts_terse=True),
    Step(
        "find_missing_video.py",
        "Declare #VIDEO for untagged videos",
        positional_songs_dir=True,
        discard_stdout=True,
        accepts_terse=True,
    ),
]


def run_step(step: Step, songs_dir: Path | None, *, terse: bool, write: bool) -> int:
    argv = [sys.executable, str(SCRIPTS_DIR / step.script)]
    if songs_dir is not None:
        argv += [str(songs_dir)] if step.positional_songs_dir else ["--songs-dir", str(songs_dir)]
    argv += step.extra_args
    if terse and step.accepts_terse:
        argv.append("--terse")
    if write:
        argv.append("--write")
    # Children inherit the console and write to it directly, so flush our own
    # buffered output first or the step headers land after their output.
    sys.stdout.flush()
    sys.stderr.flush()
    proc = subprocess.run(
        argv, stdout=subprocess.DEVNULL if step.discard_stdout else None
    )
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--songs-dir",
        type=Path,
        default=None,
        help="Override the songs directory (default: each script's own ../songs)",
    )
    parser.add_argument(
        "--terse",
        action="store_true",
        help="Pass --terse on to the steps that understand it, so they report "
        "only what they actually changed.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply changes. Without this flag every step runs as a dry run.",
    )
    args = parser.parse_args()

    mode = "WRITE" if args.write else "DRY RUN"
    print(f"===== fix_my_library ({mode}) =====")

    for number, step in enumerate(STEPS, start=1):
        print(f"\n----- step {number}/{len(STEPS)}: {step.script} -- {step.summary} -----")
        code = run_step(step, args.songs_dir, terse=args.terse, write=args.write)
        if code != 0:
            print(
                f"\nstep {number} ({step.script}) exited {code}; stopping so a "
                f"failure here can't feed bad state into later steps.",
                file=sys.stderr,
            )
            return code

    def run_report(script: str, heading: str, **kwargs: object) -> int:
        """Run a reporting script, returning its exit code."""
        print(f"\n----- report: {heading} -----")
        sys.stdout.flush()
        argv = [sys.executable, str(SCRIPTS_DIR / script)]
        if args.songs_dir is not None:
            argv.append(str(args.songs_dir))
        return subprocess.run(argv, **kwargs).returncode  # type: ignore[arg-type]

    for script, heading in (
        ("find_missing_audio.py", "songs with no audio at all"),
        ("find_missing_video.py", "songs still missing a video"),
    ):
        code = run_report(script, heading, stdout=subprocess.DEVNULL)
        if code != 0:
            print(f"\n{script} exited {code}; stopping.", file=sys.stderr)
            return code

    # Keep the listing beside the library it describes, so pointing --songs-dir
    # somewhere else can never overwrite the real one.
    library_root = (
        args.songs_dir.resolve().parent if args.songs_dir is not None else SCRIPTS_DIR.parent
    )
    target = library_root / "usdb-missing.txt"
    heading = "songs not yet cross-referenced against USDB"
    if args.write:
        # Write to a temporary file and swap it in, so a failed run cannot
        # leave usdb-missing.txt half-written or truncated.
        staged = target.with_suffix(target.suffix + ".partial")
        with staged.open("w", encoding="utf-8") as handle:
            code = run_report("find_missing_usdb.py", heading, stdout=handle)
        if code != 0:
            staged.unlink(missing_ok=True)
            print(f"\nfind_missing_usdb.py exited {code}; {target.name} left as it was.", file=sys.stderr)
            return code
        staged.replace(target)
        print(f"wrote {target}")
    else:
        code = run_report("find_missing_usdb.py", heading, stdout=subprocess.DEVNULL)
        if code != 0:
            print(f"\nfind_missing_usdb.py exited {code}; stopping.", file=sys.stderr)
            return code
        print(f"(pass --write to regenerate {target})")

    print(f"\n===== fix_my_library done ({mode}) =====")
    if not args.write:
        print("Re-run with --write to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
