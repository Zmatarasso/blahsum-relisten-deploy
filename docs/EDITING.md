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
| Cut song files | `/mnt/SolidStateApps/relisten/tracks/` (owned by uid 65534; written only by importer-watch) |
| Import status | `/mnt/SolidStateApps/relisten/importer-status/import-status.txt` (public at `/import-status.txt`) |
| Funnel socket dir | `/mnt/SolidStateApps/tailscale/relisten-edge/` (owned by uid 101; holds only `edge.sock`) |

SSH: `truenas_admin@192.168.1.166` (key auth). Docker needs `sudo -n`.
The NAS login shell is zsh; pipe scripts through `bash -s`.

## 2. Architecture & isolation (do not break)

```
Internet ─Funnel :10000─> unix:edge.sock ─> edge (nginx, uid 101) ──app net──> web (Next.js) ──api net──> api (.NET) ──data net──> db, redis
                                              ├─ /audio/   (read-only bind of AUDIO_DIR only)
                                              ├─ /tracks/  (read-only: songs cut by importer-watch)
                                              └─ /import-status.txt
importer-watch ──data net──> db, redis   (audio read-only; writes only tracks/ and importer-status/)
```

- Every Docker network is `internal: true` + `inhibit_ipv4` + no IPv6. Containers
  can reach each other only; **no internet, no LAN, no NAS host (UI/SMB/SSH)**.
  Verified with probes. Don't add `ports:`, don't add a non-internal network to
  `edge`/`web`/`api`/`db`/`redis`, don't mount any other host path.
- `edge` and the importers read the audio share through the NAS `builtin_users`
  group (gid 545, via its ACL entry), not root. `edge` serves only `/audio/*`,
  `/tracks/*`, `/import-status.txt` (static) and proxies **GET/HEAD**
  to `web`. Everything else → 405. The API (Swagger, Hangfire dashboard,
  admin/import endpoints) is **not routed publicly at all**; only `web` talks
  to it server-side.
- All services: `cap_drop: ALL`, `no-new-privileges`, read-only root FS
  (except `db`), memory limits. Hangfire background server is off.
- Play scrobbling to api.relisten.net and OpenTelemetry export to upstream's
  collector are disabled in the web fork (privacy + no egress).
- `importer-watch` runs all the time on the internal `data` network only, as
  uid 65534 with a read-only root filesystem. Adminer and the one-off
  `importer` are `--profile tools` only.
  Adminer binds `127.0.0.1:18080` only.

Funnel config (inside `ix-tailscale-tailscale-1`): port 443 = File Browser,
8443 = Family Gifts, **10000 = relisten**. Never touch 443/8443.
Backup of the serve config before relisten was added:
`/mnt/SolidStateApps/relisten/backups/tailscale-serve-before-relisten.json`.
Remove relisten's route only with `tailscale funnel --https=10000 off`.

## 3. Making code changes and deploying

1. Work on a feature branch in the relevant repo and open a PR to `main`
   (`main` is what is live in all three repos; see README.md).
2. Check deps before deploying:
   - web: `pnpm install && pnpm audit --prod && pnpm run build`
   - api: `dotnet list RelistenApi/RelistenApi.csproj package --vulnerable --include-transitive && dotnet build -c Release`
3. After merging, pull `main` on the NAS checkout (`git pull --ff-only` in the
   repo(s) that changed), then:
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

## 3a. Where to put shows (adding new recordings)

**Folder:** `\\truenas\Blahsum\shared\radioTracksStorage`
(NAS: `/mnt/BlahNas/Blahsum/shared/radioTracksStorage`). Anyone in the NAS
`builtin_users` group (zach, gabe, nate, rube) can write there over SMB.
Subfolders are fine. Folders starting with `_` or `.` are ignored.

**It's automatic.** The `importer-watch` container checks the folder every
5 minutes. When a show file or `.tracks.txt` file is added, changed or
removed, it re-imports. New or changed shows appear on the site about
5–10 minutes later. Check the result at
**https://truenas-scale.tail5a1866.ts.net:10000/import-status.txt**. It shows
the last check time, the shows it found, and any problems, such as a
`.tracks.txt` line it didn't understand.

**File name = default info.** One file = one show recording:

```
BLAHSUM live in <City>[ <ST>] <M-D-YY> <Venue or notes>.mp3
e.g.  BLAHSUM live in Brooklyn NY 4-26-25 The Gutter.mp3
```

