# blahsum_importer

Walks `AUDIO_DIR`, parses each filename into catalog metadata, and
upserts `artists / years / shows / sources / source_sets / source_tracks`
in the Relisten schema. One audio file = one show = one source = one
track today (the BLAHSUM live recordings ship as a single mp3 per
show). When the file layout grows multiple tracks per show, only
`parser.py` and the `1`/`Set 1` defaults need to change.

## Run

The importer is a Compose profile-gated service so it doesn't show up in
the regular `docker compose up`.

```sh
# preview what would happen, parse 50 files, also probe duration
docker compose --profile tools run --rm importer dry-run --probe

# do it
docker compose --profile tools run --rm importer import
```

It's idempotent: re-running on the same files updates rows rather than
duplicating them. Keys:

| level         | uniqueness                                    |
| ------------- | --------------------------------------------- |
| artists       | `slug` (unique index added by the importer)   |
| years         | `(artist_id, year)`                            |
| shows         | `(artist_id, display_date)`                    |
| sources       | `(artist_id, upstream_identifier)`             |
| source_tracks | `(artist_id, mp3_url)`                         |

`upstream_identifier` is the relative file path under `AUDIO_DIR` —
that's how we recognize the same file across runs even if the title or
date changes.

## Config

Set in `.env` (or per-invocation):

| var                       | what                                            |
| ------------------------- | ----------------------------------------------- |
| `AUDIO_DIR`               | host path to the audio root, bind-mounted into the container at `/audio` |
| `AUDIO_PUBLIC_URL_BASE`   | URL prefix that the audio nginx serves files at, e.g. `http://truenas.local:8080/audio`. Eventually `https://relisten.blahsum.com/audio`. |

`DATABASE_URL` is hardcoded in `docker-compose.yml` to the local `db`
service.

## Today's filename heuristics

```
"BLAHSUM live in Brooklyn NY 4-25-25 Frost St Gallery.mp3"
   → artist BLAHSUM, date 2025-04-25, venue "Frost St Gallery", live
"BLAHSUM - RING ON YOUR BELL.mp3"
   → artist BLAHSUM, date = file mtime, title "RING ON YOUR BELL", studio
```

Two-digit years under 70 are 20YY, the rest 19YY. Files without a date
in the name fall back to the file's mtime.
