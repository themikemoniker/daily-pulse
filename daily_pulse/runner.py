"""Runs each enabled source adapter in isolation and collects a RunResult."""
from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from daily_pulse.adapters import registry
from daily_pulse.adapters.base import AdapterContext
from daily_pulse.config import Config
from daily_pulse.state import FailureCounters, StateError, StateStore, serialize_json

log = logging.getLogger("daily_pulse")


@dataclass
class RunResult:
    date: str
    sources: dict[str, dict] = field(default_factory=dict)
    flagged: list[str] = field(default_factory=list)
    error: str | None = None  # set when a state error aborted the run part-way
    generated_at: str = field(  # when the run happened (UTC); the digest measures item ages from it
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        out = {"date": self.date, "generated_at": self.generated_at, "sources": self.sources, "flagged": self.flagged}
        if self.error:
            out["error"] = self.error
        return out


def _run_adapter(name, adapter, ctx, state, first_run_cap, items: list) -> Exception | None:
    """Process one adapter's new items into `items`. Adapter exceptions are returned (first
    one), never raised. StateError (our own state I/O) propagates: it is not the adapter's fault."""
    try:
        candidates = adapter.list_new(ctx)
        new, seen = [], set()
        for c in candidates:
            if c.id not in seen and not state.is_processed(name, c.id):
                seen.add(c.id)
                new.append(c)
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        log.error("%s: listing failed\n%s", name, traceback.format_exc())
        return exc
    if state.is_first_run(name):
        # Don't chew through a backlog: keep the newest few, and mark the rest as seen
        # (in one write) so later runs only handle genuinely new items.
        state.mark_processed(name, *(c.id for c in new[first_run_cap:]))
        new = new[:first_run_cap]
    error = None
    for candidate in new:
        try:
            item = adapter.fetch_details(candidate, ctx)
            # Prove the item can be reported *before* it is marked processed, so a bad
            # item is retried next run instead of being consumed and lost.
            serialize_json(item.to_dict())
        except Exception as exc:  # noqa: BLE001
            log.error("%s: item %s failed\n%s", name, candidate.id, traceback.format_exc())
            error = error or exc
            continue
        state.mark_processed(name, candidate.id)
        items.append(item)
    return error


def _run_source(name, source_cfg, config, ctx, state, failures, result) -> None:
    if not source_cfg.get("enabled", True):
        result.sources[name] = {"status": "disabled", "items": []}
        return
    adapter = registry.get(name)
    if adapter is None:
        result.sources[name] = {"status": "not configured: no adapter registered", "items": []}
        return
    items: list = []
    # Keep what was collected even if a StateError aborts the run part-way.
    entry: dict = {"status": "aborted", "items": items}
    result.sources[name] = entry
    try:
        status = adapter.status(ctx)
    except Exception as exc:  # noqa: BLE001
        log.error("%s: status failed\n%s", name, traceback.format_exc())
        status, error = None, exc
    else:
        if status is not None:
            result.sources[name] = {"status": status, "items": []}
            return
        error = _run_adapter(name, adapter, ctx, state, config.first_run_cap, items)
    entry["items"] = [i.to_dict() for i in items]
    if error is None:
        failures.record_success(name)
        entry["status"] = "ok"
    else:
        failures.record_failure(name, f"{type(error).__name__}: {error}")
        entry["status"] = "failed"
        entry["error"] = f"{type(error).__name__}: {error}"
    entry["consecutive_failures"] = failures.consecutive(name)
    if entry["consecutive_failures"] >= config.alert_after_failures:
        result.flagged.append(name)


def run_pipeline(
    config: Config, date: str, fixtures_dir: Path | None, dry_run: bool = False
) -> RunResult:
    """Run every enabled adapter. StateError while reading state at startup propagates;
    one raised mid-run stops the run but is returned in result.error with what was done."""
    result = RunResult(date=date)
    state = StateStore(config.data_dir, persist=not dry_run)
    failures = FailureCounters(config.data_dir, persist=not dry_run)
    for name, source_cfg in config.sources.items():
        ctx = AdapterContext(config=source_cfg, fixtures_dir=fixtures_dir, date=date)
        try:
            _run_source(name, source_cfg, config, ctx, state, failures, result)
        except StateError as exc:
            log.error("state error, aborting run\n%s", traceback.format_exc())
            partial = result.sources.get(name)
            if partial is not None and partial["status"] == "aborted":
                partial["items"] = [i.to_dict() if hasattr(i, "to_dict") else i for i in partial["items"]]
            result.error = str(exc)
            break
    return result