- Date: `M-D-YY`, `MM-DD-YY` or `M-D-YYYY` with dashes (2-digit years under
  70 → 20YY). Text after the date becomes the title. The name must contain
  `live`. For studio songs, use `BLAHSUM - <Title>.mp3` (the date comes from
  the file's modified time).
- Formats: prefer `.mp3`. `.m4a`, `.ogg`, `.flac` and `.wav` are picked up
  too, but browsers stream mp3 best.
- Two files with the same date become one show with two alternate
  recordings.
- **Removing a file removes the show** from the site at the next import.
  There's a safety stop if more than half the files seem to vanish at once.
  Renaming a file is the same as removing it and adding a new one.

A `.tracks.txt` file next to the show (§6) can override the date, title and
venue, and add song markers. Prefer that over renaming files.

Manual run (optional, on the NAS):
`sudo -n docker compose --profile tools run --rm importer import`.

## 3b. For Gabe (quick version)

1. Copy the show into `\\truenas\Blahsum\shared\radioTracksStorage`, named like
   `BLAHSUM live in Brooklyn NY 4-26-25 The Gutter.mp3`.
2. Optional: next to it, make a text file with the **same name** but ending in
   `.tracks.txt` instead of `.mp3`
   (`BLAHSUM live in Brooklyn NY 4-26-25 The Gutter.tracks.txt`):
   ```
   date: 2025-04-26
   title: The Gutter
   venue: The Gutter, Brooklyn NY

   0:00     Intro
   3:00     Crystal Eel
   12:05    Sundown
   1:58:30  Last Song *
   ```
   - Each song line is a start time (minutes:seconds, or hours:minutes:seconds)
     then the song name. Times count from the start of the recording and
     must go up.
   - Put `*` at the end of a song name to put it in the Encore.
   - All lines are optional. Leave out the song lines to keep one long track.
     Lines starting with `#` are ignored, and so is anything after a `#`, so
     don't use `#` in song names.
3. Wait about 10 minutes, then check
   https://truenas-scale.tail5a1866.ts.net:10000/import-status.txt. Your show
   should be listed (e.g. `[4 songs]`), with problems shown underneath.
4. To fix something, edit the `.tracks.txt` file and save. The site updates
   on the next check. To go back to one long track, delete the `.tracks.txt`.

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
⚠ The importer re-applies file-name / `.tracks.txt` values on every run, so
SQL edits to dates and titles of imported shows get overwritten. **Use a
`.tracks.txt` sidecar for dates, titles, venues and songs** (§3b and §6).
Use SQL only for things the sidecar doesn't cover, like `taper_notes`.

**Add new audio:** see §3a. Drop it in the folder and it imports automatically.

## 6. Track markers (songs inside a long show recording)

**Use the `.tracks.txt` sidecar** (format in §3b). On each import,
`importer-watch`:

1. Reads `<show file>.tracks.txt`, which must have the same name as the audio
   file but end in `.tracks.txt`.
2. Applies the `date:`, `title:` and `venue:` overrides. They stick on every
   re-import. A date change moves the recording to the new date, and the
   empty old show is removed.
3. If there are song lines, it cuts one file per song with
   `ffmpeg -c copy` (no re-encoding, so quality is unchanged) into
   `/mnt/SolidStateApps/relisten/tracks/<show-key>/NN-<song>.mp3`. That
   folder belongs to relisten and is served read-only at `/tracks/…`. The
   original mp3 is only ever read.
4. Replaces the show's tracks with one row per song: `Set 1` holds the songs,
   an `Encore` set holds the songs marked `*`, and durations come from the
   cut files. Track UUIDs are kept by position.
5. Re-cuts only when the audio file or the song list changes. A signature
   file in each cut folder tracks this. Removing the sidecar, or all its song
   lines, deletes the cut files and restores the single full-length track.

Every problem is listed in `/import-status.txt` and the import keeps going.
Examples: an unreadable line, times that don't go up, or a last song that
starts after the recording ends. In the case of a bad song list, the show
keeps one long track.

**Timestamp links** (`/blahsum/2025/04/26/the-gutter?t=12m30s`) also still
work. They come from upstream Relisten and jump the player to a time.

**Hand-made tracks in SQL** (advanced): sources whose tracks point somewhere
other than the whole file or `/tracks/`, e.g. files under `_tracks/` that
you wired up yourself, are left alone by the importer as long as there's no
sidecar with song lines. If a sidecar has song lines, it wins.

**Not built:** song markers *inside* one file with the player seeking
(`?t=start,end`, see `docs/track-splits.md`). Cut files made that
unnecessary.

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
