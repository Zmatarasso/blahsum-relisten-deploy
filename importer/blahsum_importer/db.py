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


def rebuild_show_source_information(cur: psycopg.Cursor) -> int:
    """Rebuild show_source_information from sources.

    Several controllers (ShowService, ArtistService) INNER JOIN this
    table — no rows here means the API returns 404 for shows that
    actually exist. Upstream maintains this table from the importers
    (ImporterBase.cs). We rebuild from scratch each run; cheap at our
    scale and avoids drift.
    """
    cur.execute("TRUNCATE show_source_information;")
    cur.execute(
        """
        INSERT INTO show_source_information (
            show_id, max_updated_at, source_count, artist_id,
            max_avg_rating_weighted, has_soundboard_source, has_flac,
            max_created_at
        )
        SELECT
            src.show_id,
            MAX(src.updated_at)            AS max_updated_at,
            COUNT(*)                       AS source_count,
            src.artist_id,
            MAX(src.avg_rating_weighted)   AS max_avg_rating_weighted,
            BOOL_OR(src.is_soundboard)     AS has_soundboard_source,
            BOOL_OR(src.flac_type IN (1, 2)) AS has_flac,
            MAX(src.created_at)            AS max_created_at
        FROM sources src
        WHERE src.show_id IS NOT NULL
        GROUP BY src.show_id, src.artist_id;
        """
    )
    return cur.rowcount


def ensure_features(cur: psycopg.Cursor, artist_id: int) -> None:
    """Every artist needs a `features` row.

    ShowService.cs accesses `artist.features.tours` etc. without
    null-checking; without a row we get NREs on every show query.
    Default everything to false except `track_durations` (so the player
    shows lengths) and `years` (so the year nav works). We can flip
    flags later as we add features.
    """
    cur.execute("SELECT 1 FROM features WHERE artist_id = %s LIMIT 1;", (artist_id,))
    if cur.fetchone() is not None:
        return
    cur.execute(
        """
        INSERT INTO features (
            artist_id, years, track_durations, sets, multiple_sources
        )
        VALUES (%s, true, true, true, true);
        """,
        (artist_id,),
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


def upsert_venue(cur: psycopg.Cursor, artist_id: int, text: str) -> int:
    """Venue from a sidecar line like "The Gutter, Brooklyn NY"."""
    name, _, location = (x.strip() for x in text.partition(","))
    slug = slugify(name)
    ident = f"blahsum:{slug}"
    cur.execute(
        "SELECT id FROM venues WHERE artist_id = %s AND upstream_identifier = %s;",
        (artist_id, ident),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE venues SET name=%s, location=%s, slug=%s, updated_at=NOW() WHERE id=%s;",
            (name, location or None, slug, row[0]),
        )
        return row[0]
    cur.execute(
        """
        INSERT INTO venues (artist_id, name, location, slug, upstream_identifier, uuid, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW()) RETURNING id;
        """,
        (artist_id, name, location or None, slug, ident, str(uuid.uuid4())),
    )
    return cur.fetchone()[0]


def _ensure_set(cur: psycopg.Cursor, source_id: int, index: int, name: str, is_encore: bool) -> int:
    cur.execute("SELECT id FROM source_sets WHERE source_id=%s AND index=%s;", (source_id, index))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE source_sets SET name=%s, is_encore=%s, updated_at=NOW() WHERE id=%s;",
            (name, is_encore, row[0]),
        )
        return row[0]
    cur.execute(
        """
        INSERT INTO source_sets (source_id, index, name, is_encore, uuid, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW()) RETURNING id;
        """,
        (source_id, index, name, is_encore, str(uuid.uuid4())),
    )
    return cur.fetchone()[0]


