#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["audio-separator[gpu]", "torch", "torchaudio"]
#
# [[tool.uv.index]]
# name = "pytorch-cu128"
# url = "https://download.pytorch.org/whl/cu128"
# explicit = true
#
# [tool.uv.sources]
# torch = { index = "pytorch-cu128" }
# torchaudio = { index = "pytorch-cu128" }
# ///
"""Generate vocals.ogg / instrumental.ogg for songs that only have a full mix,
using a UVR separation model on the GPU.

Every other script here reacts to stems that already exist -- tagging them,
measuring them, refusing to trust the desynced ones. This is the one that
creates them, so the library's split-audio songs stop being whatever Melody
Mania happened to produce and become something reproducible.

The model is loaded once and reused for the whole run: loading costs ~13s
against ~60s of separation per song, so reloading per song would add hours
across a full library pass.

Separation happens in a temp directory and the results are only moved into the
song folder once they measure the same length as the mix they came from. A
partially written vocals.ogg would otherwise look exactly like a finished one
to the next run -- the presence of the stems *is* the resume marker, so it has
to mean "complete".

The source mix is decoded to a plain stereo WAV first (see stage_input) --
the model refuses anything that isn't stereo, and this library has both mono
and 5.1 rips in it.

Note the stems come out at the model's own 44.1kHz regardless of the mix's
sample rate. Duration is preserved to the microsecond, which is what the
charts are timed against, so a 48kHz source is not a reason to skip a song.

Defaults to a dry run; pass --write to actually separate anything.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import audio_lengths
import tag_split_audio

# Windows consoles are frequently stuck on a legacy codepage (e.g. cp1252)
# that can't represent every character in these songs' filenames. Reconfigure
# stdout/stderr to UTF-8 so printing a path never crashes the run.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

VOCALS_NAME = "vocals.ogg"
INSTRUMENTAL_NAME = "instrumental.ogg"

# BS-Roformer (Viperx 1297). Best instrumental SDR of the models audio-separator
# ships (16.45), with vocals close behind the leaders (11.77) -- and the
# instrumental is what someone actually sings over. Override with --model;
# --list-models prints the alternatives with their scores.
DEFAULT_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"

# audio-separator defaults its model cache to /tmp/audio-separator-models/,
# which on Windows lands somewhere unhelpful and is not persistent. These
# checkpoints are ~640MB, so they want a stable home.
DEFAULT_MODEL_DIR = Path(
    os.environ.get("AUDIO_SEPARATOR_MODELS", Path.home() / ".cache" / "audio-separator-models")
)

OUTPUT_BITRATE = "192k"


def find_candidates(songs_dir: Path, *, force: bool) -> tuple[list[tuple[Path, Path]], int, int]:
    """Song folders that have a full mix but no stems yet.

    Returns (candidates, already_done, no_mix). A folder with vocals.ogg is
    considered done -- see the module docstring on why that is a safe marker.
    """
    candidates: list[tuple[Path, Path]] = []
    already_done = no_mix = 0

    for song_dir in sorted(songs_dir.iterdir()):
        if not song_dir.is_dir() or song_dir.name.startswith("."):
            continue
        if not force and tag_split_audio.find_case_insensitive(song_dir, VOCALS_NAME):
            already_done += 1
            continue
        mix = audio_lengths.find_full_mix(song_dir)
        if mix is None:
            # Video-only folders land here. A music video's duration
            # legitimately differs from the song's, so there would be nothing
            # trustworthy to validate the stems against -- leave them be.
            no_mix += 1
            continue
        candidates.append((song_dir, mix))

    return candidates, already_done, no_mix


def stage_input(mix: Path, workdir: Path) -> Path:
    """Decode the mix to a plain stereo WAV the model is guaranteed to accept.

    Three problems, one step:
      * BS-Roformer asserts on anything that isn't stereo, and this library
        holds both mono rips and 5.1 soundtrack rips; -ac 2 downmixes or
        upmixes them (a no-op for the stereo majority).
      * The filename becomes ASCII, keeping unicode titles away from the
        ffmpeg/pydub layers that stranded Melody Mania's output (CLAUDE.md).
      * Whatever exotic thing the mix is encoded as stops mattering.

    16-bit is not a downgrade: the sources are lossy and audio-separator
    writes 16-bit output regardless.
    """
    staged = workdir / "input.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(mix), "-ac", "2", "-c:a", "pcm_s16le", str(staged)],
        check=True,
        capture_output=True,
    )
    return staged


def separate_one(separator, mix: Path, workdir: Path) -> tuple[Path, Path]:
    """Run the model over one mix. Returns (vocals, instrumental) in workdir."""
    staged = stage_input(mix, workdir)
    outdir = workdir / "out"
    outdir.mkdir(exist_ok=True)

    # The loaded model keeps its own copy of output_dir (it is baked into the
    # config at load_model time), so pointing only the Separator at the temp
    # directory silently writes the stems to the working directory instead.
    separator.output_dir = str(outdir)
    if getattr(separator, "model_instance", None) is not None:
        separator.model_instance.output_dir = str(outdir)
    separator.separate(str(staged))

    produced = [p for p in outdir.iterdir() if p.is_file()]
    vocals = next((p for p in produced if "vocals" in p.name.lower()), None)
    instrumental = next(
        (p for p in produced if "instrumental" in p.name.lower() or "no_vocals" in p.name.lower()),
        None,
    )
    if vocals is None or instrumental is None:
        got = ", ".join(sorted(p.name for p in produced)) or "nothing"
        raise RuntimeError(f"model did not produce both stems (got: {got})")
    return vocals, instrumental


def install_stems(
    song_dir: Path, mix: Path, vocals: Path, instrumental: Path, *, tolerance: float
) -> str | None:
    """Move validated stems into the song folder. Returns an error string if
    they failed validation and were not installed."""
    for stem in (vocals, instrumental):
        agrees, explanation = audio_lengths.lengths_agree(stem, mix, tolerance)
        if agrees is None:
            return f"could not measure the result ({explanation})"
        if not agrees:
            return f"result does not match the mix ({explanation})"

    shutil.move(str(vocals), song_dir / VOCALS_NAME)
    shutil.move(str(instrumental), song_dir / INSTRUMENTAL_NAME)
    return None


def tag_charts(song_dir: Path, *, write: bool) -> list[str]:
    """Point the folder's charts at the stems that were just created."""
    notes: list[str] = []
    charts = [c for c in sorted(song_dir.glob("*.txt")) if not c.name.startswith("._")]
    for chart in charts:
        changes = tag_split_audio.ensure_tags(
            chart, VOCALS_NAME, INSTRUMENTAL_NAME, fix_instrumental=True, write=write
        )
        if changes:
            notes.append(f"{chart.name}: {'; '.join(changes)}")
    return notes


