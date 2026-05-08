# Design: virtual track splits

**Status:** proposed
**Author:** zmatarasso
**Constraint:** must work with the upstream Relisten schema unchanged
(so any Relisten DB can be swapped into this stack and Just Work).

## Problem

A `source_tracks` row points at one URL and plays the whole file. Our
current BLAHSUM library has shows that are *one continuous mp3* even
though they contain multiple songs. We want to expose those songs as
separate tracks in the UI — separate titles, separate `track_position`,
the player advances through them gaplessly — *without* splitting the
underlying audio file or changing the DB schema.

## Constraints

- **Schema is locked.** No new columns on `source_tracks`, no new
  tables. Anything we add has to live in fields that already exist.
- **DB swap-in.** A user must be able to drop Relisten's public DB
  backup into this stack and the player must keep working without
  these features turned on. (i.e. our split logic has to be a no-op
  for normal whole-file tracks.)
- **No double-fetch.** If three tracks all live in the same file, the
  player should fetch the file once.

## Proposal: encode start/end in the mp3_url query string

Each "virtual track" row gets the same base URL with different query
parameters:

```
source_tracks (showing relevant fields):
  id  track_position  title              mp3_url
  ─── ──────────────  ─────────────────  ────────────────────────────────────────────────────
  101 1               Intro              https://.../03-15-24.mp3?t=0,180
  102 2               Crystal Eel        https://.../03-15-24.mp3?t=180,720
  103 3               Sundown            https://.../03-15-24.mp3?t=720,1620
  104 4               Encore             https://.../03-15-24.mp3?t=1620
```

