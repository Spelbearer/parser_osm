from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path


def configure_utf8():
    """Keep JSON, worker pipes and logs independent of Windows console encoding."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def cleanup_index(path: Path):
    """Delete a disposable index, deferring only Windows sharing violations.

    libosmium's named file map can retain its native descriptor until process
    exit. This is independent of whether parsing succeeded. Never suppress
    permission errors or I/O failures for application data.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        if getattr(error, "winerror", None) not in {32, 33}:
            raise
        logging.warning("Index still held by libosmium; cleanup deferred until process exit/next run: %s", path)


@contextmanager
def process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Kernel locks are released on a crash; the persistent file is not a stale lock.
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("Another POI process is already running") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_json(path: Path, payload: dict):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
