#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Scan song folders for split vocals/instrumental audio and ensure the
corresponding USDX chart(s) declare correct #VOCALS / #INSTRUMENTAL tags.

For every immediate subdirectory of --songs-dir that has a vocals.ogg:
  * if instrumental.ogg is also present, use it as-is;
  * else if accompaniment.ogg is present (the name some older tooling used),
    rename it to instrumental.ogg;
  * every *.txt chart in that folder then has its #VOCALS/#INSTRUMENTAL tags
    added if missing, corrected if they still point at accompaniment.ogg
    after a rename, and deduplicated if the tag appears more than once.

--import-stranded FILE handles the one-off case where Melody Mania (a vocal
separation tool) fails to write its split output into a song folder when the
filename contains unicode characters, leaving a "<name>.vocals.ogg" /
"<name>.accompaniment.ogg" pair stranded elsewhere (typically under
%APPDATA%/LocalLow). Point it at either stranded file and it moves both into
songs/<name>/ as vocals.ogg/instrumental.ogg and tags the chart, then exits
without running the full scan.

Defaults to a dry run; pass --write to actually modify files.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from utils import audio_lengths

# Windows consoles are frequently stuck on a legacy codepage (e.g. cp1252)
# that can't represent every character in these songs' filenames. Reconfigure
# stdout/stderr to UTF-8 so printing a path never crashes the run.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

VOCALS_NAME = "vocals.ogg"
INSTRUMENTAL_NAME = "instrumental.ogg"
ACCOMPANIMENT_NAME = "accompaniment.ogg"

VOCALS_STRANDED_SUFFIX = ".vocals.ogg"
ACCOMPANIMENT_STRANDED_SUFFIX = ".accompaniment.ogg"

# utf-8-sig decodes UTF-8 with or without a leading BOM and drops it. Charts
# are always written back as plain UTF-8, so a BOM never survives an edit.
TAG_ENCODINGS = ("utf-8-sig", "cp1252")


def find_case_insensitive(directory: Path, name: str) -> Path | None:
    lower = name.lower()
    for entry in directory.iterdir():
        if entry.is_file() and entry.name.lower() == lower:
            return entry
    return None


