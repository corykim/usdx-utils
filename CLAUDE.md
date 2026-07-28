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

`scripts/` holds small maintenance utilities, run via `uv run scripts/<name>.py` (no Python deps; `ffprobe` must be on PATH for the audio-length checks). All default to a dry run and need `--write` to change anything.

- `fix_my_library.py` — **the orchestrator**; runs the routine remediations in a deliberate order: `strip_bom` → `tag_split_audio` → `resolve_duplicate_songs` → `tag_split_audio` → `fix_missing_mp3` → `find_missing_video`, then reports remaining video/USDB gaps and regenerates `usdb-missing.txt` on `--write`. BOMs are cleared first because one breaks header parsing for every other script; duplicate resolution precedes the per-folder fixes because it moves whole folders; stem tagging brackets it so duplicates can be found by conventional stem names and merged-in stems still get normalized. In a dry run the two tagging passes report identical counts simply because nothing was applied between them. Deliberately excludes `prune_desynced_stems.py` (permanent deletion).

- `tag_split_audio.py` — for every `songs/` folder with `vocals.ogg`: renames a lone `accompaniment.ogg` to `instrumental.ogg` (see quirk below), then on each chart in the folder adds missing `#VOCALS`/`#INSTRUMENTAL` tags, fixes `#INSTRUMENTAL` values left pointing at `accompaniment.ogg` after a rename, and dedupes the tag if it appears more than once. Defaults to dry run; `--write` applies changes. `--import-stranded <file>` instead handles a single Melody Mania stranded vocals/accompaniment pair (see quirk below), moving it into its song folder as `vocals.ogg`/`instrumental.ogg` and tagging it, then exits without running the full scan.
- `fix_missing_mp3.py` — some folders only have split stems and no full mix, so `#MP3` (required by UltraStar clients as the primary audio reference) was never set. Adds `#MP3` to any chart missing it, pointing at the folder's video file if present, else `instrumental.ogg`; reports (without modifying) folders with neither. Defaults to dry run; `--write` applies changes.
- `resolve_duplicate_songs.py` — reconciles `<name> (N)` duplicate folders against their `<name>` base folder. One copy is the **keeper**, the other is retired into `songs.replaced/`; the keeper always ends up named `<name>`. **The USDB copy wins, newer first**: if only one copy has a `.usdb` marker it keeps; if **both** do, the newer entry keeps — ranked on the marker JSON's own `usdb_mtime` (upstream revision date), falling back to the marker file's mtime to separate two downloads of the same unchanged entry, with an exact tie leaving the original; if neither does, the original keeps. Whenever either copy is USDB-sourced the keeper is authoritative by construction, so its own metadata is left alone; only when *neither* has a marker are `#LANGUAGE`/`#EDITION`/`#GENRE`/`#YEAR`/`#CREATOR` merged in from the duplicate. Assets the keeper lacks are moved over and tagged: `#COVER`/`#BACKGROUND`/`#VIDEO` plus `#VOCALS`/`#INSTRUMENTAL` (stems are picked up even if the retired chart never declared them). An asset counts as missing if the keeper's chart lacks the tag *or* names a file that isn't there (so broken references get repaired); an asset the keeper already has is never replaced. A `(N)` folder with no base folder is renamed in place to drop the suffix. Defaults to dry run; `--write` applies changes. Needs `ffprobe` on PATH.
  - **Split stems are only merged when their length matches the keeper's own audio** (1s tolerance, measured via ffprobe) — stems come from a full mix, so a mismatch means a different rip that would play offset against the keeper's chart. One probe covers both stems since they come from the same separation run. A keeper with only a video counts as having no audio and merges stems unchecked; a keeper with neither audio nor video falls back to using the merged instrumental as its `#MP3`.
  - Timing tags (`#BPM`/`#GAP`/`#MEDLEY*`/`#START`/`#PREVIEWSTART`/`#VIDEOGAP`/`#END`) and `#MP3` are never copied between charts — they're calibrated to their own chart's note beats and audio.
- `strip_bom.py` — removes UTF-8 BOMs from charts, anywhere in the file. **A BOM is not whitespace**, so a header line carrying one never matches a `#TAG:` check: `lstrip().startswith("#")` fails, the header is treated as empty, and tools re-add tags the chart already had or insert lines above the BOM. All chart-editing scripts here now decode with `utf-8-sig` and write plain UTF-8 so a BOM can't survive an edit; this script clears pre-existing ones.
- `prune_desynced_stems.py` — deletes stems whose length differs from their folder's own full-mix audio by more than `--tolerance` (default 1s), and strips their tags — leftovers from a superseded rip that would play offset. **Permanent: `songs/` is gitignored, so there is no undo**; not part of `fix_my_library.py`. Skips folders with no full-mix audio (a video is not used as reference — music videos carry extra footage, so their duration legitimately differs) and folders whose `#MP3` points at a stem. Run it *before* `fix_missing_mp3.py`, which creates that kind of `#MP3` pointer.
- `find_missing_video.py` — reports songs with no usable video in three categories (`--category none|broken|untagged|all`): no video file, `#VIDEO` naming a missing file, or a video present but undeclared. `--write` fixes the last case, only when the folder holds exactly one video.
- `find_missing_usdb.py` — lists song folders with no `<youtube-id>.usdb` marker file (i.e. not yet cross-referenced against USDB), sorted, one per line on stdout with a count on stderr; redirect it to regenerate `usdb-missing.txt`. Emits bare `<Artist> - <Title>` folder names by default, since that's how songs are matched against USDB; `--full-paths` prints full paths instead. Skips dot-directories, so a stray `songs/.claude` isn't a false positive.

## Working conventions

- Filenames routinely contain characters PowerShell/bash quoting can trip on: `&`, `'`, smart quotes (`’`), accented characters, brackets. Always quote paths and prefer tools that don't require manual shell-escaping.
- Some chart files have mixed/incorrect text encoding (visible as `�` replacement characters in lyrics) — this is a pre-existing data quality issue in individual songs, not something to "fix" globally.
- Mac `.DS_Store` files are scattered throughout — avoid treating them as meaningful content.
- When reconciling `songs/` against `usdb-missing.txt` or USDB itself, match by the `<Artist> - <Title>` folder name.
- A one-off Melody Mania (vocal separation tool) bug used to strand split output under `%APPDATA%/LocalLow` for songs with unicode filenames, and a since-removed fixup script for it inserted `#VOCALS`/`#INSTRUMENTAL` tags positionally without checking for existing ones — leaving some charts with `accompaniment.ogg` (instead of `instrumental.ogg`) and/or duplicated tags. `tag_split_audio.py` now normalizes both; re-run it after adding new split-audio songs.
