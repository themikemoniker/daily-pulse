import json

from daily_pulse.adapters import registry
from daily_pulse.cli import main
from conftest import FakeAdapter


def run(cfg, out, fixtures, *extra):
    return main(["run", "--config", str(cfg), "--fixtures", str(fixtures), "--out", str(out),
                 "--date", "2026-01-15", *extra])


def result(out):
    return json.loads((out / "run-2026-01-15.json").read_text())


def test_run_on_fixtures_with_fake_adapter_exits_zero_and_reports_items(make_env, fixtures_dir):
    registry.register(FakeAdapter())
    cfg, data, out = make_env()
    assert run(cfg, out, fixtures_dir) == 0
    res = result(out)
    assert res["date"] == "2026-01-15"
    assert res["sources"]["fake"]["status"] == "ok"
    assert [i["id"] for i in res["sources"]["fake"]["items"]] == ["a1", "a2"]
    assert res["sources"]["fake"]["items"][0]["text"] == "excerpt for a1"
