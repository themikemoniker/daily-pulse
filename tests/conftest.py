import json
import shutil
import sys
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
def fixtures_dir(tmp_path):
    """A per-test copy of tests/fixtures, safe to add to."""
    dest = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, dest)
    return dest


FAKE_CLAUDE = '''#!{python}
import json, pathlib, sys
d = pathlib.Path({dir!r})
n = int((d / "count").read_text()) if (d / "count").exists() else 0
(d / "count").write_text(str(n + 1))
(d / f"argv-{{n}}.json").write_text(json.dumps(sys.argv[1:]))
(d / f"stdin-{{n}}.txt").write_text(sys.stdin.read())
responses = json.loads((d / "responses.json").read_text())
r = responses[min(n, len(responses) - 1)]
if isinstance(r, dict) and "exit" in r:
    sys.stderr.write("boom")
    sys.exit(r["exit"])
sys.stdout.write(r if isinstance(r, str) else json.dumps(r))
'''


class FakeClaude:
    """A stand-in `claude` executable: replies from a canned list (the last one repeats) and
    records every call's argv and stdin."""

    def __init__(self, directory: Path, responses):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "responses.json").write_text(json.dumps(responses))
        self.path = self.dir / "fake-claude"
        self.path.write_text(FAKE_CLAUDE.format(python=sys.executable, dir=str(self.dir)))
        self.path.chmod(0o755)

    @property
    def calls(self) -> int:
        count = self.dir / "count"
        return int(count.read_text()) if count.exists() else 0

    def stdin(self, n: int = 0) -> str:
        return (self.dir / f"stdin-{n}.txt").read_text()

    def argv(self, n: int = 0) -> list[str]:
        return json.loads((self.dir / f"argv-{n}.json").read_text())


@pytest.fixture
def fake_claude(tmp_path):
    def _make(*responses):
        return FakeClaude(tmp_path / "fake-claude-dir", list(responses))

    return _make
