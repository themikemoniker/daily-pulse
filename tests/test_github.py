import json

import pytest

from daily_pulse.adapters import registry
from daily_pulse.adapters.github import GitHubAdapter
from daily_pulse.cli import main

GITHUB_CFG = """
[sources.github]
enabled = true
rising_days = 7
watch_accounts = ["alice"]
watch_repos = ["bob/tool"]
"""


@pytest.fixture(autouse=True)
def register_github():
    registry.register(GitHubAdapter())


def run(make_env, fixtures, date="2026-01-15"):
    cfg, data, out = make_env(sources=(), extra=GITHUB_CFG)
    code = main(["run", "--config", str(cfg), "--fixtures", str(fixtures), "--out", str(out),
                 "--date", date])
    res = json.loads((out / f"run-{date}.json").read_text()) if (out / f"run-{date}.json").exists() else None
    return code, res


def items_by_id(res):
    return {i["id"]: i for i in res["sources"]["github"]["items"]}


def test_first_run_reports_rising_new_release_and_push_items(make_env, fixtures_dir):
    code, res = run(make_env, fixtures_dir)
    assert code == 0
    assert res["sources"]["github"]["status"] == "ok"
    assert set(items_by_id(res)) == {
        "rising:newrepo/agent-evals",
        "rising:other/gizmo",
        "repo:alice/brand-new",
        "push:alice/active:2026-01-14",
        "release:alice/proj:v1.2.0",
        "release:bob/tool:v0.3.0",
    }
