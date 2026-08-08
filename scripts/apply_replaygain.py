#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["ffmpeg-normalize", "mutagen"]
# ///
"""Give every song a ReplayGain tag, and its stems the same one.

UltraStar Deluxe reads ReplayGain tags (2025.4.0 and later, once ReplayGain
is switched on under Tools -> Options -> Sound) but never writes them, so
something else has to. usdb_syncer writes them for the audio it fetches --
about two thirds of the full mixes here already carry one -- and this fills
in the rest, then propagates the value to vocals.ogg/instrumental.ogg, which
nothing has ever tagged.

**The audio is never re-encoded.** A ReplayGain tag records how much to turn
a track down at playback; the samples are untouched, so this is reversible,
repeatable, and costs no quality. Only the tag is written, in place.

The same gain goes on the stems as on the mix, and that is the whole point.
A stem measured on its own is much quieter than the mix it came from -- for
one song here, -19.8 dB RMS for the vocal and -16.8 for the instrumental
against -12.7 for the mix -- so normalizing each independently would boost
the vocal about 3 dB more than the instrumental and pull apart the balance
UltraStar's vocals toggle depends on. One measurement, taken on the mix,
applied to all three, keeps it intact. Peaks stay per-file, since a peak
describes the file rather than the correction.

Where a folder has stems but no full mix (41 of them here), the reference is
reconstructed by summing the stems in a temp directory -- separation output
adds back up to its input, measured at r = 0.9971 against the real mix for
the song that has both. That reconstruction never enters the song folder:
audio_lengths.find_full_mix would start returning it, quietly changing what
prune_desynced_stems and tag_split_audio compare against.

Targets follow usdb_syncer: -18 LUFS, or -23 for Opus, whose header carries
an R128 gain referenced to that instead.

Running it again over a tagged library is a no-op and cheap: a gain already
present is reused rather than re-measured, and a stem already carrying it is
skipped. ReplayGain never accumulates -- the tag is an instruction to the
player, not a change to the samples -- so repeating this can never attenuate
anything twice, unlike the re-encoding kind of normalization.

--force distrusts a stored value and measures again, but still writes only
where the answer differs. It measures on a throwaway copy to do that, since
ffmpeg-normalize will only tell you a gain by writing it. Rewriting a file
with the number it already had costs a fresh modification time, and a sync
that compares timestamps would then resend the whole library for nothing.

Reading an existing tag has to understand every container's spelling of it --
`replaygain_track_gain` in a Vorbis comment, `TXXX:REPLAYGAIN_TRACK_GAIN` in
ID3, `----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN` in MP4. Matching only the
bare lowercase name found nothing outside the Ogg files, so every mp3 and m4a
mix looked untagged however often it had been tagged and was measured from
scratch on each run.

Some files push loudnorm into dynamic mode, which is what the true-peak
warning ffmpeg-normalize prints is about. There is then no single linear gain
for it to record, so it writes nothing while logging that it did, and the
song came back unmeasurable and stayed untagged. ReplayGain is target minus
integrated loudness, which is defined whatever the filter would have done to
the audio, so that is computed here instead when the normal path yields
nothing -- within 0.04 dB of the tags usdb_syncer wrote.

A song that genuinely cannot be measured leaves a .replaygain-failed.json
memo naming the audio it failed on, and later runs skip it, saying so unless
--terse. --force ignores the memo and tries again, and any run that succeeds
deletes it.

Defaults to a dry run; pass --write to tag anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import mutagen
from mutagen.oggvorbis import OggVorbis

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import audio_lengths, song_folders  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

STEM_NAMES = ("vocals.ogg", "instrumental.ogg")

# usdb_syncer's own values (postprocessing.py): ReplayGain 2.0 references
# -18 LUFS, while Opus carries an R128 gain referenced to -23.
TARGET_LUFS = -18.0
TARGET_LUFS_OPUS = -23.0

GAIN_KEY = "replaygain_track_gain"
PEAK_KEY = "replaygain_track_peak"

# Left in a song folder when a reference could not be measured at all, so a
# later run skips it instead of paying the same failed decode again.
FAILURE_MEMO_NAME = ".replaygain-failed.json"


def measure_loudness(path: Path) -> tuple[float, float] | None:
    """Integrated loudness and true peak, from one loudnorm analysis pass."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-v", "info", "-i", str(path),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    match = re.search(r'\{[^{}]*"input_i"[^{}]*\}', result.stderr or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return float(data["input_i"]), float(data["input_tp"])
    except (ValueError, KeyError):
        return None


def write_gain_tags(path: Path, gain: str, peak: str | None) -> None:
    """Write ReplayGain tags in whatever spelling this container uses."""
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        from mutagen.id3 import ID3, TXXX, ID3NoHeaderError

        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=[gain]))
        if peak:
            tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_PEAK", text=[peak]))
        tags.save(path)
        return
    if suffix in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4, MP4FreeForm

        audio = MP4(path)
        audio["----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN"] = [MP4FreeForm(gain.encode())]
        if peak:
            audio["----:com.apple.iTunes:REPLAYGAIN_TRACK_PEAK"] = [MP4FreeForm(peak.encode())]
        audio.save()
        return
    audio = mutagen.File(path)          # ogg / opus / flac all take plain comments
    audio[GAIN_KEY] = [gain]
    if peak:
        audio[PEAK_KEY] = [peak]
    audio.save()


