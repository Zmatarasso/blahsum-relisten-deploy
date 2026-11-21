"""`<show file>.tracks.txt` sidecars: per-show overrides + song markers.

Format (UTF-8, one item per line, `#` starts a comment):

    date: 2025-04-26          # also 4-26-25, 4/26/2025
    title: The Gutter         # title when the show is one long track
    venue: The Gutter, Brooklyn NY

    0:00     Intro
    3:00     Crystal Eel
    12:05    Sundown
    1:58:30  Encore Song *    # trailing * = encore

Times are H:MM:SS, M:SS or plain seconds, measured from the start of the
file, and must go up. Each song runs until the next one starts; the last
runs to the end of the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

SIDECAR_SUFFIX = ".tracks.txt"

_HEADER_RE = re.compile(r"^(date|title|venue)\s*[:=]\s*(.*?)\s*$", re.IGNORECASE)
_SONG_RE = re.compile(r"^(\d+(?::\d{1,2}){0,2}(?:\.\d+)?)\s+(.+?)\s*$")


@dataclass
class Song:
    start: float
    title: str
    encore: bool = False


@dataclass
class Sidecar:
    path: Path
    date: _date | None = None
    title: str | None = None
    venue: str | None = None
    songs: list[Song] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def sidecar_path(audio: Path) -> Path:
    return audio.with_name(audio.stem + SIDECAR_SUFFIX)


def parse_time(text: str) -> float:
    parts = text.split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def parse_date(text: str) -> _date | None:
    t = text.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", t)
        if not m:
            return None
        mo, d, y = (int(x) for x in m.groups())
        if y < 100:
            y = 2000 + y if y < 70 else 1900 + y
    try:
        return _date(y, mo, d)
    except ValueError:
        return None


def load(audio: Path) -> Sidecar | None:
    p = sidecar_path(audio)
    if not p.is_file():
        return None
    sc = Sidecar(path=p)
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        sc.problems.append(f"can't read: {e}")
        return sc

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _HEADER_RE.match(line)
        if m:
            key, val = m.group(1).lower(), m.group(2)
            if not val:
                continue
            if key == "date":
                d = parse_date(val)
                if d is None:
                    sc.problems.append(f"line {n}: date {val!r} not understood (use 2025-04-26)")
                else:
                    sc.date = d
            elif key == "title":
                sc.title = val[:200]
            elif key == "venue":
                sc.venue = val[:200]
            continue
        m = _SONG_RE.match(line)
        if m:
            title = m.group(2).strip()
            encore = title.endswith("*")
            title = title.rstrip("*").strip()[:200] or "Untitled"
            try:
                start = parse_time(m.group(1))
            except ValueError:
                sc.problems.append(f"line {n}: time {m.group(1)!r} not understood")
                continue
            sc.songs.append(Song(start=start, title=title, encore=encore))
            continue
        sc.problems.append(f"line {n}: not understood: {line[:60]!r}")

    for a, b in zip(sc.songs, sc.songs[1:]):
        if b.start <= a.start:
            sc.problems.append(
                f"song times must go up ({b.title!r} starts before or with {a.title!r}); song markers ignored"
            )
            sc.songs = []
            break
    return sc
