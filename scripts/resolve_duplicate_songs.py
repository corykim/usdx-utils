#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Find song folders that hold the same song more than once and reconcile
each set down to a single copy.

Copies are recognized by a normalized name: a trailing " (N)" copy marker is
stripped, all punctuation is removed, case is folded, and runs of whitespace
collapse. Punctuation is removed rather than turned into a space so
initialisms survive ("Born In The U.S.A" and "Born in the USA" both give
"usa"). Words inside square brackets remain, so "[DUET]" keeps a variant
distinct. Two folders with the same normalized name are copies of one song --
no " (N)" suffix required, which is what catches a re-download that merely
re-punctuated the title ("Bon Jovi - It's my life" vs "It's My Life").

With --merge-variants a parenthesized phrase is treated as noise instead of
part of the title, so "Song (Live)" and "Song (Album Version)" collapse onto
"Song" as well.

One copy is the "keeper"; the rest are retired into --replaced-dir. The
USDB-sourced copy is the better version, so it wins:

  * only one copy has a .usdb marker -> that copy keeps;
  * several have one -> the newest keeps, ranked on the marker's own
    "usdb_mtime" (when the entry was last revised upstream) and falling back
    to the marker file's mtime to separate two downloads of the same
    unchanged entry;
  * none has one, or they tie -> the copy without a " (N)" marker stays.

--interactive confirms each set instead: ENTER accepts the recommendation, a
number picks a different copy, S skips the set.

The normalized name is only ever used for grouping. The keeper's folder name
is preserved exactly, except that a trailing " (N)" is stripped once the
copies it was competing with have moved out of the way.

