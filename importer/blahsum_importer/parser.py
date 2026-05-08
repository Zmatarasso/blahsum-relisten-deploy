"""Parse audio filenames into structured show metadata.

Today's convention is messy because the source files weren't authored for
this importer. Every parser decision is one line in here — when we
formalize a layout we'll narrow this down.

Current heuristics for the BLAHSUM library:
  "BLAHSUM live in <City>[, <ST>] <date> <venue/notes>.mp3"  → live show
  "BLAHSUM - <Title>.mp3"                                    → studio track

`<date>` is `M-D-YY`, `MM-DD-YY`, or any combination. Two-digit years
under 70 are treated as 20YY, the rest as 19YY.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as _date, datetime
from pathlib import Path

# Anchored on word boundaries so it doesn't match e.g. "set 1-2" inside the title.
_DATE_RE = re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{2,4})\b")


@dataclass
class ParsedTrack:
    """A single audio file translated into the rows we'll insert."""

    path: Path                  # absolute path on the host
    relpath: str                # relative to the audio root, with forward slashes
    artist_name: str
    artist_slug: str
    show_date: _date            # used for shows.date and the year row
    display_date: str           # YYYY-MM-DD; the unique-key column on shows
    venue_name: str | None      # text after the date in the filename
    title: str                  # what shows up in the player
    is_live: bool


def _two_digit_year(yy: int) -> int:
    return 2000 + yy if yy < 70 else 1900 + yy


def _safe_date(month: int, day: int, year: int) -> _date | None:
    try:
        return _date(year, month, day)
    except ValueError:
        return None


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "untitled"


def parse_filename(path: Path, audio_root: Path) -> ParsedTrack | None:
    """Return a ParsedTrack, or None if we can't make sense of the file."""
    if path.suffix.lower() not in {".mp3", ".flac", ".m4a", ".ogg", ".wav"}:
        return None

    stem = path.stem
    relpath = str(path.relative_to(audio_root)).replace("\\", "/")

    # --- artist (today: hardcoded BLAHSUM, since the live tree is single-artist) ---
    artist_name = "BLAHSUM"
    artist_slug = "blahsum"

    # --- date in filename ---
    show_date: _date | None = None
    pre_date = stem
    post_date = ""
    is_live = stem.lower().startswith("blahsum live ") or " live " in stem.lower()

    for m in _DATE_RE.finditer(stem):
        mm, dd, yy_raw = (int(m.group(i)) for i in (1, 2, 3))
        yy = _two_digit_year(yy_raw) if yy_raw < 100 else yy_raw
        candidate = _safe_date(mm, dd, yy)
        if candidate is not None:
            show_date = candidate
            pre_date = stem[: m.start()].strip(" -—")
            post_date = stem[m.end():].strip(" -—")
            break

    if show_date is None:
        # Fall back to file mtime; means studio singles still get a show row.
        show_date = datetime.fromtimestamp(path.stat().st_mtime).date()

    display_date = show_date.isoformat()

    # --- venue / title heuristics ---
    venue_name = post_date or None
    if is_live:
        title = post_date or pre_date or stem
    else:
        # studio: strip "BLAHSUM - " prefix if present
        m = re.match(r"^\s*BLAHSUM\s*[-–—]\s*(.+?)\s*$", stem, re.IGNORECASE)
        title = m.group(1).strip() if m else stem

    return ParsedTrack(
        path=path,
        relpath=relpath,
        artist_name=artist_name,
        artist_slug=artist_slug,
        show_date=show_date,
        display_date=display_date,
        venue_name=venue_name,
        title=title,
        is_live=is_live,
    )


def slugify(text: str) -> str:
    """Public re-export of the slug helper."""
    return _slugify(text)
