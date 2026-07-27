# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

This is **not a software project** — there is no source code, build system, or test suite. It is a personal [UltraStar](https://usdb.animux.de/) karaoke song library (~2,250 songs) tracked in git so that additions, removals, and edits to the collection can be reviewed and reverted like normal changes. Nearly all work here is file management: adding songs, fixing metadata, reconciling duplicates, and cross-referencing against USDB (usdb.animux.de).

The git repo is freshly initialized with no prior commit history — treat `git log` as empty and rely on file inspection instead.

## Directory layout

- `songs/` — the active library. Every subfolder is one song, named `<Artist> - <Title>` (occasionally with a `[DUET]` or `[MULTI]` suffix for duet/multi-language variants).
- `songs.replaced/` — older versions of songs that were superseded by a better rip/sync and pulled out of `songs/`, kept around for reference/rollback rather than deleted.
- `songs.bad/` — songs that failed quality control (bad sync, wrong audio, etc.) and were pulled from `songs/`. These folders typically contain a `<youtube-id>.usdb` marker file recording which USDB entry they came from.
- `tmp/` — scratch/staging area for in-progress song replacements, with `tmp/old/` and `tmp/new/` holding the before/after file sets while a swap is being validated.
- `usdb-missing.txt` — a flat list of song folder names (under `songs/`) that have no corresponding match on USDB yet; used to track sync status against the USDB database.

## Song folder contents

A typical song folder contains some subset of:
- `<name>.txt` — the UltraStar chart file (see format below). This is the one file every song must have.
- Audio: `.mp3`, or split `vocals.ogg` + `instrumental.ogg`
- Video: `.mp4`, `.webm`, `.mkv`, or `.avi`
- Cover art: `<name> [CO].jpg`/`.png`
- Background image: `<name> [BG].jpg`
- `<youtube-id>.usdb` — present on songs pulled from USDB, records provenance (only seen in `songs.bad/` currently)

## UltraStar `.txt` chart format

Each chart file starts with `#KEY:VALUE` metadata tags, followed by note lines:

```
#TITLE:...        #ARTIST:...       #MP3:<audio file>
#BPM:...          #GAP:<ms offset>  #COVER:<image file>
#VIDEO:<video file>  #YEAR:...      #LANGUAGE:...      #GENRE:...
#EDITION:...      #VOCALS:/#INSTRUMENTAL:<split audio, if used>
```

Note lines:
- `: <beat> <length> <pitch> <text>` — normal sung note
- `* <beat> <length> <pitch> <text>` — golden (bonus) note
- `F <beat> <length> <pitch> <text>` — freestyle note
- `- <beat>` — line break
- `P1` / `P2` — marks the start of a part for duet songs, splitting the chart between two singers
- `E` — end of file

When editing or generating chart files, preserve this exact tag/note syntax — it's consumed by UltraStar-family game clients (UltraStar Deluxe, Vocaluxe, etc.), not by any code in this repo.

## Scripts

`scripts/` holds small maintenance utilities, run via `uv run scripts/<name>.py` (no external deps needed):

- `tag_split_audio.py` — for every `songs/` folder with `vocals.ogg`: renames a lone `accompaniment.ogg` to `instrumental.ogg` (see quirk below), then on each chart in the folder adds missing `#VOCALS`/`#INSTRUMENTAL` tags, fixes `#INSTRUMENTAL` values left pointing at `accompaniment.ogg` after a rename, and dedupes the tag if it appears more than once. Defaults to dry run; `--write` applies changes. `--import-stranded <file>` instead handles a single Melody Mania stranded vocals/accompaniment pair (see quirk below), moving it into its song folder as `vocals.ogg`/`instrumental.ogg` and tagging it, then exits without running the full scan.
- `fix_missing_mp3.py` — some folders only have split stems and no full mix, so `#MP3` (required by UltraStar clients as the primary audio reference) was never set. Adds `#MP3` to any chart missing it, pointing at the folder's video file if present, else `instrumental.ogg`; reports (without modifying) folders with neither. Defaults to dry run; `--write` applies changes.
- `find-missing-usdb.sh` — lists song folders with no `<youtube-id>.usdb` marker file (i.e. not yet cross-referenced against USDB). Writes WSL-style `/mnt/c/...` paths by default.

## Working conventions

- Filenames routinely contain characters PowerShell/bash quoting can trip on: `&`, `'`, smart quotes (`’`), accented characters, brackets. Always quote paths and prefer tools that don't require manual shell-escaping.
- Some chart files have mixed/incorrect text encoding (visible as `�` replacement characters in lyrics) — this is a pre-existing data quality issue in individual songs, not something to "fix" globally.
- Mac `.DS_Store` files are scattered throughout — avoid treating them as meaningful content.
- When reconciling `songs/` against `usdb-missing.txt` or USDB itself, match by the `<Artist> - <Title>` folder name.
- A one-off Melody Mania (vocal separation tool) bug used to strand split output under `%APPDATA%/LocalLow` for songs with unicode filenames, and a since-removed fixup script for it inserted `#VOCALS`/`#INSTRUMENTAL` tags positionally without checking for existing ones — leaving some charts with `accompaniment.ogg` (instead of `instrumental.ogg`) and/or duplicated tags. `tag_split_audio.py` now normalizes both; re-run it after adding new split-audio songs.