def read_text_preserving_encoding(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in TAG_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        # A stray BOM anywhere breaks header parsing (it is not whitespace,
        # so a line starting with one never looks like a #TAG line).
        return text.replace("﻿", ""), "utf-8" if encoding == "utf-8-sig" else encoding
    # cp1252 maps every byte, so this should be unreachable, but fall back
    # to a lossy decode rather than crashing on a stray file.
    return raw.decode("utf-8", errors="replace"), "utf-8"


def tag_indices(lines: list[str], tag: str) -> list[int]:
    prefix = f"#{tag}:".lower()
    return [i for i, line in enumerate(lines) if line.strip().lower().startswith(prefix)]


def header_end_index(lines: list[str]) -> int:
    """Index of the first line that is not a #TAG: header line."""
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            return i
    return len(lines)


def ensure_tags(
    chart: Path,
    vocals_name: str,
    instrumental_name: str,
    *,
    fix_instrumental: bool,
    write: bool,
) -> list[str]:
    """Add/fix/dedupe #VOCALS and #INSTRUMENTAL tags. Returns a description
    of each change made (empty if the chart already looked correct)."""
    text, encoding = read_text_preserving_encoding(chart)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    changes: list[str] = []

    def process(tag: str, canonical_value: str, force_fix: bool) -> None:
        indices = tag_indices(lines, tag)
        if not indices:
            insert_at = header_end_index(lines)
            lines.insert(insert_at, f"#{tag}:{canonical_value}")
            changes.append(f"added #{tag}:{canonical_value}")
            return

        keep = indices[0]
        if len(indices) > 1:
            for idx in indices[1:][::-1]:
                del lines[idx]
            changes.append(f"removed {len(indices) - 1} duplicate #{tag} tag(s)")

        if force_fix:
            current_value = lines[keep].split(":", 1)[1].strip() if ":" in lines[keep] else ""
            if current_value.lower() != canonical_value.lower():
                lines[keep] = f"#{tag}:{canonical_value}"
                changes.append(f"fixed #{tag}:{current_value} -> #{tag}:{canonical_value}")

    process("VOCALS", vocals_name, False)
    process("INSTRUMENTAL", instrumental_name, fix_instrumental)

    if not changes:
        return []

    if write:
        new_text = newline.join(lines) + newline
        chart.write_bytes(new_text.encode(encoding))

    return changes


def process_song_dir(
    song_dir: Path, songs_dir: Path, *, tolerance: float, terse: bool, write: bool
) -> tuple[int, int, bool] | None:
    """Returns (charts_checked, charts_changed, stems_desynced), or None if
    this folder has no split audio to act on."""
    vocals = find_case_insensitive(song_dir, VOCALS_NAME)
    instrumental = find_case_insensitive(song_dir, INSTRUMENTAL_NAME)
    accompaniment = None
    if vocals and not instrumental:
        accompaniment = find_case_insensitive(song_dir, ACCOMPANIMENT_NAME)
    if not vocals or not (instrumental or accompaniment):
        return None

    fix_instrumental = accompaniment is not None
    instrumental_name = INSTRUMENTAL_NAME if fix_instrumental else instrumental.name
    vocals_name = vocals.name
    charts = [
        chart
        for chart in sorted(song_dir.glob("*.txt"))
        if not chart.name.startswith("._")  # macOS AppleDouble sidecar file
    ]

    # Work out what would change before measuring anything. Most folders are
    # already tagged, and probing every one of those with ffprobe would cost
    # minutes to learn there was nothing to do.
    pending = {
        chart: ensure_tags(
            chart, vocals_name, instrumental_name, fix_instrumental=fix_instrumental, write=False
        )
        for chart in charts
    }
    if not fix_instrumental and not any(pending.values()):
        return len(charts), 0, False

    # Tagging stems that don't belong to this folder's own mix would point the
    # chart at audio that plays offset against its notes, so leave those alone
    # and say so. Nothing to compare against is not a mismatch.
    matches, explanation = audio_lengths.stems_match_mix(song_dir, tolerance)
    if matches is False:
        if not terse:
            print(f"skip (stems do not match the full mix): {song_dir.relative_to(songs_dir)}")
            print(f"    {explanation}")
        return len(charts), 0, True

    if accompaniment:
        target = song_dir / INSTRUMENTAL_NAME
        verb = "renamed" if write else "would rename"
        print(f"{verb}: {accompaniment.relative_to(songs_dir)} -> {target.relative_to(songs_dir)}")
        if write:
            accompaniment.rename(target)

    changed = 0
    for chart, changes in pending.items():
        if not changes:
            continue
        changed += 1
        if write:
            ensure_tags(
                chart, vocals_name, instrumental_name, fix_instrumental=fix_instrumental, write=True
            )
        verb = "updated" if write else "would update"
        print(f"{verb}: {chart.relative_to(songs_dir)} -> {'; '.join(changes)}")

    return len(charts), changed, False


def import_stranded(stranded_file: Path, songs_dir: Path, *, write: bool) -> int:
    """Move a Melody Mania stranded vocals/accompaniment pair into its song
    folder as vocals.ogg/instrumental.ogg, then tag the chart(s)."""
    name = stranded_file.name
    parent = stranded_file.parent

    if name.endswith(ACCOMPANIMENT_STRANDED_SUFFIX):
        song_name = name[: -len(ACCOMPANIMENT_STRANDED_SUFFIX)]
        accompaniment_src = stranded_file
        vocals_src = parent / f"{song_name}{VOCALS_STRANDED_SUFFIX}"
    elif name.endswith(VOCALS_STRANDED_SUFFIX):
        song_name = name[: -len(VOCALS_STRANDED_SUFFIX)]
        vocals_src = stranded_file
        accompaniment_src = parent / f"{song_name}{ACCOMPANIMENT_STRANDED_SUFFIX}"
    else:
        print(
            f"error: filename must end with '{VOCALS_STRANDED_SUFFIX}' or "
            f"'{ACCOMPANIMENT_STRANDED_SUFFIX}': {stranded_file}",
            file=sys.stderr,
        )
        return 1

    if not vocals_src.is_file():
        print(f"error: sibling file not found: {vocals_src}", file=sys.stderr)
        return 1
    if not accompaniment_src.is_file():
        print(f"error: sibling file not found: {accompaniment_src}", file=sys.stderr)
        return 1

    song_dir = songs_dir / song_name
    if not song_dir.is_dir():
        print(f"error: no song folder named {song_name!r} under {songs_dir}", file=sys.stderr)
        return 1

    vocals_dst = song_dir / VOCALS_NAME
    instrumental_dst = song_dir / INSTRUMENTAL_NAME
    verb = "moved" if write else "would move"
    print(f"{verb}: {vocals_src} -> {vocals_dst}")
    print(f"{verb}: {accompaniment_src} -> {instrumental_dst}")
    if write:
        shutil.move(str(vocals_src), str(vocals_dst))
        shutil.move(str(accompaniment_src), str(instrumental_dst))

    for chart in sorted(song_dir.glob("*.txt")):
        if chart.name.startswith("._"):
            continue
        changes = ensure_tags(
            chart, VOCALS_NAME, INSTRUMENTAL_NAME, fix_instrumental=True, write=write
        )
        if changes:
            verb2 = "updated" if write else "would update"
            print(f"{verb2}: {chart.relative_to(songs_dir)} -> {'; '.join(changes)}")

    return 0


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
        "--tolerance",
        type=float,
        default=audio_lengths.DEFAULT_TOLERANCE_S,
        help="How far a stem may differ in seconds from the folder's full mix "
        "before it is left untagged (default: %(default)s).",
    )
    parser.add_argument(
        "--terse",
        action="store_true",
        help="Say nothing about folders left untagged because their stems "
        "disagree with the full mix; the closing count still reports them.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually modify files. Without this flag, only report what would change.",
    )
    parser.add_argument(
        "--import-stranded",
        dest="import_stranded",
        metavar="FILE",
        type=Path,
        help="Import one stranded Melody Mania vocals/accompaniment file into its song "
        "folder and tag it, instead of running the full scan.",
    )
    args = parser.parse_args()

    songs_dir: Path = args.songs_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    if args.import_stranded is not None:
        return import_stranded(args.import_stranded, songs_dir, write=args.write)

    changed_charts = 0
    checked_charts = 0
    folders_with_split_audio = 0
    desynced_folders = 0

    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        result = process_song_dir(
            song_dir, songs_dir, tolerance=args.tolerance, terse=args.terse, write=args.write
        )
        if result is None:
            continue
        folders_with_split_audio += 1
        checked, changed, desynced = result
        checked_charts += checked
        changed_charts += changed
        desynced_folders += desynced

    mode = "write" if args.write else "dry-run"
    print(
        f"\n[{mode}] folders with split audio: {folders_with_split_audio}, "
        f"charts checked: {checked_charts}, charts {'changed' if args.write else 'needing changes'}: {changed_charts}"
    )
    if desynced_folders:
        print(
            f"[{mode}] folders left untagged, stems disagree with the full mix: "
            f"{desynced_folders} (see prune_desynced_stems.py)"
        )
    if not args.write and changed_charts:
        print("Re-run with --write to apply changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
