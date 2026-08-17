#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Report missing background videos, fix untagged ones, and download from YouTube.

Without a song argument, scans the library. The default view is a dry-run
summary of what would change. --report-only instead prints a bare song listing
to stdout (redirectable, matching what find_missing_video.py used to produce).
--write applies the changes: adds #VIDEO to charts whose folder has an untagged
video file already on disk. --write --download also fetches a video for every
song that has a known video id (from .usdb marker or fix-metadata.json) but
no video file yet.

With a song argument, fixes that one song:

    fix_missing_video.py "Artist - Title" mh4CgxITgbE --write
    fix_missing_video.py 7456 "https://youtu.be/..." --write
    fix_missing_video.py "./songs/Artist - Title/" --write   # id from .usdb marker

Three separate problems are counted, since they need different fixes:

  none      the folder has no video file at all
  broken    a chart declares #VIDEO but names a file that isn't there
  untagged  a video file is sitting in the folder but no chart declares #VIDEO

"none" splits further in --report-only mode by --category into
skipped-unavailable (USDB never offered a video) and download-failed
(the fetch failed, retryable).

When yt-dlp reports "Video unavailable", a .video-unavailable.json memo is
written into the song folder and future library scans skip that song
automatically. Use --retry-unavailable to attempt it again anyway.

Playlist and radio URLs work fine -- only the v= id is used. Quote the URL in
PowerShell or the shell will treat & as a command separator.

This script is local-only: it does not touch usdb.animux.de itself. In
single-song mode it prints the #VIDEO:a=<id>,v=<id> tag the site expects,
but updating the site entry is a manual step.

Video-only download: UltraStar plays the background video muted; an audio
track would be wasted bandwidth and a second, unwanted source of sound.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from utils import fix_metadata, song_folders, youtube

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

VIDEO_EXTENSIONS = frozenset({".flv", ".mp4", ".webm", ".mkv", ".avi", ".mov", ".mpg", ".mpeg"})
TAG_ENCODINGS = ("utf-8-sig", "cp1252")
VIDEO_UNAVAILABLE_MEMO = ".video-unavailable.json"


# ---------- helpers ----------

