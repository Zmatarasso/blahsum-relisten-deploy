"""CLI entrypoint: `python -m blahsum_importer [import|dry-run]`.

Walks AUDIO_DIR for audio files, parses each filename, optionally probes
for duration, and upserts catalog rows.

Env config:
  AUDIO_DIR              path to scan (read inside the container)
  AUDIO_PUBLIC_URL_BASE  e.g. http://truenas.local:8080/audio
                         The importer writes "{base}/{relpath}" into mp3_url.
  DATABASE_URL           postgresql://...

Usage:
  python -m blahsum_importer import
  python -m blahsum_importer dry-run     # parse + print, never touch DB
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote

from .db import connect, ensure_slug_unique_index, import_track
from .parser import parse_filename
from .probe import probe


_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".wav"}


def _walk_audio(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS:
            yield p


def _build_public_url(base: str, relpath: str) -> str:
    base = base.rstrip("/")
    encoded = "/".join(quote(seg, safe="") for seg in relpath.split("/"))
    return f"{base}/{encoded}"


def _config_or_die(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"ERROR: {name} not set", file=sys.stderr)
        sys.exit(2)
    return v


def cmd_dry_run(args: argparse.Namespace) -> int:
    audio_dir = Path(_config_or_die("AUDIO_DIR"))
    base = _config_or_die("AUDIO_PUBLIC_URL_BASE")
    files = list(_walk_audio(audio_dir))
    print(f"Found {len(files)} audio files under {audio_dir}")
    for p in files[: args.limit]:
        parsed = parse_filename(p, audio_dir)
        if parsed is None:
            print(f"  ?? skip {p.name}")
            continue
        info = probe(p) if args.probe else None
        url = _build_public_url(base, parsed.relpath)
        dur = f"{info.duration_seconds}s" if info else "?"
        print(
            f"  {parsed.display_date}  artist={parsed.artist_slug}  "
            f"title={parsed.title!r}  venue={parsed.venue_name!r}  "
            f"duration={dur}  url={url}"
        )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    audio_dir = Path(_config_or_die("AUDIO_DIR"))
    base = _config_or_die("AUDIO_PUBLIC_URL_BASE")
    dsn = _config_or_die("DATABASE_URL")

    files = list(_walk_audio(audio_dir))
    print(f"Importing {len(files)} files from {audio_dir} -> {base}")

    inserted = updated = skipped = 0
    with connect(dsn) as conn:
        with conn.cursor() as cur:
            ensure_slug_unique_index(cur)
        conn.commit()

        for p in files:
            parsed = parse_filename(p, audio_dir)
            if parsed is None:
                skipped += 1
                continue
            info = probe(p)
            url = _build_public_url(base, parsed.relpath)

            with conn.cursor() as cur:
                ids = import_track(cur, parsed, info, url)
            conn.commit()
            print(
                f"  ok   {parsed.display_date} {parsed.title!r}  "
                f"track_id={ids['track_id']} duration={info.duration_seconds}s"
            )
            inserted += 1

    print(f"Done. inserted/updated={inserted}  skipped={skipped}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blahsum-importer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dry = sub.add_parser("dry-run", help="parse + print without touching DB")
    p_dry.add_argument("--limit", type=int, default=50)
    p_dry.add_argument("--probe", action="store_true", help="also run mutagen probe")
    p_dry.set_defaults(fn=cmd_dry_run)

    p_imp = sub.add_parser("import", help="upsert all files into the catalog")
    p_imp.set_defaults(fn=cmd_import)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
