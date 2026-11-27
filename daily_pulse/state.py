"""Local state under the configured data dir: processed IDs per source. Written atomically."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def serialize_json(payload: dict, default=None) -> str:
    """The one serialization used by every state/result writer and by the pre-commit item check."""
    return json.dumps(payload, indent=2, sort_keys=True, default=default)


def atomic_write_json(path: Path, payload: dict, default=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(serialize_json(payload, default))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class StateError(Exception):
    """State on disk is unreadable. Never guess: resetting would reprocess everything."""


def _valid_counter(entry: dict) -> bool:
    return all(
        isinstance(entry.get(k, 0), int) and not isinstance(entry.get(k, 0), bool) and entry.get(k, 0) >= 0
        for k in ("consecutive", "total")
    )


def _valid_ids(entry: list) -> bool:
    return all(isinstance(i, str) for i in entry)


def _read_json(path: Path, value_type: type) -> dict:
    """Read a state file: an object whose values are all `value_type` (and well-formed)."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise StateError(f"cannot read state file {path}: {exc}. Fix or remove it and re-run.") from exc
    check = _valid_ids if value_type is list else _valid_counter
    if (
        not isinstance(data, dict)
        or not all(isinstance(v, value_type) for v in data.values())
        or not all(check(v) for v in data.values())
    ):
        raise StateError(
            f"state file {path} has an unexpected shape (expected an object of "
            f"{value_type.__name__}s). Fix or remove it and re-run."
        )
    return data


def _flush_json(path: Path, payload: dict) -> None:
    try:
        atomic_write_json(path, payload)
    except OSError as exc:
        raise StateError(f"cannot write state file {path}: {exc}") from exc


class StateStore:
    def __init__(self, data_dir: Path, persist: bool = True):
        self._persist = persist
        self._processed_path = Path(data_dir) / "state" / "processed.json"
        self._processed: dict[str, list[str]] = _read_json(self._processed_path, list)
        self._seen: dict[str, set[str]] = {k: set(v) for k, v in self._processed.items()}

    def is_first_run(self, source: str) -> bool:
        """True until the source has at least one processed item, so an empty first
        listing doesn't end the first-run cap."""
        return not self._processed.get(source)

    def is_processed(self, source: str, item_id: str) -> bool:
        return item_id in self._seen.get(source, ())

    def mark_processed(self, source: str, *item_ids: str) -> None:
        """Record items (one flush for the whole call, so pass one id per item as it completes)."""
        if not item_ids:
            return
        ids = self._processed.setdefault(source, [])
        seen = self._seen.setdefault(source, set())
        for item_id in item_ids:
            if item_id not in seen:
                seen.add(item_id)
                ids.append(item_id)
        self._flush()

    def _flush(self) -> None:
        if self._persist:
            _flush_json(self._processed_path, self._processed)


class FailureCounters:
    """Per-module failure counters that persist across runs."""

    def __init__(self, data_dir: Path, persist: bool = True):
        self._persist = persist
        self._path = Path(data_dir) / "state" / "failures.json"
        self._counters: dict[str, dict] = _read_json(self._path, dict)
        for entry in self._counters.values():  # partial entries get defaults
            entry.setdefault("consecutive", 0)
            entry.setdefault("total", 0)

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
            _flush_json(self._path, self._counters)