def read_text_preserving_encoding(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in TAG_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return text.replace("﻿", ""), "utf-8" if encoding == "utf-8-sig" else encoding
    return raw.decode("utf-8", errors="replace"), "utf-8"


def header_end_index(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            return i
    return len(lines)


def video_tag_value(chart: Path) -> str | None:
    for line in read_text_preserving_encoding(chart)[0].splitlines():
        if not line.lstrip().startswith("#"):
            break
        key, _, value = line.lstrip()[1:].partition(":")
        if key.strip().upper() == "VIDEO":
            return value.strip() or None
    return None


def charts_in(directory: Path) -> list[Path]:
    return [p for p in sorted(directory.glob("*.txt")) if not p.name.startswith("._")]


def set_video_tag(chart: Path, filename: str) -> None:
    """Add #VIDEO, or repoint an existing broken one, keeping its place in the header."""
    text, encoding = read_text_preserving_encoding(chart)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if (line.lstrip().startswith("#")
                and line.lstrip()[1:].partition(":")[0].strip().upper() == "VIDEO"):
            lines[i] = f"#VIDEO:{filename}"
            break
    else:
        lines.insert(header_end_index(lines), f"#VIDEO:{filename}")
    chart.write_bytes((newline.join(lines) + newline).encode(encoding))


def declares_missing_video(chart: Path, song_dir: Path) -> bool:
    """Whether the chart's #VIDEO names a file that isn't in the folder."""
    declared = video_tag_value(chart)
    if declared is None:
        return False
    wanted = declared.lower()
    return not any(p.is_file() and p.name.lower() == wanted for p in song_dir.iterdir())


def probe_has_video_stream(path: Path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return "video" in result.stdout


def remux_to_standard_mp4(path: Path) -> bool:
    """Remux a DASH-branded or otherwise non-standard MP4 to a plain ISO MP4 in-place.

    yt-dlp's -f bestvideo downloads YouTube DASH segments, which carry
    major_brand=dash in the container header. VLC plays them fine, but stricter
    decoders (Unity's, Melody Mania's) show a black screen. A stream-copy remux
    fixes the brand without touching the video data.

    Returns True on success, False if ffmpeg failed (original kept intact).
    """
    staged = path.with_suffix(".remuxing" + path.suffix)
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-i", str(path),
            "-c", "copy",
            "-movflags", "+faststart",
            "-brand", "mp42",
            str(staged),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        staged.unlink(missing_ok=True)
        detail = (result.stderr or "").strip().splitlines()
        print(
            f"warning: remux failed ({detail[-1] if detail else f'exit {result.returncode}'}), "
            f"keeping original",
            file=sys.stderr,
        )
        return False
    staged.replace(path)
    return True


def video_id_from_marker(song_dir: Path) -> str | None:
    """Read the video id from .usdb marker (v= preferred over a=) or fix-metadata.json."""
    for marker in sorted(song_dir.glob("*.usdb")):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        sources: dict[str, str] = {}
        for token in str(payload.get("meta_tags", "")).split(","):
            if "=" in token:
                key, _, value = token.partition("=")
                key, value = key.strip(), value.strip()
                if key in ("v", "a") and value:
                    sources[key] = value
        result = sources.get("v") or sources.get("a")
        if result:
            return result
    video_section = fix_metadata.read(song_dir).get("video")
    if isinstance(video_section, dict):
        vid = str(video_section.get("id", "")).strip()
        if vid:
            return vid
    return None


def describe_fetch(directory: Path) -> str:
    """What the syncer recorded about this folder's media, for --details."""
    bits: list[str] = []
    for marker in sorted(directory.glob("*.usdb")):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            bits.append(f"{marker.stem}: unreadable marker")
            continue
        statuses = ", ".join(
            f"{part}={payload[part].get('status', '?')}"
            for part in ("video", "audio")
            if isinstance(payload.get(part), dict)
        )
        sources = {
            key: value
            for token in str(payload.get("meta_tags", "")).split(",")
            if "=" in token
            for key, value in [token.split("=", 1)]
            if key in ("v", "a")
        }
        source = " ".join(f"{k}={v}" for k, v in sorted(sources.items()))
        bits.append(
            f"usdb#{payload.get('song_id', '?')} {statuses}"
            + (f" [{source}]" if source else "")
        )
    return "; ".join(bits) if bits else "no .usdb marker"


def usdb_video_status(directory: Path) -> str | None:
    for marker in sorted(directory.glob("*.usdb")):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        video = payload.get("video")
        if isinstance(video, dict) and video.get("status"):
            return str(video["status"])
    return None


# ---------- video-unavailable memo ----------

def _is_unavailable_error(msg: str) -> bool:
    lower = msg.lower()
    return "video unavailable" in lower or "this video is not available" in lower


def _read_unavailable_memo(song_dir: Path) -> dict | None:
    path = song_dir / VIDEO_UNAVAILABLE_MEMO
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_unavailable_memo(song_dir: Path, video_id: str, error: str) -> None:
    memo = {
        "video_id": video_id,
        "error": error[-500:],
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }
    (song_dir / VIDEO_UNAVAILABLE_MEMO).write_text(
        json.dumps(memo, indent=2), encoding="utf-8"
    )


# ---------- download and tag one song ----------

_OK = 0
_ERROR = 1
_UNAVAILABLE = 2


def _process_one(
    song_dir: Path,
    video_id: str,
    *,
    force: bool,
    write: bool,
    extractor_args: str,
    verbose: bool = True,
) -> int:
    """Download and tag one song's video. Returns _OK / _ERROR / _UNAVAILABLE."""
    existing_videos = sorted(
        p.name for p in song_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )
    if existing_videos and not force:
        if verbose:
            print(
                f"{song_dir.name}: already has a video ({', '.join(existing_videos)}), "
                f"skipping -- pass --force to add another anyway",
                file=sys.stderr,
            )
        return _ERROR

    charts = charts_in(song_dir)
    if not charts:
        print(f"error: {song_dir.name} has no .txt chart to tag", file=sys.stderr)
        return _ERROR
    untagged_charts = [
        c for c in charts
        if video_tag_value(c) is None or declares_missing_video(c, song_dir)
    ]
    already_tagged = [c for c in charts if c not in untagged_charts]

    if verbose:
        print(f"song:   {song_dir.name}")
        print(f"video:  https://www.youtube.com/watch?v={video_id}")
        print(
            f"USDB tag (paste into the site's own edit form yourself -- this "
            f"script never touches usdb.animux.de): #VIDEO:a={video_id},v={video_id}"
        )
        print(
            f"charts: {len(untagged_charts)} to tag, "
            f"{len(already_tagged)} already declare #VIDEO (left alone)"
        )
        for c in already_tagged:
            print(f"  already tagged: {c.name} -> {video_tag_value(c)}")
        for c in untagged_charts:
            broken = video_tag_value(c)
            if broken is not None:
                print(f"  will repoint: {c.name} -> #VIDEO:{broken} (no such file in the folder)")

    if not write:
        print(
            f"\nDRY RUN -- would download to "
            f"{song_dir.name}/{song_dir.name}.<ext> and tag "
            f"{len(untagged_charts)} chart(s). Pass --write to apply."
        )
        return _OK

    if not untagged_charts:
        print("nothing to tag, not downloading")
        return _OK

    dest_stem = song_dir / song_dir.name
    print(f"\ndownloading to {dest_stem}.<ext> ...")
    try:
        video_path = youtube.fetch(
            video_id, dest_stem,
            ["-f", "bestvideo", "--extractor-args", extractor_args],
        )
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        msg = str(exc)
        if _is_unavailable_error(msg):
            _write_unavailable_memo(song_dir, video_id, msg)
            print(
                f"warning: {song_dir.name}: video unavailable -- memo written, "
                f"will skip on future runs (--retry-unavailable to override)",
                file=sys.stderr,
            )
            return _UNAVAILABLE
        print(f"error: {exc}", file=sys.stderr)
        return _ERROR

    if not probe_has_video_stream(video_path):
        video_path.unlink(missing_ok=True)
        print("error: downloaded file has no video stream, deleted", file=sys.stderr)
        return _ERROR

    remux_to_standard_mp4(video_path)

    size_mb = video_path.stat().st_size / 1_000_000
    print(f"downloaded: {video_path.name} ({size_mb:.1f} MB)")

    for chart in untagged_charts:
        set_video_tag(chart, video_path.name)
        print(f"tagged: {chart.name}")

    fix_metadata.set_video(song_dir, video_path, video_id=video_id)
    fix_metadata.record_basics(song_dir)

    (song_dir / VIDEO_UNAVAILABLE_MEMO).unlink(missing_ok=True)
    return _OK


# ---------- library: report-only mode ----------

def _report_library(
    songs_dir: Path,
    *,
    category: str,
    usdb_only: bool,
    full_paths: bool,
    details: bool,
) -> int:
    """Print the song listing to stdout and counts to stderr (like find_missing_video.py)."""
    no_video: list[str] = []
    broken: list[str] = []
    untagged: list[str] = []
    skipped_unavailable: list[str] = []
    download_failed: list[str] = []
    skipped_unmanaged = 0

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        if song_dir.name.startswith("."):
            continue
        if usdb_only and not any(song_dir.glob("*.usdb")):
            skipped_unmanaged += 1
            continue

        videos = {
            entry.name
            for entry in song_dir.iterdir()
            if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS
        }
        label = str(song_dir) if full_paths else song_dir.name
        if details:
            label = f"{label}  --  {describe_fetch(song_dir)}"

        if not videos:
            no_video.append(label)
            status = usdb_video_status(song_dir)
            if status == "skipped_unavailable":
                skipped_unavailable.append(label)
            elif status in ("failure", "failure_existing"):
                download_failed.append(label)
        else:
            for chart in charts_in(song_dir):
                declared = video_tag_value(chart)
                chart_label = str(chart) if full_paths else f"{song_dir.name}/{chart.name}"
                if declared is None:
                    untagged.append(chart_label)
                elif not (song_dir / declared).is_file():
                    broken.append(f"{chart_label}  ->  #VIDEO:{declared}")

    listing = {
        "none": no_video,
        "broken": broken,
        "untagged": untagged,
        "all": no_video + broken + untagged,
        "skipped-unavailable": skipped_unavailable,
        "download-failed": download_failed,
    }[category]
    for entry in listing:
        print(entry)

    other_no_video = len(no_video) - len(skipped_unavailable) - len(download_failed)
    print(
        f"\nno video file: {len(no_video)}"
        f" | #VIDEO names a missing file: {len(broken)}"
        f" | video present but untagged: {len(untagged)}",
        file=sys.stderr,
    )
    print(
        f"  of the {len(no_video)} with no video file: "
        f"{len(skipped_unavailable)} never offered by USDB, "
        f"{len(download_failed)} download failed, "
        f"{other_no_video} other (no marker, or not usdb-managed)",
        file=sys.stderr,
    )
    if usdb_only and skipped_unmanaged:
        print(f"skipped {skipped_unmanaged} folder(s) with no .usdb marker", file=sys.stderr)
    return 0


# ---------- library: fix and/or download ----------

def _fix_library(
    songs_dir: Path,
    *,
    usdb_only: bool,
    force: bool,
    write: bool,
    download: bool,
    terse: bool,
    retry_unavailable: bool,
    extractor_args: str,
) -> int:
    """Scan the library: fix untagged videos and/or download missing ones."""
    tagged: list[str] = []
    ambiguous: list[str] = []
    tagged_dirs: set[Path] = set()
    broken_count = 0
    untagged_count = 0
    no_video_count = 0
    skipped_unavailable_count = 0
    download_candidates: list[tuple[Path, str]] = []
    skipped_unmanaged = 0

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        if song_dir.name.startswith("."):
            continue
        if usdb_only and not any(song_dir.glob("*.usdb")):
            skipped_unmanaged += 1
            continue

        videos = {
            entry.name
            for entry in song_dir.iterdir()
            if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS
        }

        if not videos:
            no_video_count += 1
            if download:
                raw_id = video_id_from_marker(song_dir)
                if raw_id is not None:
                    try:
                        vid = youtube.parse_video_id(raw_id)
                    except ValueError:
                        pass
                    else:
                        if not retry_unavailable:
                            memo = _read_unavailable_memo(song_dir)
                            if memo and memo.get("video_id") == vid:
                                skipped_unavailable_count += 1
                                if not terse:
                                    print(
                                        f"skipping {song_dir.name}: video previously "
                                        f"unavailable (--retry-unavailable to try again)",
                                        file=sys.stderr,
                                    )
                                continue
                        download_candidates.append((song_dir, vid))
        else:
            for chart in charts_in(song_dir):
                declared = video_tag_value(chart)
                chart_label = f"{song_dir.name}/{chart.name}"
                if declared is None:
                    untagged_count += 1
                    if write:
                        if len(videos) == 1:
                            only = next(iter(videos))
                            set_video_tag(chart, only)
                            tagged.append(f"{chart_label}  ->  #VIDEO:{only}")
                            tagged_dirs.add(song_dir)
                        else:
                            ambiguous.append(
                                f"{chart_label}  ({len(videos)} video files, not guessing)"
                            )
                elif not (song_dir / declared).is_file():
                    broken_count += 1

    for sd in tagged_dirs:
        fix_metadata.record_basics(sd)

    if write:
        for entry in tagged:
            print(f"tagged: {entry}", file=sys.stderr)
        if not terse:
            for entry in ambiguous:
                print(f"skipped (ambiguous): {entry}", file=sys.stderr)
        summary = (
            f"no video file: {no_video_count}"
            f" | broken #VIDEO: {broken_count}"
            f" | untagged: {untagged_count}"
            f" | charts tagged: {len(tagged)}"
            f" | skipped as ambiguous: {len(ambiguous)}"
        )
        if download and skipped_unavailable_count:
            summary += f" | skipped as unavailable: {skipped_unavailable_count}"
        print(summary, file=sys.stderr)
    else:
        print(f"  {untagged_count} chart(s) with an untagged video file (would add #VIDEO)")
        print(f"  {broken_count} chart(s) with a broken #VIDEO reference")
        print(f"  {no_video_count} song(s) have no video file")
        if download:
            print(
                f"  of those, {len(download_candidates)} have a known video id "
                f"(would download with --write --download)"
            )
        suffix = ", --write --download to also download" if not download else ""
        print(f"Pass --write to apply{suffix}.")

    if usdb_only and skipped_unmanaged:
        print(f"skipped {skipped_unmanaged} folder(s) with no .usdb marker", file=sys.stderr)

    if not write or not download or not download_candidates:
        return 0

    try:
        youtube.require()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    errors = unavailable = 0
    for i, (song_dir, video_id) in enumerate(download_candidates, start=1):
        print(f"\n[{i}/{len(download_candidates)}] {song_dir.name}")
        code = _process_one(
            song_dir, video_id,
            force=force, write=True, extractor_args=extractor_args, verbose=False,
        )
        if code == _UNAVAILABLE:
            unavailable += 1
        elif code != _OK:
            errors += 1

    if unavailable:
        print(
            f"\n{unavailable}/{len(download_candidates)} song(s) had an unavailable video "
            f"(memo written; --retry-unavailable to override).",
            file=sys.stderr,
        )
    if errors:
        print(f"{errors}/{len(download_candidates)} song(s) failed.", file=sys.stderr)
        return 1
    return 0


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "song", nargs="?", default=None,
        help=f"{song_folders.HELP}. Omit to scan the whole library.",
    )
    parser.add_argument(
        "youtube_id", nargs="?", default=None,
        help="YouTube URL or bare 11-char video id; omit to read from .usdb marker or fix-metadata.json",
    )
    parser.add_argument(
        "--songs-dir", type=Path,
        default=Path(__file__).resolve().parent.parent / "songs",
        help="Directory containing one song folder per subdirectory (default: ../songs)",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="Print a bare song listing to stdout and exit without fixing or downloading. "
             "Matches the old find_missing_video.py behavior. Use --category to filter.",
    )
    parser.add_argument(
        "--category",
        choices=("none", "broken", "untagged", "all", "skipped-unavailable", "download-failed"),
        default="none",
        help="Which list to print with --report-only (default: none -- no video file). "
             "skipped-unavailable and download-failed are subsets of none.",
    )
    parser.add_argument(
        "--usdb-only", action="store_true",
        help="Only consider folders that have a .usdb marker.",
    )
    parser.add_argument(
        "--full-paths", action="store_true",
        help="Print full paths instead of just folder names (--report-only mode).",
    )
    parser.add_argument(
        "--details", action="store_true",
        help="Append what the .usdb marker recorded about the video status (--report-only mode).",
    )
    parser.add_argument(
        "--terse", action="store_true",
        help="Suppress messages about songs that are skipped; report only what changed.",
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Apply changes: add #VIDEO to charts whose folder already has a video file.",
    )
    parser.add_argument(
        "--download", action="store_true",
        help="With --write: also download a video for songs that have a known id "
             "(from .usdb marker or fix-metadata.json) but no video file.",
    )
    parser.add_argument(
        "--retry-unavailable", action="store_true",
        help=f"Ignore {VIDEO_UNAVAILABLE_MEMO} memos and retry songs whose video "
             f"was previously reported unavailable.",
    )
    parser.add_argument(
        "--extractor-args", default="youtube:player-client=web_embedded,web,tv",
        metavar="EXTRACTOR_ARGS",
        help="yt-dlp --extractor-args value (default: youtube:player-client=web_embedded,web,tv)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Download even if the folder already has a video file.",
    )
    args = parser.parse_args()

    if not args.songs_dir.is_dir():
        print(f"error: {args.songs_dir} is not a directory", file=sys.stderr)
        return 1

    # Single-song mode.
    if args.song is not None:
        if args.youtube_id is not None and args.report_only:
            print("error: --report-only does not apply in single-song mode", file=sys.stderr)
            return 1
        try:
            song_dir = song_folders.resolve(args.song, args.songs_dir)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        raw_id = args.youtube_id
        if raw_id is None:
            raw_id = video_id_from_marker(song_dir)
            if raw_id is None:
                print(
                    "error: no YouTube id given and none found in .usdb marker "
                    "or fix-metadata.json",
                    file=sys.stderr,
                )
                return 1
            print(f"video id from marker: {raw_id}")

        memo = _read_unavailable_memo(song_dir)
        if memo and memo.get("video_id") == raw_id and not args.retry_unavailable:
            print(
                f"note: a previous run recorded this video as unavailable "
                f"(--retry-unavailable to ignore the memo and try again)",
                file=sys.stderr,
            )

        try:
            video_id = youtube.parse_video_id(raw_id)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        if args.write:
            try:
                youtube.require()
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1

        code = _process_one(
            song_dir, video_id,
            force=args.force, write=args.write, extractor_args=args.extractor_args,
        )
        return 0 if code == _UNAVAILABLE else code

    # Library mode.
    if args.youtube_id is not None:
        print("error: a YouTube id can only be given alongside a song name", file=sys.stderr)
        return 1

    if args.report_only:
        return _report_library(
            args.songs_dir,
            category=args.category,
            usdb_only=args.usdb_only,
            full_paths=args.full_paths,
            details=args.details,
        )

    return _fix_library(
        args.songs_dir,
        usdb_only=args.usdb_only,
        force=args.force,
        write=args.write,
        download=args.download,
        terse=args.terse,
        retry_unavailable=args.retry_unavailable,
        extractor_args=args.extractor_args,
    )


if __name__ == "__main__":
    raise SystemExit(main())
