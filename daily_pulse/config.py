"""TOML config loading and validation."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path("config/daily-pulse.toml")


class ConfigError(Exception):
    """The config file is readable TOML but its contents are unusable."""


@dataclass
class Config:
    data_dir: Path
    first_run_cap: int = 10
    alert_after_failures: int = 2
    base_url: str = ""  # public site root; digest links are built from it
    sources: dict[str, dict] = field(default_factory=dict)
    claude_cmd: str = "claude"
    max_claude_calls: int = 30


def _table(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _int(table: dict, key: str, default: int, minimum: int, label: str) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label} must be an integer, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{label} must be >= {minimum}, got {value}")
    return value


def load_config(path: Path) -> Config:
    """A relative paths.data_dir is resolved against the config file's directory, so cron
    started from any cwd uses the same state and lock."""
    path = Path(path)
    raw = tomllib.loads(path.read_text())
    run, paths, sources = _table(raw, "run"), _table(raw, "paths"), _table(raw, "sources")
    site = _table(raw, "site")
    base_url = site.get("base_url", "")
    if not isinstance(base_url, str):
        raise ConfigError(f"site.base_url must be a string, got {base_url!r}")
    summarizer = _table(raw, "summarizer")
    claude_cmd = summarizer.get("claude_cmd", "claude")
    if not isinstance(claude_cmd, str) or not claude_cmd.strip():
        raise ConfigError(f"summarizer.claude_cmd must be a non-empty string, got {claude_cmd!r}")
    data_dir = paths.get("data_dir", "data")
    if not isinstance(data_dir, str) or not data_dir:
        raise ConfigError(f"paths.data_dir must be a non-empty string, got {data_dir!r}")
    for name, source in sources.items():
        if not isinstance(source, dict):
            raise ConfigError(f"[sources.{name}] must be a table")
        if not isinstance(source.get("enabled", True), bool):
            raise ConfigError(f"sources.{name}.enabled must be true or false")
    return Config(
        data_dir=path.parent / data_dir,
        first_run_cap=_int(run, "first_run_cap", 10, 0, "run.first_run_cap"),
        alert_after_failures=_int(run, "alert_after_failures", 2, 1, "run.alert_after_failures"),
        base_url=base_url.strip(),
        sources=sources,
        claude_cmd=claude_cmd,
        max_claude_calls=_int(summarizer, "max_calls", 30, 0, "summarizer.max_calls"),
    )
