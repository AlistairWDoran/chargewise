"""Build the NAS deploy bundle at %TEMP%\\chargewise-nas.tgz.

Windows tar's pax headers break the NAS's GNU tar, so the bundle is built
with Python tarfile using GNU_FORMAT (see docs/PROJECT-STATUS.md, hard-won
lessons). Contents: backend/ (minus .env, data/, caches, egg-info) plus
docker-compose.nas.yml. Feed the result to scripts/deploy-nas.py.

Run with any Python 3.9+:  python scripts/build-nas-bundle.py
"""

from __future__ import annotations

import io
import os
import tarfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.environ.get("TEMP", "/tmp"), "chargewise-nas.tgz")

EXCLUDE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "htmlcov",
    "data",
    "chargewise.egg-info",
}
EXCLUDE_FILES = {".env", "chargewise.sqlite", ".coverage"}


def _needs_lf(name: str) -> bool:
    """Files the Linux container executes or parses line by line.

    A Windows checkout can leave these with CRLF endings (4 Oct 2026:
    scheduler.sh in the working folder had them), and `sh` then fails on
    the stray carriage returns. They are written into the bundle with LF.
    """
    base = os.path.basename(name)
    return base.endswith((".sh", ".yml", ".yaml")) or base == "Dockerfile"


def _add_lf(tar: tarfile.TarFile, path: str, arcname: str) -> None:
    with open(path, "rb") as fh:
        data = fh.read().replace(b"\r\n", b"\n")
    ti = tarfile.TarInfo(arcname)
    ti.size = len(data)
    ti.mtime = int(os.path.getmtime(path))
    ti.mode = 0o755 if arcname.endswith(".sh") else 0o644
    tar.addfile(ti, io.BytesIO(data))


def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = ti.name.split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return None
    base = os.path.basename(ti.name)
    if base in EXCLUDE_FILES or base.endswith((".env", ".log", ".sqlite")):
        return None
    if ti.isfile() and _needs_lf(ti.name):
        return None  # added separately with LF endings (see main)
    # Normalise modes: Windows file attributes once produced a read-only
    # tree on the NAS, which broke subsequent re-extraction. Dirs and shell
    # scripts 755, everything else 644.
    if ti.isdir() or ti.name.endswith(".sh"):
        ti.mode = 0o755
    else:
        ti.mode = 0o644
    return ti


def main() -> None:
    with tarfile.open(OUT, "w:gz", format=tarfile.GNU_FORMAT) as tar:
        backend = os.path.join(REPO, "backend")
        tar.add(backend, arcname="backend", filter=_filter)
        for name in sorted(os.listdir(backend)):
            path = os.path.join(backend, name)
            if os.path.isfile(path) and _needs_lf(name):
                _add_lf(tar, path, f"backend/{name}")
        _add_lf(tar, os.path.join(REPO, "docker-compose.nas.yml"), "docker-compose.nas.yml")
    with tarfile.open(OUT) as tar:
        names = tar.getnames()
        crlf = [
            m.name for m in tar.getmembers()
            if m.isfile() and _needs_lf(m.name) and b"\r" in tar.extractfile(m).read()  # type: ignore[union-attr]
        ]
    if crlf:
        raise SystemExit(f"bundle still has CRLF line endings in: {crlf} - aborting")
    ok = "backend/Dockerfile" in names and "backend/scheduler.sh" in names
    print(f"bundle -> {OUT}")
    print(f"  entries={len(names)} dockerfile+scheduler present={ok}")
    if not ok:
        raise SystemExit("bundle is missing Dockerfile or scheduler.sh — aborting")


if __name__ == "__main__":
    main()
