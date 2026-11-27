import json
from pathlib import Path

import pytest

from daily_pulse.adapters import registry
from daily_pulse.adapters.base import SourceAdapter
from daily_pulse.models import Item

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    """Each test starts with an empty adapter registry."""
    monkeypatch.setattr(registry, "_ADAPTERS", {})


class FakeAdapter(SourceAdapter):
    """Reads items from <fixtures>/<name>/items.json. No network, no subprocess."""

    def __init__(self, name="fake", fail_list=False, fail_details_for=()):
        self.name = name
        self.fail_list = fail_list
        self.fail_details_for = set(fail_details_for)

    def list_new(self, ctx):
        if self.fail_list:
            raise RuntimeError(f"{self.name} exploded")
        raw = json.loads((ctx.fixtures_dir / self.name / "items.json").read_text())
        return [
            Item(source=self.name, id=r["id"], url=r["url"], title=r["title"], author=r["author"])
            for r in raw
        ]

    def fetch_details(self, item, ctx):
        if item.id in self.fail_details_for:
            raise RuntimeError(f"details failed for {item.id}")
        item.text = f"excerpt for {item.id}"
        return item


@pytest.fixture
def make_env(tmp_path):
    """Write a config enabling the given sources; return paths + a run() helper."""

    def _make(sources=("fake",), first_run_cap=10, extra=""):
        data_dir = tmp_path / "state-dir"
        cfg = tmp_path / "config.toml"
        body = f'[run]\nfirst_run_cap = {first_run_cap}\nalert_after_failures = 2\n\n'
        body += f'[paths]\ndata_dir = "{data_dir}"\n\n'
        for s in sources:
            body += f"[sources.{s}]\nenabled = true\n\n"
        cfg.write_text(body + extra)
        return cfg, data_dir, tmp_path / "out"

    return _make


@pytest.fixture
def fixtures_dir():
    return FIXTURES
