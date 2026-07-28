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

Requires [`uv`](https://docs.astral.sh/uv/), plus `ffprobe` (ffmpeg) on PATH for the audio-length checks.

Every script defaults to a **dry run** and needs `--write` to change anything.

### The one to reach for

- **`scripts/fix_my_library.py`** — runs every routine remediation in the order the individual scripts need, then reports what still wants a human.

  ```bash
  uv run scripts/fix_my_library.py            # preview everything
  uv run scripts/fix_my_library.py --write    # apply everything
  ```

  The sequence is `strip_bom` → `tag_split_audio` → `resolve_duplicate_songs` → `tag_split_audio` again → `fix_missing_mp3` → `find_missing_video`. BOMs go first because one makes a header line invisible to every other tool here; duplicate resolution comes before the per-folder fixes because it moves whole folders around; and stem tagging runs on both sides of it so duplicate resolution can find split audio by its conventional name, and so anything it merges in under a non-standard name still gets normalized. The second tagging pass is idempotent and normally a no-op — in a *dry run* it reports the same counts as the first only because nothing was actually applied in between.

  It deliberately leaves out `prune_desynced_stems.py`, which deletes files permanently. Run that one by hand.

### Individual remediations

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

- **`scripts/resolve_duplicate_songs.py`** — finds song folders that are the same song stored more than once and reconciles each set. One copy becomes the **keeper**; the rest are retired into `songs.replaced/`. Defaults to a dry run. Requires `ffprobe` (ffmpeg) on PATH.

  Folders are grouped by **billing and title**, matched separately because they follow different rules. The title has to agree exactly once normalized; the billing has to *describe the same act*, which means either naming exactly the same people in any order — `Lita Ford with Ozzy Osbourne` = `Ozzy Osbourne And Lita Ford` — or being the same lead plus guests: `Bob Marley & The Wailers` = `Bob Marley`, `Gotye feat. Kimbra` = `Gotye & Kimbra` = `Gotye`. A leading `The` is dropped, so `Bangles` = `The Bangles`.

  A **parenthesized name in the billing counts as a performer**, not decoration, so `Disney's Frozen (Idina Menzel)` finds `Idina Menzel` and `Girls' Generation (SNSD)` finds `Girls' Generation`. Reading them rather than discarding them is what keeps `Disney's Moana (Alessia Cara)` away from `Disney's Moana (Auli'i Cravalho)` — two singers of one song, which stripping the parentheses would have merged.

  Sharing *a* name isn't enough, deliberately: `Michael Bublé feat. Mariah Carey - All I Want For Christmas` is a different recording from Mariah Carey's own, and Disney's two `Beauty and the Beast` duets (Céline Dion/Peabo Bryson vs Ariana Grande/John Legend) stay apart despite the shared franchise name.

  The title itself is normalized, in order:

  | Step | Effect |
  |---|---|
  | Strip a trailing `" (N)"` | `Song (1)` → `Song` |
  | Fold accents to ASCII | `Beyoncé` → `beyonce`, `Señorita` → `senorita` |
  | Drop a featured-artist credit | `Eminem feat. Rihanna - Love The Way You Lie` → `Eminem - Love The Way You Lie` |
  | Drop `and`, `the`, `a` | `Hall and Oates` = `Hall & Oates`; `Bringin' On A Heartbreak` = `Bringin' On The Heartbreak` |
  | Expand casual spellings | `Wanna` = `Want To`; `Believin'` = `Believing`; `Till` = `'Til` = `Until` |
  | Remove punctuation, case and **all whitespace** | `B. B. King` → `bbking`, `Big Bang` → `bigbang` |

  Punctuation and spaces are *removed* rather than turned into separators, so `Born In The U.S.A` matches `Born in the USA`, `BIGBANG` matches `Big Bang`, `blink-182` matches `Blink 182`, and `Salt-N-Pepa` matches `Salt N' Pepa`. `and` goes because the `&` it stands in for is punctuation and drops out anyway; the articles go because they drift between one filing and the next. All three are matched on word boundaries, leaving `The Band`, `Andy Williams`, `Theory Of A Deadman`, `Bananarama` and `Thelma Houston` alone. Words inside brackets stay, so variant markers still separate songs: `Barbie Girl [DUET]` normalizes to `barbiegirlduet`, which isn't `barbiegirl`.

  The featuring credit is only stripped from the **artist** half, so a title that mentions one survives — `Radiohead - Creep (Gamper & Dadoni feat. Ember Island Remix)` keeps its remix credit. Non-Latin scripts pass through untouched: Hangul, Cyrillic and CJK titles are left exactly as they are.

  This means a re-download that merely re-punctuated the title is caught even when neither folder carries a `" (N)"` — `Bon Jovi - It's my life` and `Bon Jovi - It’s My Life` are one song, not two.

  **The normalized form is only used for grouping; it is never written to disk.** The keeper's folder name is preserved exactly as it is, with one exception: a trailing `" (N)"` is stripped once the copies it was competing with have moved out of the way. So a keeper named `R.E.M. - What’s The Frequency, Kenneth! (1)` ends up as `R.E.M. - What’s The Frequency, Kenneth!` — every period, comma and curly quote intact.

  **`--merge-variants`** additionally treats a parenthesized phrase as noise rather than part of the title, so `Song (Live)` and `Song (Album Version)` count as copies of `Song`. Square brackets are still respected, so `[DUET]` stays a separate song.

  The phrase is dropped wherever it sits, so the `<artist> - (<variant>) <title>` form is caught too — `Artist - (Live) Some Song` collapses onto `Artist - Some Song`.

  Be deliberate with this one: in a karaoke library a parenthetical is often a genuinely different chart, not a redundant copy. On the current library it groups 223 sets, and among them are `In Da Club` vs `(Explicit Version)`, `Jungle P` vs `(TV)` (a short TV edit), `Dirty Deeds Done Dirt Cheap` vs `(Live at Donington)`, and `Girlfriend` vs `(German)` — different lyrics, lengths, performances and languages.

  So in a set that only holds together *because* a parenthetical was discarded, **every USDB-sourced copy is kept** — a live cut is its own song, not a stale copy of the studio one. Only copies with no `.usdb` marker are retired, and only after anything useful has been salvaged from them. A set where all copies carry a marker therefore has nothing to retire and is left alone (134 of the 223 sets here); the remaining 59 give up a single unsourced folder each.

  What gets salvaged from a retired copy is decided per surviving variant, by length:

  - **stems** transfer to a variant whose own audio is the same length (the usual rule);
  - **video** transfers only to a variant whose audio is the same length — for a variant set that equality is the evidence the two are the same recording, and a live backdrop doesn't belong on the studio chart.

  Where several variants qualify the file is copied to each and the original travels into `songs.replaced/` intact; with a single keeper it's moved as usual. Sets that would group even without the flag (`Don't` vs `Don’t`) are unaffected: they follow the ordinary newest-marker-wins rule, and their video transfers without the audio-length gate, since a music video's duration legitimately differs from the song's.

  **A USDB download whose media never arrived sits the round out.** Those folders hold a chart, artwork and a `.usdb` marker but nothing to play (see `find_missing_audio.py`); they're shells awaiting a re-fetch rather than copies of anything, so by default they aren't grouped, ranked or touched, and the run says how many it left alone. `--include-unplayable` brings them in — and when it does, a copy with nothing to play can never win the keeper slot, however good its marker, so the playable copy survives and the shell is retired.

  **`--quiet`** drops the report for any set that ends up untouched, printing only the ones something actually happened to. The tallies at the end are unchanged, so nothing goes unaccounted for. It pairs naturally with `--merge-variants`, where most sets are left alone by design — on the current library that's 838 lines of output down to 80, with all 10 acted-on sets still shown in full. In an `--interactive` run it has little to do: the prompt for a set is on screen before you decide to skip it.

  ```bash
  uv run scripts/resolve_duplicate_songs.py --merge-variants --quiet
  ```

  **`--interactive`** confirms each set before touching it: `ENTER` accepts the recommended keeper, a number picks a different copy, `S` skips the set entirely. Each copy is listed with what it has going for it (`.usdb` date, split audio, video). It implies `--write`, since answering a prompt per set only to be told what *would* have happened is busywork — nothing changes without an explicit keypress. Running out of input (Ctrl-D) stops the run without applying anything further.

  When **every** copy in a set has a `.usdb` marker, `ENTER` defaults to *skip* instead. Two USDB-sourced folders are usually two entries somebody deliberately downloaded — a studio cut and a live one — rather than the same song fetched twice, so the safe answer is the easy one. Typing a number still merges them.

  ```bash
  uv run scripts/resolve_duplicate_songs.py --merge-variants --interactive
  ```

  ```bash
  uv run scripts/resolve_duplicate_songs.py            # preview changes
  uv run scripts/resolve_duplicate_songs.py --write     # apply changes
  ```

  **The USDB copy wins**, and when both are USDB-sourced the newer one does:

  | Markers | Keeper |
  |---|---|
  | Only the `" (N)"` copy has one | the `" (N)"` copy |
  | Only the original has one | the original |
  | **Both** have one | **the newer entry** — ranked on the marker's own `usdb_mtime` (when the entry was last revised on USDB), falling back to the marker file's mtime to separate two downloads of the same unchanged entry. An exact tie leaves the original in place. |
  | Neither has one | the original |

  Whenever either copy is USDB-sourced the keeper is the authoritative one by construction, so its own chart metadata is left alone. Only when *neither* has a marker is there no authority — then `#LANGUAGE`/`#EDITION`/`#GENRE`/`#YEAR`/`#CREATOR` are merged in from the duplicate (duplicate wins on conflict).

  Assets the keeper is missing are moved over from the retired copy and tagged on its chart: `#COVER`, `#BACKGROUND`, `#VIDEO`, plus `#VOCALS`/`#INSTRUMENTAL` — a locally separated stem pair is worth preserving, and fresh USDB downloads never include one. Stems are picked up even if the retired chart never declared them, by falling back to the conventional `vocals.ogg`/`instrumental.ogg` names. An asset counts as missing if the keeper's chart doesn't declare that tag, or declares it but names a file that isn't actually there (so broken references get repaired); an asset the keeper already has is never replaced.

  **Stems are only merged when they match the keeper's own audio length** (1 second tolerance, measured with ffprobe). Stems are separated from a full mix, so a length mismatch means they came from a different rip and would play offset against the keeper's chart. Both stems come from one separation run, so a single probe settles it for the pair. A keeper holding only a video counts as having no audio and merges its stems unchecked; a keeper with neither audio nor video falls back to using the merged instrumental as its `#MP3`.

  Timing tags (`#BPM`, `#GAP`, `#MEDLEYSTARTBEAT`/`#MEDLEYENDBEAT`, `#START`, `#PREVIEWSTART`, `#VIDEOGAP`, `#END`) and `#MP3` are never copied between charts: they're calibrated to their own chart's note-beat numbers and audio — e.g. `Heart - Alone`'s two charts differ by exactly 2x in both BPM and every note position, so copying one chart's BPM onto the other's notes would desync playback.

- **`scripts/strip_bom.py`** — removes UTF-8 byte-order marks from charts. A BOM is invisible but isn't whitespace, so a header line carrying one never looks like a `#TAG:` line to a parser: tools conclude the chart declares no tags at all and re-add ones it already had. Marks are stripped wherever they appear, including mid-file (the tell-tale of a tool having inserted a line above a BOM'd header).

  ```bash
  uv run scripts/strip_bom.py --write
  ```

