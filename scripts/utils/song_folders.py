"""Turning whatever the caller typed into a song folder.

Every script that acts on a single song takes the same argument, and there
are three reasonable things to hand it: a path, the bare folder name, or the
USDB song id. The find_missing_* scripts print bare folder names, shells
tab-complete paths, and .usdb markers carry the id -- so insisting on any
one of them means the caller converts by hand, from output another script in
this same suite just gave them. Accepting all three lives here so they stay
interchangeable everywhere rather than per-script.

This is a plain module rather than a `uv run` script, so it lives in
scripts/utils/ with the other import-only modules. A script run as
`uv run scripts/<name>.py` has scripts/ on sys.path, which makes
`from utils import song_folders` resolve without any sys.path fixing.
"""

from __future__ import annotations

import json
from pathlib import Path


def find_by_song_id(songs_dir: Path, song_id: int) -> list[Path]:
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


def clean_argument(target: str) -> str:
    """Strip what a shell adds to a directory argument but does not mean.

    Completion supplies a trailing separator, and on Windows a trailing
    backslash immediately before a closing double quote escapes that quote,
    so the argument can arrive with a stray " on the end as well. Neither is
    a mistake the caller made, so neither should be their problem.
    """
    return target.strip().rstrip('"').rstrip("\\/").strip()


def resolve(target: str, songs_dir: Path) -> Path:
    """The song folder named by a path, a bare folder name, or a USDB id.

    An existing directory wins over a numeric reading: a folder literally
    named "7456" is far-fetched, but if one is sitting there it is what the
    caller pointed at. Raises ValueError, explaining, when nothing matches.
    """
    cleaned = clean_argument(target)
    if not cleaned:
        raise ValueError("no song given")

    as_path = Path(cleaned)
    if as_path.is_dir():
        return as_path.resolve()

    if cleaned.isdigit():
        matches = find_by_song_id(songs_dir, int(cleaned))
        if not matches:
            raise ValueError(
                f"no folder under {songs_dir} has a .usdb marker for song id {cleaned}"
            )
        if len(matches) > 1:
            listed = "\n".join(f"  {m.name}" for m in matches)
            raise ValueError(
                f"{len(matches)} folders claim usdb song id {cleaned} "
                f"-- resolve the duplicate first:\n{listed}"
            )
        return matches[0]

    # A bare folder name, which is what the find_missing_* scripts print.
    by_name = songs_dir / cleaned
    if by_name.is_dir():
        return by_name.resolve()

    raise ValueError(
        f"{target!r} is not a song folder, a folder name under {songs_dir}, "
        f"or a USDB song id"
    )


HELP = "the song: a folder path, a folder name under --songs-dir, or a USDB song id"
