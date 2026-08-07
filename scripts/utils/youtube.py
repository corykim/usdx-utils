"""Fetching a single YouTube stream, shared by the fix_/extract_ scripts.

yt-dlp is run through `uv run --with yt-dlp` rather than being imported, so
nothing here has to be a dependency of the calling script and the version
used is always current. Callers supply their own format selection; only the
parts that are the same either way live here.

This is a plain module rather than a `uv run` script, so it lives in
scripts/utils/ with the other import-only modules. A script run as
`uv run scripts/<name>.py` has scripts/ on sys.path, which makes
`from utils import youtube` resolve without any sys.path fixing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_video_id(text: str) -> str:
    """Accept a bare 11-char id or any of the common URL shapes."""
    text = text.strip()
    if VIDEO_ID_RE.match(text):
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

    if not VIDEO_ID_RE.match(candidate):
        raise ValueError(f"couldn't find a YouTube video id in {text!r}")
    return candidate


def require() -> None:
    """Fail before downloading anything if yt-dlp cannot be run.

    Fetching it through uv means an unavailable package otherwise surfaces
    as a resolver or network error buried in yt-dlp's own stderr, after the
    caller has already reported what it intends to do. Checking up front
    turns that into one clear line.
    """
    try:
        result = subprocess.run(
            ["uv", "run", "--with", "yt-dlp", "yt-dlp", "--version"],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "uv is not on PATH, so yt-dlp cannot be fetched -- install uv, "
            "or put yt-dlp on PATH and adjust utils/youtube.py"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "timed out preparing yt-dlp (first run downloads it; check the network)"
        ) from None
    if result.returncode != 0:
        raise RuntimeError(
            "yt-dlp is not available:\n" + (result.stderr.strip()[-1000:] or "(no output)")
        )


def fetch(video_id: str, dest_stem: Path, format_args: list[str], *, timeout: int = 900) -> Path:
    """Download one stream named after dest_stem, and return where it landed.

    The extension is yt-dlp's to choose -- it depends on the format picked
    and, for audio, on what it transcoded to -- so the final path is read
    back from yt-dlp rather than assumed.
    """
    result = subprocess.run(
        [
            "uv", "run", "--with", "yt-dlp", "yt-dlp",
            *format_args,
            "--no-playlist",
            "-o", f"{dest_stem}.%(ext)s",
            "--print", "after_move:filepath",
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp failed (exit {result.returncode}):\n{result.stderr.strip()[-2000:]}"
        )
    path_line = next((line for line in result.stdout.splitlines() if line.strip()), "").strip()
    if not path_line or not Path(path_line).is_file():
        raise RuntimeError(f"yt-dlp reported success but no file found:\n{result.stdout}")
    return Path(path_line)
