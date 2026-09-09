from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("scanner.audio")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_mp3(src: Path, dest: Path, *, cheap: bool = False) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg nicht gefunden. Bitte ffmpeg installieren.")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-b:a",
        "128k",
    ]
    if cheap:
        cmd.extend(["-t", "45"])
    cmd.append(str(dest))
    log.info("ffmpeg %s → %s", src.name, dest.name)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg fehlgeschlagen: {proc.stderr[-400:]}")
    return dest
