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

- `fix_my_library.py` — **the orchestrator**; runs the routine remediations in a deliberate order: `strip_bom` → `tag_split_audio` → `resolve_duplicate_songs` → `tag_split_audio` → `fix_missing_mp3` → `find_missing_video`, then reports remaining video/USDB gaps and regenerates `usdb-missing.txt` on `--write`. BOMs are cleared first because one breaks header parsing for every other script; duplicate resolution precedes the per-folder fixes because it moves whole folders; stem tagging brackets it so duplicates can be found by conventional stem names and merged-in stems still get normalized. In a dry run the two tagging passes report identical counts simply because nothing was applied between them. Deliberately excludes `prune_desynced_stems.py` (permanent deletion). `--terse` is forwarded only to steps whose `Step.accepts_terse` is set, since passing it to one that doesn't understand it would abort the run. **Any step exiting non-zero aborts the run** and its code is propagated, reports included; with `--write` the USDB listing is staged to a `.partial` file and swapped in, so a failed run can't truncate it.

- `audio_lengths.py` — **not a script**, the shared ffprobe length check (`stems_match_mix`, `find_full_mix`, `audio_duration`, `DEFAULT_TOLERANCE_S`). Imported by `tag_split_audio`, `prune_desynced_stems` and `resolve_duplicate_songs`; it sits beside them so a plain `import audio_lengths` resolves.
- `tag_split_audio.py` — for every `songs/` folder with `vocals.ogg`: renames a lone `accompaniment.ogg` to `instrumental.ogg` (see quirk below), then on each chart in the folder adds missing `#VOCALS`/`#INSTRUMENTAL` tags, fixes `#INSTRUMENTAL` values left pointing at `accompaniment.ogg` after a rename, and dedupes the tag if it appears more than once. **Stems are only tagged if they match the folder's own full mix** (`--tolerance`, default 1s) — tagging a chart with stems from a different rip would play them offset against its notes; 5 folders here are refused on that basis. No full mix to compare against is not a mismatch, so those are tagged as before. The probe only runs where a chart actually needs tagging, which keeps a tagged library at ~26s rather than >10min. Defaults to dry run; `--write` applies changes. `--import-stranded <file>` instead handles a single Melody Mania stranded vocals/accompaniment pair (see quirk below), moving it into its song folder as `vocals.ogg`/`instrumental.ogg` and tagging it, then exits without running the full scan.
- `fix_missing_mp3.py` — some folders only have split stems and no full mix, so `#MP3` (required by UltraStar clients as the primary audio reference) was never set. Adds `#MP3` to any chart missing it, pointing at the folder's video file if present, else `instrumental.ogg`; reports (without modifying) folders with neither. Defaults to dry run; `--write` applies changes.
- `resolve_duplicate_songs.py` — reconciles `<name> (N)` duplicate folders against their `<name>` base folder. One copy is the **keeper**, the other is retired into `songs.replaced/`; the keeper always ends up named `<name>`. **The USDB copy wins, newer first**: if only one copy has a `.usdb` marker it keeps; if **both** do, the newer entry keeps — ranked on the marker JSON's own `usdb_mtime` (upstream revision date), falling back to the marker file's mtime to separate two downloads of the same unchanged entry, with an exact tie leaving the original; if neither does, the original keeps. Whenever either copy is USDB-sourced the keeper is authoritative by construction, so its own metadata is left alone; only when *neither* has a marker are `#LANGUAGE`/`#EDITION`/`#GENRE`/`#YEAR`/`#CREATOR` merged in from the duplicate. Assets the keeper lacks are moved over and tagged: `#COVER`/`#BACKGROUND`/`#VIDEO` plus `#VOCALS`/`#INSTRUMENTAL` (stems are picked up even if the retired chart never declared them). An asset counts as missing if the keeper's chart lacks the tag *or* names a file that isn't there (so broken references get repaired); an asset the keeper already has is never replaced. A `(N)` folder with no base folder is renamed in place to drop the suffix. Defaults to dry run; `--write` applies changes. Needs `ffprobe` on PATH.
  - Copies are grouped by **billing + title**, matched by different rules (`song_signature`/`billings_match`), and **without needing a `(N)` suffix on either**. Title must agree exactly once normalized. Billings match if they name the same people in any order (`Lita Ford with Ozzy Osbourne` = `Ozzy Osbourne And Lita Ford`) or are the same lead plus guests (`Bob Marley & The Wailers` = `Bob Marley`, `Gotye feat. Kimbra` = `Gotye`). A leading `The` is dropped (`Bangles` = `The Bangles`). Artist separators: `&`, `+`, `,`, `/`, `with`, `and`, `feat`/`ft`/`featuring`.
  - A **parenthesized name in the billing is a performer**, not decoration: `Disney's Frozen (Idina Menzel)`=`Idina Menzel`, `Girls' Generation (SNSD)`=`Girls' Generation`. Reading them as acts (rather than stripping them) is what keeps `Disney's Moana (Alessia Cara)` apart from `(Auli'i Cravalho)` — two singers of one song. A shorter billing matches a longer one when its lead is among the longer's parenthesized acts.
  - **Sharing one name is deliberately not enough.** Requiring same-lead *and* a subset relation is what keeps `Michael Bublé feat. Mariah Carey` off Mariah Carey's own recording, and keeps Disney's two `Beauty and the Beast` duets apart — a franchise name splits to the same leading fragment (`disneysbeauty`) for both casts, so the lead alone would wrongly merge them.
  - Title normalization, in order: strip a trailing `" (N)"` → fold accents to ASCII → expand casual spellings (`wanna`→`want to`, `till`/`'til`→`until`, `-in'`→`-ing`; the latter requires the apostrophe to end the word, or `Ain't` and `Dolphin's` would be mangled) → drop a featured-artist credit → drop the noise words `and`/`the`/`a` → remove punctuation, case and **all whitespace**.
  - Punctuation and spaces are removed, not replaced with separators, so initialisms and run-together band names match: `U.S.A`=`USA`, `B. B. King`=`B.B. King`, `Big Bang`=`BIGBANG`, `blink-182`=`Blink 182`, `Salt-N-Pepa`=`Salt N' Pepa`, `C & C`=`C&C`. Words inside brackets remain, so `[DUET]` still separates variants (`barbiegirlduet` ≠ `barbiegirl`).
  - `and`/`the`/`a` are dropped as noise: `and` because the `&` it stands in for is punctuation and vanishes anyway (`Hall and Oates`=`Hall & Oates`), the articles because they drift between filings (`Bringin' On A Heartbreak`=`Bringin' On The Heartbreak`, `Bangles`=`The Bangles`). All word-bounded, so `The Band`/`Andy`/`Theory Of A Deadman`/`Bananarama`/`Thelma` survive. Only English `and` — Spanish `y`, German `und` are not folded.
  - Accent folding pairs `Beyonce`/`Beyoncé`, `Sinead O'Connor`/`Sinéad O’Connor`. Glyph-baked letters (`ø`, `æ`, `ß`, `ł`, …) are transliterated via an explicit table since stripping combining marks misses them. Decomposition is followed by recomposition so **non-Latin scripts survive intact** — without that, NFKD scatters a Hangul syllable into jamo.
  - The `feat.`/`ft`/`featuring` credit is stripped **only from the artist half** (split on the first `" - "`), so `Eminem feat. Rihanna - …` pairs with `Eminem - …` while a title that mentions a feature — `Creep (Gamper & Dadoni feat. Ember Island Remix)` — is untouched.
  - **The normalized form is never written to disk** — it only groups. The keeper's folder name is preserved exactly, except that a trailing `" (N)"` is stripped after the other copies move out.
  - `--merge-variants` also drops parenthesized phrases, so `Song (Live)`/`Song (Album Version)` group with `Song` (square brackets still respected). **Aggressive by design** — in this library a parenthetical is frequently a genuinely different chart (`(Explicit Version)`, `(TV)` short edits, `(Live at Donington)`, `(German)`), so it groups 223 sets versus 0 without it.
    - In a set that only groups *because* a parenthetical was dropped, **every USDB-sourced copy is a keeper** (a live cut is its own song); only marker-less copies are retired. So an all-marker set has nothing to retire and is left alone — 134 of 223 sets here, with 59 giving up one unsourced folder each.
    - Salvage from a retired copy is decided per surviving variant: **stems** go to a variant whose audio is the same length; **video** goes only to a variant whose audio is the same length, since for variants that equality is the evidence they're the same recording. With several qualifying variants the file is copied to each and the original is archived intact; with one keeper it's moved. Non-variant sets are unaffected and migrate video without the audio-length gate (music-video durations legitimately differ).
  - **USDB-sourced folders with no playable media are excluded by default** — chart + artwork + marker but no audio/video, i.e. the geo-blocked syncer failures `find_missing_audio.py` lists. They are shells awaiting a re-fetch, not copies, so they are neither grouped nor modified; the run reports how many it left alone. `--include-unplayable` opts them in, and then playability outranks everything in `choose_keeper` (a copy with nothing to play never wins), with the variant path likewise refusing to retire the only playable copy.
  - `--terse` withholds the report for any set that ends up untouched, printing only sets something happened to; the end tallies are unchanged. Most useful with `--merge-variants`, where most sets are deliberately left alone (838 → 80 lines here). Little effect under `--interactive`, whose prompt is already on screen before you skip. The same flag is on every script that can pass over a song: `tag_split_audio`, `fix_missing_mp3`, `prune_desynced_stems`, `find_missing_video`.
  - `--interactive` confirms each set: ENTER accepts the recommended keeper, a number picks another, `S` skips. Lists each copy with its `.usdb` date / split audio / video. **Implies `--write`** (nothing moves without a keypress); EOF aborts without further changes. When *every* copy in a set has a `.usdb` marker, ENTER defaults to **skip** — those are usually distinct entries deliberately downloaded (studio vs live), not one song fetched twice; a number still merges them.
  - **Split stems are only merged when their length matches the keeper's own audio** (1s tolerance, measured via ffprobe) — stems come from a full mix, so a mismatch means a different rip that would play offset against the keeper's chart. One probe covers both stems since they come from the same separation run. A keeper with only a video counts as having no audio and merges stems unchecked; a keeper with neither audio nor video falls back to using the merged instrumental as its `#MP3`.
  - Timing tags (`#BPM`/`#GAP`/`#MEDLEY*`/`#START`/`#PREVIEWSTART`/`#VIDEOGAP`/`#END`) and `#MP3` are never copied between charts — they're calibrated to their own chart's note beats and audio.