def build_separator(model: str, model_dir: Path, *, quiet: bool):
    """Import and initialise audio-separator. Imported lazily so a dry run
    doesn't pay ~10s of torch import time to print a list of folder names."""
    from audio_separator.separator import Separator

    model_dir.mkdir(parents=True, exist_ok=True)
    separator = Separator(
        model_file_dir=str(model_dir),
        output_format="ogg",
        output_bitrate=OUTPUT_BITRATE,
        log_level=logging.WARNING if quiet else logging.INFO,
    )
    separator.load_model(model_filename=model)
    return separator


def list_models() -> int:
    from audio_separator.separator import Separator

    for arch, entries in Separator().list_supported_model_files().items():
        rows = []
        for name, value in entries.items():
            if not isinstance(value, dict):
                continue
            scores = value.get("scores") or {}
            vocals = (scores.get("vocals") or {}).get("SDR")
            instrumental = (scores.get("instrumental") or {}).get("SDR")
            if vocals is None and instrumental is None:
                continue
            rows.append((value.get("filename", "?"), vocals, instrumental, name))
        if not rows:
            continue
        print(f"\n=== {arch} ===")
        for filename, vocals, instrumental, name in sorted(
            rows, key=lambda r: -(r[1] or 0) - (r[2] or 0)
        ):
            v = f"{vocals:5.2f}" if vocals is not None else "    -"
            i = f"{instrumental:5.2f}" if instrumental is not None else "    -"
            print(f"  voc SDR {v}  inst SDR {i}  {filename}")
            print(f"{'':>32}{name}")
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Create vocals.ogg/instrumental.ogg from a song's full mix on the GPU.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--songs-dir", type=Path, default=repo_root / "songs", help="library root (default: songs/)"
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        metavar="SONG_DIR",
        help="separate only this one song folder, instead of scanning the library",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model file (default: {DEFAULT_MODEL})")
    parser.add_argument(
        "--model-dir", type=Path, default=DEFAULT_MODEL_DIR, help="where checkpoints are cached"
    )
    parser.add_argument("--list-models", action="store_true", help="list models with SDR scores and exit")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=audio_lengths.DEFAULT_TOLERANCE_S,
        help="seconds a stem may differ from the mix before it is rejected",
    )
    parser.add_argument("--limit", type=int, default=None, help="stop after this many songs")
    parser.add_argument(
        "--force", action="store_true", help="re-separate even if stems already exist (overwrites)"
    )
    parser.add_argument("--terse", action="store_true", help="only report songs something happened to")
    parser.add_argument(
        "--verbose", action="store_true", help="show audio-separator's own INFO logging"
    )
    parser.add_argument("--write", action="store_true", help="actually separate and install stems")
    args = parser.parse_args()

    if args.list_models:
        return list_models()

    if args.dir is not None:
        song_dir = args.dir if args.dir.is_absolute() else Path.cwd() / args.dir
        song_dir = song_dir.resolve()
        if not song_dir.is_dir():
            print(f"not a directory: {song_dir}", file=sys.stderr)
            return 2
        mix = audio_lengths.find_full_mix(song_dir)
        if mix is None:
            print(f"no full-mix audio to separate in {song_dir}", file=sys.stderr)
            return 1
        existing = tag_split_audio.find_case_insensitive(song_dir, VOCALS_NAME)
        if existing and not args.force:
            print(f"already has stems (use --force to redo): {song_dir.name}")
            return 0
        candidates, already_done, no_mix = [(song_dir, mix)], 0, 0
    else:
        songs_dir = args.songs_dir
        if not songs_dir.is_dir():
            print(f"songs directory not found: {songs_dir}", file=sys.stderr)
            return 2
        candidates, already_done, no_mix = find_candidates(songs_dir, force=args.force)

    if args.limit is not None:
        candidates = candidates[: args.limit]

    mode = "write" if args.write else "dry-run"
    print(f"[{mode}] {len(candidates)} song(s) to separate", end="")
    if args.dir is None:
        print(f"; {already_done} already have stems, {no_mix} have no full mix")
    else:
        print()

    if not candidates:
        return 0

    if not args.write:
        for song_dir, mix in candidates:
            print(f"would separate: {song_dir.name}  <-  {mix.name}")
        print(f"\nRe-run with --write to separate. Expect roughly a minute of GPU time per song.")
        return 0

    print(f"[{mode}] loading {args.model} ...")
    load_started = time.monotonic()
    separator = build_separator(args.model, args.model_dir, quiet=not args.verbose)
    print(f"[{mode}] model ready in {time.monotonic() - load_started:.1f}s\n")

    done = failed = 0
    elapsed_total = 0.0
    for index, (song_dir, mix) in enumerate(candidates, start=1):
        started = time.monotonic()
        prefix = f"[{index}/{len(candidates)}]"
        try:
            with tempfile.TemporaryDirectory(prefix="stems-") as tmp:
                vocals, instrumental = separate_one(separator, mix, Path(tmp))
                problem = install_stems(
                    song_dir, mix, vocals, instrumental, tolerance=args.tolerance
                )
        except KeyboardInterrupt:
            print(f"\ninterrupted after {done} song(s); re-run to resume where it stopped")
            return 130
        except Exception as exc:  # a bad file should not end a 13-hour run
            failed += 1
            print(f"{prefix} FAILED {song_dir.name}: {exc}")
            continue

        if problem:
            failed += 1
            print(f"{prefix} rejected {song_dir.name}: {problem}")
            continue

        notes = tag_charts(song_dir, write=True)
        done += 1
        took = time.monotonic() - started
        elapsed_total += took
        remaining = (len(candidates) - index) * (elapsed_total / done)
        if not args.terse:
            print(
                f"{prefix} {song_dir.name}  ({took:.0f}s, ~{remaining / 3600:.1f}h left)"
            )
            for note in notes:
                print(f"        tagged {note}")

    print(f"\n[{mode}] separated {done} song(s), {failed} failed")
    if failed:
        print("Failed songs keep their mix untouched and will be retried on the next run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
