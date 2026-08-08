#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["yt-dlp"]
# ///
"""Fix one song's missing video: download it and tag the chart(s).

Takes the song -- as a folder path, a bare folder name, or a USDB song id
matched against the folder's own .usdb marker -- and a YouTube URL or bare
video id, downloads video-only (UltraStar plays a background video muted -- the
real audio comes from #MP3/vocals+instrumental, so an audio track would
just be wasted bandwidth and a second, unwanted source of sound), and adds
#VIDEO to every chart in that folder that doesn't already declare one.

This is local-only: it does not touch usdb.animux.de itself. It does print
the #VIDEO:a=<id>,v=<id> line USDB itself expects (confirmed against
usdb_syncer's own Headers.str_for_usdb() whitelist and MetaTags format),
so pairing the video with the USDB entry is a one-line paste into the
site's own edit form -- still a manual, separate step.

Duration is not checked against the folder's audio -- a background video
legitimately runs a different length than the song (same convention as
resolve_duplicate_songs.py and find_missing_video.py).

    uv run scripts/fix_missing_video.py 7456 https://www.youtube.com/watch?v=mh4CgxITgbE
    uv run scripts/fix_missing_video.py "38 Special - Hold On Loosely" mh4CgxITgbE
    uv run scripts/fix_missing_video.py "./songs/38 Special - Hold On Loosely/" mh4CgxITgbE --write

The id was the only accepted form to begin with, which was awkward: the
find_missing_* scripts that tell you a video is missing print bare folder
names, so using their output meant opening the .usdb marker to look the
number up. A name or a path is taken directly now.

Defaults to a dry run; --write applies it. If the folder already has a
video file, this refuses to add another without --force. Charts that
already declare #VIDEO are left alone either way -- this fixes the missing
case, not a replace-what's-there case.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_missing_video import (  # noqa: E402
    VIDEO_EXTENSIONS,
    charts_in,
    header_end_index,
    read_text_preserving_encoding,
    video_tag_value,
)
from utils import song_folders, youtube  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

def declares_missing_video(chart: Path, song_dir: Path) -> bool:
    """Whether the chart's #VIDEO names a file that isn't in the folder.

    A broken reference is not the same as a deliberate one, and this is the
    usual state of a song whose video never downloaded: the chart arrived
    from USDB naming a video the fetch then failed to produce. Leaving those
    alone meant the script declined to do anything for exactly the songs
    find_missing_video.py had just listed.
    """
    declared = video_tag_value(chart)
    if declared is None:
        return False
    wanted = declared.lower()
    return not any(p.is_file() and p.name.lower() == wanted for p in song_dir.iterdir())


def set_video_tag(chart: Path, filename: str) -> None:
    """Add #VIDEO, or repoint an existing one, keeping its place in the header."""
    text, encoding = read_text_preserving_encoding(chart)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and line.lstrip()[1:].partition(":")[0].strip().upper() == "VIDEO":
            lines[i] = f"#VIDEO:{filename}"
            break
    else:
        lines.insert(header_end_index(lines), f"#VIDEO:{filename}")
    chart.write_bytes((newline.join(lines) + newline).encode(encoding))


def probe_has_video_stream(path: Path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return "video" in result.stdout


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
        "--force", action="store_true",
        help="Download even if the folder already has a video file.",
    )
    parser.add_argument("--write", action="store_true", help="Apply changes.")
    args = parser.parse_args()

    if not args.songs_dir.is_dir():
        print(f"error: {args.songs_dir} is not a directory", file=sys.stderr)
        return 1

    try:
        video_id = youtube.parse_video_id(args.youtube)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        song_dir = song_folders.resolve(args.song, args.songs_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    existing_videos = sorted(
        p.name for p in song_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )
    if existing_videos and not args.force:
        print(f"{song_dir.name}: already has a video ({', '.join(existing_videos)}), "
              f"skipping -- pass --force to add another anyway", file=sys.stderr)
        return 1

    charts = charts_in(song_dir)
    if not charts:
        print(f"error: {song_dir.name} has no .txt chart to tag", file=sys.stderr)
        return 1
    untagged_charts = [
        c for c in charts
        if video_tag_value(c) is None or declares_missing_video(c, song_dir)
    ]
    already_tagged = [c for c in charts if c not in untagged_charts]

    print(f"song:   {song_dir.name}")
    print(f"video:  https://www.youtube.com/watch?v={video_id}")
    print(f"USDB tag (paste into the site's own edit form yourself -- this "
          f"script never touches usdb.animux.de): #VIDEO:a={video_id},v={video_id}")
    print(f"charts: {len(untagged_charts)} to tag, "
          f"{len(already_tagged)} already declare #VIDEO (left alone)")
    for c in already_tagged:
        print(f"  already tagged: {c.name} -> {video_tag_value(c)}")
    for c in untagged_charts:
        broken = video_tag_value(c)
        if broken is not None:
            print(f"  will repoint: {c.name} -> #VIDEO:{broken} (no such file in the folder)")

    if not args.write:
        print(f"\nDRY RUN -- would download to "
              f"{song_dir.name}/{song_dir.name}.<ext> and tag "
              f"{len(untagged_charts)} chart(s). Pass --write to apply.")
        return 0

    if not untagged_charts:
        print("nothing to tag, not downloading")
        return 0

    try:
        youtube.require()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    dest_stem = song_dir / song_dir.name
    print(f"\ndownloading to {dest_stem}.<ext> ...")
    try:
        # Video only: UltraStar plays the background muted, so an audio
        # track would be wasted bandwidth and a second source of sound.
        video_path = youtube.fetch(video_id, dest_stem, ["-f", "bestvideo"])
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not probe_has_video_stream(video_path):
        video_path.unlink(missing_ok=True)
        print(f"error: downloaded file has no video stream, deleted", file=sys.stderr)
        return 1

    size_mb = video_path.stat().st_size / 1_000_000
    print(f"downloaded: {video_path.name} ({size_mb:.1f} MB)")

    for chart in untagged_charts:
        set_video_tag(chart, video_path.name)
        print(f"tagged: {chart.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