From each retired copy the keeper takes:
  * assets it is missing -- #COVER/#BACKGROUND/#VIDEO plus
    #VOCALS/#INSTRUMENTAL, since a locally split vocals/instrumental pair is
    worth preserving and fresh USDB downloads don't include one. Stems are
    picked up even if the retired chart never declared them, by falling back
    to their conventional vocals.ogg/instrumental.ogg names. An asset counts
    as missing if the keeper's chart doesn't declare that tag, or declares it
    but the file it names isn't there; an asset the keeper already has is
    never replaced. #MP3 is excluded -- the keeper's note timings belong to
    the keeper's own full-mix audio;
  * split stems only when they match the keeper's own audio length (within
    AUDIO_LENGTH_TOLERANCE_S, measured with ffprobe). Stems are separated
    from a full mix, so a mismatch means they came from a different rip and
    would play offset against the keeper's chart. Both stems come from one
    separation run, so a single probe settles it. A keeper holding only a
    video counts as having no audio and takes stems unchecked; a keeper with
    no audio *and* no video falls back to using the merged instrumental as
    its #MP3;
  * #LANGUAGE/#EDITION/#GENRE/#YEAR/#CREATOR, overwriting on conflict -- but
    only when no copy has a .usdb marker, since otherwise the keeper is
    authoritative and its own metadata already wins. Identity tags
    (#VERSION/#TITLE/#ARTIST) shouldn't need reconciling, and timing tags
    (#BPM/#GAP/#MEDLEYSTARTBEAT/#MEDLEYENDBEAT/#START/#PREVIEWSTART/
    #VIDEOGAP/#END) are calibrated to that chart's own note-beat numbers --
    copying one without rescaling every note line would desync playback -- so
    both are always left alone.

Each .usdb marker stays with its own folder; the keeper is already the
winning bearer, so none need moving. If the keeper or a retired copy has
anything other than exactly one chart, the metadata and asset merges are
skipped for that pair and reported (the folder moves still happen).

Requires ffprobe (ffmpeg) on PATH for the stem length check.

Defaults to a dry run; pass --write to actually move/modify files.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# The " (N)" a re-download leaves on a folder name. Used both to group copies
# and to name the survivor, so the two can never disagree about what the
# suffix is.
TRAILING_COPY_NUMBER_RE = re.compile(r"\s*\(\s*\d+\s*\)\s*$")

# A parenthesized phrase, e.g. the "(Live)" or "(Album Version)" that marks a
# variant. Innermost-first, so repeated substitution unwraps nested groups.
PARENTHESIZED_PHRASE_RE = re.compile(r"\([^()]*\)")

# The only descriptive tags merged from the duplicate's chart into the base's.
# Deliberately narrow: identity and timing tags are excluded, see docstring.
MERGED_TAGS = {"LANGUAGE", "EDITION", "GENRE", "YEAR", "CREATOR"}

# Assets pulled over when the keeper folder lacks them. #MP3 is deliberately
# absent: the keeper's chart timing belongs to the keeper's own full-mix audio.
ASSET_TAGS = ("COVER", "BACKGROUND", "VIDEO", "VOCALS", "INSTRUMENTAL")

# Split-audio stems are worth preserving even when the source chart never
# declared them, so fall back to their conventional filenames.
CONVENTIONAL_ASSET_NAMES = {"VOCALS": "vocals.ogg", "INSTRUMENTAL": "instrumental.ogg"}
SPLIT_AUDIO_TAGS = frozenset({"VOCALS", "INSTRUMENTAL"})

AUDIO_EXTENSIONS = frozenset({".mp3", ".ogg", ".m4a", ".wav", ".flac", ".opus"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mkv", ".avi"})
STEM_FILENAMES = frozenset({"vocals.ogg", "instrumental.ogg", "accompaniment.ogg"})

# Stems are separated from a full mix, so they should match it near-exactly.
# A whole second of slack absorbs container/codec padding while still
# catching stems that came from a different rip entirely.
AUDIO_LENGTH_TOLERANCE_S = 1.0

_duration_cache: dict[Path, float | None] = {}


def audio_duration(path: Path) -> float | None:
    """Length in seconds via ffprobe, or None if it can't be determined."""
    if path in _duration_cache:
        return _duration_cache[path]
    result: float | None = None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nokey=1:noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0:
            try:
                result = float(proc.stdout.strip())
            except ValueError:
                result = None
    except (OSError, subprocess.TimeoutExpired):
        result = None
    _duration_cache[path] = result
    return result


def classify_audio(directory: Path, chart_tags: dict[str, tuple[int, str]]) -> tuple[Path | None, bool]:
    """Return (the folder's full-mix audio file, whether it has any video).
    Split stems never count as the full mix. A folder with only a video is
    treated as having no audio file."""
    audio: Path | None = None
    tagged = chart_tags["MP3"][1].strip() if "MP3" in chart_tags else ""
    if tagged:
        candidate = directory / tagged
        if (
            candidate.is_file()
            and candidate.suffix.lower() in AUDIO_EXTENSIONS
            and candidate.name.lower() not in STEM_FILENAMES
        ):
            audio = candidate
    has_video = False
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        suffix = entry.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            has_video = True
        elif (
            audio is None
            and suffix in AUDIO_EXTENSIONS
            and entry.name.lower() not in STEM_FILENAMES
        ):
            audio = entry
    return audio, has_video

# utf-8-sig decodes UTF-8 with or without a leading BOM and drops it. Charts
# are always written back as plain UTF-8, so a BOM never survives an edit.
TAG_ENCODINGS = ("utf-8-sig", "cp1252")


def base_name_for(dir_name: str) -> str | None:
    """The name without its " (N)" copy marker, or None if it has none. Only
    the marker is removed -- the rest of the name is preserved exactly, since
    this is what the surviving folder gets called on disk."""
    stripped = TRAILING_COPY_NUMBER_RE.sub("", dir_name)
    return stripped if stripped != dir_name else None


def normalize_name(name: str, *, merge_variants: bool = False) -> str:
    """Fold a folder name to what it is *about*, so two spellings of the same
    song pair up: "The Police - Don't Stand So Close To Me" and the smart-quote
    "Don’t" version, or "Wham! - Last Christmas" and "Wham - Last Christmas".

    A trailing " (N)" is a re-download marker rather than part of the title,
    so it comes off first. Then all punctuation goes, case is folded, and
    runs of whitespace collapse to a single space (stripping punctuation
    leaves gaps behind, and some folder names carry stray double spaces).

    Punctuation is removed rather than turned into a space so initialisms
    survive: "Born In The U.S.A" and "Born in the USA" both land on "usa",
    as do "Y.M.C.A" and "YMCA". Words inside brackets are kept, so variant
    markers still separate songs -- "Barbie Girl [DUET]" normalizes to
    "barbie girl duet", which is not "barbie girl".

    With merge_variants, parenthesized phrases are dropped wholesale rather
    than just losing their brackets, so "Song (Live)" and "Song (Album
    Version)" both collapse onto "song". Square brackets are untouched even
    then -- a [DUET] is a different chart, not a different mix of one.
    """
    text = TRAILING_COPY_NUMBER_RE.sub("", name.lower())
    if merge_variants:
        while True:
            collapsed = PARENTHESIZED_PHRASE_RE.sub(" ", text)
            if collapsed == text:
                break
            text = collapsed
    kept = "".join(c if c.isalnum() else " " if c.isspace() else "" for c in text)
    return " ".join(kept.split())


def usdb_stamp(directory: Path) -> tuple[int, int] | None:
    """How recent this folder's USDB provenance is, or None if it has no
    marker. Ranks on the marker's own "usdb_mtime" -- when the entry was last
    revised upstream, which is what makes one download newer than another --
    and falls back to the marker file's own mtime to break ties between two
    downloads of the same unchanged entry. Takes the newest of several."""
    stamps: list[tuple[int, int]] = []
    for marker in sorted(directory.glob("*.usdb")):
        upstream = 0
        try:
            payload = json.loads(marker.read_text(encoding="utf-8", errors="replace"))
            value = payload.get("usdb_mtime")
            if isinstance(value, (int, float)):
                upstream = int(value)
        except (OSError, ValueError):
            pass  # unreadable or not JSON -- fall back to the file's own mtime
        try:
            downloaded = int(marker.stat().st_mtime)
        except OSError:
            downloaded = 0
        stamps.append((upstream, downloaded))
    return max(stamps) if stamps else None


def describe_stamp(stamp: tuple[int, int], *, by_download: bool = False) -> str:
    """Render whichever of the two timestamps actually decided the ranking."""
    upstream, downloaded = stamp
    if by_download or not upstream:
        return (
            f"downloaded {datetime.date.fromtimestamp(downloaded).isoformat()}"
            if downloaded
            else "unknown date"
        )
    return f"revised {datetime.date.fromtimestamp(upstream).isoformat()}"


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
    return raw.decode("utf-8", errors="replace"), "utf-8"


def header_end_index(lines: list[str]) -> int:
    """Index of the first line that is not a #TAG: header line."""
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            return i
    return len(lines)


def parse_header_tags(lines: list[str]) -> dict[str, tuple[int, str]]:
    """Map TAG -> (line index, value) for #TAG:value lines in the header."""
    tags: dict[str, tuple[int, str]] = {}
    for i in range(header_end_index(lines)):
        line = lines[i]
        if not line.startswith("#") or ":" not in line:
            continue
        key, _, value = line[1:].partition(":")
        tags[key.strip().upper()] = (i, value)
    return tags


def plan_asset_merges(
    src_dir: Path, dst_dir: Path, src_chart: Path, dst_chart: Path
) -> tuple[list[tuple[str, str, Path | None]], list[str]]:
    """Find ASSET_TAGS assets dst_dir is missing but src_dir has. Returns
    (plans, messages), where each plan is (tag, filename, source path to move
    -- or None if a file of that name is already sitting in dst_dir
    untagged)."""
    src_text, _ = read_text_preserving_encoding(src_chart)
    src_tags = parse_header_tags(src_text.splitlines())
    dst_text, _ = read_text_preserving_encoding(dst_chart)
    dst_tags = parse_header_tags(dst_text.splitlines())

    messages: list[str] = []
    dst_audio, dst_has_video = classify_audio(dst_dir, dst_tags)
    dst_audio_duration = audio_duration(dst_audio) if dst_audio else None

    def resolve_src(tag: str) -> Path | None:
        value = src_tags[tag][1].strip() if tag in src_tags else ""
        if not value:
            # Untagged split audio still counts -- preserving the stems matters
            # more than whether the source chart bothered to declare them.
            value = CONVENTIONAL_ASSET_NAMES.get(tag, "")
        if not value:
            return None
        candidate = src_dir / value
        return candidate if candidate.is_file() else None

    # Stems were separated from the retired copy's mix. If they don't match the
    # keeper's own audio length they're from a different rip and would play out
    # of sync against the keeper's chart. Both stems come out of the same
    # separation run, so one probe settles it for the pair.
    stems_match = True
    if dst_audio is not None:
        probe = resolve_src("INSTRUMENTAL") or resolve_src("VOCALS")
        if probe is not None:
            stem_duration = audio_duration(probe)
            if stem_duration is None or dst_audio_duration is None:
                messages.append(
                    f"note: could not measure {probe.name} or {dst_audio.name}; "
                    f"merging stems without a length check"
                )
            elif abs(stem_duration - dst_audio_duration) > AUDIO_LENGTH_TOLERANCE_S:
                stems_match = False
                messages.append(
                    f"skip stems: {probe.name} {stem_duration:.1f}s vs keeper audio "
                    f"{dst_audio.name} {dst_audio_duration:.1f}s (different rip)"
                )

    plans: list[tuple[str, str, Path | None]] = []
    for tag in ASSET_TAGS:
        src = resolve_src(tag)
        if src is None:
            continue  # nothing declared, or the tag points at a missing file

        if tag in dst_tags:
            dst_value = dst_tags[tag][1].strip()
            if dst_value and (dst_dir / dst_value).is_file():
                continue  # keeper already has this asset; never replace it

        if tag in SPLIT_AUDIO_TAGS and not stems_match:
            continue

        dest = dst_dir / src.name
        # If a file of that name is already there, point the tag at it rather
        # than overwriting whatever it is.
        plans.append((tag, src.name, None if dest.exists() else src))

    if dst_audio is None and not dst_has_video:
        # Nothing to play at all -- fall back to the instrumental stem, which
        # is what fix_missing_mp3.py would pick anyway.
        instrumental = next((name for tag, name, _ in plans if tag == "INSTRUMENTAL"), None)
        if instrumental is None and (dst_dir / "instrumental.ogg").is_file():
            instrumental = "instrumental.ogg"
        if instrumental is not None:
            plans.append(("MP3", instrumental, None))
            messages.append(
                f"note: songs/{dst_dir.name} has no audio file or video; "
                f"using {instrumental} as #MP3"
            )
        else:
            messages.append(
                f"PROBLEM: songs/{dst_dir.name} has no audio file, no video, "
                f"and no instrumental stem to fall back on"
            )

    return plans, messages


def merge_chart_metadata(
    dup_chart: Path,
    base_chart: Path,
    *,
    descriptive: bool = True,
    extra_tags: dict[str, str] | None = None,
    write: bool,
) -> list[str]:
    """Overwrite/add tags in base_chart: MERGED_TAGS from dup_chart's header
    when `descriptive` (skipped when base_chart is itself the USDB-sourced
    one, since then its own metadata is authoritative), plus any extra_tags
    (asset tags whose files were just moved over). Returns a description of
    each change made."""
    dup_text, _ = read_text_preserving_encoding(dup_chart)
    dup_tags = parse_header_tags(dup_text.splitlines())

    base_text, base_encoding = read_text_preserving_encoding(base_chart)
    base_newline = "\r\n" if "\r\n" in base_text else "\n"
    base_lines = base_text.splitlines()
    base_tags = parse_header_tags(base_lines)

    changes: list[str] = []
    to_insert: list[str] = []

    merges = {k: v for k, (_, v) in dup_tags.items() if k in MERGED_TAGS} if descriptive else {}
    merges.update(extra_tags or {})

    for key, dup_value in merges.items():
        if key in base_tags:
            base_idx, base_value = base_tags[key]
            if base_value != dup_value:
                changes.append(f"#{key}:{base_value} -> #{key}:{dup_value}")
                base_lines[base_idx] = f"#{key}:{dup_value}"
        else:
            to_insert.append(f"#{key}:{dup_value}")
            changes.append(f"added #{key}:{dup_value}")

    if to_insert:
        insert_at = header_end_index(base_lines)
        base_lines[insert_at:insert_at] = to_insert

    if not changes:
        return []

    if write:
        new_text = base_newline.join(base_lines) + base_newline
        base_chart.write_bytes(new_text.encode(base_encoding))

    return changes


def charts_in(directory: Path) -> list[Path]:
    return [p for p in sorted(directory.glob("*.txt")) if not p.name.startswith("._")]


def choose_keeper(members: list[Path]) -> tuple[Path, str]:
    """Pick which copy of a song survives, and explain why. USDB provenance
    wins, then the newer entry. Failing that the plainest name stays: no
    " (N)" copy marker, then the shortest name -- which under --merge-variants
    prefers "Song" over "Song (Live)" -- then alphabetical, so the choice
    never depends on directory order."""

    def rank(directory: Path) -> tuple[int, tuple[int, int], int, int]:
        stamp = usdb_stamp(directory)
        return (
            1 if stamp is not None else 0,
            stamp if stamp is not None else (0, 0),
            0 if base_name_for(directory.name) else 1,
            -len(directory.name),
        )

    # Sorting by name first and then stably by rank leaves the alphabetically
    # first copy on top whenever the ranks are equal.
    ordered = sorted(sorted(members, key=lambda d: d.name), key=rank, reverse=True)
    keeper = ordered[0]
    stamps = [usdb_stamp(m) for m in members]
    marked = [s for s in stamps if s is not None]

    if not marked:
        why = "no copy has a .usdb marker, so the plainest name stays"
    elif len(marked) == 1:
        why = "it has the only .usdb marker"
    elif len(set(marked)) == 1:
        why = f"every copy has the same .usdb marker ({describe_stamp(marked[0])}), so the plainest name stays"
    else:
        # Equal upstream revisions mean the download date ranked them.
        by_download = len({s[0] for s in marked}) == 1
        why = f"it has the newest .usdb marker ({describe_stamp(max(marked), by_download=by_download)})"
    return keeper, why


def describe_copy(directory: Path) -> str:
    """One-line summary of what a copy has going for it, for the picker."""
    stamp = usdb_stamp(directory)
    bits = [describe_stamp(stamp) if stamp else "no .usdb marker"]
    stems = sorted(
        p.name for p in directory.iterdir() if p.is_file() and p.name.lower() in STEM_FILENAMES
    )
    if stems:
        bits.append("split audio")
    if any(p.suffix.lower() in VIDEO_EXTENSIONS for p in directory.iterdir() if p.is_file()):
        bits.append("video")
    return ", ".join(bits)


def prompt_for_keeper(
    members: list[Path], recommended: Path, why: str, *, default_skip: bool = False
) -> Path | str:
    """Ask which copy to keep. Returns the chosen folder, "skip" to leave the
    group alone, or "abort" if input ran out. With default_skip, ENTER leaves
    the set alone instead of accepting the recommendation."""
    default = members.index(recommended) + 1
    for number, member in enumerate(members, start=1):
        marker = " " if default_skip else ("*" if member == recommended else " ")
        print(f"  {marker} [{number}] {member.name}")
        print(f"        {describe_copy(member)}")

    if default_skip:
        print("  every copy is USDB-sourced, so these are probably distinct entries")
        prompt = f"  keep which? [ENTER=skip, number, S=skip]: "
    else:
        print(f"  recommended: [{default}] -- {why}")
        prompt = f"  keep which? [ENTER={default}, number, S=skip]: "

    while True:
        try:
            answer = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "abort"
        if not answer:
            return "skip" if default_skip else recommended
        if answer.lower() == "s":
            return "skip"
        if answer.isdigit() and 1 <= int(answer) <= len(members):
            return members[int(answer) - 1]
        hint = "ENTER to skip" if default_skip else f"ENTER for {default}"
        print(f"  enter 1-{len(members)}, {hint}, or S to skip")


def archive_destination(replaced_dir: Path, retired: Path) -> Path | None:
    """Where a retired copy lands. Prefers its name without the " (N)" copy
    marker, but keeps the marker rather than colliding with something already
    archived; returns None when both are taken."""
    for candidate in (base_name_for(retired.name) or retired.name, retired.name):
        destination = replaced_dir / candidate
        if not destination.exists():
            return destination
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        # The module docstring is a page long; --help should stay scannable,
        # so summarize and point at the source for the full rules.
        description=(
            "Reconcile song folders that hold the same song more than once, keeping one "
            "copy and retiring the rest into --replaced-dir.\n\n"
            "Copies are matched on a normalized name: a trailing \" (N)\" is stripped, "
            "punctuation removed, case folded and whitespace collapsed -- so no \" (N)\" "
            "suffix is needed to pair up two spellings of one title. The USDB-sourced "
            "copy wins, newest first; its folder name is preserved exactly apart from "
            "shedding a trailing \" (N)\".\n\n"
            "Defaults to a dry run -- pass --write to apply. Needs ffprobe on PATH.\n"
            "See the docstring at the top of this file for the full rules."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--songs-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "songs",
        help="Directory containing one song folder per subdirectory (default: ../songs)",
    )
    parser.add_argument(
        "--replaced-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "songs.replaced",
        help="Destination for retired duplicate folders (default: ../songs.replaced)",
    )
    parser.add_argument(
        "--merge-variants",
        action="store_true",
        help="Treat a parenthesized phrase as noise rather than part of the title, so "
        "'Song (Live)' and 'Song (Album Version)' count as copies of 'Song'. "
        "Square brackets are still respected, so [DUET] stays a separate song.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Confirm each set of copies: ENTER accepts the recommended keeper, a "
        "number picks a different one, S skips the set. Implies --write, since "
        "nothing is touched without your say-so anyway.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually move/modify files. Without this flag, only report what would change.",
    )
    args = parser.parse_args()

    # Answering a prompt per group only to be told what would have happened
    # is busywork, and every change still waits on an explicit keypress.
    if args.interactive and not args.write:
        args.write = True
        print("--interactive implies --write; each set still waits for your confirmation.\n")

    songs_dir: Path = args.songs_dir
    replaced_dir: Path = args.replaced_dir
    if not songs_dir.is_dir():
        print(f"error: {songs_dir} is not a directory", file=sys.stderr)
        return 1

    resolved = 0
    renamed = 0
    skipped = 0
    problems = 0
    vacated: set[Path] = set()

    # Group every folder by normalized name: two spellings of the same song
    # are duplicates of each other whether or not either carries a " (N)".
    groups: dict[str, list[Path]] = {}
    for song_dir in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        if song_dir.name.startswith("."):
            continue
        key = normalize_name(song_dir.name, merge_variants=args.merge_variants)
        groups.setdefault(key, []).append(song_dir)

    for _key, members in sorted(groups.items()):
        if len(members) == 1:
            # Only copy of this song. Nothing to reconcile, but drop a stray
            # " (N)" so it stops looking like a duplicate.
            only = members[0]
            keeper_target = base_name_for(only.name)
            if keeper_target is None:
                continue
            target = songs_dir / keeper_target
            if target.exists():
                print(f"skip (songs/{keeper_target} already exists): {only.name}")
                skipped += 1
                continue
            verb = "renamed" if args.write else "would rename"
            print(f"{verb}: songs/{only.name} -> songs/{keeper_target}")
            if args.write:
                only.rename(target)
            renamed += 1
            continue

        keeper, why = choose_keeper(members)

        if args.interactive:
            print(f"\nduplicate ({len(members)} copies):")
            # If every copy came from USDB they are more likely deliberate,
            # distinct entries than one song downloaded twice, so make the
            # safe answer the easy one.
            choice = prompt_for_keeper(
                members,
                keeper,
                why,
                default_skip=all(usdb_stamp(m) is not None for m in members),
            )
            if choice == "abort":
                print("no more input; stopping here.", file=sys.stderr)
                break
            if choice == "skip":
                print("  skipped")
                skipped += 1
                continue
            keeper = choice
            why = "chosen interactively"
        else:
            # Names routinely contain commas, so separate them with something
            # that cannot be mistaken for part of one.
            print(f"\nduplicate ({len(members)} copies): {' | '.join(m.name for m in members)}")

        keeper_target = base_name_for(keeper.name) or keeper.name
        others = [m for m in members if m != keeper]
        print(f"  keeping songs/{keeper.name} -- {why}")

        keeper_charts = charts_in(keeper)
        any_marker = any(usdb_stamp(m) is not None for m in members)

        for retired in others:
            retired_charts = charts_in(retired)
            if len(keeper_charts) == 1 and len(retired_charts) == 1:
                asset_plans, asset_messages = plan_asset_merges(
                    retired, keeper, retired_charts[0], keeper_charts[0]
                )
                for message in asset_messages:
                    print(f"  {message}")
                    if message.startswith("PROBLEM"):
                        problems += 1
                for _tag, filename, src in asset_plans:
                    if src is None:
                        continue  # already in the keeper folder, just needs the tag
                    verb = "moved" if args.write else "would move"
                    print(f"  {verb}: songs/{retired.name}/{filename} -> songs/{keeper.name}/{filename}")
                    if args.write:
                        shutil.move(str(src), str(keeper / filename))

                changes = merge_chart_metadata(
                    retired_charts[0],
                    keeper_charts[0],
                    # Whenever any copy is USDB-sourced the keeper is the
                    # authoritative one by construction, so its own metadata
                    # wins and only missing assets get pulled across. With no
                    # marker anywhere there's no authority, so the other
                    # copy's descriptive tags are merged in.
                    descriptive=not any_marker,
                    extra_tags={tag: filename for tag, filename, _ in asset_plans},
                    write=args.write,
                )
                if changes:
                    verb = "updated" if args.write else "would update"
                    print(f"  {verb}: songs/{keeper.name}/{keeper_charts[0].name} -> {'; '.join(changes)}")
            else:
                print(
                    f"  note: songs/{keeper.name} has {len(keeper_charts)} chart(s), "
                    f"songs/{retired.name} has {len(retired_charts)}; skipping metadata/asset merge"
                )

            # Each .usdb marker stays with its own folder; the keeper is
            # already the winning bearer, so none need moving.
            destination = archive_destination(replaced_dir, retired)
            if destination is None:
                print(f"  skip move (already archived under that name): {retired.name}")
                skipped += 1
                continue

            verb = "moved" if args.write else "would move"
            print(f"  {verb}: songs/{retired.name} -> songs.replaced/{destination.name}")
            if args.write:
                replaced_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(retired), str(destination))
            vacated.add(retired)
            resolved += 1

        # The survivor sheds any " (N)" of its own, now that the copies it
        # was competing with have moved out of the way.
        if keeper.name != keeper_target:
            target = songs_dir / keeper_target
            # In a dry run the copies "moved" out are still on disk, so a
            # target this run vacates does not count as occupied.
            if target.exists() and target not in vacated:
                print(f"  skip rename (songs/{keeper_target} still exists): {keeper.name}")
                skipped += 1
            else:
                verb = "renamed" if args.write else "would rename"
                print(f"  {verb}: songs/{keeper.name} -> songs/{keeper_target}")
                if args.write:
                    keeper.rename(target)

    mode = "write" if args.write else "dry-run"
    print(
        f"\n[{mode}] resolved: {resolved}, renamed: {renamed}, "
        f"skipped: {skipped}, problems: {problems}"
    )
    if not args.write and (resolved or renamed):
        print("Re-run with --write to apply changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
