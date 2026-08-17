#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Run every routine library remediation, in an order that respects how the
individual scripts interact.

    uv run scripts/fix_my_library.py            # preview everything
    uv run scripts/fix_my_library.py --write    # apply everything
    uv run scripts/fix_my_library.py --full --write  # also split stems + ReplayGain

The sequence:

  1. strip_bom            A byte-order mark makes a header line invisible to
                          every other tool here, so clear them before anything
                          parses a chart.
  2. prune_desynced_stems Deletes stems that disagree with their own song's
                          audio, and strips their tags. Early, so the tagging
                          steps neither measure nor complain about stems that
                          are about to go; it recognizes accompaniment.ogg
                          itself, so it does not need step 3's renaming first.
                          It must come after step 1 (it reads #MP3, which a
                          BOM hides) and before step 6, which points #MP3 at a
                          stem for stems-only folders -- a folder in that state
                          is one this step then skips forever.
  3. tag_split_audio      Normalizes accompaniment.ogg -> instrumental.ogg and
                          tags the stems, so step 4 can find split audio by its
                          conventional name.
  4. resolve_duplicate_songs
                          Reconciles "<name> (N)" folders against "<name>",
                          moving whole folders around. Everything else works
                          per-folder, so settle which folders exist first.
  5. tag_split_audio      Again: step 4 can merge in a stem that was declared
                          under a non-standard name. Idempotent, so this is
                          usually a no-op.
  6. fix_missing_mp3      Backfills #MP3, which needs the final audio layout.
  7. fix_missing_video    Without --full: reports songs still missing a video.
                          With --full: declares #VIDEO for videos already in
                          the folder untagged, then downloads for songs that
                          have a known id in their .usdb marker or
                          fix-metadata.json but no video file on disk yet.

With --full, three more steps run after the above:

  8. split_audio_stems    Separates stems for every song that has a full mix
                          but no vocals.ogg/instrumental.ogg yet. Requires a
                          GPU; on CPU the step will stop and say so. Each new
                          stem pair is tagged with the mix's ReplayGain value
                          as it is created.
  9. apply_replaygain     Writes a ReplayGain tag on every song and propagates
                          the same value to its stems. Covers the full mixes
                          and any stems that pre-dated step 9.
 10. backfill_metadata    Populates fix-metadata.json for every song: filename,
                          size, cached duration, video_id from .usdb, and (with
                          --full/--ffprobe) the full audio probe via ffprobe.

--backfill-metadata runs only step 10 as an extra step after the base seven.
--ffprobe adds the ffprobe audio probe to the backfill step; implied by --full.

--full is omitted by default because stem separation can take hours of GPU
time -- not something to start as a side effect of a routine tidy-up.

--terse is handed on to the steps that understand it, so they report only
what they actually changed.

Then it reports what still needs a human: songs whose audio never downloaded,
songs with no video at all, and songs not yet cross-referenced against USDB
(regenerating usdb-missing.txt when --write is given).

**Step 2 deletes files, and songs/ is gitignored, so git will not bring them
back.** It was left out of this run for exactly that reason until
split_audio_stems.py existed; now a wrongly pruned stem can be regenerated
from the song's own mix, which is what makes automating the deletion
reasonable. It still only deletes under --write, like every other step, so
the dry run remains the place to check what it has picked out.
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
    # Deletes files rather than editing charts. Worth saying out loud in the
    # step header, since songs/ is gitignored and nothing here is undoable.
    destructive: bool = False
    # Scripts with non-empty PEP 723 dependency headers must be launched via
    # "uv run --script" so uv resolves and installs their packages; plain
    # sys.executable bypasses that and lands on an ImportError.
    uv_script: bool = False
    extra_args: list[str] = field(default_factory=list)


BASE_STEPS = [
    Step("strip_bom.py", "Remove UTF-8 BOMs from charts"),
    Step(
        "prune_desynced_stems.py",
        "Delete stems that disagree with their song's own audio",
        accepts_terse=True,
        destructive=True,
    ),
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
]

# Two variants of the video step — picked by --full.
_VIDEO_STEP = Step(
    "fix_missing_video.py",
    "Report songs still missing a video",
    extra_args=["--report-only"],
    accepts_terse=True,
)
_VIDEO_STEP_FULL = Step(
    "fix_missing_video.py",
    "Declare #VIDEO for untagged videos; download for songs with a known id",
    extra_args=["--download"],
    accepts_terse=True,
)