def target_for(path: Path) -> float:
    return TARGET_LUFS_OPUS if path.suffix.lower() == ".opus" else TARGET_LUFS


def _tag_text(value: object) -> str | None:
    """One readable string out of whatever mutagen hands back for a tag.

    Each container wraps the value differently: a Vorbis comment is a list of
    str, an ID3 TXXX frame is an object with .text, and an MP4 freeform atom
    is a list of bytes.
    """
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    text = getattr(value, "text", None)  # ID3 TXXX
    if text is not None:
        if isinstance(text, list):
            text = text[0] if text else None
        return None if text is None else str(text).strip()
    return str(value).strip()


def read_gain(path: Path) -> str | None:
    """The file's existing ReplayGain track gain, as written, or None.

    The key is spelled differently in every container -- `replaygain_track_gain`
    in a Vorbis comment, `TXXX:REPLAYGAIN_TRACK_GAIN` in ID3,
    `----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN` in MP4 -- so matching the
    bare lowercase name only ever found the Ogg files. Everything else looked
    untagged however often it had been tagged, which meant re-measuring 366
    mp3 and m4a mixes from scratch on every run and rewriting tags that were
    already right.
    """
    try:
        tags = mutagen.File(path)
    except Exception:
        return None
    if tags is None:
        return None
    try:
        items = dict(tags)
    except Exception:
        return None
    for key, value in items.items():
        if str(key).lower().rsplit(":", 1)[-1] == GAIN_KEY:
            return _tag_text(value) or None
    return None


def measure_peak(path: Path) -> str | None:
    """The file's own sample peak, linear, as ReplayGain records it."""
    # volumedetect reports at info level, so -v error would hide the one line
    # this reads and the peak would silently go unwritten.
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-v", "info",
         "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    for line in (result.stderr or "").splitlines():
        if "max_volume:" in line:
            try:
                decibels = float(line.split("max_volume:")[1].strip().split()[0])
            except (ValueError, IndexError):
                return None
            return f"{10 ** (decibels / 20):.6f}"
    return None


