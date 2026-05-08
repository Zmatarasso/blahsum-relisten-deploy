"""Audio metadata probing.

We use mutagen for cheap header reads (duration, bitrate). It handles mp3
without invoking a subprocess and is good enough for our purposes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile


@dataclass
class AudioInfo:
    duration_seconds: int | None
    bitrate_bps: int | None


def probe(path: Path) -> AudioInfo:
    try:
        m = MutagenFile(path)
    except Exception:
        return AudioInfo(None, None)
    if m is None or m.info is None:
        return AudioInfo(None, None)
    duration = getattr(m.info, "length", None)
    bitrate = getattr(m.info, "bitrate", None)
    return AudioInfo(
        duration_seconds=int(duration) if duration else None,
        bitrate_bps=int(bitrate) if bitrate else None,
    )
