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


def test_second_run_with_same_state_finds_nothing_new(make_env, fixtures_dir):
    registry.register(FakeAdapter())
    cfg, data, out = make_env()
    assert run(cfg, out, fixtures_dir) == 0
    assert len(result(out)["sources"]["fake"]["items"]) == 2
    assert run(cfg, out, fixtures_dir) == 0
    assert result(out)["sources"]["fake"]["items"] == []


def test_raising_adapter_is_logged_and_counted_and_others_still_run(make_env, fixtures_dir):
    registry.register(FakeAdapter("boom", fail_list=True))
    registry.register(FakeAdapter("fake"))
    (fixtures_dir / "boom").mkdir()
    cfg, data, out = make_env(sources=("boom", "fake"))
    assert run(cfg, out, fixtures_dir) == 0
    res = result(out)
    assert res["sources"]["boom"]["status"] == "failed"
    assert "boom exploded" in res["sources"]["boom"]["error"]
    assert res["sources"]["fake"]["status"] == "ok"
    assert len(res["sources"]["fake"]["items"]) == 2
    logs = list((data / "logs").glob("*.log"))
    assert logs and "boom exploded" in logs[0].read_text()
    assert "Traceback" in logs[0].read_text()


def test_one_bad_item_fails_the_module_but_keeps_other_items(make_env, fixtures_dir):
    registry.register(FakeAdapter(fail_details_for={"a1"}))
    cfg, data, out = make_env()
    assert run(cfg, out, fixtures_dir) == 0
    src = result(out)["sources"]["fake"]
    assert src["status"] == "failed"
    assert [i["id"] for i in src["items"]] == ["a2"]
    # the failed item is retried next run; the good one is not reprocessed
    registry.register(FakeAdapter())
    assert run(cfg, out, fixtures_dir) == 0
    assert [i["id"] for i in result(out)["sources"]["fake"]["items"]] == ["a1"]


def test_failure_counters_persist_and_two_consecutive_failures_are_flagged(make_env, fixtures_dir):
    (fixtures_dir / "boom").mkdir()
    registry.register(FakeAdapter("boom", fail_list=True))
    cfg, data, out = make_env(sources=("boom",))
    run(cfg, out, fixtures_dir)
    first = result(out)
    assert first["sources"]["boom"]["consecutive_failures"] == 1
    assert first["flagged"] == []
    run(cfg, out, fixtures_dir)
    second = result(out)
    assert second["sources"]["boom"]["consecutive_failures"] == 2
    assert second["flagged"] == ["boom"]
    # a success resets the streak
    registry.register(FakeAdapter("boom"))
    (fixtures_dir / "boom" / "items.json").write_text("[]")
    run(cfg, out, fixtures_dir)
    third = result(out)
    assert third["sources"]["boom"]["consecutive_failures"] == 0
    assert third["flagged"] == []