def scan_gain(path: Path) -> str | None:
    """Measure a file and leave a ReplayGain tag on it, returning the gain.

    ffmpeg-normalize in replaygain mode writes the tag into the input file
    and sends its (unwanted) re-encode to the null device, so the audio is
    never rewritten -- the same trick usdb_syncer uses.
    """
    from ffmpeg_normalize import FFmpegNormalize

    normalizer = FFmpegNormalize(
        normalization_type="ebu",
        target_level=target_for(path),
        keep_loudness_range_target=True,
        dynamic=False,
        progress=False,
        replaygain=True,
    )
    normalizer.add_media_file(str(path), os.devnull)
    try:
        normalizer.run_normalization()
    except Exception:
        # Undecodable input raises here rather than returning nothing. Fall
        # through: the measuring pass below reports the same failure as a
        # None, which the caller turns into a memo instead of a traceback.
        pass
    written = read_gain(path)
    if written is not None:
        return written

    # Some files push loudnorm into dynamic mode -- the true-peak warning
    # ffmpeg-normalize prints is about exactly this. There is then no single
    # linear gain for it to record, and it writes nothing while logging that
    # it did, so the song came back "unmeasurable" and stayed untagged run
    # after run. ReplayGain is target minus integrated loudness, which is
    # defined whatever the filter would have done to the audio, so it is
    # computed here instead. Measured against tags usdb_syncer wrote, this
    # agrees to within 0.04 dB -- inaudible, and only used where the normal
    # path produced nothing at all.
    measured = measure_loudness(path)
    if measured is None:
        return None
    integrated, true_peak = measured
    gain = f"{target_for(path) - integrated:.2f} dB"
    try:
        write_gain_tags(path, gain, f"{10 ** (true_peak / 20):.6f}")
    except Exception:
        return None
    return read_gain(path)


def measure_gain(path: Path) -> str | None:
    """What the gain *would* be, without touching the file.

    ffmpeg-normalize only reports a gain by writing it, so re-measuring a
    file that already carries a tag is done on a throwaway copy. That keeps
    --force meaning "do not trust the stored value" rather than "rewrite
    regardless": a verification pass over an already-correct library then
    changes nothing on disk, and a sync that compares modification times has
    nothing to send.
    """
    with tempfile.TemporaryDirectory(prefix="rg-measure-") as tmp:
        scratch = Path(tmp) / f"probe{path.suffix.lower()}"
        try:
            shutil.copy2(path, scratch)
        except OSError:
            return None
        return scan_gain(scratch)


