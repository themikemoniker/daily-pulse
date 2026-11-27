"""Runs each enabled source adapter in isolation and collects a RunResult."""
from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from daily_pulse.adapters import registry
from daily_pulse.adapters.base import AdapterContext
from daily_pulse.config import Config
from daily_pulse.state import FailureCounters, StateStore

log = logging.getLogger("daily_pulse")


@dataclass
class RunResult:
    date: str
    sources: dict[str, dict] = field(default_factory=dict)
    flagged: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"date": self.date, "sources": self.sources, "flagged": self.flagged}


def _run_adapter(name, adapter, ctx, state, first_run_cap) -> tuple[list, Exception | None]:
    """Process one adapter's new items. Never raises; returns (items, first_error)."""
    items, error = [], None
    try:
        candidates = adapter.list_new(ctx)
        new, seen = [], set()
        for c in candidates:
            if c.id not in seen and not state.is_processed(name, c.id):
                seen.add(c.id)
                new.append(c)
        if state.is_first_run(name):
            # Don't chew through a backlog: keep the newest few, and mark the rest as seen
            # (in one write) so later runs only handle genuinely new items.
            state.mark_processed(name, *(c.id for c in new[first_run_cap:]))
            new = new[:first_run_cap]
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        log.error("%s: listing failed\n%s", name, traceback.format_exc())
        return items, exc
    for candidate in new:
        try:
            items.append(adapter.fetch_details(candidate, ctx))
            state.mark_processed(name, candidate.id)
        except Exception as exc:  # noqa: BLE001
            log.error("%s: item %s failed\n%s", name, candidate.id, traceback.format_exc())
            error = error or exc
    return items, error


def run_pipeline(
    config: Config, date: str, fixtures_dir: Path | None, dry_run: bool = False
) -> RunResult:
    result = RunResult(date=date)
    state = StateStore(config.data_dir, persist=not dry_run)
    failures = FailureCounters(config.data_dir, persist=not dry_run)
    for name, source_cfg in config.sources.items():
        if not source_cfg.get("enabled", True):
            result.sources[name] = {"status": "disabled", "items": []}
            continue
        adapter = registry.get(name)
        if adapter is None:
            result.sources[name] = {"status": "not configured: no adapter registered", "items": []}
            continue
        ctx = AdapterContext(config=source_cfg, fixtures_dir=fixtures_dir, date=date)
        try:
            status = adapter.status(ctx)
        except Exception as exc:  # noqa: BLE001
            log.error("%s: status failed\n%s", name, traceback.format_exc())
            status, items, error = None, [], exc
        else:
            if status is not None:
                result.sources[name] = {"status": status, "items": []}
                continue
            items, error = _run_adapter(name, adapter, ctx, state, config.first_run_cap)
        entry: dict = {"items": [i.to_dict() for i in items]}
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
        result.sources[name] = entry
    return result