- **`scripts/prune_desynced_stems.py`** — one-time cleanup for stems left over from an earlier version of a song. Compares each folder's instrumental against its own full-mix audio and, when they differ by more than `--tolerance` (default 1s), **deletes both stems** and strips `#VOCALS`/`#INSTRUMENTAL` from its charts.

  ```bash
  uv run scripts/prune_desynced_stems.py            # preview — always look first
  uv run scripts/prune_desynced_stems.py --write    # PERMANENT deletion
  ```

  **Deletion cannot be undone** — `songs/` is gitignored, so there's no git history to restore from. This is why `fix_my_library.py` doesn't run it. It skips, and reports, any folder with no full-mix audio to compare against (a video is *not* used as the reference: music videos routinely carry extra footage, so their duration legitimately differs) and any folder whose `#MP3` points at a stem (deleting those would leave no audio at all). Since `fix_missing_mp3.py` creates exactly that kind of `#MP3` pointer for stems-only folders, run this *before* `fix_my_library.py` if you want those folders considered.

- **`scripts/find_missing_audio.py`** — lists songs whose audio never arrived. A USDB download can fetch the chart and artwork but fail on the media when the source is geo-restricted or the API refuses it, leaving a folder that holds a chart, a cover, a background and a `.usdb` marker but nothing to play. Re-tagging can't fix those — there is no file for `#MP3` to point at — so they need the media fetching again.

  ```bash
  uv run scripts/find_missing_audio.py --details      # what the syncer recorded
  uv run scripts/find_missing_audio.py --usdb-only    # only songs the syncer manages
  ```

  `--details` reads the `.usdb` marker and reports the syncer's own verdict plus the source it was reaching for, e.g. `usdb#5942 audio=failure, video=skipped_unavailable [a=CfDOP7WrDpw]`. Like its video sibling it splits the problem three ways (`--category none|broken|untagged|all`): no audio *and* no video, `#MP3` naming a file that isn't there, or audio present that no chart declares. A folder holding only a video counts as having audio, since clients play the video's own track.

