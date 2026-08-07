#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["yt-dlp"]
# ///
"""Fix one song's missing video: download it and tag the chart(s).

Takes a USDB song id (matched against the folder's own .usdb marker, same
id find_missing_video.py --details prints) and a YouTube URL or bare video
id, downloads video-only (UltraStar plays a background video muted -- the
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
    uv run scripts/fix_missing_video.py 7456 mh4CgxITgbE --write

Defaults to a dry run; --write applies it. If the folder already has a
video file, this refuses to add another without --force. Charts that
already declare #VIDEO are left alone either way -- this fixes the missing
case, not a replace-what's-there case.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_missing_video import (  # noqa: E402
    VIDEO_EXTENSIONS,
    add_video_tag,
    charts_in,
    video_tag_value,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_youtube_id(text: str) -> str:
    """Accept a bare 11-char id or any of the common URL shapes."""
    text = text.strip()
    if YOUTUBE_ID_RE.match(text):
        return text

    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")

    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            candidate = parsed.path.split("/")[2] if len(parsed.path.split("/")) > 2 else ""
        else:
            candidate = ""
    else:
        candidate = ""

    if not YOUTUBE_ID_RE.match(candidate):
        raise ValueError(f"couldn't find a YouTube video id in {text!r}")
    return candidate


def find_song_folder(songs_dir: Path, song_id: int) -> list[Path]:
    """Folders whose .usdb marker matches this USDB song id."""
    matches = []
    for marker in songs_dir.glob("*/*.usdb"):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if payload.get("song_id") == song_id:
            matches.append(marker.parent)
    return sorted(set(matches))


def download_video(video_id: str, dest_stem: Path) -> Path:
    """Fetch the best video-only stream, named after dest_stem, and return its path."""
    result = subprocess.run(
        [
            "uv", "run", "--with", "yt-dlp", "yt-dlp",
            "-f", "bestvideo",
            "--no-playlist",
            "-o", f"{dest_stem}.%(ext)s",
            "--print", "after_move:filepath",
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp failed (exit {result.returncode}):\n{result.stderr.strip()[-2000:]}"
        )
    path_line = next(
        (line for line in result.stdout.splitlines() if line.strip()), ""
    ).strip()
    if not path_line or not Path(path_line).is_file():
        raise RuntimeError(f"yt-dlp reported success but no file found:\n{result.stdout}")
    return Path(path_line)


def probe_has_video_stream(path: Path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return "video" in result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("song_id", type=int, help="USDB song id, e.g. 7456")
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
        video_id = parse_youtube_id(args.youtube)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    matches = find_song_folder(args.songs_dir, args.song_id)
    if not matches:
        print(f"error: no folder under {args.songs_dir} has a .usdb marker "
              f"for song id {args.song_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: {len(matches)} folders claim usdb song id {args.song_id} "
              f"-- resolve the duplicate first:", file=sys.stderr)
        for m in matches:
            print(f"  {m.name}", file=sys.stderr)
        return 1
    song_dir = matches[0]

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
    untagged_charts = [c for c in charts if video_tag_value(c) is None]
    already_tagged = [c for c in charts if c not in untagged_charts]

    print(f"song:   {song_dir.name}")
    print(f"video:  https://www.youtube.com/watch?v={video_id}")
    print(f"USDB tag (paste into the site's own edit form yourself -- this "
          f"script never touches usdb.animux.de): #VIDEO:a={video_id},v={video_id}")
    print(f"charts: {len(untagged_charts)} to tag, "
          f"{len(already_tagged)} already declare #VIDEO (left alone)")
    for c in already_tagged:
        print(f"  already tagged: {c.name}")

    if not args.write:
        print(f"\nDRY RUN -- would download to "
              f"{song_dir.name}/{song_dir.name}.<ext> and tag "
              f"{len(untagged_charts)} chart(s). Pass --write to apply.")
        return 0

    if not untagged_charts:
        print("nothing to tag, not downloading")
        return 0

    dest_stem = song_dir / song_dir.name
    print(f"\ndownloading to {dest_stem}.<ext> ...")
    try:
        video_path = download_video(video_id, dest_stem)
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
        add_video_tag(chart, video_path.name)
        print(f"tagged: {chart.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
