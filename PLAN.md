# blahsum-relisten — Implementation Plan

Living document. Update as we go.

## Goal

Self-host a Relisten-derived live-music streaming site with our own catalog, hosted from a home TrueNAS server first, then promoted to paid hosting once we want public traffic. Audio files served from local storage initially, migrated onto archive.org later.

## Portability as a hard constraint

The TrueNAS box is the **first** target, not the **only** one. Every Phase 1–6 decision must keep the stack movable to a different host (Hetzner, Fly.io, Railway, a friend's server, a different NAS) with at most a config change. Concretely:

- **No TrueNAS-specific deploy tooling.** No "Custom Apps" YAMLs, no SCALE chart definitions, no `ix-applications` integration. The single source of truth is `docker-compose.yml` plus environment files. If TrueNAS later wants a Custom App wrapper, that's an additional layer — it never replaces the compose file.
- **Bind-mounted paths are configurable.** Audio dataset, Postgres data, Redis data — all paths come from env vars / `.env`, with TrueNAS-style `/mnt/<pool>/...` paths only as defaults. Same compose file works on a Hetzner VPS where the audio lives at `/srv/audio/`.
- **No host-network assumptions.** Services talk to each other by Compose service name, not LAN IP. The web app references the API as `http://api:3823` internally and `https://<public-domain>/api` externally — never the NAS's LAN IP.
- **Storage is abstracted at the URL layer.** The API stores `mp3_url`/`flac_url` as full URLs. Phase 1 nginx serves from a local volume; Phase 7 swaps in R2/S3 URLs. The DB rows don't change shape, only the host portion of the URLs (and the importer's destination).
- **Secrets via `.env`, never baked into images.** `.env.example` checked in, real `.env` gitignored. Same image runs anywhere; environment supplies the secrets.
- **Stateless app containers.** API and web write nothing to the local filesystem that needs to persist. All state lives in Postgres, Redis, or the audio volume — each of which has a documented swap-in for managed equivalents (Neon/Supabase, Upstash, R2).

**Acceptance test for portability** (added at the end of Phase 1): a one-command rehearsal on a clean Linux VM (a temporary cloud instance or a local VirtualBox) — `git clone && ./bootstrap.sh && docker compose up` — that brings the stack up identically. If that test fails, we've drifted into NAS-specific territory and need to fix it before continuing.

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

### Phase 7 — Promote to paid / cloud hosting
Trigger: home upload becomes the bottleneck OR uptime expectations grow.

Three concrete deploy targets, ordered by likely fit. Pick one when the time comes; all three are reachable from the same compose file and same images.

#### Option A — Single VPS (Hetzner / DigitalOcean / Linode)
**Best fit if:** we want one box we control end-to-end, lowest monthly cost, willing to do a tiny bit of ops.

- Provision a Hetzner CX22 (€4.51/mo, 2 vCPU, 4 GB RAM, 40 GB disk) or DO $6 droplet.
- `git clone blahsum-relisten-deploy && ./bootstrap.sh && docker compose up -d` — same flow as TrueNAS.
- Audio either stays on local SSD (cheap, capped by disk size) or moves to R2 (Phase 7B below).
- Postgres + Redis run as containers same as locally; back up via `pg_dump` to S3-compatible storage nightly.
- Caddy already in compose handles TLS.

**Migration from NAS:** `pg_dump` from NAS → restore on VPS, `rsync` audio dataset → VPS, swap DNS. ~30 min of downtime, much less if we run them in parallel and cut over.

#### Option B — Managed services (Fly.io / Railway / Render)
**Best fit if:** we want zero-ops and are okay paying a bit more for it.

- **App containers (API + web):** Fly.io is the cleanest fit — accepts our existing Dockerfiles unchanged, regional deploy, scale-to-zero possible. Each service gets a `fly.toml`. Railway and Render are similar; pick on price/UX.
- **Postgres:** Neon (serverless PG, free tier with paid scale-up) or Supabase. Both speak vanilla PG; the only thing to verify is TimescaleDB extension support — Neon supports it, Supabase doesn't. **If Supabase, we'd need to remove TimescaleDB hypertables from the schema.** Track this if we go that route.
- **Redis:** Upstash (free tier covers us indefinitely at our scale).
- **Audio:** required to move off local disk — managed app tiers don't do persistent volumes well. See Option C.
- Compose file no longer orchestrates production but stays as the dev/local source of truth.

