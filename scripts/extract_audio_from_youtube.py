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
    uv run scripts/extract_audio_from_youtube.py "Artist - Title" ./downloads/clip.mkv --write

The source can equally be a **local video or audio file**, in which case the
track is copied straight out of it with ffmpeg and nothing is downloaded --
useful when the folder already holds a video that has sound, or when the
audio came from somewhere yt-dlp cannot reach. A file that turns out to have
no audio track is refused before anything is written, since that is exactly
the case this script exists to repair and re-creating it would be no help.

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

--trim cuts the result down to the song and shifts the timing tags to match.
The window is #START to #END -- the spec's own "start and end point for the
song" -- so what it removes is exactly what playback already skipped. #GAP is
deliberately not used as the lower bound: it marks beat 0, and the bars of
intro before the first note are what a singer comes in on.

Everything is measured from the start of the audio file, so cutting T seconds
off the front moves #GAP and #END earlier by T and retires #START. #VIDEOGAP
moves the *other* way: per the spec it is the video's delay relative to the
audio, positive delaying it and negative skipping into it, so with the audio
now starting later in the song the video must skip an equal amount to stay
level and the value goes down. The check that matters is that the same video
frame still lands on beat 0, and it does: 30000/+5 and 10000/-15 both put it
at 25.0s. That sign cannot be inferred from a library's values -- this one
uses both -- so it comes from the spec:
https://github.com/UltraStar-Deluxe/format

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
    header_end_index,
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


# What to encode with for a given yt-dlp --audio-format name, when pulling the
# track out of a local file. yt-dlp picks these itself; ffmpeg has to be told.
LOCAL_ENCODERS = {
    "vorbis": ["-c:a", "libvorbis", "-q:a", "8"],
    "mp3": ["-c:a", "libmp3lame", "-q:a", "0"],
    "opus": ["-c:a", "libopus", "-b:a", "192k"],
    "flac": ["-c:a", "flac"],
    "wav": ["-c:a", "pcm_s16le"],
    "aac": ["-c:a", "aac", "-b:a", "256k"],
    "m4a": ["-c:a", "aac", "-b:a", "256k"],
}


def extract_from_file(source: Path, dest_stem: Path, audio_format: str) -> Path:
    """Copy the audio track out of a local video (or audio) file.

    -vn drops the video rather than transcoding it, so this costs seconds
    even for a large file: only the audio is re-encoded, and only because
    the library wants one container throughout.
    """
    destination = dest_stem.with_name(f"{dest_stem.name}.{expected_extension(audio_format)}")
    encoder = LOCAL_ENCODERS.get(audio_format, [])
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-vn", *encoder, str(destination)],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise RuntimeError(
            f"ffmpeg could not extract audio from {source.name}: "
            f"{detail[-1] if detail else f'exit {result.returncode}'}"
        )
    if not destination.is_file():
        raise RuntimeError(f"ffmpeg reported success but {destination.name} is not there")
    return destination


def format_number(value: float) -> str:
    """Write a tag value back without gaining a spurious ".0"."""
    rounded = round(value, 3)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:g}"


def find_header(lines: list[str], key: str) -> int | None:
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            return None  # past the header, into the notes
        if line.lstrip()[1:].partition(":")[0].strip().upper() == key:
            return index
    return None


def update_header_tags(chart: Path, updates: dict[str, str | None], *, write: bool) -> list[str]:
    """Set, add or remove header tags, keeping existing ones where they are."""
    text, encoding = read_text_preserving_encoding(chart)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    changes: list[str] = []

    for key, value in updates.items():
        index = find_header(lines, key)
        if value is None:
            if index is not None:
                changes.append(f"removed #{key}:{lines[index].split(':', 1)[1].strip()}")
                del lines[index]
        elif index is None:
            lines.insert(header_end_index(lines), f"#{key}:{value}")
            changes.append(f"added #{key}:{value}")
        else:
            previous = lines[index].split(":", 1)[1].strip()
            if previous != value:
                lines[index] = f"#{key}:{value}"
                changes.append(f"#{key}:{previous} -> {value}")

    if changes and write:
        chart.write_bytes((newline.join(lines) + newline).encode(encoding))
    return changes


def trim_bounds(charts: list[Path]) -> tuple[float, float | None]:
    """Where the song actually starts and stops inside the audio.

    #START and #END are the spec's own "start and end point for the song",
    so the region outside them is what playback already skips -- cutting it
    loses nothing that was ever heard. #GAP is deliberately *not* used as a
    lower bound: it marks beat 0, and the bars of intro before the first
    note are what a singer comes in on.

    Across a duet's two charts the widest window wins, so trimming can never
    remove audio one of them still needs.
    """
    starts = [t.start for t in map(chart_timing, charts) if t.start]
    ends = [t.end for t in map(chart_timing, charts) if t.end]
    return (min(starts) if starts else 0.0), (max(ends) if ends else None)


def trim_audio(audio: Path, start: float, stop: float | None, audio_format: str) -> None:
    """Cut the file down to [start, stop] seconds, in place."""
    staged = audio.with_name(f"{audio.stem}.trimming{audio.suffix}")
    argv = ["ffmpeg", "-v", "error", "-y"]
    if start > 0:
        argv += ["-ss", f"{start:.3f}"]
    if stop is not None:
        # A duration, not an end point: -to's meaning shifts depending on
        # whether it sits before or after -i, and -t's does not.
        argv += ["-t", f"{stop - start:.3f}"]
    argv += ["-i", str(audio), "-vn", *LOCAL_ENCODERS.get(audio_format, []), str(staged)]

    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        staged.unlink(missing_ok=True)
        detail = result.stderr.strip().splitlines()
        raise RuntimeError(
            f"ffmpeg could not trim {audio.name}: "
            f"{detail[-1] if detail else f'exit {result.returncode}'}"
        )
    staged.replace(audio)
    # Same path, new length: the cached duration describes a file that is gone.
    audio_lengths.forget(audio)