- **`scripts/find_missing_video.py`** — reports songs with no usable background video, split into three separately-fixable problems: no video file at all, `#VIDEO` naming a file that isn't there, and a video sitting in the folder that no chart declares. `--write` fixes the third case by adding `#VIDEO` (only when the folder has exactly one video — it won't guess between several). `--usdb-only` narrows either script to folders with a `.usdb` marker, i.e. songs the syncer manages and could be asked to fetch again.

  ```bash
  uv run scripts/find_missing_video.py > video-missing.txt   # default: songs with no video
  uv run scripts/find_missing_video.py --category broken     # or: untagged, all
  uv run scripts/find_missing_video.py --write               # tag the untagged ones
  ```

- **`scripts/find_missing_usdb.py`** — lists every song folder that has no `<youtube-id>.usdb` marker file, i.e. hasn't been cross-referenced against USDB yet. Prints one bare `<Artist> - <Title>` folder name per line (sorted) to stdout — the form songs are matched against USDB in — with a count on stderr, so it can be redirected straight into `usdb-missing.txt`. Pass `--full-paths` for full paths instead, or a directory to scan somewhere other than `songs/`.

  ```bash
  uv run scripts/find_missing_usdb.py > usdb-missing.txt
  ```

## Known quirks

- Filenames routinely contain characters that need careful shell-quoting: `&`, `'`, smart quotes (`’`), accented characters, brackets.
- Some chart files have mixed/incorrect text encoding, visible as `�` replacement characters in lyrics — a pre-existing data quality issue in individual songs, not something globally fixed.
- Mac `.DS_Store` files are scattered throughout the library.
- A handful of songs processed through the old unicode-filename fixup ended up with `accompaniment.ogg` instead of `instrumental.ogg`, sometimes with duplicated `#VOCALS`/`#INSTRUMENTAL` tags. `scripts/tag_split_audio.py` normalizes both issues; run it after adding new split-audio songs.
