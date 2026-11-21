"""CLI entrypoint: `python -m blahsum_importer [import|dry-run|watch]`.

Walks AUDIO_DIR for audio files, parses each filename (plus an optional
`<file>.tracks.txt` sidecar), and upserts catalog rows. With song markers in
the sidecar it cuts per-song files into TRACKS_DIR (never touching the
original) and creates one track per song.

Env config:
  AUDIO_DIR               path to scan (read-only inside the container)
  AUDIO_PUBLIC_URL_BASE   e.g. https://host:10000/audio
  DATABASE_URL            postgresql://...
  TRACKS_DIR              writable folder for cut songs (optional; no cutting if unset)
  TRACKS_PUBLIC_URL_BASE  e.g. https://host:10000/tracks
  STATUS_DIR              where import-status.txt is written (optional)
  REDIS_URL               redis://redis:6379 (optional; clears the API's artist cache)

Usage:
  python -m blahsum_importer import
  python -m blahsum_importer dry-run          # parse + print, never touch DB
  python -m blahsum_importer watch --interval 300
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from . import sidecar as sidecar_mod
from .db import (
    artist_uuid,
    cleanup_empty,
    connect,
    delete_sources,
    ensure_slug_unique_index,
    import_show,
    rebuild_show_source_information,
    remove_missing_sources,
)
from .parser import parse_filename
from .probe import probe
from .tracks import ensure_cut, remove_unused, source_key

_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".wav"}
ARTIST_SLUG = "blahsum"

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _TZ = timezone.utc


def _now() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d %I:%M %p %Z")


def _walk_audio(root: Path):
    for p in sorted(root.rglob("*")):
        # Skip anything under a folder starting with "_" or "." (e.g. _tracks/,
        # hand-split files wired up via SQL) and dotfiles.
        rel = p.relative_to(root).parts
        if any(part.startswith(("_", ".")) for part in rel):
            continue
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


def _parse_with_sidecar(p: Path, audio_dir: Path):
    parsed = parse_filename(p, audio_dir)
    sc = sidecar_mod.load(p)
    if parsed is not None and sc is not None and sc.date is not None:
        parsed.show_date = sc.date
        parsed.display_date = sc.date.isoformat()
    return parsed, sc


def _redis_del(url: str | None, key: str) -> None:
    if not url:
        return
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or "redis", u.port or 6379), timeout=3) as s:
            k = key.encode()
            s.sendall(b"*2\r\n$3\r\nDEL\r\n$%d\r\n%s\r\n" % (len(k), k))
            s.recv(64)
    except OSError as e:
        print(f"  (couldn't clear API cache: {e})")


def cmd_dry_run(args: argparse.Namespace) -> int:
    audio_dir = Path(_config_or_die("AUDIO_DIR"))
    base = _config_or_die("AUDIO_PUBLIC_URL_BASE")
    files = list(_walk_audio(audio_dir))
    print(f"Found {len(files)} audio files under {audio_dir}")
    for p in files[: args.limit]:
        parsed, sc = _parse_with_sidecar(p, audio_dir)
        if parsed is None:
            print(f"  ?? skip {p.name}")
            continue
        info = probe(p) if args.probe else None
        url = _build_public_url(base, parsed.relpath)
        dur = f"{info.duration_seconds}s" if info else "?"
        extra = ""
        if sc:
            extra = f"  sidecar: songs={len(sc.songs)} title={sc.title!r} venue={sc.venue!r}"
            if sc.problems:
                extra += f" PROBLEMS={sc.problems}"
        print(
            f"  {parsed.display_date}  title={parsed.title!r}  venue={parsed.venue_name!r}  "
            f"duration={dur}  url={url}{extra}"
        )
    return 0


def run_import() -> dict:
    audio_dir = Path(_config_or_die("AUDIO_DIR"))
    base = _config_or_die("AUDIO_PUBLIC_URL_BASE")
    dsn = _config_or_die("DATABASE_URL")
    tracks_dir = Path(os.environ["TRACKS_DIR"]) if os.environ.get("TRACKS_DIR") else None
    tracks_base = os.environ.get("TRACKS_PUBLIC_URL_BASE", "").rstrip("/")
    if tracks_dir and not tracks_base:
        tracks_dir = None

    report = dict(started=_now(), shows=[], problems=[], removed=[], changed=False)
    files = list(_walk_audio(audio_dir))
    report["files"] = len(files)
    present: set[str] = set()
    keep_keys: set[str] = set()

    with connect(dsn) as conn:
        with conn.cursor() as cur:
            ensure_slug_unique_index(cur)
        conn.commit()

        for p in files:
            rel = str(p.relative_to(audio_dir)).replace("\\", "/")
            present.add(rel)
            try:
                parsed, sc = _parse_with_sidecar(p, audio_dir)
                if parsed is None:
                    report["problems"].append(f"{rel}: file name not understood, skipped")
                    continue
                if sc:
                    for msg in sc.problems:
                        report["problems"].append(f"{sc.path.name}: {msg}")
                info = probe(p)
                url = _build_public_url(base, parsed.relpath)

                cut, recut, url_for = None, False, None
                if sc and sc.songs:
                    if tracks_dir is None:
                        report["problems"].append(f"{rel}: song markers found but track cutting is not configured")
                    elif info.duration_seconds and sc.songs[-1].start >= info.duration_seconds:
                        report["problems"].append(
                            f"{sc.path.name}: last song starts after the recording ends "
                            f"({info.duration_seconds // 60}m{info.duration_seconds % 60:02d}s); song markers ignored"
                        )
                    else:
                        key = source_key(parsed.relpath)
                        keep_keys.add(key)
                        cut, recut = ensure_cut(p, sc.songs, tracks_dir, key)
                        url_for = (lambda fn, k=key: f"{tracks_base}/{k}/{quote(fn, safe='')}")

                with conn.cursor() as cur:
                    res = import_show(
                        cur, parsed, info, url,
                        title_override=sc.title if sc else None,
                        venue=sc.venue if sc else None,
                        cut_tracks=cut, url_for=url_for,
                        tracks_url_prefix=tracks_base,
                    )
                conn.commit()
                changed = res["changed"] or recut
                report["changed"] |= changed
                if res["mode"] == "songs":
                    label = f"{len(cut or [])} songs"
                elif res["mode"] == "manual":
                    label = "hand-edited tracks (left alone)"
                else:
                    label = "1 full-length track"
                report["shows"].append(
                    f"{parsed.display_date}  {(sc.title if sc and sc.title else parsed.title)}  [{label}]"
                    + ("  (updated)" if changed else "")
                )
            except Exception as e:  # keep going with the other files
                conn.rollback()
                report["problems"].append(f"{rel}: {e}")
                traceback.print_exc()

        # Files that disappeared: remove their recordings (with a safety limit).
        with conn.cursor() as cur:
            gone = remove_missing_sources(cur, ARTIST_SLUG, present)
            total = len(present) + len(gone)
            if gone and (not present or len(gone) > max(3, total // 2)):
                report["problems"].append(
                    f"SAFETY STOP: {len(gone)} recordings' files look missing; not removing anything. "
                    "Is the audio folder mounted?"
                )
            elif gone:
                delete_sources(cur, [i for i, _ in gone])
                report["removed"] = [u for _, u in gone]
                report["changed"] = True
            if cleanup_empty(cur):
                report["changed"] = True
            rebuild_show_source_information(cur)
            uid = artist_uuid(cur, ARTIST_SLUG)
        conn.commit()

    if tracks_dir:
        remove_unused(tracks_dir, keep_keys)
    if report["changed"] and uid:
        _redis_del(os.environ.get("REDIS_URL"), f"full-artist:{uid}")
    report["finished"] = _now()
    return report


def _format_status(report: dict, note: str = "") -> str:
    lines = [
        "blahsum.live import status",
        "==========================",
        f"Last check:  {report.get('checked', report.get('finished', '?'))}",
        f"Last import: {report.get('finished', '?')}" + (f"  ({note})" if note else ""),
        f"Audio files: {report.get('files', '?')}",
        "",
    ]
    if report.get("error"):
        lines += ["IMPORT FAILED:", "  " + report["error"], ""]
    lines.append("Problems:" if report.get("problems") else "Problems: none")
    lines += [f"  - {p}" for p in report.get("problems", [])]
    if report.get("removed"):
        lines += ["", "Removed (file no longer in the folder):"] + [f"  - {r}" for r in report["removed"]]
    lines += ["", "Shows:"] + [f"  {s}" for s in sorted(report.get("shows", []))]
    lines += ["", "New or changed shows appear on the site within about 5 minutes of the import."]
    return "\n".join(lines) + "\n"


def _write_status(text: str) -> None:
    d = os.environ.get("STATUS_DIR")
    if not d:
        return
    p = Path(d) / "import-status.txt"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def cmd_import(args: argparse.Namespace) -> int:
    report = run_import()
    text = _format_status(report)
    _write_status(text)
    print(text)
    return 0


def _fingerprint(audio_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(audio_dir.rglob("*")):
        rel = p.relative_to(audio_dir).parts
        if any(part.startswith(("_", ".")) for part in rel) or not p.is_file():
            continue
        if p.suffix.lower() in _AUDIO_EXTS or p.name.endswith(sidecar_mod.SIDECAR_SUFFIX):
            st = p.stat()
            h.update(f"{'/'.join(rel)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def cmd_watch(args: argparse.Namespace) -> int:
    audio_dir = Path(_config_or_die("AUDIO_DIR"))
    last_fp, last_report, last_run = None, {}, 0.0
    print(f"watching {audio_dir} every {args.interval}s")
    while True:
        try:
            fp = _fingerprint(audio_dir)
            due = fp != last_fp or (time.time() - last_run) > args.full_every
            if due:
                last_report = run_import()
                last_fp, last_run = fp, time.time()
                print(f"[{last_report['finished']}] imported {len(last_report['shows'])} shows, "
                      f"{len(last_report['problems'])} problems, changed={last_report['changed']}", flush=True)
            last_report["checked"] = _now()
            _write_status(_format_status(last_report))
        except Exception as e:
            traceback.print_exc()
            last_report = dict(last_report, error=str(e)[:300], checked=_now())
            last_fp = None
            try:
                _write_status(_format_status(last_report))
            except Exception:
                pass
        time.sleep(args.interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blahsum-importer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dry = sub.add_parser("dry-run", help="parse + print without touching DB")
    p_dry.add_argument("--limit", type=int, default=200)
    p_dry.add_argument("--probe", action="store_true", help="also read durations")
    p_dry.set_defaults(fn=cmd_dry_run)

    p_imp = sub.add_parser("import", help="upsert all files into the catalog")
    p_imp.set_defaults(fn=cmd_import)

    p_w = sub.add_parser("watch", help="re-import whenever the folder changes")
    p_w.add_argument("--interval", type=int, default=300)
    p_w.add_argument("--full-every", type=int, default=6 * 3600,
                     help="force a full import this often even without changes")
    p_w.set_defaults(fn=cmd_watch)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
