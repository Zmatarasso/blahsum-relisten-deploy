# blahsum.live — editing & deploying guide (for AI agents and humans)

Public site: **https://truenas-scale.tail5a1866.ts.net:10000/**
(Tailscale Funnel → unix socket → `edge` nginx on the TrueNAS box.)

Read this whole file before changing anything. The NAS that hosts this also
holds irreplaceable family photos/videos. Rule #1: **never widen what the
public containers can reach.**

---

## 1. Where things live

| What | Where |
| --- | --- |
| Deploy repo (compose, importer, edge nginx, docs) | `Zmatarasso/blahsum-relisten-deploy`, checked out on the NAS at `/mnt/SolidStateApps/relisten/blahsum-relisten-deploy` (and on Zach's PC at `C:\Users\Kazta\Documents\blahsum-relisten`) |
| Backend (.NET 10, fork of RelistenApi) | `./RelistenApi` → `Zmatarasso/blahsum-relisten-api` |
| Frontend (Next.js 16, fork of relisten-web) | `./relisten-web` → `Zmatarasso/blahsum-relisten` |
| Audio files (served read-only) | `/mnt/BlahNas/Blahsum/shared/radioTracksStorage` (`AUDIO_DIR` in `.env`) |
| Postgres data | `/mnt/SolidStateApps/relisten/data/postgres` |
| DB dumps / config backups | `/mnt/SolidStateApps/relisten/backups/` (mode 700) |
| Secrets | `db.env`, `api.env` in the deploy dir (mode 600, gitignored). **Never print, cat, or commit them.** Use `./gen-secrets.sh` / `./rotate-db-password.sh`. |
| Funnel socket dir | `/mnt/SolidStateApps/tailscale/relisten-edge/` (owned by uid 101; holds only `edge.sock`) |

SSH: `truenas_admin@192.168.1.166` (key auth). Docker needs `sudo -n`.
The NAS login shell is zsh; pipe scripts through `bash -s`.

## 2. Architecture & isolation (do not break)

```
Internet ─Funnel :10000─> unix:edge.sock ─> edge (nginx, uid 101) ──app net──> web (Next.js) ──api net──> api (.NET) ──data net──> db, redis
                                              └─ /audio/  (read-only bind of AUDIO_DIR only)
```

- Every Docker network is `internal: true` + `inhibit_ipv4` + no IPv6. Containers
  can reach each other only; **no internet, no LAN, no NAS host (UI/SMB/SSH)**.
  Verified with probes. Don't add `ports:`, don't add a non-internal network to
  `edge`/`web`/`api`/`db`/`redis`, don't mount any other host path.
- `edge` only serves `/audio/*` (static, byte ranges) and proxies **GET/HEAD**
  to `web`. Everything else → 405. The API (Swagger, Hangfire dashboard,
  admin/import endpoints) is **not routed publicly at all**; only `web` talks
  to it server-side.
- All services: `cap_drop: ALL`, `no-new-privileges`, read-only root FS
  (except `db`), memory limits. Hangfire background server is off.
- Play scrobbling to api.relisten.net and OpenTelemetry export to upstream's
  collector are disabled in the web fork (privacy + no egress).
- Adminer and the importer are `--profile tools` only (not running normally).
  Adminer binds `127.0.0.1:18080` only.

Funnel config (inside `ix-tailscale-tailscale-1`): port 443 = File Browser,
8443 = Family Gifts, **10000 = relisten**. Never touch 443/8443.
Backup of the serve config before relisten was added:
`/mnt/SolidStateApps/relisten/backups/tailscale-serve-before-relisten.json`.
Remove relisten's route only with `tailscale funnel --https=10000 off`.

## 3. Making code changes and deploying

1. Work on a branch in the relevant repo (currently all three use
   `blahsum/security-hardening-2026-11`). Don't push to GitHub unless Zach asks.
2. Check deps before deploying:
   - web: `pnpm install && pnpm audit --prod && pnpm run build`
   - api: `dotnet list RelistenApi/RelistenApi.csproj package --vulnerable --include-transitive && dotnet build -c Release`
3. Get the commits onto the NAS checkout (git bundle via scp works without
   GitHub), then on the NAS:
   ```sh
   cd /mnt/SolidStateApps/relisten/blahsum-relisten-deploy
   sudo -n docker compose build --pull web api      # only what changed
   sudo -n docker compose up -d                       # recreates changed services
   sudo -n docker compose ps; sudo -n docker compose logs --tail 50 web api edge
   ```
4. Verify from outside the LAN: `curl -I https://truenas-scale.tail5a1866.ts.net:10000/`,
   load a show page, and a range request on an `/audio/...` URL (expect 206).
   Note some networks block port 10000 outbound.
5. Before any DB-touching change: `sudo -n docker compose exec -T db pg_dump -U relisten -d relisten_db -Fc > /mnt/SolidStateApps/relisten/backups/relisten_db-$(date +%Y%m%d-%H%M).dump`

Do **not** restart, update, or stop any non-relisten container (filebrowser,
family-gifts, radio, homebridge, cactpot, tailscale, cloudflared) and don't
change TrueNAS settings.

## 4. Database model (upstream Relisten schema, unchanged)

```
artists 1─* years 1─* shows 1─* sources 1─* source_sets 1─* source_tracks
                    shows *─1 venues, *─1 tours, *─1 eras
```

| Table | Key fields | Notes |
| --- | --- | --- |
| `artists` | `id`, `name`, `slug` (URL), `sort_name`, `uuid` | One row: BLAHSUM (`/blahsum`). Changing `slug` breaks links. |
| `years` | `artist_id`, `year` (text), counts | Unique `(artist_id, year)`. |
| `shows` | `artist_id`, `year_id`, `venue_id`, `tour_id`, `date` (date), `display_date` (text `YYYY-MM-DD`), `uuid` | A night. URL is `/blahsum/YYYY/MM/DD`. `date` and `display_date` **must match**; unique `(artist_id, display_date)`. |
| `sources` | `show_id`, `artist_id`, `upstream_identifier` (= file path relative to `AUDIO_DIR`), `display_date`, `duration`, `description`, `taper_notes`, `venue_id`, `uuid` | A recording of a show. Importer key: `(artist_id, upstream_identifier)`. |
| `source_sets` | `source_id`, `index` (0-based), `name` (“Set 1”, “Encore”), `is_encore` | Unique `(source_id, index)`. |
| `source_tracks` | `source_id`, `source_set_id`, `artist_id`, `track_position` (1-based), `title`, `slug` (lowercase-dashes), `duration` (int seconds), `mp3_url`, `flac_url`, `uuid` | What the player plays. `mp3_url` is a full public URL; `(artist_id, mp3_url)` is the importer key. |
| `venues` | `artist_id`, `name`, `location`, `slug`, `latitude/longitude` | Optional. |
| `show_source_information` | per-show aggregates (counts, max dates) | Rebuilt by the importer; re-run importer after big edits. |

Don't edit `id`, `uuid`, or `versioninfo`. FK note: deleting a `source`
**sets** `source_tracks.source_id` to NULL (does not cascade) — delete its
tracks explicitly.

Caching: the web app caches API responses ~5 min and the API caches in
Redis. After edits, either wait ~5 min or run
`sudo -n docker compose restart redis api web` (relisten containers only).

## 5. Common content edits

Open a SQL shell (no password needed inside the container):
```sh
sudo -n docker compose exec db psql -U relisten -d relisten_db
```
or use Adminer (section 7). Wrap edits in `BEGIN; ... COMMIT;`.

**Fix a show's date** (e.g. 2025-04-25 → 2025-04-24):
```sql
BEGIN;
UPDATE shows   SET date='2025-04-24', display_date='2025-04-24', updated_at=now()
 WHERE artist_id=(SELECT id FROM artists WHERE slug='blahsum') AND display_date='2025-04-25';
UPDATE sources SET display_date='2025-04-24', updated_at=now() WHERE display_date='2025-04-25';
-- if the year changed, also point shows.year_id at the right years row (insert it if missing)
COMMIT;
```

**Rename a show/track title, set notes:**
```sql
UPDATE source_tracks SET title='Frost St Gallery (Set 1)', slug='frost-st-gallery-set-1', updated_at=now() WHERE id=…;
UPDATE sources SET description='…', taper_notes='…' WHERE id=…;
```
⚠ For a whole-file (not split) show, re-running the importer resets the track
title to the one parsed from the filename. Either rename the file (see README
gotcha) or split the show into tracks (below), which the importer then leaves
alone.

**Add new audio:** drop the mp3 into `AUDIO_DIR` named like
`BLAHSUM live in <City>[, <ST>] <M-D-YY> <Venue>.mp3`, then
`sudo -n docker compose --profile tools run --rm importer import`.

## 6. Marking individual songs inside a long show recording

Today every show is one long mp3 = one track. Two ways to split it:

### A. Works today: physical split files + SQL (recommended now)

1. Write a cue list (start times). Cut without re-encoding:
   ```sh
   SRC="/mnt/BlahNas/Blahsum/shared/radioTracksStorage/BLAHSUM live in Brooklyn NY 4-26-25 The Gutter.mp3"
   OUT="/mnt/BlahNas/Blahsum/shared/radioTracksStorage/_tracks/2025-04-26"
   mkdir -p "$OUT"
   # song 1: 0:00–3:00, song 2: 3:00–12:00, ... (last one: omit -to)
   ffmpeg -n -i "$SRC" -ss 0   -to 180 -c copy "$OUT/01-intro.mp3"
   ffmpeg -n -i "$SRC" -ss 180 -to 720 -c copy "$OUT/02-crystal-eel.mp3"
   ```
   (No ffmpeg on the NAS host: run it in a throwaway container with that one
   folder mounted, e.g. the importer image which has ffmpeg:
   `sudo -n docker run --rm --network none -v "$AUDIO_DIR:/a" --entrypoint ffmpeg blahsum-relisten-deploy-importer …`.
   Writing into the audio share is a change to Zach's files — ask first.)
   Files under any `_`-prefixed folder are **ignored by the importer**, so
   they won't become fake shows.
2. Replace the show's single track with per-song rows (same source & set):
   ```sql
   BEGIN;
   -- find the source
   SELECT so.id AS source_id, ss.id AS set_id FROM sources so JOIN source_sets ss ON ss.source_id=so.id
    WHERE so.display_date='2025-04-26';
   DELETE FROM source_tracks WHERE source_id = :source_id;   -- the whole-file row
   INSERT INTO source_tracks (artist_id, source_id, source_set_id, track_position, title, slug, mp3_url, duration, uuid, updated_at)
   VALUES
    ((SELECT id FROM artists WHERE slug='blahsum'), :source_id, :set_id, 1, 'Intro',       'intro',       'https://truenas-scale.tail5a1866.ts.net:10000/audio/_tracks/2025-04-26/01-intro.mp3',       180, gen_random_uuid(), now()),
    ((SELECT id FROM artists WHERE slug='blahsum'), :source_id, :set_id, 2, 'Crystal Eel', 'crystal-eel', 'https://truenas-scale.tail5a1866.ts.net:10000/audio/_tracks/2025-04-26/02-crystal-eel.mp3', 540, gen_random_uuid(), now());
   COMMIT;
   ```
   URL-encode spaces etc. in `mp3_url` (`%20`). `duration` is seconds.
   For an encore, add a `source_sets` row (`index`=1, `name`='Encore',
   `is_encore`=true) and point those tracks' `source_set_id` at it.
3. The importer sees the source has non-whole-file tracks and leaves them
   alone on later runs (it still updates show/source metadata).

### B. Not built yet: virtual splits (`?t=start,end`)

`docs/track-splits.md` designs splitting **without** cutting files:
several `source_tracks` rows share one file with `mp3_url=...mp3?t=180,720`.
The DB/API/edge already accept such URLs, but the **web player does not
implement `?t=` yet** — it would play the whole file for every row. Missing
work: player changes in `relisten-web/src/lib/player.tsx` (parse bounds, seek,
stop at end, gapless within one file, scrubber math) and an importer/cue-sheet
input (e.g. a `<file>.cue` or `.tracks.txt` sidecar → rows). Until then use A.

### Missing today
- No web admin UI for editing; edits are SQL/Adminer only.
- No upload pipeline; new audio goes in via the SMB share + importer.
- No cue-sheet importer.

## 7. Adminer (GUI), on demand only

```sh
sudo -n docker compose --profile tools up -d adminer         # on the NAS
ssh -L 18080:127.0.0.1:18080 truenas_admin@192.168.1.166      # from a PC
```
Open `http://localhost:18080` → PostgreSQL, server `db`, user `relisten`,
DB `relisten_db`, password = `POSTGRES_PASSWORD` from `db.env` (read it on the
NAS yourself; never paste it into chat/logs). Stop it after:
`sudo -n docker compose stop adminer`.

## 8. Security checklist for any change
- [ ] No new `ports:`; no new host mounts; networks stay internal.
- [ ] `edge` still allows only GET/HEAD and only `/` + `/audio/`.
- [ ] `pnpm audit --prod` and `dotnet list package --vulnerable` clean.
- [ ] Images pinned to patched tags; rebuild with `--pull`.
- [ ] No secrets in git, logs, or chat.
- [ ] Verified public URL, a 206 audio range request, and that
      `/api-docs`, `/api/v3/artists` return 404 and `POST /` returns 405.
