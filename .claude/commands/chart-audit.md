---
description: Check a chart's timing/pitch against its vocals.ogg stem and fix defects the user points at
allowed-tools: Bash, Read, Edit, Write, Glob, Grep
---

## Context

Applies to any folder under `songs/` that has a `vocals.ogg` stem (Melody
Mania split). The user will describe a defect at a moment in the song —
usually a timestamp plus a lyric fragment plus a symptom ("held too long",
"not held long enough", "should be scoreable", "extra note", "gap but the
audio has singing", "slop", "should be golden"). Sometimes several of these
arrive back-to-back, faster than they can be handled one at a time — that's
normal for this workflow, not a sign to rush; queue them and work through
each with the same rigor.

## Your Task

For each defect report, measure the real audio and fix the chart to match —
never guess, never eyeball it. `songs/` is gitignored, so there is no git
undo; back up the chart file before every write (a numbered `.bak*` suffix
per session is fine).

### Steps

1. **Resolve the timestamp.** Ask (or recall from earlier in the
   conversation) whether the in-game clock counts up or down — UltraStar
   clients commonly count DOWN from the track's total length. If down,
   `elapsed = track_length - quoted`. Get track length from `ffprobe` on the
   `#MP3` file. If the resolved time doesn't land near the lyric the user
   named, search by the lyric text instead — it's the reliable key, not the
   number.

2. **Map beats to time.** `t(beat) = (#GAP + beat * 60000/(#BPM*4)) / 1000`
   seconds. Parse the chart's header tags and note lines (`: beat len pitch
   text`, `*` golden, `F` freestyle, `-` line break) directly — don't rely on
   a stale in-memory copy after edits.

3. **Pitch-track `vocals.ogg`.** FFT-autocorrelation per ~10ms frame (16kHz
   mono, ~1024-sample window, 160-sample hop) is sufficient: convert lag to
   f0, f0 to MIDI, gate on autocorrelation clarity (~0.45+) and an RMS floor
   to mark voiced frames. Score pitch **mod 12** (pitch class) wherever
   possible — UltraStar scores mod 12 too, and it makes octave-detection
   errors in the tracker vanish instead of contaminating the result.

4. **Compare chart to audio** for the specific defect type:
   - *Held too long / too short*: find where voiced frames actually start
     and stop within and around the note's charted span. Reverb and
     instrumental bleed in a separated stem keep raw energy elevated well
     after the singer stops — use voiced frames, not an energy envelope, to
     judge this.
   - *Wrong/unscoreable pitch, freestyle (`F`) note*: measure the dominant
     pitch class over the note's voiced frames (mode, or circular mean if
     the note is long), placed in the octave nearest its neighbours. USKMaker
     and similar auto-charters use `F` + pitch `0` as their "couldn't detect
     a pitch" marker — that combination is a strong prior that the note is
     simply wrong, not that it's meant to be freestyle.
   - *Missing/extra note*: scan the gap between two charted notes for a
     continuous voiced run with no note covering it (a genuinely missing
     note), or a charted note with under ~20% voiced coverage (charted over
     silence — likely misplaced or spurious).
   - *"Slop" / vague complaint*: pull the full phrase's voiced-run segments
     and lay them next to the chart's note list for that span; the mismatch
     is usually obvious once both are visible together.

5. **Apply the fix**, then re-verify: no overlapping/misordered notes, note
   counts sane, and — if you changed a note's beat or length — recheck for
   newly-introduced touching notes at the same pitch. Moving a boundary into
   the middle of a sustained vowel is a common way to accidentally split one
   sung tone into two same-pitch notes, which forces an audible
   re-articulation that wasn't in the recording. Diff the set of touching
   same-pitch pairs against the pre-edit chart so only *newly introduced*
   ones get merged back — this library's charts already contain plenty of
   legitimate same-pitch neighbours between genuinely separate syllables.

### Guardrails

- **Never convert a whole line to freestyle** to sidestep a hard note. Fix
  notes individually where the pitch reads confidently; leave one or two
  genuinely unpitchable notes (function words, unvoiced consonants) as `F`.
  If asked to apply a "some fraction of the line is freestyle → convert the
  whole line" rule, still fix what's fixable *first*, then apply the
  threshold to what's left — not before.
- Before writing, measure with a confidence gate (voiced-frame count,
  coverage percentage, dominance fraction) and show it — a value backed by 3
  ambiguous frames is not the same claim as one backed by 40 clean ones.
- If a user report doesn't match anything at the resolved timestamp, don't
  force a fix onto the nearest thing — surface the mismatch and reconsider
  the clock-direction assumption before guessing further.
- If a report turns out to already be fixed (e.g. from a stale in-game song
  cache), say so plainly rather than re-deriving a no-op fix.

---
*Generated by /reflect-skills from repeated chart-timing correction requests
in this project's session history.*
