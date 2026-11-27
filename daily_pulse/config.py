"""TOML config loading."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path("config/daily-pulse.toml")


@dataclass
class Config:
    data_dir: Path
    first_run_cap: int = 10
    alert_after_failures: int = 2
    sources: dict[str, dict] = field(default_factory=dict)


def load_config(path: Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    run = raw.get("run", {})
    return Config(
        data_dir=Path(raw.get("paths", {}).get("data_dir", "data")),
        first_run_cap=int(run.get("first_run_cap", 10)),
        alert_after_failures=int(run.get("alert_after_failures", 2)),
        sources=raw.get("sources", {}),
    )