- `strip_bom.py` — removes UTF-8 BOMs from charts, anywhere in the file. **A BOM is not whitespace**, so a header line carrying one never matches a `#TAG:` check: `lstrip().startswith("#")` fails, the header is treated as empty, and tools re-add tags the chart already had or insert lines above the BOM. All chart-editing scripts here now decode with `utf-8-sig` and write plain UTF-8 so a BOM can't survive an edit; this script clears pre-existing ones.
- `prune_desynced_stems.py` — deletes stems whose length differs from their folder's own full-mix audio by more than `--tolerance` (default 1s), and strips their tags — leftovers from a superseded rip that would play offset. **Permanent: `songs/` is gitignored, so there is no undo**; not part of `fix_my_library.py`. Skips folders with no full-mix audio (a video is not used as reference — music videos carry extra footage, so their duration legitimately differs) and folders whose `#MP3` points at a stem. Run it *before* `fix_missing_mp3.py`, which creates that kind of `#MP3` pointer.
- `find_missing_video.py` — reports songs with no usable video in three categories (`--category none|broken|untagged|all`): no video file, `#VIDEO` naming a missing file, or a video present but undeclared. `--write` fixes the last case, only when the folder holds exactly one video. Both find_missing_* media scripts print bare folder names (`--full-paths` for paths) and take `--usdb-only` to consider only syncer-managed folders.
- `find_missing_audio.py` — lists songs with **no audio at all** (`--category none|broken|untagged|all`): the USDB syncer fetched chart and artwork but failed on the media, typically geo-restriction or an API refusal, leaving nothing to play. Not fixable by tagging — the media must be re-downloaded. `--details` reads the `.usdb` marker for the syncer's own verdict and source id (`audio=failure ... [a=<id>]`). A video-only folder counts as having audio.
- `find_missing_usdb.py` — lists song folders with no `<youtube-id>.usdb` marker file (i.e. not yet cross-referenced against USDB), sorted, one per line on stdout with a count on stderr; redirect it to regenerate `usdb-missing.txt`. Emits bare `<Artist> - <Title>` folder names by default, since that's how songs are matched against USDB; `--full-paths` prints full paths instead. Skips dot-directories, so a stray `songs/.claude` isn't a false positive.

## Working conventions

- Filenames routinely contain characters PowerShell/bash quoting can trip on: `&`, `'`, smart quotes (`’`), accented characters, brackets. Always quote paths and prefer tools that don't require manual shell-escaping.
- Some chart files have mixed/incorrect text encoding (visible as `�` replacement characters in lyrics) — this is a pre-existing data quality issue in individual songs, not something to "fix" globally.
- Mac `.DS_Store` files are scattered throughout — avoid treating them as meaningful content.
- When reconciling `songs/` against `usdb-missing.txt` or USDB itself, match by the `<Artist> - <Title>` folder name.
- A one-off Melody Mania (vocal separation tool) bug used to strand split output under `%APPDATA%/LocalLow` for songs with unicode filenames, and a since-removed fixup script for it inserted `#VOCALS`/`#INSTRUMENTAL` tags positionally without checking for existing ones — leaving some charts with `accompaniment.ogg` (instead of `instrumental.ogg`) and/or duplicated tags. `tag_split_audio.py` now normalizes both; re-run it after adding new split-audio songs.
