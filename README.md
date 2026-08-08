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
- `R` / `G` — rap and golden rap notes, which carry no meaningful pitch
- `F <beat> <length> <pitch> <text>` — freestyle note: no pitch, and scores nothing
- `- <beat>` — line break
- `P1` / `P2` — marks the start of a part for duet songs, splitting the chart between two singers
- `E` — end of file

These files are consumed by UltraStar-family game clients (UltraStar Deluxe, Vocaluxe, etc.), not by anything in this repo.

[The UltraStar File Format (v1)](https://github.com/UltraStar-Deluxe/format/blob/main/The%20UltraStar%20File%20Format%20(v1).md) is the authoritative definition, and worth consulting rather than inferring from the files here — a few of its rules are genuinely surprising:

| | |
|---|---|
| `#GAP` | milliseconds from the start of the audio to beat 0 |
| `#START` | **seconds** — where playback begins |
| `#END` | **milliseconds** — where playback stops |
| `#VIDEOGAP` | seconds; **positive delays the video**, negative skips that much of its start |
| `#AUDIO` | the newer equivalent of `#MP3` |

The `#VIDEOGAP` sign is the one to be careful with. This library contains both signs, and the largest positive values sit with videos that plainly carry extra footage — which suggests the opposite reading to the correct one. Beat *b* falls at `#GAP + b × 60000/(#BPM×4)` ms.

## Scripts

Requires [`uv`](https://docs.astral.sh/uv/), plus `ffmpeg`/`ffprobe` on PATH for the audio-length checks. `split_audio_stems.py` additionally wants a CUDA GPU; every other script is pure filesystem and text work with no Python dependencies at all.

Every script defaults to a **dry run** and needs `--write` to change anything.

### The one to reach for

- **`scripts/fix_my_library.py`** — runs every routine remediation in the order the individual scripts need, then reports what still wants a human.

  ```bash
  uv run scripts/fix_my_library.py            # preview everything
  uv run scripts/fix_my_library.py --write    # apply everything
  uv run scripts/fix_my_library.py --terse    # only report what changed
  ```

  `--terse` is handed on to the steps that understand it, and only those — a step that doesn't would abort the run on an unknown argument. Any step exiting non-zero stops the run and its code is returned, so a failure never feeds bad state into a later step.

  The sequence is `strip_bom` → `tag_split_audio` → `resolve_duplicate_songs` → `tag_split_audio` again → `fix_missing_mp3` → `find_missing_video`. BOMs go first because one makes a header line invisible to every other tool here; duplicate resolution comes before the per-folder fixes because it moves whole folders around; and stem tagging runs on both sides of it so duplicate resolution can find split audio by its conventional name, and so anything it merges in under a non-standard name still gets normalized. The second tagging pass is idempotent and normally a no-op — in a *dry run* it reports the same counts as the first only because nothing was actually applied in between.

  The sequence is `strip_bom` → `prune_desynced_stems` → `tag_split_audio` → `resolve_duplicate_songs` → `tag_split_audio` → `fix_missing_mp3` → `find_missing_video`.

  Pruning sits second because it is boxed in from both sides: it reads `#MP3`, which a byte-order mark hides, so it cannot precede `strip_bom`; and `fix_missing_mp3` points `#MP3` at a stem for folders that have nothing else, which is a state pruning skips — leave it until after and those folders are exempt for good. Inside that window it goes as early as it can, so the tagging passes don't measure or complain about stems that are about to be deleted. It **deletes files, and `songs/` is gitignored**, so this used to be left out on the grounds that it couldn't be undone; now that `split_audio_stems.py` can regenerate a stem from the song's own mix, automating it is reasonable. It still only deletes under `--write`, and a `--write` run says so in the step header.

  It deliberately leaves out `split_audio_stems.py` — several hours of GPU time is not something to start as a side effect of a routine tidy-up.

### Individual remediations

- **`scripts/tag_split_audio.py`** — scans every folder under `songs/` that has `vocals.ogg`. If `instrumental.ogg` is present too, it's used as-is; if only `accompaniment.ogg` is present (see the naming quirk below), it's renamed to `instrumental.ogg`. Every chart (`.txt`) in the folder then has its `#VOCALS`/`#INSTRUMENTAL` tags added if missing, corrected if they still point at `accompaniment.ogg` after a rename, and deduplicated if a tag appears more than once (a pre-existing bug used to insert tags blindly, sometimes duplicating them). Defaults to a dry run.

  ```bash
  uv run scripts/tag_split_audio.py            # preview changes across songs/
  uv run scripts/tag_split_audio.py --write     # apply changes
  ```

  Stems are only tagged when they belong to the folder's **own** full mix — the same length check `prune_desynced_stems.py` uses, shared between them in `scripts/utils/audio_lengths.py`. Pointing a chart at stems from a different rip would play them offset against its notes, so a mismatch is reported and left untagged instead. Having nothing to compare against is not a mismatch: a folder with no full mix is tagged as before. `--tolerance` sets how far apart is too far (default 1 second). The check only runs where there is actually something to tag, so an already-tagged library isn't slowed by measuring it.

  It also has a one-off mode for a Melody Mania (vocal-separation tool) bug: when a song's filename contains unicode characters, Melody Mania fails to write its split output into the song folder and instead strands a `<name>.vocals.ogg` / `<name>.accompaniment.ogg` pair under `%APPDATA%/LocalLow`. Point `--import-stranded` at either stranded file and it moves both into the song's folder as `vocals.ogg`/`instrumental.ogg` and tags the chart, skipping the full scan.

  ```bash
  uv run scripts/tag_split_audio.py --import-stranded "/path/to/LocalLow/.../SongName.vocals.ogg" --write
  ```

- **`scripts/split_audio_stems.py`** — separates a song's full mix into `vocals.ogg` / `instrumental.ogg` on the GPU, then tags the charts to match. Every other script here reacts to stems that already exist; this is the one that makes them. Defaults to a dry run.

  ```bash
  uv run scripts/split_audio_stems.py                                  # what would be separated
  uv run scripts/split_audio_stems.py --write                          # separate everything missing stems
  uv run scripts/split_audio_stems.py --write --dir "songs/Artist - Title"   # just this one song
  uv run scripts/split_audio_stems.py --list-models                    # alternatives, with SDR scores
  ```

  Separation is done by [`audio-separator`](https://github.com/nomadkaraoke/python-audio-separator) running a UVR model, BS-Roformer (Viperx-1297) by default — the best instrumental SDR of the models it bundles, and the instrumental is the half anyone actually sings over. `--model` takes any of the others. The checkpoint is ~640MB and downloads itself on first use, into `~/.cache/audio-separator-models` unless `AUDIO_SEPARATOR_MODELS` says otherwise.

  It doesn't strictly *need* a GPU — `audio-separator` falls back through CUDA, Apple MPS, DirectML and finally CPU, and never errors — but a GPU is the difference between practical and not. On one 30-second clip with the same model, an RTX 5070 took 11 seconds and a 48-core CPU took 132, about 4× slower than realtime, and a machine with an ordinary core count is slower still. So the script names the device it found before starting, and a `--write` run that lands on the CPU stops and asks for `--allow-cpu` rather than quietly beginning something that would run for days. `--use-directml` opts into the experimental AMD/Intel graphics path, which also needs `torch-directml` adding to the dependency header at the top of the script.

  This is the one script with Python dependencies, and the one that wants a GPU. They're declared in the script's own PEP 723 header, so `uv run` still installs nothing by hand; `requirements.txt` at the repo root pins the same set for anyone who'd rather use a normal venv. What it does need is a CUDA build of torch with kernels for your actual card — `torch.cuda.is_available()` reports True on a Blackwell GPU even when the wheel has no `sm_120` in it, and you don't find out until the first separation dies. Reckon on 30–60s per song.

  Two details worth knowing, because both look like bugs otherwise. The mix is decoded to a **stereo** WAV before it reaches the model: BS-Roformer refuses anything that isn't 2-channel, and this library has mono rips and 5.1 soundtrack rips in it. And the stems come back at the model's own **44.1kHz** no matter what the source was — most of this collection is 48kHz. Their duration is preserved to the microsecond, which is what the chart is timed against, so nothing plays offset; but it does mean comparing a stem against its mix sample-for-sample tells you nothing.

  New stems are given the mix's **ReplayGain** value as they're created, by handing the folder to `apply_replaygain.py`. A stem created without one is out of step with the rest of the library the moment it exists, and it has to inherit the *mix's* gain rather than measure its own — a stem on its own is several dB quieter, so measuring it separately would pull the vocal/instrumental balance apart. Most mixes already carry a gain, so this is usually one tag read and two writes against a minute of separation. `--no-replaygain` skips it.

  Nothing is installed into a song folder until it has been measured against the mix it came from, because the presence of `vocals.ogg` is what marks a song done — a half-written one would look finished to the next run. An interrupted run simply resumes from there.

  A song that **fails** is a different matter: it leaves a `.stem-separation-failed.json` memo in its folder and later runs skip it, because at a minute of GPU time each, rediscovering the same broken files on every pass costs hours. The memo names the audio's file *and its size*, so replacing the mix puts the song back in the queue with nothing to remember; a successful separation deletes any memo it finds. `--retry-failed` ignores them, and `--terse` skips them without saying so. The song being worked on is printed before the model starts rather than after it finishes, so the progress bar isn't anonymous. It is deliberately **not** part of `fix_my_library.py`: a full pass over this library is several hours of GPU time and isn't something to trigger by accident.

- **`scripts/fix_missing_mp3.py`** — `#MP3` is the tag UltraStar clients use as the primary audio reference, and some song folders never got one: they hold only split `vocals.ogg`/`instrumental.ogg` stems, or a chart arrived without it. Scans every chart under `songs/` and points the tag at the best audio the folder actually has — its **full mix**, failing that its **video**, failing that **`instrumental.ogg`**. Folders with none of the three are reported and left alone. Defaults to a dry run.

  ```bash
  uv run scripts/fix_missing_mp3.py            # preview changes
  uv run scripts/fix_missing_mp3.py --write     # apply changes
  ```

  A video only counts as audio if it **actually has an audio stream**. `fix_missing_video.py` downloads `-f bestvideo` deliberately — UltraStar plays the background video muted — so the file it leaves behind carries a video stream and nothing else. Between them the two scripts pointed `#MP3` at silence for 11 songs, which played without a sound. Nine had an `instrumental.ogg` to fall back to; the remaining two need audio fetching with `extract_audio_from_youtube.py`, since no tag can conjure audio that isn't there. A silent video in `#MP3` is corrected whether or not the folder has anything better — it isn't a second-best choice, it's simply wrong.

  The video is otherwise a *fallback*, and the ordering matters more than it looks. This script used to reach for the video first, which was harmless for the handful of stems-only folders it was written for but wrong anywhere a real audio file existed — and a video's duration is not the song's. `Demi Lovato - Gift Of A Friend` ended up playing a 203.1s `.avi` against a chart whose stems had been separated out of the 205.5s `.mp3` sitting next to it.

  So it also **corrects** an existing tag, not just a missing one, in the two cases where the value is actually wrong rather than merely second-choice: it names a file that isn't in the folder, or it names a video or a stem while a full mix is available. A `#MP3` pointing at a video in a folder with no audio at all is the fallback doing its job and is left untouched.

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

  **`--terse`** drops the report for any set that ends up untouched, printing only the ones something actually happened to. The tallies at the end are unchanged, so nothing goes unaccounted for. Every script that can pass over a song takes the same flag — `tag_split_audio`, `fix_missing_mp3`, `prune_desynced_stems` and `find_missing_video` too. It pairs naturally with `--merge-variants`, where most sets are left alone by design — on the current library that's 838 lines of output down to 80, with all 10 acted-on sets still shown in full. In an `--interactive` run it has little to do: the prompt for a set is on screen before you decide to skip it.

  ```bash
  uv run scripts/resolve_duplicate_songs.py --merge-variants --terse
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

  **Deletion cannot be undone from git** — `songs/` is gitignored, so there's no history to restore from. It can, however, be undone by `split_audio_stems.py`, which regenerates stems from the song's own mix; that is what changed this from something to run by hand into step 2 of `fix_my_library.py`. It skips, and reports, any folder with no full-mix audio to compare against (a video is *not* used as the reference: music videos routinely carry extra footage, so their duration legitimately differs) and any folder whose `#MP3` points at a stem (deleting those would leave no audio at all). `fix_missing_mp3.py` creates exactly that kind of pointer for stems-only folders, which is why the orchestrator runs this before it — the other way round, those folders would be skipped forever.

  Each deleted stem also has its cached duration dropped, so the measurement never outlives the file it describes.

- **`scripts/utils/audio_lengths.py`** — not a command, but the shared length check the others use, and the only place anything external is run. It lives in `scripts/utils/`, a package for modules that hold functions and constants and do nothing when imported; anything that *does* something when you run it is a script and sits one level up. Scripts reach it as `from utils import audio_lengths`. Stems come out of a song's full mix, so they should be the same length as it; when they aren't they came from a different rip and will play offset against the chart. `tag_split_audio` won't tag such stems, `prune_desynced_stems` deletes them, and `resolve_duplicate_songs` won't move them onto a keeper they don't fit.

  Which file counts as "the full mix" is decided by **what the chart names in `#MP3`**, not by what turns up first in the folder. A song folder can hold more than one candidate — 51 here do, usually an `.mp3` and an `.ogg` left behind by a re-rip — and 35 of those pairs turn out to be genuinely different recordings, in one case 92 seconds apart. Taking whichever sorted first meant measuring stems against a rip the chart never plays, which quietly inverts the check rather than merely weakening it: `prune_desynced_stems` would delete good stems for disagreeing with the wrong file, and `tag_split_audio` would approve stems that disagree with the right one. That had already happened to 34 folders. Only when no chart names an audio file does it fall back to the first one alphabetically.

  Worth remembering that a full mix is often **not** an mp3, whatever the tag is called: `.ogg`, `.flac` and `.opus` all show up, and `.ogg` is by far the most common in this library.

  Measuring a library's worth of audio takes minutes, and the answer only changes when a file does, so results are kept between runs in `scripts/utils/.audio-lengths.json` (gitignored), beside the module that owns it. Entries are keyed by path **and file size**, so replacing a file re-measures it by itself, and entries whose file has gone or changed are dropped when the cache is written. `prune_desynced_stems` drops an entry outright via `forget()` as it deletes each stem — that matches on the path alone rather than path-and-size, since a file that has just been deleted can no longer be measured for the size half of its own key. Only successful measurements are stored — remembering a failure would make one bad run permanent. The file is written every hundred new measurements rather than only at the end, so interrupting a long run keeps what it had measured so far; stale entries are weeded out on the final write, which keeps those interim ones cheap. Set `AUDIO_LENGTH_CACHE` to move the file, or to an empty value to measure every time.

- **`scripts/find_missing_audio.py`** — lists songs whose audio never arrived. A USDB download can fetch the chart and artwork but fail on the media when the source is geo-restricted or the API refuses it, leaving a folder that holds a chart, a cover, a background and a `.usdb` marker but nothing to play. Re-tagging can't fix those — there is no file for `#MP3` to point at — so they need the media fetching again.

  ```bash
  uv run scripts/find_missing_audio.py --details      # what the syncer recorded
  uv run scripts/find_missing_audio.py --usdb-only    # only songs the syncer manages
  ```

  `--details` reads the `.usdb` marker and reports the syncer's own verdict plus the source it was reaching for, e.g. `usdb#5942 audio=failure, video=skipped_unavailable [a=CfDOP7WrDpw]`. Like its video sibling it splits the problem three ways (`--category none|broken|untagged|all`): no audio *and* no video, `#MP3` naming a file that isn't there, or audio present that no chart declares. A folder holding only a video counts as having audio, since clients play the video's own track.

- **`scripts/find_missing_video.py`** — reports songs with no usable background video, split into three separately-fixable problems: no video file at all, `#VIDEO` naming a file that isn't there, and a video sitting in the folder that no chart declares. `--write` fixes the third case by adding `#VIDEO` (only when the folder has exactly one video — it won't guess between several). Both media scripts print bare `<Artist> - <Title>` names, with `--full-paths` for full paths; `--usdb-only` narrows either to folders with a `.usdb` marker, i.e. songs the syncer manages and could be asked to fetch again.

  ```bash
  uv run scripts/find_missing_video.py > video-missing.txt   # default: songs with no video
  uv run scripts/find_missing_video.py --category broken     # or: untagged, all
  uv run scripts/find_missing_video.py --write               # tag the untagged ones
  ```

- **`scripts/fix_missing_video.py`** — the other half of the above: give it a song and a YouTube link and it downloads the video and tags the charts. Only the video stream is fetched, since UltraStar plays a background video muted — the sound comes from `#MP3` or the stems, so an audio track would be wasted bandwidth and a second, unwanted source of noise. Defaults to a dry run.

  ```bash
  uv run scripts/fix_missing_video.py "38 Special - Hold On Loosely" mh4CgxITgbE
  uv run scripts/fix_missing_video.py "./songs/38 Special - Hold On Loosely/" mh4CgxITgbE --write
  uv run scripts/fix_missing_video.py 7456 https://www.youtube.com/watch?v=mh4CgxITgbE --write
  ```

  The song can be a **folder path, a bare folder name, or a USDB song id**, whichever is to hand — the `find_missing_*` scripts print bare names, shells tab-complete paths, and `.usdb` markers carry the id. A path arrives from tab-completion with a trailing separator, and on Windows a trailing backslash inside double quotes escapes the quote and leaves a stray `"` on the end; both are trimmed, since neither is something you did wrong.

  A `#VIDEO` tag naming a file that **isn't in the folder** counts as missing rather than as already handled. That's the usual state of a song whose video never downloaded — the chart comes from USDB already naming a video the fetch then failed to produce — so treating any tag at all as "leave this alone" meant declining to help with exactly the songs this exists for. A `#VIDEO` naming a file that really is there is still left alone unless you pass `--force`.

  It's local-only: it prints the `#VIDEO:a=<id>,v=<id>` line USDB's own edit form expects, but pairing the video with the USDB entry stays a manual step you do yourself. `yt-dlp` is checked before anything is downloaded, so a missing one is a single clear line rather than a resolver error buried in yt-dlp's output after the script has already said what it planned to do.

- **`scripts/extract_audio_from_youtube.py`** — the audio counterpart to the above, for what `find_missing_audio.py` reports: a USDB fetch that got the chart and artwork but failed on the media leaves a folder with nothing to play, and no amount of re-tagging conjures audio that isn't there. Fetches audio only — no video stream at all — and points `#MP3` at the result. Defaults to a dry run.

  ```bash
  uv run scripts/extract_audio_from_youtube.py "Artist - Title" mh4CgxITgbE
  uv run scripts/extract_audio_from_youtube.py "Artist - Title" ./downloads/clip.mkv --write
  ```

  The source can be a YouTube URL or id, **or a path to a local video or audio file** — then ffmpeg lifts the track straight out of it and nothing is downloaded, which suits a folder whose video does have sound, or audio from somewhere yt-dlp can't reach. A local file with no audio track is refused before anything is written: that's the very state this script exists to repair.

  **`--trim`** cuts the result down to the song and moves the timing tags to match. The window is `#START` to `#END`, which the format spec calls the start and end point of the song, so what goes is exactly what playback was skipping anyway. `#GAP` is *not* used as the lower bound, deliberately — it marks beat 0, and the bars of intro before the first note are what a singer comes in on. For a duet the wider of the two charts' windows wins.

  Cutting T seconds off the front moves `#GAP` and `#END` earlier by T and retires `#START`, since all of them are measured from the start of the file. `#VIDEOGAP` moves the opposite way: it is the video's delay relative to the audio, so with the audio now starting later in the song the video has to skip the same amount to stay level, and the value goes down. `#GAP:30000` with `#VIDEOGAP:5` becomes `#GAP:10000` with `#VIDEOGAP:-15` — both put the same video frame on beat 0. That sign is taken from the [format spec](https://github.com/UltraStar-Deluxe/format), not guessed from the library, which contains both signs and would have suggested the wrong one.

  Unlike a video, the result's **length is checked against the chart**. A background video legitimately runs longer or shorter than the song, but `#MP3` is the audio the notes are timed against, so a chart whose singing runs past the end of the file is being played against the wrong recording — a radio edit, another live take, the wrong song. `#END` is respected, since playback stops there and notes beyond it are never reached. A download that fails the check is deleted and reported; `--force` keeps it. Afterwards it reminds you to re-check `#GAP`, because a different source almost always starts at a different offset and nothing here can verify that for you.

- **`scripts/apply_replaygain.py`** — gives every song a ReplayGain tag, and gives its stems the same one. UltraStar Deluxe *reads* these tags (from 2025.4.0, once you switch ReplayGain on under Tools → Options → Sound) but never writes them, so something else has to. The USDB syncer writes them for the audio it fetches — roughly two thirds of the full mixes here already carry one — and this fills in the rest and passes the value on to `vocals.ogg` and `instrumental.ogg`, which nothing had ever tagged. Defaults to a dry run.

  ```bash
  uv run scripts/apply_replaygain.py                        # what would be tagged
  uv run scripts/apply_replaygain.py --write                # tag the library
  uv run scripts/apply_replaygain.py --dir "Artist - Title" --write
  ```

  **The audio is never re-encoded.** A ReplayGain tag only records how much to turn a track down at playback, so nothing is resampled or recompressed and the change can be undone by deleting the tag. Confirmed rather than assumed: decoding a mix and both its stems to raw PCM before and after tagging gives byte-identical hashes.

  Every file in a folder gets the **same gain**, taken from the full mix, and that's the point of the script. A stem measured on its own is much quieter than the mix it came from — for one song here the mix scores −9.90 dB against −4.07 for the vocal and −5.95 for the instrumental — so normalizing each independently would push the two stems nearly 2 dB apart and undo the balance UltraStar's vocals toggle relies on. Peaks stay per-file, because a peak describes the file rather than the correction, and they matter: plenty of these gains are *positive* (one quiet rip scored +5.27 dB), and a player needs the peak to avoid clipping when it turns a track up.

  It's safe to run again over a library that's already tagged, and cheap: a gain that's already there is reused rather than re-measured, and a stem already carrying it is skipped. ReplayGain values never accumulate — the tag tells the player what to do rather than changing the samples — so running twice can't attenuate anything twice, which is the trap with the re-encoding kind of normalization.

  `--force` means *distrust the stored value and measure again*, not *rewrite regardless*. It measures on a throwaway copy, because ffmpeg-normalize will only tell you a gain by writing it, and then writes only where the answer actually differs. Rewriting a file with the number it already had isn't free even when the bytes come out identical: it earns a fresh modification time, and a sync that compares timestamps would resend the whole library for nothing.

  Give it a song — a path, a folder name or a USDB id — to do just that one; omit it to scan the library. Songs that can't be measured leave a `.replaygain-failed.json` note in their folder and are skipped by later runs, which say so unless you pass `--terse`; `--force` retries them, and any successful run removes the note.

  A folder with stems but no full mix gets its reference rebuilt by summing the stems — separation output adds back up to what it came from. That sum happens in a temp directory and never lands in the song folder, where it would start being mistaken for the real mix. It's approximate, about 1.7 dB off on the song that has both to compare, which is the best available for the 41 folders in that state and still better than normalizing them by a different rule than everything else.

- **`scripts/find_missing_stems.py`** — lists songs with no usable `vocals.ogg` + `instrumental.ogg` pair, separated into two problems because they need different fixes. **`none`** is a song with no stems at all, split again by `--category has-source|no-source` depending on whether there's a full mix to separate from; a `has-source` listing is precisely the work queue for `split_audio_stems.py`, while `no-source` needs audio fetching first. **`partial`** is a song with exactly one of the two files — separation always produces both, so that's a data problem rather than a normal state.

  ```bash
  uv run scripts/find_missing_stems.py --category has-source   # ready to separate
  uv run scripts/find_missing_stems.py --category partial --details
  ```

  Read-only, and deliberately so: separating a library is an hours-long job worth starting on purpose rather than as a side effect of asking what's missing. It also doesn't flag stems that exist but disagree with their mix — that's `prune_desynced_stems.py`'s question, about correctness rather than presence.

- **`scripts/find_missing_usdb.py`** — lists every song folder that has no `<youtube-id>.usdb` marker file, i.e. hasn't been cross-referenced against USDB yet. Prints one bare `<Artist> - <Title>` folder name per line (sorted) to stdout — the form songs are matched against USDB in — with a count on stderr, so it can be redirected straight into `usdb-missing.txt`. Pass `--full-paths` for full paths instead, or a directory to scan somewhere other than `songs/`.

  ```bash
  uv run scripts/find_missing_usdb.py > usdb-missing.txt
  ```

## Known quirks

- Filenames routinely contain characters that need careful shell-quoting: `&`, `'`, smart quotes (`’`), accented characters, brackets.
- Some chart files have mixed/incorrect text encoding, visible as `�` replacement characters in lyrics — a pre-existing data quality issue in individual songs, not something globally fixed.
- Mac `.DS_Store` files are scattered throughout the library.
- A handful of songs processed through the old unicode-filename fixup ended up with `accompaniment.ogg` instead of `instrumental.ogg`, sometimes with duplicated `#VOCALS`/`#INSTRUMENTAL` tags. `scripts/tag_split_audio.py` normalizes both issues; run it after adding new split-audio songs.
