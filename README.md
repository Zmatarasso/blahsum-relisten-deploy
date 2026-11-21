# blahsum-relisten

A self-hosted fork of [Relisten](https://relisten.net) for streaming a
custom catalog of live recordings. Audio files are served from local
storage (eventually object storage / archive.org); the database, API,
and web UI are derived from the upstream Relisten codebase under
AGPL-3.0.

Currently: **blahsum.live** — every Blahsum show, ever.

## Repos

| Repo | Purpose | Branch |
| --- | --- | --- |
| [`Zmatarasso/blahsum-relisten-deploy`](https://github.com/Zmatarasso/blahsum-relisten-deploy) | This repo. docker-compose, importer, nginx, plan. | `main` |
| [`Zmatarasso/blahsum-relisten-api`](https://github.com/Zmatarasso/blahsum-relisten-api) | Backend (.NET) — fork of RelistenNet/RelistenApi | `blahsum/empty-db-bootstrap` |
| [`Zmatarasso/blahsum-relisten`](https://github.com/Zmatarasso/blahsum-relisten) | Frontend (Next.js) — fork of RelistenNet/relisten-web | `blahsum/dockerfile-zfs-fix` |

`bootstrap.sh` clones both forks at the right branches.

## Stack (hardened Nov 2026)

Public URL: **https://truenas-scale.tail5a1866.ts.net:10000/** (Tailscale Funnel).

| Service | Image / source | Networks | Notes |
| --- | --- | --- | --- |
| `edge` | `nginxinc/nginx-unprivileged:1.31.7-alpine` | app | Only public entry. Listens on a unix socket Funnel proxies to. Serves `/audio/` (read-only bind of `AUDIO_DIR`) and proxies GET/HEAD to `web`. |
| `web` | `./relisten-web` (Next.js 16.4) | app, api | Server-side renders from the API. |
| `api` | `./RelistenApi` (.NET 10) | api, data | Never routed publicly (no Swagger/Hangfire/admin exposure). |
| `db` | `timescale/timescaledb:2.32.1-pg17` | data | No host port. |
| `redis` | `redis:7.4.12-alpine` | data | Cache only, no persistence. |
| `importer` | `./importer` | data | `--profile tools`, run on demand. |
| `adminer` | `adminer:5.5.1` | data, admin | `--profile tools`, loopback-only port 18080. |

All networks are Docker `internal` with `inhibit_ipv4` and no IPv6: containers
can talk to each other but cannot reach the internet, the LAN, or the NAS host
itself (UI/SMB/SSH). No container publishes a LAN port. All run with
`cap_drop: ALL`, `no-new-privileges`, read-only root filesystems (except db).
PgBouncer was removed (it was bypassed anyway).

## Bringing it up

```sh
./bootstrap.sh                 # clones the API + web forks
cp .env.example .env           # host paths
./gen-secrets.sh               # creates db.env / api.env (random, mode 600)
mkdir -p "$EDGE_SOCKET_DIR" && chown 101:0 "$EDGE_SOCKET_DIR" && chmod 700 "$EDGE_SOCKET_DIR"
docker compose up -d --build
./rotate-db-password.sh        # only for an existing DB created with an old password
docker compose --profile tools run --rm importer import
# public route (in the tailscale container):
tailscale funnel --bg --https=10000 unix:/var/lib/tailscale/relisten-edge/edge.sock
```

## Editing metadata

The catalog is just rows in Postgres.

### Adminer (on demand, loopback only)

```sh
docker compose --profile tools up -d adminer       # on the NAS
ssh -L 18080:127.0.0.1:18080 truenas_admin@truenas  # from your PC
```

Open `http://localhost:18080`: System PostgreSQL, Server `db`, user
`relisten`, database `relisten_db`, password = `POSTGRES_PASSWORD` in `db.env`.
Stop it when done: `docker compose stop adminer`.

### Tables you'll typically want to edit

| Want to change | Table | Field(s) |
| --- | --- | --- |
| Show date | `shows` | `date` (date) **and** `display_date` (text, `YYYY-MM-DD`). They have to match — `display_date` is the public unique key. |
| Track title | `source_tracks` | `title`, `slug` (slug must be lowercase-with-dashes) |
| Track number / order | `source_tracks` | `track_position` (1-based) |
| Set name | `source_sets` | `name`, `index`, `is_encore` |
| Artist display name | `artists` | `name` (visible), `slug` (URL — changing breaks bookmarks), `sort_name` (alphabetic order) |
| Year nav label | `years` | `year` (text) |
| What audio URL the player hits | `source_tracks` | `mp3_url` or `flac_url` |
| Hide a show without deleting | drop the source's `show_id` to NULL — show row stays but un-listed |

After bigger edits, re-run the importer's aggregation pass to refresh
the join table the API uses for show counts:

```sh
docker compose --profile tools run --rm importer import
```

(The importer is idempotent — it re-upserts existing files and
rebuilds `show_source_information` at the end.)

### What NOT to edit

- `uuid` columns — these are referenced by clients; if you change one
  you'll break in-flight bookmarks and the mobile app's caches. Treat
  them as immutable.
- `id` columns — primary keys. Don't.
- `versioninfo` — that's the migration history. Touching it will
  confuse the migrator.

### Renaming audio files: the gotcha

The importer keys sources by `(artist_id, upstream_identifier)`, where
`upstream_identifier` is the file's relative path under `AUDIO_DIR`.
**If you rename a file, the importer treats the new path as a new
source.** The old row stays around as orphaned metadata.

Two ways to handle this:

1. **Edit metadata in Adminer instead of renaming the file.** Change
   `source_tracks.title`, `shows.display_date`, etc. — leave the file
   alone. Recommended.
2. **Rename, then clean up.** After rename + reimport, delete the
   orphaned source from `sources` (cascades to its set + track via FK
   ON DELETE).

Eventually the importer will detect renames by content hash; not built
yet.

## Adding new audio

1. Drop the file into the host path that's bind-mounted as
   `${AUDIO_DIR}` (e.g. `/mnt/BlahNas/Blahsum/shared/radioTracksStorage/`
   on the production NAS).
2. `docker compose --profile tools run --rm importer import`
3. Refresh the web app.

Filename heuristics today (subject to drift; see
[`importer/blahsum_importer/parser.py`](importer/blahsum_importer/parser.py)):

```
"BLAHSUM live in <City>[, <ST>] <date> <venue>.mp3"
   → live show, parses date out
"BLAHSUM - <Title>.mp3"
   → studio track, date = file mtime
```

## Project plan

[`PLAN.md`](PLAN.md) is the implementation plan and decision log. Read
that for context on phases, why TrueNAS Community Edition was chosen,
the path to paid hosting, etc.

## License

AGPL-3.0, inherited from upstream Relisten. All forks are public:
- https://github.com/Zmatarasso/blahsum-relisten-deploy
- https://github.com/Zmatarasso/blahsum-relisten-api
- https://github.com/Zmatarasso/blahsum-relisten

Upstream:
- https://github.com/RelistenNet/RelistenApi
- https://github.com/RelistenNet/relisten-web