def retag_after_trim(
    chart: Path, removed: float, *, has_video: bool, write: bool
) -> list[str]:
    """Shift a chart's timing tags to match audio whose head was cut off.

    Everything is measured from the start of the audio file, so removing
    `removed` seconds from it moves every one of them earlier by that much.

    #VIDEOGAP is the exception, and it moves the other way. Per the format
    spec it is the delay of the video *relative to the audio*, positive
    delaying the video and negative skipping into it -- so with the audio
    now starting later in the song, the video has to skip an equal amount to
    stay level, and the value goes down. Getting that sign backwards would
    desync every video it touched, and it cannot be inferred from the values
    in a library: this one holds both signs.
    """
    timing = chart_timing(chart)
    updates: dict[str, str | None] = {}

    text = read_text_preserving_encoding(chart)[0]
    lines = text.splitlines()

    gap_index = find_header(lines, "GAP")
    if gap_index is not None:
        current = _header_float(lines[gap_index].split(":", 1)[1])
        if current is not None:
            updates["GAP"] = format_number(current - removed * 1000.0)

    # #START described where the song began in the old file; the new file
    # begins there, so the tag has nothing left to say.
    if timing.start:
        updates["START"] = None
    if timing.end is not None:
        updates["END"] = format_number((timing.end - removed) * 1000.0)

    if has_video:
        videogap_index = find_header(lines, "VIDEOGAP")
        current = 0.0
        if videogap_index is not None:
            current = _header_float(lines[videogap_index].split(":", 1)[1]) or 0.0
        updates["VIDEOGAP"] = format_number(current - removed)

    return update_header_tags(chart, updates, write=write)


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
    parser.add_argument(
        "source",
        help="a YouTube URL or bare 11-char video id, or a path to a local video "
             "or audio file to pull the track out of",
    )
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
        "--extractor-args", default="youtube:player-client=web_embedded,web,tv",
        metavar="EXTRACTOR_ARGS",
        help="yt-dlp --extractor-args value (default: youtube:player-client=web_embedded,web,tv)",
    )
    parser.add_argument(
        "--trim", action="store_true",
        help="Cut the result down to the song, using #START/#END, and shift "
             "#GAP/#END/#VIDEOGAP to match.",
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

    # An existing file wins over a YouTube reading, the same precedence
    # song_folders.resolve uses: what is on disk is unambiguous.
    local_source = Path(song_folders.clean_argument(args.source))
    video_id = None
    if local_source.is_file():
        if audio_lengths.has_audio_stream(local_source) is False:
            print(f"error: {local_source.name} has no audio track to extract",
                  file=sys.stderr)
            return 1
    else:
        local_source = None
        try:
            video_id = youtube.parse_video_id(args.source)
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

    described = (
        str(local_source) if local_source is not None
        else f"https://www.youtube.com/watch?v={video_id}"
    )
    print(f"song:   {song_dir.name}")
    print(f"source: {described}  (as {args.audio_format})")
    print(f"charts: {len(untagged)} to tag, {len(already_tagged)} already point at audio "
          f"that is there (left alone)")
    for chart in already_tagged:
        print(f"  already tagged: {chart.name}")

    for chart in charts:
        requirement = describe_requirement(chart, chart_timing(chart))
        if requirement is not None:
            print(f"  {requirement}")

    trim_start, trim_stop = trim_bounds(charts) if args.trim else (0.0, None)
    has_video = any(
        p.is_file() and p.suffix.lower() in audio_lengths.VIDEO_EXTENSIONS
        for p in song_dir.iterdir()
    )
    if args.trim:
        if trim_start <= 0 and trim_stop is None:
            print("  --trim: no #START or #END to trim to, keeping the whole file")
        else:
            window = f"{trim_start:.1f}s to " + (f"{trim_stop:.1f}s" if trim_stop else "the end")
            print(f"  --trim: cutting to {window}"
                  + (f", shifting tags back by {trim_start:.1f}s" if trim_start > 0 else ""))
            if trim_start > 0 and has_video:
                print(f"          and #VIDEOGAP down by {trim_start:.1f}s, so the "
                      f"video skips the same amount and stays level")

    if not args.write:
        print(f"\nDRY RUN -- would extract audio to "
              f"{song_dir.name}/{song_dir.name}.{expected_extension(args.audio_format)} "
              f"and tag {len(untagged)} chart(s). Pass --write to apply.")
        return 0

    if not untagged and not args.force:
        print("nothing to tag, not extracting")
        return 0

    if local_source is None:
        try:
            youtube.require()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    dest_stem = song_dir / song_dir.name
    print(f"\nextracting audio to {dest_stem}.{expected_extension(args.audio_format)} ...")
    try:
        if local_source is not None:
            audio_path = extract_from_file(local_source, dest_stem, args.audio_format)
        else:
            audio_path = youtube.fetch(
                video_id,
                dest_stem,
                ["-f", "bestaudio/best", "-x",
                 "--audio-format", args.audio_format,
                 "--audio-quality", "0",
                 "--extractor-args", args.extractor_args],
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

    # Trimming happens after the fit check, so the check still compares the
    # chart against the file its tags actually describe.
    if args.trim and (trim_start > 0 or trim_stop is not None):
        try:
            trim_audio(audio_path, trim_start, trim_stop, args.audio_format)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print("the untrimmed audio is still in place.", file=sys.stderr)
            return 1
        if trim_start > 0:
            for chart in charts:
                for change in retag_after_trim(
                    chart, trim_start, has_video=has_video, write=True
                ):
                    print(f"retimed: {chart.name} -> {change}")

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
