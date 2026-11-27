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


def _run_adapter(name, adapter, ctx, state) -> tuple[list, Exception | None]:
    """Process one adapter's new items. Never raises; returns (items, first_error)."""
    items, error = [], None
    try:
        candidates = adapter.list_new(ctx)
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        log.error("%s: list_new failed\n%s", name, traceback.format_exc())
        return items, exc
    for candidate in candidates:
        if state.is_processed(name, candidate.id):
            continue
        try:
            items.append(adapter.fetch_details(candidate, ctx))
        except Exception as exc:  # noqa: BLE001
            log.error("%s: fetch_details failed for %s\n%s", name, candidate.id, traceback.format_exc())
            error = error or exc
            continue
        state.mark_processed(name, candidate.id)
    return items, error


def run_pipeline(config: Config, date: str, fixtures_dir: Path | None) -> RunResult:
    result = RunResult(date=date)
    state = StateStore(config.data_dir)
    failures = FailureCounters(config.data_dir)
    for name, source_cfg in config.sources.items():
        adapter = registry.get(name)
        ctx = AdapterContext(config=source_cfg, fixtures_dir=fixtures_dir, date=date)
        items, error = _run_adapter(name, adapter, ctx, state)
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
