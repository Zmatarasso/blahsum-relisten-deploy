"""Cut per-song files out of a show recording (ffmpeg -c copy, no re-encode).

Output: TRACKS_DIR/<key>/NN-<slug>.<ext>, plus a `.signature` file so we only
re-cut when the source file or the song list changes. The original audio is
only ever read.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .parser import slugify
from .probe import probe
from .sidecar import Song

CUT_VERSION = "v1"


@dataclass
class CutTrack:
    position: int
    title: str
    encore: bool
    filename: str
    duration: int | None


def source_key(relpath: str) -> str:
    stem = Path(relpath).stem
    return f"{slugify(stem)[:60]}-{hashlib.sha1(relpath.encode()).hexdigest()[:8]}"


def signature(audio: Path, songs: list[Song]) -> str:
    st = audio.stat()
    h = hashlib.sha256()
    h.update(f"{CUT_VERSION}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    for s in songs:
        h.update(f"{s.start:.3f}|{s.title}|{int(s.encore)}\n".encode())
    return h.hexdigest()


def _filenames(songs: list[Song], ext: str) -> list[str]:
    return [f"{i:02d}-{slugify(s.title)[:50]}{ext}" for i, s in enumerate(songs, 1)]


def ensure_cut(audio: Path, songs: list[Song], tracks_dir: Path, key: str) -> tuple[list[CutTrack], bool]:
    """Return the cut tracks and whether we (re)cut this run."""
    ext = audio.suffix.lower()
    names = _filenames(songs, ext)
    out = tracks_dir / key
    sig = signature(audio, songs)
    sig_file = out / ".signature"
    fresh = (
        sig_file.is_file()
        and sig_file.read_text().strip() == sig
        and all((out / n).is_file() for n in names)
    )
    recut = False
    if not fresh:
        tmp = tracks_dir / f".tmp-{key}-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            for i, (s, name) in enumerate(zip(songs, names)):
                cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                       "-ss", f"{s.start:.3f}", "-i", str(audio)]
                if i + 1 < len(songs):
                    cmd += ["-t", f"{songs[i + 1].start - s.start:.3f}"]
                cmd += ["-map", "0:a:0", "-c", "copy", "-map_metadata", "-1",
                        "-metadata", f"title={s.title}", "-metadata", "artist=BLAHSUM",
                        "-metadata", f"track={i + 1}", str(tmp / name)]
                subprocess.run(cmd, check=True, timeout=600, capture_output=True)
            (tmp / ".signature").write_text(sig + "\n")
            shutil.rmtree(out, ignore_errors=True)
            tmp.rename(out)
            recut = True
        except subprocess.CalledProcessError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            msg = (e.stderr or b"").decode(errors="replace").strip().splitlines()[-1:] or ["ffmpeg failed"]
            raise RuntimeError(f"cutting failed: {msg[0][:200]}") from None
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    cut = [
        CutTrack(position=i, title=s.title, encore=s.encore, filename=n,
                 duration=probe(out / n).duration_seconds)
        for i, (s, n) in enumerate(zip(songs, names), 1)
    ]
    return cut, recut


def remove_unused(tracks_dir: Path, keep: set[str]) -> list[str]:
    """Delete cut folders we created that no current show uses."""
    removed = []
    if not tracks_dir.is_dir():
        return removed
    for d in tracks_dir.iterdir():
        if not d.is_dir():
            continue
        if d.name.startswith(".tmp-"):
            shutil.rmtree(d, ignore_errors=True)
            continue
        if d.name not in keep and (d / ".signature").is_file():
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return removed