**Migration from NAS:** `pg_dump` → Neon, audio → R2, deploy each service to Fly. DNS cutover.

#### Option C — Audio on object storage (additive to A or B)
**Best fit if:** the audio library grows past what fits cheaply on a VPS, OR we go managed and need to.

- **Cloudflare R2** — zero egress fees, S3-compatible API. The single biggest cost lever for a streaming site. Audio = bandwidth, and S3 egress at ~$0.09/GB will dwarf everything else; R2 is $0.015/GB for storage and $0 for egress.
- **B2 + Cloudflare CDN** — alternative if we don't want R2's quirks; similar effective cost.
- **Avoid AWS S3 + CloudFront for audio** unless we have egress credits.
- **Importer change:** add an `S3_AUDIO_BUCKET` mode where the importer uploads files via S3 API and sets `flac_url` to the public R2 URL. The compose-side `nginx-audio` becomes optional / dev-only.
- **Hot-link protection (optional):** Cloudflare Worker that signs short-lived URLs in front of R2. Defer until we have a reason.

#### Cost ballpark (rough, monthly)
| Setup                                       | App     | DB       | Redis | Audio storage  | Total      |
| ------------------------------------------- | ------- | -------- | ----- | -------------- | ---------- |
| Hetzner CX22 + local audio                  | €4.50   | included | local | included       | **~€5**    |
| Hetzner CX22 + R2 (100 GB audio, 100 GB egress) | €4.50 | included | local | $1.50          | **~€6**    |
| Fly.io shared-1x + Neon free + Upstash free + R2 | $5–10  | $0–19    | $0    | $1.50 / 100 GB | **~$7–30** |

Numbers move once we know real audio size and listener traffic, but this gives the order of magnitude.

#### Cutover playbook
1. Stand the new target up in parallel with the NAS (different domain, e.g. `staging.<domain>`).
2. `pg_dump` NAS → restore on cloud DB; verify row counts, run a few sample API queries.
3. `rsync` (or `rclone` to R2) the audio library; verify a few URLs return bytes.
4. Run the importer in "URL rewrite" mode if the audio host changed, so DB rows match new URLs.
5. DNS cutover. NAS keeps running for a week as fallback.
6. Once stable: turn the NAS into a backup target — nightly `pg_dump` and `rclone` from cloud back to NAS.

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
- **TimescaleDB lock-in risk for managed PG.** Some managed PG providers (Supabase, RDS) don't support the TimescaleDB extension. If we ever pick one of those, we'd need to remove hypertable usage from the schema first. Phase 7 calls this out; flagging here so it doesn't get forgotten.

## Portability checklist (run at the end of every phase)

If any of these stop being true, fix it before moving on. This is what keeps Phase 7 cheap.

- [ ] Stack comes up with `git clone && ./bootstrap.sh && docker compose up` on a clean Linux box.
- [ ] No path in `docker-compose.yml` is hardcoded to `/mnt/<pool>/...`. All bind-mount sources resolve through env vars with sensible defaults.
- [ ] No service references another by IP — only by Compose service name internally, public domain externally.
- [ ] No secrets in committed files. `.env.example` lists every variable with a placeholder; real `.env` is gitignored.
- [ ] API and web containers are stateless — `docker compose down && up` (without removing volumes) loses no data.
- [ ] Database and Redis are reachable via standard URLs (`postgresql://...`, `redis://...`) so they can be swapped for managed equivalents by changing one env var.
- [ ] Audio URLs in DB rows are full URLs (not paths), so swapping the audio host = updating one env var + reimporting.
- [ ] Both forks (`blahsum-relisten-api`, `blahsum-relisten`) build in CI from a fresh clone with no external setup beyond Docker.
