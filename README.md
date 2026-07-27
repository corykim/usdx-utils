# ultrastar

A personal [UltraStar](https://usdb.animux.de/) karaoke song library (~2,250 songs), version-controlled so that additions, edits, and removals to the collection can be tracked and reverted like any other change.

This is a data repository, not a software project — aside from a couple of maintenance scripts, there's no code here, just song folders.

## Layout

- **`songs/`** — the active library. Every subfolder is one song, named `<Artist> - <Title>` (occasionally suffixed `[DUET]` or `[MULTI]` for duet/multi-language variants).
- **`songs.replaced/`** — older versions of songs that were superseded by a better rip/sync and pulled out of `songs/`. Kept for reference/rollback instead of being deleted.
- **`songs.bad/`** — songs that failed quality control (bad sync, wrong audio, etc.) and were pulled from `songs/`. These folders typically include a `<youtube-id>.usdb` marker file recording which USDB entry they came from.
- **`tmp/`** — scratch/staging area for in-progress song replacements, with `tmp/old/` and `tmp/new/` holding the before/after file sets while a swap is being validated.
- **`usdb-missing.txt`** — a flat list of song folder names (under `songs/`) that have no corresponding match on USDB yet, used to track sync status against the USDB database.
- **`scripts/`** — small maintenance utilities for the library (see below).

`songs/`, `songs.replaced/`, `songs.bad/`, `tmp/`, and `usdb-missing.txt` are all `.gitignore`d — the media library itself is large and not meant to live in git history; only the tooling around it (this README, `CLAUDE.md`, `scripts/`) is tracked.

## Song folder contents

A typical song folder contains some subset of:

| File | Purpose |
|---|---|
| `<name>.txt` | The UltraStar chart file (see format below). Every song has at least one. |
| `.mp3` / `vocals.ogg` + `instrumental.ogg` (or `accompaniment.ogg`) | Audio, either a single mixed track or split vocal/instrumental stems. |
| `.mp4` / `.webm` / `.mkv` / `.avi` | Background video. |
| `<name> [CO].jpg`/`.png` | Cover art. |
| `<name> [BG].jpg` | Background image (used when no video is present). |
| `<youtube-id>.usdb` | Present on songs pulled from USDB; records provenance. Currently only seen in `songs.bad/`. |

### UltraStar `.txt` chart format

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

These files are consumed by UltraStar-family game clients (UltraStar Deluxe, Vocaluxe, etc.), not by anything in this repo.

## Scripts

Requires [`uv`](https://docs.astral.sh/uv/).

- **`scripts/tag_split_audio.py`** — scans every folder under `songs/` that has `vocals.ogg`. If `instrumental.ogg` is present too, it's used as-is; if only `accompaniment.ogg` is present (see the naming quirk below), it's renamed to `instrumental.ogg`. Every chart (`.txt`) in the folder then has its `#VOCALS`/`#INSTRUMENTAL` tags added if missing, corrected if they still point at `accompaniment.ogg` after a rename, and deduplicated if a tag appears more than once (a pre-existing bug used to insert tags blindly, sometimes duplicating them). Defaults to a dry run.

  ```bash
  uv run scripts/tag_split_audio.py            # preview changes across songs/
  uv run scripts/tag_split_audio.py --write     # apply changes
  ```

  It also has a one-off mode for a Melody Mania (vocal-separation tool) bug: when a song's filename contains unicode characters, Melody Mania fails to write its split output into the song folder and instead strands a `<name>.vocals.ogg` / `<name>.accompaniment.ogg` pair under `%APPDATA%/LocalLow`. Point `--import-stranded` at either stranded file and it moves both into the song's folder as `vocals.ogg`/`instrumental.ogg` and tags the chart, skipping the full scan.

  ```bash
  uv run scripts/tag_split_audio.py --import-stranded "/path/to/LocalLow/.../SongName.vocals.ogg" --write
  ```

- **`scripts/fix_missing_mp3.py`** — some song folders have only split `vocals.ogg`/`instrumental.ogg` stems and no full mix, so `#MP3` (the tag UltraStar clients use as the primary audio reference) was never set. Scans every chart under `songs/` for a missing `#MP3` tag and adds one pointing at the folder's video file if present, otherwise `instrumental.ogg`; folders with neither are reported and left alone. Defaults to a dry run.

  ```bash
  uv run scripts/fix_missing_mp3.py            # preview changes
  uv run scripts/fix_missing_mp3.py --write     # apply changes
  ```

- **`scripts/find-missing-usdb.sh`** — lists every song folder (default `songs/`) that has no `<youtube-id>.usdb` marker file, i.e. hasn't been cross-referenced against USDB yet. Writes bash/WSL paths (`/mnt/c/...`); pass a different path as `$1` if running outside WSL.

  ```bash
  ./scripts/find-missing-usdb.sh [target-dir]
  ```

## Known quirks

- Filenames routinely contain characters that need careful shell-quoting: `&`, `'`, smart quotes (`’`), accented characters, brackets.
- Some chart files have mixed/incorrect text encoding, visible as `�` replacement characters in lyrics — a pre-existing data quality issue in individual songs, not something globally fixed.
- Mac `.DS_Store` files are scattered throughout the library.
- A handful of songs processed through the old unicode-filename fixup ended up with `accompaniment.ogg` instead of `instrumental.ogg`, sometimes with duplicated `#VOCALS`/`#INSTRUMENTAL` tags. `scripts/tag_split_audio.py` normalizes both issues; run it after adding new split-audio songs.