def _unique_slugs(titles: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for t in titles:
        base = slugify(t)
        n = seen.get(base, 0) + 1
        seen[base] = n
        out.append(base if n == 1 else f"{base}-{n}")
    return out


def _delete_tracks_where(cur, where: str, params: tuple) -> int:
    """Delete source_tracks (and their legacy play rows, which have no ON DELETE)."""
    cur.execute(
        f"DELETE FROM source_tracks_plays WHERE source_track_id IN (SELECT id FROM source_tracks WHERE {where});",
        params,
    )
    cur.execute(f"DELETE FROM source_tracks WHERE {where};", params)
    return cur.rowcount


def sync_split_tracks(cur, *, artist_id: int, source_id: int, cut_tracks, url_for) -> bool:
    """Make the source's tracks exactly the cut songs. Returns True if changed."""
    has_encore = any(t.encore for t in cut_tracks)
    set1 = _ensure_set(cur, source_id, 0, "Set 1", False)
    enc = _ensure_set(cur, source_id, 1, "Encore", True) if has_encore else None
    slugs = _unique_slugs([t.title for t in cut_tracks])
    want = [
        (t.position, t.title, slug, url_for(t.filename), t.duration, enc if t.encore else set1)
        for t, slug in zip(cut_tracks, slugs)
    ]
    cur.execute(
        """SELECT track_position, title, slug, mp3_url, duration, source_set_id, uuid
             FROM source_tracks WHERE source_id=%s ORDER BY track_position, id;""",
        (source_id,),
    )
    have = cur.fetchall()
    if [tuple(r[:6]) for r in have] == want and (has_encore or not _has_set(cur, source_id, 1)):
        return False
    uuids = {r[0]: r[6] for r in have}
    _delete_tracks_where(cur, "source_id=%s", (source_id,))
    for pos, title, slug, url, dur, set_id in want:
        cur.execute(
            """
            INSERT INTO source_tracks (artist_id, source_id, source_set_id, track_position,
                                       title, slug, mp3_url, duration, uuid, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW());
            """,
            (artist_id, source_id, set_id, pos, title, slug, url, dur,
             str(uuids.get(pos) or uuid.uuid4())),
        )
    if not has_encore:
        cur.execute("DELETE FROM source_sets WHERE source_id=%s AND index > 0;", (source_id,))
    return True


def _has_set(cur, source_id: int, index: int) -> bool:
    cur.execute("SELECT 1 FROM source_sets WHERE source_id=%s AND index=%s;", (source_id, index))
    return cur.fetchone() is not None


def import_show(
    cur: psycopg.Cursor,
    parsed: ParsedTrack,
    info: AudioInfo,
    public_url: str,
    *,
    title_override: str | None = None,
    venue: str | None = None,
    cut_tracks=None,
    url_for=None,
    tracks_url_prefix: str = "",
) -> dict:
    """Upsert one audio file (one show recording). Returns a summary dict."""
    artist_id = upsert_artist(cur, parsed.artist_name, parsed.artist_slug)
    ensure_features(cur, artist_id)
    year_id = upsert_year(cur, artist_id, str(parsed.show_date.year))
    show_id = upsert_show(cur, artist_id, year_id, parsed.show_date, parsed.display_date)
    source_id = upsert_source(
        cur,
        artist_id=artist_id,
        show_id=show_id,
        upstream_identifier=parsed.relpath,
        display_date=parsed.display_date,
        duration_seconds=info.duration_seconds,
    )
    if venue:
        venue_id = upsert_venue(cur, artist_id, venue)
        cur.execute("UPDATE shows SET venue_id=%s WHERE id=%s;", (venue_id, show_id))
        cur.execute("UPDATE sources SET venue_id=%s WHERE id=%s;", (venue_id, source_id))

    result = dict(artist_id=artist_id, show_id=show_id, source_id=source_id, mode="whole", changed=False)

    if cut_tracks:
        result["mode"] = "songs"
        result["changed"] = sync_split_tracks(
            cur, artist_id=artist_id, source_id=source_id, cut_tracks=cut_tracks, url_for=url_for
        )
        return result

    # No song markers. Drop tracks we generated earlier (sidecar removed).
    if tracks_url_prefix:
        if _delete_tracks_where(
            cur, "source_id=%s AND mp3_url LIKE %s", (source_id, tracks_url_prefix.rstrip("/") + "/%")
        ):
            result["changed"] = True
            cur.execute("DELETE FROM source_sets WHERE source_id=%s AND index > 0;", (source_id,))

    set_id = upsert_default_set(cur, source_id)
    # Hand-curated in SQL (tracks that aren't the whole file)? leave alone.
    cur.execute(
        "SELECT 1 FROM source_tracks WHERE source_id = %s AND mp3_url <> %s LIMIT 1;",
        (source_id, public_url),
    )
    if cur.fetchone() is not None:
        result["mode"] = "manual"
        return result
    title = title_override or parsed.title
    upsert_track(
        cur,
        artist_id=artist_id,
        source_id=source_id,
        source_set_id=set_id,
        title=title,
        track_position=1,
        mp3_url=public_url,
        duration_seconds=info.duration_seconds,
    )
    return result


def remove_missing_sources(cur: psycopg.Cursor, artist_slug: str, present: set[str]) -> list[str]:
    """Sources whose audio file is gone. Caller applies safety limits."""
    cur.execute(
        """SELECT so.id, so.upstream_identifier FROM sources so JOIN artists a ON a.id = so.artist_id
            WHERE a.slug = %s;""",
        (artist_slug,),
    )
    gone = [(i, u) for i, u in cur.fetchall() if u not in present]
    return gone


def delete_sources(cur: psycopg.Cursor, source_ids: list[int]) -> None:
    if not source_ids:
        return
    _delete_tracks_where(cur, "source_id = ANY(%s)", (source_ids,))
    cur.execute("DELETE FROM source_sets WHERE source_id = ANY(%s);", (source_ids,))
    cur.execute("DELETE FROM sources WHERE id = ANY(%s);", (source_ids,))


def cleanup_empty(cur: psycopg.Cursor) -> int:
    """Delete shows with no recordings left (e.g. after a date change), then empty years."""
    cur.execute(
        "DELETE FROM shows sh WHERE NOT EXISTS (SELECT 1 FROM sources so WHERE so.show_id = sh.id);"
    )
    n = cur.rowcount
    cur.execute(
        "DELETE FROM years y WHERE NOT EXISTS (SELECT 1 FROM shows sh WHERE sh.year_id = y.id);"
    )
    return n


def artist_uuid(cur: psycopg.Cursor, slug: str) -> str | None:
    cur.execute("SELECT uuid FROM artists WHERE slug=%s;", (slug,))
    row = cur.fetchone()
    return str(row[0]) if row else None