# Steps added only under --full, after the video step.
FULL_ONLY_STEPS = [
    Step(
        "split_audio_stems.py",
        "Separate stems for songs that have a full mix but no stems yet",
        accepts_terse=True,
        uv_script=True,
    ),
    Step(
        "apply_replaygain.py",
        "Write ReplayGain tags across the library",
        accepts_terse=True,
        uv_script=True,
    ),
]

_BACKFILL_STEP = Step(
    "backfill_metadata.py",
    "Backfill fix-metadata.json from filesystem, audio cache, and .usdb markers",
    accepts_terse=True,
)
_BACKFILL_STEP_FFPROBE = Step(
    "backfill_metadata.py",
    "Backfill fix-metadata.json (including full audio probe via ffprobe)",
    extra_args=["--ffprobe"],
    accepts_terse=True,
)


def run_step(step: Step, songs_dir: Path | None, *, terse: bool, write: bool) -> int:
    if step.uv_script:
        argv = ["uv", "run", "--script", str(SCRIPTS_DIR / step.script)]
    else:
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
        "--full",
        action="store_true",
        help="Also download missing videos, run split_audio_stems, apply_replaygain, "
             "and backfill metadata with ffprobe. Omitted by default because stem "
             "separation can take hours of GPU time.",
    )
    parser.add_argument(
        "--backfill-metadata",
        action="store_true",
        help="Add a final step that backfills fix-metadata.json for every song. "
             "Implied by --full.",
    )
    parser.add_argument(
        "--ffprobe",
        action="store_true",
        help="When --backfill-metadata is active, also run ffprobe for the full audio "
             "section. Implied by --full.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply changes. Without this flag every step runs as a dry run.",
    )
    args = parser.parse_args()

    backfill = args.backfill_metadata or args.full
    ffprobe = args.ffprobe or args.full

    video_step = _VIDEO_STEP_FULL if args.full else _VIDEO_STEP
    backfill_step = _BACKFILL_STEP_FFPROBE if ffprobe else _BACKFILL_STEP
    steps = BASE_STEPS + [video_step] + (FULL_ONLY_STEPS if args.full else [])
    if backfill:
        steps = steps + [backfill_step]

    mode = "WRITE" if args.write else "DRY RUN"
    qualifiers = []
    if args.full:
        qualifiers.append("full")
    elif args.backfill_metadata:
        qualifiers.append("backfill-metadata")
        if ffprobe:
            qualifiers.append("ffprobe")
    print(f"===== fix_my_library ({mode}{', ' + ', '.join(qualifiers) if qualifiers else ''}) =====")

    for number, step in enumerate(steps, start=1):
        print(f"\n----- step {number}/{len(steps)}: {step.script} -- {step.summary} -----")
        if step.destructive and args.write:
            print("      (deletes files; songs/ is gitignored, so this is permanent)")
        code = run_step(step, args.songs_dir, terse=args.terse, write=args.write)
        if code != 0:
            print(
                f"\nstep {number} ({step.script}) exited {code}; stopping so a "
                f"failure here can't feed bad state into later steps.",
                file=sys.stderr,
            )
            return code

    def run_report(
        script: str,
        heading: str,
        *,
        extra_args: list[str] = (),
        songs_dir_as_flag: bool = False,
        **kwargs: object,
    ) -> int:
        """Run a reporting script, returning its exit code."""
        print(f"\n----- report: {heading} -----")
        sys.stdout.flush()
        argv = [sys.executable, str(SCRIPTS_DIR / script)]
        if args.songs_dir is not None:
            if songs_dir_as_flag:
                argv += ["--songs-dir", str(args.songs_dir)]
            else:
                argv.append(str(args.songs_dir))
        argv += list(extra_args)
        return subprocess.run(argv, **kwargs).returncode  # type: ignore[arg-type]

    for script, heading in (
        ("extract_audio_from_youtube.py", "songs with no audio at all"),
        ("fix_missing_video.py", "songs still missing a video"),
    ):
        code = run_report(
            script, heading,
            extra_args=["--report-only"], songs_dir_as_flag=True,
            stdout=subprocess.DEVNULL,
        )
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