def reconstruct_mix(stems: list[Path], workdir: Path) -> Path:
    """Sum the stems back into the mix they were separated from.

    normalize=0 is load-bearing: amix divides by the number of inputs by
    default, which would hand back something 6 dB quiet and a gain to match.
    """
    destination = workdir / "reconstructed.ogg"
    argv = ["ffmpeg", "-v", "error", "-y"]
    for stem in stems:
        argv += ["-i", str(stem)]
    argv += [
        "-filter_complex", f"amix=inputs={len(stems)}:duration=longest:normalize=0",
        "-c:a", "libvorbis", "-q:a", "8", str(destination),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise RuntimeError(
            f"could not rebuild a reference from the stems: "
            f"{detail[-1] if detail else f'exit {result.returncode}'}"
        )
    return destination


def write_stem_tags(stem: Path, gain: str, *, write: bool) -> str | None:
    """Put the reference gain on a stem, with the stem's own peak."""
    if not write:
        # Measuring the peak means an ffmpeg pass per stem -- thousands of
        # them library-wide. A preview says what it would do; it does not
        # need the number to say it.
        return f"{stem.name} -> {GAIN_KEY}={gain} (+ its own peak)"
    peak = measure_peak(stem)
    audio = OggVorbis(stem)
    audio[GAIN_KEY] = [gain]
    if peak:
        audio[PEAK_KEY] = [peak]
    audio.save()
    return f"{stem.name} -> {GAIN_KEY}={gain}" + (f", {PEAK_KEY}={peak}" if peak else "")


def concise_error(error: object) -> str:
    """The one useful line out of a wall of ffmpeg banner text.

    ffmpeg-normalize raises with the whole command line and ffmpeg's build
    configuration attached -- two thousand characters of which one matters.
    Storing that verbatim makes the memo unreadable and the log useless.
    """
    text = str(error).replace("\\r\\n", "\n").replace("\\n", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        lowered = line.lower()
        if any(word in lowered for word in ("error", "invalid", "no such", "failed")):
            return line[:200]
    return (lines[-1] if lines else text)[:200]


def reference_fingerprint(song_dir: Path) -> str:
    """Identify what a failure was about, by name and size like the other memos.

    Replace the audio and the memo stops applying by itself, so a re-download
    is retried without anyone having to remember to clear anything.
    """
    mix = audio_lengths.find_full_mix(song_dir)
    parts = []
    for path in ([mix] if mix else []) + stems_in(song_dir):
        try:
            parts.append(f"{path.name}|{path.stat().st_size}")
        except OSError:
            parts.append(path.name)
    return ";".join(parts)


def failure_recorded_for(song_dir: Path) -> str | None:
    """Why this exact audio failed before, if it did."""
    try:
        memo = json.loads((song_dir / FAILURE_MEMO_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(memo, dict) and memo.get("audio") == reference_fingerprint(song_dir):
        return str(memo.get("error", "(no reason recorded)"))
    return None


def write_failure_memo(song_dir: Path, error: str) -> None:
    payload = {
        "audio": reference_fingerprint(song_dir),
        "error": error[:2000],
        "failed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": (
            "Written by apply_replaygain.py. Delete this file, or pass --force, "
            "to measure this song again."
        ),
    }
    try:
        (song_dir / FAILURE_MEMO_NAME).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass  # a memo that cannot be written is not worth failing the run over


def clear_failure_memo(song_dir: Path) -> None:
    (song_dir / FAILURE_MEMO_NAME).unlink(missing_ok=True)


def unmeasurable(song_dir: Path, *, terse: bool) -> str:
    """Record that this song's loudness could not be established, and say so.

    Measuring is the expensive half of a run -- a full decode, sometimes two
    -- so a song that cannot be measured should not be re-attempted on every
    pass. The memo names the audio it failed on, so replacing the file clears
    it automatically.
    """
    reason = "could not measure the reference loudness"
    write_failure_memo(song_dir, reason)
    if not terse:
        print(f"    {reason}; recorded in {FAILURE_MEMO_NAME}")
    return "unmeasurable"


def stems_in(song_dir: Path) -> list[Path]:
    return [
        p for p in sorted(song_dir.iterdir())
        if p.is_file() and p.name.lower() in STEM_NAMES
    ]


def process(song_dir: Path, *, force: bool, terse: bool, write: bool) -> str:
    """Tag one song. Returns a one-word outcome for the tallies."""
    mix = audio_lengths.find_full_mix(song_dir)
    stems = stems_in(song_dir)
    if mix is None and not stems:
        return "nothing to tag"

    if not force:
        previously = failure_recorded_for(song_dir)
        if previously is not None:
            # Said out loud unless --terse: a song silently skipped forever is
            # indistinguishable from one that is fine. --force ignores the memo
            # and measures it again.
            if not terse:
                print(f"    SKIPPED, failed before: {previously}"
                      f"  (--force to retry)")
            return "failed before"

    reported: list[str] = []
    gain = None

    if mix is not None:
        existing = read_gain(mix)
        gain = existing
        if existing is not None and not force:
            reported.append(f"{mix.name} already {GAIN_KEY}={existing}")
        elif not write:
            reported.append(
                f"{mix.name} would be re-measured" if existing
                else f"{mix.name} would be measured and tagged"
            )
        elif existing is None:
            gain = scan_gain(mix)          # nothing there yet, so write directly
            if gain is None:
                return unmeasurable(song_dir, terse=terse)
            reported.append(f"{mix.name} -> {GAIN_KEY}={gain}")
        else:
            # --force over an existing tag: measure on a copy first, so a value
            # that turns out to be the same leaves the file entirely alone.
            gain = measure_gain(mix)
            if gain is None:
                return unmeasurable(song_dir, terse=terse)
            if gain == existing:
                reported.append(f"{mix.name} re-measured, unchanged at {existing}")
            else:
                gain = scan_gain(mix)
                reported.append(f"{mix.name} -> {GAIN_KEY}={existing} -> {gain}")
    else:
        # Stems but no mix: rebuild the reference rather than measure a stem,
        # so these folders are normalized by the same rule as every other.
        if write:
            with tempfile.TemporaryDirectory(prefix="rg-") as tmp:
                try:
                    reference = reconstruct_mix(stems, Path(tmp))
                except RuntimeError as exc:
                    print(f"  {song_dir.name}: {exc}", file=sys.stderr)
                    return "failed"
                gain = scan_gain(reference)
            if gain is None:
                return unmeasurable(song_dir, terse=terse)
            reported.append(f"(reference rebuilt from {len(stems)} stems) {GAIN_KEY}={gain}")
        else:
            reported.append(f"would rebuild a reference from {len(stems)} stems and measure it")

    outcome = "tagged"
    if stems:
        # No --force short-circuit here on purpose. A stem that already holds
        # the right gain is already correct, and rewriting it would produce a
        # fresh mtime for nothing -- which a sync comparing timestamps reads
        # as a file to send again, thousands of times over.
        needing = [s for s in stems if gain is None or read_gain(s) != gain]
        if not needing:
            reported.append("stems already carry it")
            outcome = "already done"
        elif gain is not None:
            for stem in needing:
                reported.append(write_stem_tags(stem, gain, write=write))
        else:
            reported.append(f"{len(needing)} stem(s) would take the mix's gain")

    # Getting this far means a gain was established, so any memo describing a
    # past failure is describing something that is no longer true. It must
    # never outlive the problem, or the song stays skipped forever.
    if write and gain is not None:
        clear_failure_memo(song_dir)

    # The folder name is printed by the caller before any of this runs, so a
    # song that takes a while to measure is named while it is being measured
    # rather than after.
    if reported and not terse:
        for line in reported:
            print(f"    {line}")
    return outcome


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "song", nargs="?", default=None,
        help=f"tag only one song -- {song_folders.HELP}. Omit to scan the library.",
    )
    parser.add_argument("--songs-dir", type=Path, default=repo_root / "songs")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many songs")
    parser.add_argument(
        "--force", action="store_true",
        help="distrust the stored value and measure again; files whose gain "
             "turns out unchanged are left untouched",
    )
    parser.add_argument("--terse", action="store_true", help="only report the tallies")
    parser.add_argument("--write", action="store_true", help="actually write tags")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("error: ffmpeg is not on PATH", file=sys.stderr)
        return 1

    if args.song is not None:
        try:
            song_dirs = [song_folders.resolve(args.song, args.songs_dir)]
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        if not args.songs_dir.is_dir():
            print(f"error: {args.songs_dir} is not a directory", file=sys.stderr)
            return 1
        song_dirs = [
            d for d in sorted(args.songs_dir.iterdir())
            if d.is_dir() and not d.name.startswith(".")
        ]
    if args.limit is not None:
        song_dirs = song_dirs[: args.limit]

    mode = "write" if args.write else "dry-run"
    print(f"[{mode}] {len(song_dirs)} song folder(s)\n")

    tallies: dict[str, int] = {}
    for index, song_dir in enumerate(song_dirs, start=1):
        # Named before the work, not after: measuring a stem means decoding it
        # whole, so a song can hold the run for a second or two and there is
        # otherwise nothing on screen to say which one.
        if not args.terse:
            print(f"[{index}/{len(song_dirs)}] {song_dir.name}", flush=True)
        try:
            outcome = process(song_dir, force=args.force, terse=args.terse, write=args.write)
        except KeyboardInterrupt:
            print("\ninterrupted; tags already written are kept")
            return 130
        except Exception as exc:  # one odd file should not end the run
            reason = concise_error(exc)
            print(f"    FAILED: {reason}")
            print(f"{song_dir.name}: {reason}", file=sys.stderr)
            # Memo this too, not just the clean "could not measure" path: an
            # exception is still a song that will fail the same way tomorrow.
            if args.write:
                write_failure_memo(song_dir, reason)
            outcome = "failed"
        tallies[outcome] = tallies.get(outcome, 0) + 1

    print(f"\n[{mode}] " + ", ".join(f"{name}: {count}" for name, count in sorted(tallies.items())))
    if not args.write:
        print("Re-run with --write to apply. Only tags are written; audio is never re-encoded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
