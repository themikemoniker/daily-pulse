"""Run lock: a flock on data/run.lock so two runs cannot overlap.

The kernel drops the lock when the process dies, so a crashed run never leaves a stale lock."""
from __future__ import annotations

import fcntl
from pathlib import Path


class LockHeld(Exception):
    pass


class RunLock:
    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "run.lock"
        self._fh = None

    def __enter__(self) -> "RunLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise LockHeld(str(self._path)) from None
        self._fh = fh
        return self

    def __exit__(self, *exc) -> None:
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None
