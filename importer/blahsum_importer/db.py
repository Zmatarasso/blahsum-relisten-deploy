"""Database upserts for blahsum_importer.

One row per source file:
  artist  → year  → show  → source  → source_set  → source_track

Idempotent: re-running on the same files updates existing rows rather
than duplicating. Keys used:
  artists:        slug                                              (UNIQUE-ish; we serialize)
  years:          (artist_id, year)                                  UNIQUE
  shows:          (artist_id, display_date)                          UNIQUE
  sources:        (artist_id, upstream_identifier)                   we enforce
  source_tracks:  (artist_id, mp3_url)                               UNIQUE in schema

`upstream_identifier` is the relative file path; that's how we'll
recognize the same file across runs.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
import uuid

import psycopg

from .parser import ParsedTrack, slugify
from .probe import AudioInfo


@contextmanager
def connect(dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn, autocommit=False) as conn:
        yield conn


def upsert_artist(cur: psycopg.Cursor, name: str, slug: str) -> int:
    cur.execute(
        """
        INSERT INTO artists (name, slug, sort_name, uuid, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, updated_at = NOW()
        RETURNING id;
        """,
        (name, slug, name, str(uuid.uuid4())),
    )
    row = cur.fetchone()
    if row is not None:
        return row[0]
    # The schema doesn't actually have a UNIQUE on slug, so the ON CONFLICT
    # above won't always fire. Fall back to a SELECT.
    cur.execute("SELECT id FROM artists WHERE slug = %s", (slug,))
    return cur.fetchone()[0]


def ensure_slug_unique_index(cur: psycopg.Cursor) -> None:
    """Idempotent: add a unique index on artists.slug if missing.

    Upstream's schema doesn't enforce uniqueness on slug, but the API
    treats slugs as the public identifier. We add the index so our ON
    CONFLICT (slug) actually works.
    """
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS artists_slug_uidx ON artists (slug);
        """
    )


def upsert_year(cur: psycopg.Cursor, artist_id: int, year: str) -> int:
    cur.execute(
        """
        INSERT INTO years (artist_id, year, uuid, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (artist_id, year) DO UPDATE SET updated_at = NOW()
        RETURNING id;
        """,
        (artist_id, year, str(uuid.uuid4())),
    )
    return cur.fetchone()[0]


def upsert_show(
    cur: psycopg.Cursor,
    artist_id: int,
    year_id: int,
    show_date,
    display_date: str,
) -> int:
    cur.execute(
        """
        INSERT INTO shows (artist_id, year_id, date, display_date, uuid, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (artist_id, display_date) DO UPDATE
          SET year_id    = EXCLUDED.year_id,
              date       = EXCLUDED.date,
              updated_at = NOW()
        RETURNING id;
        """,
        (artist_id, year_id, show_date, display_date, str(uuid.uuid4())),
    )
    return cur.fetchone()[0]


def upsert_source(
    cur: psycopg.Cursor,
    *,
    artist_id: int,
    show_id: int,
    upstream_identifier: str,
    display_date: str,
    duration_seconds: int | None,
) -> int:
    """Insert or update a source keyed off (artist_id, upstream_identifier)."""
    # source schema requires a number of NOT NULL booleans/strings — fill with sane defaults.
    cur.execute(
        """
        SELECT id FROM sources
        WHERE artist_id = %s AND upstream_identifier = %s;
        """,
        (artist_id, upstream_identifier),
    )
    row = cur.fetchone()
    if row is not None:
        source_id = row[0]
        cur.execute(
            """
            UPDATE sources SET
              show_id = %s,
              display_date = %s,
              duration = %s,
              updated_at = NOW()
            WHERE id = %s;
            """,
            (show_id, display_date, duration_seconds, source_id),
        )
        return source_id

    cur.execute(
        """
        INSERT INTO sources (
            artist_id, show_id,
            is_soundboard, is_remaster, has_jamcharts,
            avg_rating, num_reviews, flac_type,
            upstream_identifier, display_date,
            duration, uuid, updated_at
        )
        VALUES (%s, %s, false, false, false, 0, 0, 0, %s, %s, %s, %s, NOW())
        RETURNING id;
        """,
        (
            artist_id,
            show_id,
            upstream_identifier,
            display_date,
            duration_seconds,
            str(uuid.uuid4()),
        ),
    )
    return cur.fetchone()[0]


def upsert_default_set(cur: psycopg.Cursor, source_id: int) -> int:
    """A single 'Set 1' per source — fine for the 1-track-per-show layout."""
    cur.execute(
        "SELECT id FROM source_sets WHERE source_id = %s AND index = 0;",
        (source_id,),
    )
    row = cur.fetchone()
    if row is not None:
        return row[0]
    cur.execute(
        """
        INSERT INTO source_sets (source_id, index, name, is_encore, uuid, updated_at)
        VALUES (%s, 0, 'Set 1', false, %s, NOW())
        RETURNING id;
        """,
        (source_id, str(uuid.uuid4())),
    )
    return cur.fetchone()[0]


def upsert_track(
    cur: psycopg.Cursor,
    *,
    artist_id: int,
    source_id: int,
    source_set_id: int,
    title: str,
    track_position: int,
    mp3_url: str,
    duration_seconds: int | None,
) -> int:
    """Idempotent on (artist_id, mp3_url) — the schema's existing UNIQUE."""
    cur.execute(
        """
        SELECT id FROM source_tracks
        WHERE artist_id = %s AND mp3_url = %s;
        """,
        (artist_id, mp3_url),
    )
    row = cur.fetchone()
    if row is not None:
        track_id = row[0]
        cur.execute(
            """
            UPDATE source_tracks SET
              source_id      = %s,
              source_set_id  = %s,
              track_position = %s,
              title          = %s,
              slug           = %s,
              duration       = %s,
              updated_at     = NOW()
            WHERE id = %s;
            """,
            (
                source_id, source_set_id, track_position,
                title, slugify(title), duration_seconds, track_id,
            ),
        )
        return track_id

    cur.execute(
        """
        INSERT INTO source_tracks (
            artist_id, source_id, source_set_id, track_position,
            title, slug, mp3_url, duration, uuid, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        RETURNING id;
        """,
        (
            artist_id, source_id, source_set_id, track_position,
            title, slugify(title), mp3_url, duration_seconds, str(uuid.uuid4()),
        ),
    )
    return cur.fetchone()[0]


def import_track(
    cur: psycopg.Cursor,
    parsed: ParsedTrack,
    info: AudioInfo,
    public_url: str,
) -> dict:
    """Run the full upsert chain for a single file. Returns row IDs for logs."""
    artist_id = upsert_artist(cur, parsed.artist_name, parsed.artist_slug)
    year_id = upsert_year(cur, artist_id, str(parsed.show_date.year))
    show_id = upsert_show(
        cur, artist_id, year_id, parsed.show_date, parsed.display_date
    )
    source_id = upsert_source(
        cur,
        artist_id=artist_id,
        show_id=show_id,
        upstream_identifier=parsed.relpath,
        display_date=parsed.display_date,
        duration_seconds=info.duration_seconds,
    )
    set_id = upsert_default_set(cur, source_id)
    track_id = upsert_track(
        cur,
        artist_id=artist_id,
        source_id=source_id,
        source_set_id=set_id,
        title=parsed.title,
        track_position=1,
        mp3_url=public_url,
        duration_seconds=info.duration_seconds,
    )
    return dict(
        artist_id=artist_id,
        year_id=year_id,
        show_id=show_id,
        source_id=source_id,
        source_set_id=set_id,
        track_id=track_id,
    )
