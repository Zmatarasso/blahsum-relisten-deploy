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

## Stack

| Service | Image / source | Port | Notes |
| --- | --- | --- | --- |
| `db` | `timescale/timescaledb:2.19.3-pg17` | 15432 | Postgres + TimescaleDB |
| `redis` | `redis:7.4` | (internal) | API cache |
| `pgbouncer` | `edoburu/pgbouncer:v1.24.0-p1` | (internal) | Currently bypassed; see compose |
| `api` | `./RelistenApi/Dockerfile` | 3823 | .NET 10, REST + Hangfire |
| `web` | `./relisten-web/Dockerfile` | `${WEB_PORT:-3000}` | Next.js 16 RSC |
| `audio` | `nginx:alpine` | 8080 | Static byte-range audio |
| `adminer` | `adminer` | 18080 | DB GUI for metadata edits |
| `importer` | `./importer/Dockerfile` | (run on demand) | `--profile tools` only |

## Bringing it up on a fresh host

```sh
git clone https://github.com/Zmatarasso/blahsum-relisten-deploy.git
cd blahsum-relisten-deploy
./bootstrap.sh                   # clones the API + web forks
cp .env.example .env             # edit paths if needed
docker compose up -d
docker compose --profile tools run --rm importer import
```

That's it. Visit `http://<host>:${WEB_PORT:-3000}/`.

For TrueNAS specifics see [`PLAN.md`](PLAN.md). Empty DBs are explicitly
supported; you don't need the upstream Relisten DB seed.

## Editing metadata

The catalog is just rows in Postgres. Edit them however you like.

### Adminer (web GUI) — quickest

`http://<host>:18080`

| Field | Value |
| --- | --- |
| System | PostgreSQL |
| Server | `db` |
| Username | `relisten` |
| Password | `local_dev_password` |
| Database | `relisten_db` |

Click `Select data` on a table, double-click a cell to edit, **Save**.
Changes are live immediately — refresh the web app to see them.

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
