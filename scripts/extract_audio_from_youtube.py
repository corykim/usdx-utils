#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Fix one song's missing audio: extract the audio track and tag the chart(s).

The counterpart to fix_missing_video.py, for what find_missing_audio.py
reports: a USDB download can fetch the chart and artwork and still fail on
the media, leaving a folder with nothing to play. No amount of re-tagging
helps -- the audio has to come from somewhere -- so this fetches it from a
YouTube source you pick and points #MP3 at the result.

    uv run scripts/extract_audio_from_youtube.py "Artist - Title" mh4CgxITgbE
    uv run scripts/extract_audio_from_youtube.py 7456 https://youtu.be/mh4CgxITgbE --write

Audio only: the video stream is not downloaded at all, so this is a small
fraction of what fix_missing_video.py transfers for the same source. yt-dlp
transcodes to Ogg Vorbis by default (--audio-format), which is what most of
this library already uses and what UltraStar handles happily.

**Unlike a video, the audio's length is checked against the chart.** A
background video legitimately runs longer or shorter than the song, but
#MP3 is the audio the notes are timed against: beat b falls at
(#GAP + b * 60000/(#BPM*4)) ms, so a chart whose last note lands past the
end of the file is being played against the wrong recording -- a radio
edit, a different live take, a track that isn't the song at all. That is
the whole failure this library's stem checks exist to catch, and it is
cheaper to catch before the file is kept than after. A download that fails
the check is deleted and reported; --force keeps it anyway.

Defaults to a dry run; --write applies it. If the folder already has audio,
this refuses without --force rather than adding a second source.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fix_missing_mp3 import (  # noqa: E402
    mp3_index,
    read_text_preserving_encoding,
    set_mp3_tag,
)
from find_missing_video import charts_in  # noqa: E402
from utils import audio_lengths, song_folders, youtube  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Ogg Vorbis: the format most of this library's full mixes already use.
DEFAULT_AUDIO_FORMAT = "vorbis"

# yt-dlp names the codec, not the container, and for two of them those differ.
# Only used to describe the result before it exists -- the real path is read
# back from yt-dlp, which knows what it actually wrote.
FORMAT_EXTENSIONS = {"vorbis": "ogg", "alac": "m4a"}


def expected_extension(audio_format: str) -> str:
    return FORMAT_EXTENSIONS.get(audio_format, audio_format)

# A chart's last note may legitimately sit a beat or two inside a fade-out,
# and #GAP is measured by hand, so demand only that the audio reach the end
# of the singing -- not that it match some exact length.
CHART_FIT_TOLERANCE_S = 2.0


class ChartTiming(NamedTuple):
    """Where a chart sits inside its audio file, in seconds from the start."""

    last_note_end: float | None  # from #GAP and the final note's beat
    start: float | None  # #START, where playback begins
    end: float | None  # #END, where playback stops

    @property
    def audio_must_reach(self) -> float | None:
        """How far into the file the audio has to go for this chart to play.

        #END stops playback, so notes past it are never reached and cannot
        be a reason to demand more audio -- a chart that leaves stray notes
        in an outro it deliberately cuts is common enough that ignoring the
        tag rejects perfectly good downloads.
        """
        if self.last_note_end is None:
            return self.end
        if self.end is None:
            return self.last_note_end
        return min(self.last_note_end, self.end)


def _header_float(value: str) -> float | None:
    try:
        # Charts written on a German locale use a decimal comma.
        return float(value.strip().replace(",", "."))
    except ValueError:
        return None


def chart_timing(chart: Path) -> ChartTiming:
    """Read a chart's timing tags and work out when its last note finishes.

    Beat b falls at (#GAP + b * 60000/(#BPM*4)) ms, so #GAP already accounts
    for however much of the file runs before the singing starts.

    Mind the units, which differ between the two tags UltraStar uses to trim
    playback: **#START is in seconds, #END is in milliseconds.** Both are
    normalized to seconds here.
    """
    gap = bpm = start = end = None
    last_beat = 0.0
    seen_note = False

    for line in read_text_preserving_encoding(chart)[0].splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            key, _, value = stripped[1:].partition(":")
            key = key.strip().upper()
            if key == "GAP":
                gap = _header_float(value)
            elif key == "BPM":
                bpm = _header_float(value)
            elif key == "START":
                start = _header_float(value)
            elif key == "END":
                milliseconds = _header_float(value)
                end = None if milliseconds is None else milliseconds / 1000.0
        elif stripped[:1] in (":", "*", "F", "R", "G"):
            parts = stripped.split()
            if len(parts) >= 3:
                try:
                    last_beat = max(last_beat, int(parts[1]) + int(parts[2]))
                    seen_note = True
                except ValueError:
                    pass

    last_note_end = None
    if gap is not None and bpm and seen_note:
        last_note_end = (gap + last_beat * 60000.0 / (bpm * 4)) / 1000.0
    return ChartTiming(last_note_end=last_note_end, start=start, end=end)


def describe_requirement(chart: Path, timing: ChartTiming) -> str | None:
    """One line on what this chart needs from the audio, or None if unknowable."""
    needed = timing.audio_must_reach
    if needed is None:
        return None
    why = f"{chart.name} needs audio out to {needed:.1f}s"
    if timing.end is not None and timing.last_note_end is not None:
        if timing.end < timing.last_note_end:
            why += f" (#END cuts playback at {timing.end:.1f}s, before the last note)"
        else:
            why += f" (last note; #END is {timing.end:.1f}s)"
    if timing.start:
        why += f", playing from #START {timing.start:.1f}s"
    return why


def chart_outruns_audio(song_dir: Path, audio: Path) -> str | None:
    """An explanation if any chart needs more audio than this file has, else None.

    Only the end is checked. Whatever runs before the song is accounted for
    by #GAP and skipped by #START, and a file that continues past the song
    is normal -- an outro, applause, a second track. Too short is the only
    way the audio can be the wrong recording.
    """
    length = audio_lengths.audio_duration(audio)
    if length is None:
        return f"could not measure {audio.name}"
    for chart in charts_in(song_dir):
        timing = chart_timing(chart)
        needed = timing.audio_must_reach
        if needed is None:
            continue  # nothing to check it against; not evidence of a problem
        if needed > length + CHART_FIT_TOLERANCE_S:
            return (
                f"{chart.name} needs audio out to {needed:.1f}s but {audio.name} is "
                f"only {length:.1f}s long -- this looks like a different recording"
            )
    return None


def declares_missing_audio(chart: Path, song_dir: Path) -> bool:
    """Whether the chart's #MP3 fails to name audio this folder can play.

    Present is not the same as playable. A folder whose video was fetched by
    fix_missing_video.py holds an .mp4 with no audio stream at all, and #MP3
    routinely points at it; treating that as audio already handled is what
    made this script decline to act on the songs it exists for.
    """
    text = read_text_preserving_encoding(chart)[0]
    lines = text.splitlines()
    index = mp3_index(lines)
    if index is None:
        return True
    named = lines[index].split(":", 1)[1].strip().lower()
    if not named:
        return True
    target = next(
        (p for p in song_dir.iterdir() if p.is_file() and p.name.lower() == named), None
    )
    if target is None:
        return True
    return audio_lengths.has_audio_stream(target) is False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("song", help=song_folders.HELP)
    parser.add_argument("youtube", help="YouTube URL or bare 11-char video id")
    parser.add_argument(
        "--songs-dir", type=Path,
        default=Path(__file__).resolve().parent.parent / "songs",
        help="Directory containing one song folder per subdirectory (default: ../songs)",
    )
    parser.add_argument(
        "--audio-format", default=DEFAULT_AUDIO_FORMAT,
        help=f"yt-dlp --audio-format value (default: {DEFAULT_AUDIO_FORMAT})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Download even if the folder already has audio, and keep a result "
             "whose length disagrees with the chart.",
    )
    parser.add_argument("--write", action="store_true", help="Apply changes.")
    args = parser.parse_args()

    if not args.songs_dir.is_dir():
        print(f"error: {args.songs_dir} is not a directory", file=sys.stderr)
        return 1

    try:
        song_dir = song_folders.resolve(args.song, args.songs_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        video_id = youtube.parse_video_id(args.youtube)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    existing = audio_lengths.find_full_mix(song_dir)
    if existing is not None and not args.force:
        print(f"{song_dir.name}: already has audio ({existing.name}), skipping "
              f"-- pass --force to add another anyway", file=sys.stderr)
        return 1

    charts = charts_in(song_dir)
    if not charts:
        print(f"error: {song_dir.name} has no .txt chart to tag", file=sys.stderr)
        return 1
    untagged = [c for c in charts if declares_missing_audio(c, song_dir)]
    already_tagged = [c for c in charts if c not in untagged]

    print(f"song:   {song_dir.name}")
    print(f"audio:  https://www.youtube.com/watch?v={video_id}  (as {args.audio_format})")
    print(f"charts: {len(untagged)} to tag, {len(already_tagged)} already point at audio "
          f"that is there (left alone)")
    for chart in already_tagged:
        print(f"  already tagged: {chart.name}")

    for chart in charts:
        requirement = describe_requirement(chart, chart_timing(chart))
        if requirement is not None:
            print(f"  {requirement}")

    if not args.write:
        print(f"\nDRY RUN -- would extract audio to "
              f"{song_dir.name}/{song_dir.name}.{expected_extension(args.audio_format)} "
              f"and tag {len(untagged)} chart(s). Pass --write to apply.")
        return 0

    if not untagged and not args.force:
        print("nothing to tag, not downloading")
        return 0

    try:
        youtube.require()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    dest_stem = song_dir / song_dir.name
    print(f"\nextracting audio to {dest_stem}.{expected_extension(args.audio_format)} ...")
    try:
        audio_path = youtube.fetch(
            video_id,
            dest_stem,
            ["-f", "bestaudio/best", "-x",
             "--audio-format", args.audio_format,
             "--audio-quality", "0"],
        )
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    problem = chart_outruns_audio(song_dir, audio_path)
    if problem and not args.force:
        audio_path.unlink(missing_ok=True)
        audio_lengths.forget(audio_path)
        print(f"error: {problem}", file=sys.stderr)
        print("deleted the download; try another source, or --force to keep it.",
              file=sys.stderr)
        return 1
    if problem:
        print(f"warning: {problem} (kept anyway, --force)")

    size_mb = audio_path.stat().st_size / 1_000_000
    length = audio_lengths.audio_duration(audio_path)
    print(f"extracted: {audio_path.name} ({size_mb:.1f} MB"
          f"{f', {length:.1f}s' if length else ''})")

    for chart in untagged:
        set_mp3_tag(chart, audio_path.name, write=True)
        print(f"tagged: {chart.name} -> #MP3:{audio_path.name}")

    print("\nThe chart is timed against this audio -- check #GAP still lines up "
          "before trusting it; a different source usually starts at a different offset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
