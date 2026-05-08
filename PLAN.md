# blahsum-relisten — Implementation Plan

Living document. Update as we go.

## Goal

Self-host a Relisten-derived live-music streaming site with our own catalog, hosted from a home TrueNAS server first, then promoted to paid hosting once we want public traffic. Audio files served from local storage initially, migrated onto archive.org later.

## Current state

- TrueNAS Community Edition 25.04 (rebranded SCALE) — native Docker available.
- Three GitHub repos forked/created under `Zmatarasso`:
  - `blahsum-relisten-api` — fork of `RelistenNet/RelistenApi`. Branch `blahsum/empty-db-bootstrap` patches `Startup.cs` so migrations work against an empty DB (runs migration 1, baselines past 2 to skip Relisten's hardcoded artist roster, then runs 3–10).
  - `blahsum-relisten` — fork of `RelistenNet/relisten-web`, untouched.
  - `blahsum-relisten-deploy` — orchestration: `docker-compose.yml`, `bootstrap.sh`, `nginx-audio.conf`, `README.md`, this plan.
- No code running yet. No SSH session into the NAS yet.

## Architecture

```
┌──────────────────────────────── TrueNAS host ─────────────────────────────────┐
│                                                                                │
│   ┌────────┐    ┌──────────┐    ┌─────────────┐    ┌─────────────────────┐    │
│   │ Caddy  │──▶│ relisten- │──▶│ RelistenApi │──▶│ pgbouncer ─▶ Postgres│    │
│   │ (TLS)  │    │   web     │    │  (.NET 10)  │    │  (TimescaleDB)     │    │
│   └────┬───┘    └──────────┘    └──────┬──────┘    └─────────────────────┘    │
│        │                               │                                       │
│        │           ┌──────────┐        ▼                                       │
│        └─────────▶│  nginx   │   ┌─────────┐                                  │
│            /audio │  audio   │   │  Redis  │                                  │
│                    └────┬─────┘   └─────────┘                                  │
│                         │                                                      │
│                         ▼                                                      │
│                   /mnt/<pool>/audio/                                           │
│                   (bind-mounted dataset)                                       │
└────────────────────────────────────────────────────────────────────────────────┘
```

- **Caddy** terminates TLS (Let's Encrypt) and routes:
  - `/` → web (Next.js)
  - `/api/*` → RelistenApi
  - `/audio/*` → nginx-audio
- **relisten-web** (Next.js, RSC) — UI, talks to API.
- **RelistenApi** (.NET 10) — catalog + playback metadata. Runs migrations on startup. Hangfire for background jobs.
- **pgbouncer** — connection pool (the API expects this).
- **Postgres / TimescaleDB** — concert/track metadata. TimescaleDB is required because the upstream schema uses hypertables for play counts.
- **Redis** — API caching.
- **nginx-audio** — serves byte-range audio from the audio dataset with CORS + long cache headers.
- **audio dataset** — TrueNAS dataset bind-mounted into the nginx container; same dataset accessible over SMB so we can drop files in from a desktop.

## Phases

### Phase 0 — Plumbing in place ✅
- Forks created, deploy repo published, empty-DB patch committed, `bootstrap.sh` written.

### Phase 1 — Boot the stack on TrueNAS
1. Enable SSH on TrueNAS (Services), add an SSH key for the admin user.
2. Create datasets:
   - `/mnt/<pool>/apps/blahsum-relisten/` — clone destination, holds the deploy repo + child clones + Postgres/Redis volumes.
   - `/mnt/<pool>/audio/` — audio library, bind-mounted read-only into the nginx container.
3. `git clone` the deploy repo into the apps dataset; run `./bootstrap.sh` to clone the two service repos at the right branches.
4. `docker compose up -d db redis pgbouncer adminer` — bring up storage layer.
5. `docker compose up -d --build api` — build and start the API. Watch `docker compose logs -f api` for the migration run. Success = `/api-docs` responds.
6. `docker compose up -d audio` — start the static audio server. Drop a test mp3 into the audio dataset, confirm `curl http://nas:8080/audio/test.mp3` streams it with `Accept-Ranges: bytes`.

**Exit criteria for Phase 1:** API + DB + audio server running, empty catalog, `/api-docs` reachable from a browser on the LAN.

### Phase 2 — Audio importer
The API has importers for archive.org/Phish.in/etc. None of them ingest local files. We write a new one.

- **Convention:**
  ```
  /mnt/<pool>/audio/<artist-slug>/YYYY-MM-DD <venue>/NN - Track Name.flac
  ```
- **Where it lives:** new tool in `RelistenApi/tools/` (a small `dotnet run` console app that reuses the existing `DbService` / model classes), OR a standalone Python script that talks to Postgres directly. Decision pending; the .NET path lets us reuse the model layer and not duplicate UUID/slug logic.
- **What it does, per scan:**
  1. Walk the audio root.
  2. Upsert `artists` (one row per top-level dir).
  3. For each show dir: upsert `shows`, create one `sources` row (us as the upstream), create `sources_sets` + `sources_tracks` from the file list.
  4. Set `mp3_url` / `flac_url` to `https://<public-host>/audio/<relative path>`.
  5. Read length + bitrate via `ffprobe` to populate `duration` / `mp3_bitrate`.
- **Re-runnable:** keyed off file path + mtime; new files added, removed files marked deleted, unchanged files skipped.
- **Trigger:** manual at first (`docker compose run --rm importer`), automated later via a Hangfire recurring job once we have a working manual run.

**Exit criteria:** dropping a folder of files into `/mnt/<pool>/audio/...` and running the importer makes them appear in the API's `/api/v3/artists/...` responses.

### Phase 3 — Web frontend wired up
1. Read `relisten-web/.env*` and `next.config.js` to find where the API URL is configured.
2. Add `web` service to `docker-compose.yml`, build from `./relisten-web`, set `API_URL` (or whatever the env var is) to `http://api:3823`.
3. Bring it up. Hit `http://nas:3000`, see our (empty or seeded) catalog.

**Exit criteria:** browser at `http://nas:3000` shows a working Relisten UI populated from our DB.

### Phase 4 — Rebrand
Required by AGPL-3.0 (we're hosting modified Relisten source publicly).
- Search-replace branding in `relisten-web/src`: header logo, page title, OG metadata, footer, favicon, theme colors if desired.
- Update `package.json` name, README to point at our repo.
- Add an "About" page noting the AGPL upstream.
- Open-source confirmation: deploy repo + both forks are already public at `Zmatarasso/blahsum-relisten*`. AGPL satisfied as long as the public site links to the source.

**Exit criteria:** the site shows blahsum branding, references `Zmatarasso/blahsum-relisten` as source.

### Phase 5 — TLS + public exposure
1. Domain (decision pending — `blahsum.com`? subdomain of an existing one?).
2. DNS to home IP (or Cloudflare Tunnel, no port forwarding needed).
3. Add a `caddy` service to compose with this routing:
   - `domain/` → `web:3000`
   - `domain/api/*` → `api:3823`
   - `domain/audio/*` → `audio:80`
4. Let's Encrypt automatic via Caddy. Cloudflare proxy off (audio = streaming = bandwidth = Cloudflare ToS friction; safer to direct).

**Exit criteria:** `https://<domain>/` works from the public internet with a valid cert.

### Phase 6 — Operational concerns
- **Backups.** Postgres `pg_dump` nightly into a TrueNAS dataset, retained 30 days. Audio dataset already on a redundant pool; snapshots via TrueNAS replication.
- **Updates.** Renovate/dependabot on the forks. We pull upstream changes manually until divergence is too painful, then we re-fork.
- **Monitoring.** `docker compose ps` cron + a healthcheck endpoint on the API. Light: a Healthchecks.io ping is enough at home-server scale.
- **Logging.** Compose default. Promote to Loki only if we get noisy.

### Phase 7 — Promote to paid hosting
Trigger: home upload becomes the bottleneck OR uptime expectations grow.

- **App tier:** Fly.io (cheap, Docker-native, regional) or Hetzner CX22 (~€4/mo, more raw resources). Hetzner wins on price-per-CPU; Fly wins on zero-ops.
- **Postgres:** Neon free → paid. Or Fly managed PG. Migrate via `pg_dump | pg_restore`.
- **Redis:** Upstash free tier indefinitely at our scale.
- **Audio (the expensive bit):** Cloudflare R2 (zero egress fees) + a small worker for signed URLs if we want hot-link protection. **Avoid AWS S3** — egress on a streaming service will eat the budget. Importer rewritten to push uploads to R2 instead of bind-mounting, and to set `flac_url` to the R2 (or R2-fronted) URL.
- **Cutover:** new deploy on the cloud target, swap DNS once the cloud DB matches the NAS DB. NAS keeps running as a hot standby.

## Open decisions

- [ ] Importer language — .NET (reuses models) vs Python (faster to write, dupes some logic). Leaning .NET.
- [ ] Domain name.
- [ ] Whether to merge `blahsum/empty-db-bootstrap` into `master` on the API fork or keep it as a topic branch the deploy script pins to. Currently pinned via `bootstrap.sh`.
- [ ] Which TrueNAS pool / dataset names to use.
- [ ] When to write a Custom App YAML so this is visible in the TrueNAS Apps UI vs continuing to run `docker compose` directly via SSH.

## Risk register

- **AGPL compliance** — both forks are public; just need to link to source from the deployed site footer.
- **Schema drift from upstream** — the empty-DB patch is small and on a topic branch, so rebasing onto upstream stays cheap. The audio importer will be all-new code so doesn't conflict.
- **Streaming bandwidth on residential** — fine for personal/test use, will tip over at first sign of traffic. Phase 7 exists for this reason.
- **TrueNAS Apps UI ignorance of compose-up** — running `docker compose` directly works but bypasses the UI's update/restart machinery. We accept this for now; revisit if it bites.
- **TimescaleDB upgrade path** — the upstream image tag is pinned (`timescaledb:2.19.3-pg17`). Upgrades require following Timescale's PG/TS upgrade docs; not a Phase-1 concern.
