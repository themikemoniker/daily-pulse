"""Local state under the configured data dir: processed IDs per source. Written atomically."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


class StateStore:
    def __init__(self, data_dir: Path, persist: bool = True):
        self._persist = persist
        self._processed_path = Path(data_dir) / "state" / "processed.json"
        self._processed: dict[str, list[str]] = _read_json(self._processed_path)

    def is_first_run(self, source: str) -> bool:
        return source not in self._processed

    def is_processed(self, source: str, item_id: str) -> bool:
        return item_id in self._processed.get(source, [])

    def begin_source(self, source: str) -> None:
        """Mark a source as seen so later runs are not 'first runs'."""
        if source not in self._processed:
            self._processed[source] = []
            self._flush()

    def mark_processed(self, source: str, item_id: str) -> None:
        """Record one item and flush to disk immediately."""
        ids = self._processed.setdefault(source, [])
        if item_id not in ids:
            ids.append(item_id)
        self._flush()

    def _flush(self) -> None:
        if self._persist:
            _atomic_write_json(self._processed_path, self._processed)


class FailureCounters:
    """Per-module failure counters that persist across runs."""

    def __init__(self, data_dir: Path, persist: bool = True):
        self._persist = persist
        self._path = Path(data_dir) / "state" / "failures.json"
        self._counters: dict[str, dict] = _read_json(self._path)

    def consecutive(self, module: str) -> int:
        return self._counters.get(module, {}).get("consecutive", 0)

    def record_failure(self, module: str, error: str) -> int:
        entry = self._counters.setdefault(module, {"consecutive": 0, "total": 0})
        entry["consecutive"] += 1
        entry["total"] += 1
        entry["last_error"] = error
        self._flush()
        return entry["consecutive"]

    def record_success(self, module: str) -> None:
        entry = self._counters.get(module)
        if entry and entry["consecutive"]:
            entry["consecutive"] = 0
            self._flush()

    def _flush(self) -> None:
        if self._persist:
            _atomic_write_json(self._path, self._counters)
