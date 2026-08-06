---
description: Turn a locally-authored chart into a valid USDB submission (header, cover/bg art, format fixes)
allowed-tools: Bash, Read, Edit, Write, mcp__claude-in-chrome__navigate, mcp__claude-in-chrome__computer, mcp__claude-in-chrome__get_page_text, mcp__claude-in-chrome__find, mcp__claude-in-chrome__javascript_tool, mcp__claude-in-chrome__file_upload, mcp__claude-in-chrome__list_connected_browsers, mcp__claude-in-chrome__select_browser, mcp__claude-in-chrome__tabs_context_mcp, mcp__claude-in-chrome__tabs_close_mcp
---

## Context

USDB (usdb.animux.de) hosts charts as plain `.txt` files, not media — the
audio/video/artwork are referenced by ID/URL, resolved client-side by
usdb_syncer at download time. Save any prepared upload copy to
`C:\ultrastar\usdb-uploads\` (create it if missing), not scattered in
scratch space — that's where the user expects to find them.

## Your Task

### Steps

1. **Resolve the source(s).** Get the YouTube video ID for the song. If
   audio and video come from the same upload, both `a=` and `v=` are that
   ID — but first confirm the file actually has both streams (`ffprobe
   -show_streams`); a "video" file pulled by some tools is video-only DASH
   with no audio track, which isn't discoverable by inspecting the container
   brand/name, only by checking for an audio stream directly.

2. **Rewrite the header to the hosted meta-tag format.** USDB's own parser
   only recognizes `a=`/`v=`/`co=`/`bg=`/`vpn=` inside `#VIDEO` — no local
   file paths, since it doesn't host media:
   ```
   #VIDEO:a=<ytid>,v=<ytid>[,co=<url-or-filename>][,bg=<filename>][,vpn=<cc>]
   ```
   If audio and video are the same source with a long intro before the
   audio proper starts, fold that offset directly into `#GAP` (`GAP_new =
   GAP_local + intro_ms`) rather than using `#VIDEOGAP` — the two streams
   can't drift apart if they're literally the same file, so a separate video
   offset is redundant and end users' syncers may not even need it.
   **Keep `#MP3`** even though it's not used to store real media — the
   classic UltraStar format still treats it as expected, and validators can
   reject a submission over its absence alone.

3. **Source cover/background art from fanart.tv.** Search
   `fanart.tv/search/?s=<artist>` (a Cloudflare interstitial may appear
   first; wait it out, it usually clears on its own within a few seconds).
   Convention observed across this library's `.usdb` markers: `bg=` is
   almost always a bare fanart.tv filename resolving under
   `https://assets.fanart.tv/fanart/<filename>`; `co=` is mixed — sometimes
   the same fanart.tv pattern, sometimes a full external URL from wherever
   the original uploader sourced it (Discogs, hitparade.ch, etc.). fanart.tv
   only supports artist-level backgrounds and album-level covers, not
   per-single artwork, so a specific track's cover may not exist there at
   all — check the artist's "Album Cover" section for a matching title.
   **Verify every URL actually resolves (`curl -o /dev/null -w '%{http_code}
   %{size_download}'`) before writing it into the chart** — a `bigpreview`
   or lightbox-only URL can 403 on direct fetch even though it displays fine
   in a browser; use the real asset host. If a category has zero images
   (e.g. no artist background), say so and leave the tag out — don't
   fabricate a plausible-looking filename.

4. **Fix common upload-validation errors:**
   - *"No Artist or Title found" despite `#ARTIST`/`#TITLE` clearly
     present*: check for CRLF. A file with pure LF line endings can defeat a
     PHP-side `explode("\r\n", ...)` parser entirely, since with no `\r\n`
     to split on the whole file reads as one blob. Confirm with a hex dump
     (`head -c N file | xxd`, look for `0a` with no preceding `0d`), then
     convert every line ending explicitly (byte-level, not a text-mode tool
     that might silently normalize things back) and re-verify chart
     structure survived (note count, no overlaps).
   - If the upload page is a bare `<input type="file">` with no visible
     format hint, the rejection is server-side content validation — the
     fastest way to learn the real requirement is to try the actual
     submission (with the user's explicit go-ahead, since it posts to a
     shared community site) and read the literal error text, rather than
     keep guessing at conventions.
   - The browser's `file_upload` tool can only see files already shared
     with that session (chat attachments or connected folders) — a real
     local path elsewhere on disk, even one this agent can read/write
     directly, will be rejected. Copy the file into a shared location first,
     or have the user attach it directly.

### Guardrails

- Never submit the upload form without the user's explicit go-ahead — it
  posts to a shared community database, not a local file.
- If multiple Chrome browsers are paired, always ask which one before any
  browser action; don't guess based on which was used last.
- Don't treat "the site returned an error" as license to strip fields
  speculatively one at a time against a live target — reason about the
  actual error text (or check the site's own source/behavior) before
  editing again.

---
*Generated by /reflect-skills from the USDB upload-preparation workflow in
this project's session history.*