Convention: `?t=<start>,<end>` in seconds. End optional (means
"play to end of file"). The choice of `?t=` mirrors the [W3C Media
Fragments URI](https://www.w3.org/TR/media-frags/) spec — same idea,
just enforced client-side instead of by the (uncooperative) browser.

### Why this works

- **Schema unchanged.** `mp3_url` is `text`. Query params are part of
  the URL. The DB doesn't care.
- **Existing UNIQUE works.** `(artist_id, mp3_url)` is already unique
  in the schema. Different query params → different `mp3_url` →
  different rows. Idempotent imports continue to work.
- **API unchanged.** v3 JSON serializer emits `mp3_url` as a string;
  it'll happily emit our query-tagged URL.
- **Audio server unchanged.** nginx ignores the query string; same
  bytes served whether the URL is `.mp3` or `.mp3?t=180,720`.
- **Mobile / Sonos / other clients** that don't know about `?t=` get
  whole-file playback. The split is a degradation-graceful
  enhancement.
- **Relisten's DB swapped in** has no `?t=` URLs, so the player path
  for those tracks is the existing one — no behavior change.

### Why this isn't just Media Fragments URI

The W3C `#t=start,end` syntax exists *and* is parsed by browsers, but
**no major browser enforces the end time** on `<audio>` elements.
Start works, end doesn't. So we can't lean on the spec; we have to do
the seek-and-stop logic in the player ourselves. Once we're doing it
ourselves, query string is more portable than fragment (fragments get
stripped at some HTTP middleware layers; query strings survive
proxies, CDNs, and analytics tools).

## Player implementation sketch

In [`relisten-web`](https://github.com/Zmatarasso/blahsum-relisten),
the player code (`src/lib/player.tsx` plus the gapless audio engine)
needs three additions:

### 1. URL parsing helper

```ts
type TrackBounds = { fetchUrl: string; startSec: number; endSec: number | null };

export function parseTrackBounds(mp3Url: string): TrackBounds {
  const u = new URL(mp3Url);
  const t = u.searchParams.get('t');
  if (!t) return { fetchUrl: mp3Url, startSec: 0, endSec: null };

  const [a, b] = t.split(',');
  const startSec = Number(a) || 0;
  const endSec = b !== undefined && b !== '' ? Number(b) : null;

  // Strip ?t= so the browser caches one entry per real file.
  u.searchParams.delete('t');
  return { fetchUrl: u.toString(), startSec, endSec };
}
```

The "strip on fetch" detail is what makes three tracks-from-one-file
hit the network once. Browser cache key = the stripped URL; we keep
the original (with `?t=`) only as a logical track identity for the
queue.

### 2. On track load

```ts
const { fetchUrl, startSec, endSec } = parseTrackBounds(track.mp3_url);
audio.src = fetchUrl;
audio.currentTime = startSec;   // browsers honor seek-to-start
```

### 3. On timeupdate

```ts
audio.addEventListener('timeupdate', () => {
  if (endSec !== null && audio.currentTime >= endSec) {
    advanceToNextTrack();
  }
});
```

If the next track is in the same file, `advanceToNextTrack` should
**not** reset `audio.src` — just call `parseTrackBounds` on the new
track and `audio.currentTime = startSec`. That's the gapless path.
The existing player already has gapless logic for adjacent files; we
extend it to also detect "adjacent virtual tracks in the same file"
(same `fetchUrl`).

### Edge cases

- **Seeking inside a virtual track**: clamp the user's seek to
  `[startSec, endSec)`. Or don't, and let the user seek out into the
  "next track" of the same file — the timeupdate logic will eventually
  catch up. Pick whichever feels less surprising; I'd lean toward
  clamping.
- **Scrubber / progress bar**: shows progress within the virtual
  track, not within the whole file. `(currentTime - startSec) /
  (endSec - startSec)`.
- **Reported duration**: `track.duration` from the DB should be the
  virtual track's duration (`endSec - startSec`), not the whole file.
  The importer is responsible for setting this correctly.
- **HTML5 audio limitation**: `<audio>` will sometimes preroll a tiny
  bit before honoring `currentTime`. ~10–50ms of "previous track"
  may be audible at the start. Acceptable for v1; if we want frame-
  accurate splits later we'd switch to Web Audio API decoded buffers.

## Importer support

A second deliverable: the importer needs a way to *generate* these
split URLs from source data. Two convention candidates, both
sidecar-based so they don't collide with the existing whole-file
flow:

### Option 1: standard `.cue` files (recommended)

Industry standard for tracklist + offsets. If the importer finds
`BLAHSUM live in Brooklyn 4-26-25.mp3` next to
`BLAHSUM live in Brooklyn 4-26-25.cue`, it parses the cue and creates
N source_tracks rows pointing at the same mp3 with `?t=` ranges. Many
existing tools generate cue files. Format is well-documented.

Example:
```
FILE "BLAHSUM live in Brooklyn 4-26-25.mp3" MP3
  TRACK 01 AUDIO
    TITLE "Intro"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Crystal Eel"
    INDEX 01 03:00:00       ← MM:SS:FF where FF is frames @ 75fps
  TRACK 03 AUDIO
    TITLE "Sundown"
    INDEX 01 12:00:00
```

The importer reads the next track's INDEX as the current track's end.
Last track has no end (plays to file end). Add a Python `cuesheet`
dependency or a 50-line custom parser.

### Option 2: simple JSON sidecar

`<filename>.splits.json`:
```json
[
  { "title": "Intro",         "start": 0 },
  { "title": "Crystal Eel",   "start": 180 },
  { "title": "Sundown",       "start": 720 },
  { "title": "Encore",        "start": 1620 }
]
```

End of each track is the next track's start. Last has no end.
Lower-friction to author by hand than .cue, but less interop.

**Recommendation**: support both. Detect `.cue` first; fall back to
`.splits.json`.

## Migration path

This rolls out behind a feature flag — the importer doesn't generate
split URLs unless told to (env var or per-file sidecar). Tracks
imported before this lands keep their whole-file URLs and play
unchanged.

Order of work:

1. Player: `parseTrackBounds` + apply at load + apply at timeupdate +
   gapless within same file (~half a day).
2. Importer: cue/json parser, generate one source_tracks row per
   virtual track, set `track_position` and `duration` correctly
   (~half a day).
3. Documentation: README section on how to author splits (~30 min).
4. UI polish (optional): scrubber clamping, "this track is part of
   `<file>`" tooltip, etc.

## What this design does NOT do

- **Server-side audio chunking.** We never touch the actual audio
  bytes — no ffmpeg, no transcoding, no on-the-fly extraction. The
  whole point is leaning on byte-range HTTP and client-side time
  offsets.
- **Frame-accurate splits.** HTML5 audio's seek precision is ~10ms in
  practice. If we ever need exact frame boundaries (rare for live
  music) we'd move to Web Audio decode.
- **Cue editing in the UI.** Splits are authored offline (write a
  `.cue` next to the file, re-run importer). An in-browser cue editor
  would be a separate project.
- **Schema changes.** Per the constraint at the top: every bit of
  state lives in the existing `mp3_url` field as a query string.
  Drop in any Relisten-shaped DB and the player still works for
  whole-file tracks.

## Open questions

- Is `?t=` the right param name? Mirrors the W3C spec, but conflicts
  with some analytics tools' tracking parameter. Alternative: `?seg=`
  or `?bs_t=`. Lean toward `?t=` for spec familiarity.
- Should the URL also encode title? No — title lives in the DB. The
  URL only carries playback metadata, not display metadata.
- Should the importer auto-generate splits from silence detection?
  Cool, but a separate project; defer.
