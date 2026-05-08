# blahsum-relisten

A self-hosted fork of [Relisten](https://relisten.net) for streaming a custom catalog of live recordings. The audio files are hosted locally (eventually on archive.org); the database, API, and web UI are derived from the upstream Relisten codebase under AGPL-3.0.

## Repos

| Path             | Source                                  | Status |
| ---------------- | --------------------------------------- | ------ |
| `RelistenApi/`   | https://github.com/RelistenNet/RelistenApi (clone for now, fork later) | cloned |
| `relisten-web/`  | https://github.com/RelistenNet/relisten-web (will be forked as Zmatarasso/blahsum-relisten) | pending |

## Stack

- **Postgres + TimescaleDB** — concert/track metadata
- **Redis** — caching used by the API
- **pgbouncer** — connection pooling
- **RelistenApi** (.NET 10) — REST API on port 3823
- **relisten-web** (Next.js) — frontend on port 3000
- **nginx** — serves audio files from `./audio/` on port 8080
- **adminer** — DB GUI on port 18080

## First-time setup

### 1. Empty-DB workaround

Upstream `Startup.cs` calls `migrator.Baseline(2)` on an empty DB, which assumes the official Relisten DB seed (artists/sources tables pre-populated). For our empty DB we need to change that to `Baseline(0)` so all migrations run from scratch. This patch goes in our fork of `RelistenApi`:

```csharp
// RelistenApi/Startup.cs ~line 180
if (migrator.CurrentMigration == null || migrator.CurrentMigration.Version == 0)
{
    migrator.Baseline(0);   // was: Baseline(2)
}
migrator.MigrateTo(10);
```

### 2. Bring it up

```
docker compose up -d db redis pgbouncer adminer
docker compose up -d --build api
```

Wait for the API to finish migrations (watch `docker compose logs -f api`). The web service is commented out until we fork it.

### 3. Audio layout

Drop files into `./audio/` using this convention:

```
audio/
  <artist-slug>/
    YYYY-MM-DD <venue>/
      01 - Track Name.flac
      02 - Track Name.flac
      ...
```

The custom importer (TODO) walks this tree and populates `artists`, `shows`, `sources`, and `sources_tracks` rows with `mp3_url`/`flac_url` pointing at `http://<host>:8080/audio/<path>`.

## Roadmap

- [x] Clone RelistenApi
- [ ] Fork RelistenApi → patch `Baseline(2)` → `Baseline(0)`
- [ ] Fork relisten-web → rebrand
- [ ] Verify migrations run cleanly on empty DB
- [ ] Custom audio importer (CLI tool that walks `./audio/` and inserts rows)
- [ ] Reverse proxy + TLS (Caddy) for public exposure
- [ ] Deploy to TrueNAS Community Edition (native Docker apps)

## Hosting target

TrueNAS Community Edition 25.04 (rebranded SCALE). Deploy via the Apps system using a Custom App backed by this `docker-compose.yml`, or `docker compose` directly inside an SSH session.

## License

This project derives from Relisten (AGPL-3.0). All modifications under the same license. Source published at https://github.com/Zmatarasso/blahsum-relisten (fork TBD).
