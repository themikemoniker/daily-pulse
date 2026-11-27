"""Runs each enabled source adapter in isolation and collects a RunResult."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from daily_pulse.adapters import registry
from daily_pulse.adapters.base import AdapterContext
from daily_pulse.config import Config
from daily_pulse.state import StateStore


@dataclass
class RunResult:
    date: str
    sources: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"date": self.date, "sources": self.sources}


def run_pipeline(config: Config, date: str, fixtures_dir: Path | None) -> RunResult:
    result = RunResult(date=date)
    state = StateStore(config.data_dir)
    for name, source_cfg in config.sources.items():
        adapter = registry.get(name)
        ctx = AdapterContext(config=source_cfg, fixtures_dir=fixtures_dir, date=date)
        items = []
        for candidate in adapter.list_new(ctx):
            if state.is_processed(name, candidate.id):
                continue
            items.append(adapter.fetch_details(candidate, ctx))
            state.mark_processed(name, candidate.id)
        result.sources[name] = {"status": "ok", "items": [i.to_dict() for i in items]}
    return result
