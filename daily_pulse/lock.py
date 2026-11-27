"""Run lock: a flock on data/run.lock so two runs cannot overlap.

The kernel drops the lock when the process dies, so a crashed run never leaves a stale lock."""
from __future__ import annotations

import fcntl
from pathlib import Path


class LockHeld(Exception):
    """Another run holds the lock."""


class LockError(Exception):
    """The lock could not be taken for a reason other than another run holding it."""


class RunLock:
    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "run.lock"
        self._fh = None

    def __enter__(self) -> "RunLock":
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self._path, "w")
        except OSError as exc:
            raise LockError(f"cannot create run lock {self._path}: {exc}") from exc
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise LockHeld(str(self._path)) from None
        except OSError as exc:
            fh.close()
            raise LockError(f"cannot take run lock {self._path}: {exc}") from exc
        self._fh = fh
        return self

    def __exit__(self, *exc) -> None:
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None
