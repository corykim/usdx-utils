#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Backfill fix-metadata.json for every song in the library.

Without --ffprobe, records only what can be determined without running ffprobe:
  * filename and size_bytes of the primary audio (from the filesystem)
  * duration_s from the audio_lengths cache, if the file has been measured before
  * video.id from the .usdb marker (v= preferred, a= fallback)

With --ffprobe, also populates the full audio section (codec, sample_rate,
channels, etc.), reads any ReplayGain tag already written into the file, and
probes the background video file (from #VIDEO tag) to populate the video section.
Only runs ffprobe where data is absent or incomplete -- a song that already
has everything is skipped without probing.

Defaults to a dry run; --write applies changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from utils import audio_lengths, fix_metadata

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--songs-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "songs",
        help="Directory containing one song folder per subdirectory (default: ../songs)",
    )
    parser.add_argument(
        "--ffprobe",
        action="store_true",
        help="Also run ffprobe to populate the full audio section (codec, sample_rate, "
             "etc.) and read existing ReplayGain tags. Only probes where data is absent.",
    )
    parser.add_argument(
        "--terse",
        action="store_true",
        help="Print only songs whose sidecar changed.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply changes. Without this flag, only report what would change.",
    )
    args = parser.parse_args()

    songs_dir: Path = args.songs_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    updated = skipped = unchanged = 0

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        if song_dir.name.startswith("."):
            continue

        mix = audio_lengths.find_full_mix(song_dir)

        if args.write:
            changed = fix_metadata.record_basics(song_dir, mix)

            if args.ffprobe and mix is not None:
                existing = fix_metadata.read(song_dir)
                audio_section = existing.get("audio", {})
                needs_probe = not audio_section or "codec" not in audio_section
                if needs_probe:
                    changed = fix_metadata.set_audio(song_dir, mix) or changed

                needs_rg = "replaygain" not in existing
                if needs_rg:
                    rg = fix_metadata._read_replaygain_tags(mix)  # noqa: SLF001
                    if rg is not None:
                        gain_db, peak = rg
                        changed = fix_metadata.set_replaygain(song_dir, gain_db, peak) or changed

            if args.ffprobe:
                video_path = fix_metadata._find_declared_video(song_dir)  # noqa: SLF001
                if video_path is not None:
                    existing = fix_metadata.read(song_dir)
                    video_section = existing.get("video", {})
                    needs_video_probe = not video_section or "codec" not in video_section
                    if needs_video_probe:
                        changed = fix_metadata.set_video(song_dir, video_path) or changed

            if changed:
                updated += 1
                if not args.terse:
                    print(f"updated: {song_dir.name}")
            else:
                unchanged += 1
        else:
            # Dry run: report what would change without writing
            current = fix_metadata.read(song_dir)
            would_change = False

            if mix is not None:
                try:
                    size = mix.stat().st_size
                except OSError:
                    size = None
                if size is not None:
                    if current.get("filename") != mix.name or current.get("size_bytes") != size:
                        would_change = True
                    if not would_change and "duration_s" not in current.get("audio", {}):
                        if audio_lengths.cached_duration(mix) is not None:
                            would_change = True

            video_section = current.get("video")
            if not would_change and not (isinstance(video_section, dict) and video_section.get("id")):
                from utils.fix_metadata import _usdb_video_id  # noqa: PLC0415
                if _usdb_video_id(song_dir):
                    would_change = True

            if not would_change and args.ffprobe and mix is not None:
                audio_section = current.get("audio", {})
                if not audio_section or "codec" not in audio_section:
                    would_change = True
                if not would_change and "replaygain" not in current:
                    would_change = True

            if not would_change and args.ffprobe:
                video_path = fix_metadata._find_declared_video(song_dir)  # noqa: SLF001
                if video_path is not None:
                    vsection = current.get("video", {})
                    if not vsection or "codec" not in vsection:
                        would_change = True

            if would_change:
                updated += 1
                if not args.terse:
                    print(f"would update: {song_dir.name}")
            else:
                unchanged += 1

    mode = "write" if args.write else "dry-run"
    probe_note = " (with ffprobe)" if args.ffprobe else ""
    print(
        f"\n[{mode}] updated{probe_note}: {updated}, already complete: {unchanged}",
        file=sys.stderr,
    )
    if not args.write and updated:
        print("Re-run with --write to apply.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
