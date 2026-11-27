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


def test_concurrent_run_exits_cleanly_with_message_and_leaves_state_alone(
    make_env, fixtures_dir, capsys
):
    inner = {}

    class Reentrant(FakeAdapter):
        def list_new(self, ctx):
            # While the outer run holds the lock, start a second run through the CLI.
            inner["code"] = run(cfg, tmp_out / "inner", fixtures_dir)
            return super().list_new(ctx)

    registry.register(Reentrant())
    cfg, data, out = make_env()
    tmp_out = out
    assert run(cfg, out, fixtures_dir) == 0
    assert inner["code"] == 0
    assert "another run is in progress" in capsys.readouterr().err
    assert not (out / "inner").exists()  # the blocked run produced nothing
    assert [i["id"] for i in result(out)["sources"]["fake"]["items"]] == ["a1", "a2"]


def test_lock_is_released_after_a_run(make_env, fixtures_dir, capsys):
    registry.register(FakeAdapter())
    cfg, data, out = make_env()
    run(cfg, out, fixtures_dir)
    run(cfg, out, fixtures_dir)
    assert "another run is in progress" not in capsys.readouterr().err


def test_deferred_and_unconfigured_adapters_record_status_without_running(make_env, fixtures_dir):
    class Deferred(FakeAdapter):
        def status(self, ctx):
            return "deferred: no route to read other accounts"

        def list_new(self, ctx):
            raise AssertionError("must not be called")

    class Unconfigured(Deferred):
        def status(self, ctx):
            return "not configured"

    registry.register(Deferred("x"))
    registry.register(Unconfigured("youtube"))
    registry.register(FakeAdapter())
    cfg, data, out = make_env(sources=("x", "youtube", "fake", "ghost"))
    assert run(cfg, out, fixtures_dir) == 0
    srcs = result(out)["sources"]
    assert srcs["x"]["status"] == "deferred: no route to read other accounts"
    assert srcs["youtube"]["status"] == "not configured"
    assert srcs["ghost"]["status"] == "not configured: no adapter registered"
    assert srcs["fake"]["status"] == "ok"
    assert result(out)["flagged"] == []


def test_disabled_sources_are_skipped(make_env, fixtures_dir):
    registry.register(FakeAdapter())
    cfg, data, out = make_env(sources=(), extra="[sources.fake]\nenabled = false\n")
    assert run(cfg, out, fixtures_dir) == 0
    assert result(out)["sources"]["fake"]["status"] == "disabled"
    assert result(out)["sources"]["fake"]["items"] == []


def test_first_run_is_capped_and_the_backlog_is_not_processed_later(make_env, fixtures_dir):
    registry.register(FakeAdapter())
    cfg, data, out = make_env(first_run_cap=1)
    run(cfg, out, fixtures_dir)
    assert [i["id"] for i in result(out)["sources"]["fake"]["items"]] == ["a1"]  # newest first
    run(cfg, out, fixtures_dir)
    assert result(out)["sources"]["fake"]["items"] == []


def test_later_runs_process_all_new_items_uncapped(make_env, fixtures_dir):
    registry.register(FakeAdapter())
    cfg, data, out = make_env(first_run_cap=1)
    run(cfg, out, fixtures_dir)
    items = json.loads((fixtures_dir / "fake" / "items.json").read_text())
    items = [{"id": f"n{k}", "title": "t", "url": "u", "author": "a"} for k in range(3)] + items
    (fixtures_dir / "fake" / "items.json").write_text(json.dumps(items))
    run(cfg, out, fixtures_dir)
    assert [i["id"] for i in result(out)["sources"]["fake"]["items"]] == ["n0", "n1", "n2"]


def test_dry_run_reports_but_writes_nothing_and_does_not_mark_items_processed(
    make_env, fixtures_dir
):
    registry.register(FakeAdapter())
    cfg, data, out = make_env()
    assert run(cfg, out, fixtures_dir, "--dry-run") == 0
    assert not data.exists()
    assert not out.exists()
    assert run(cfg, out, fixtures_dir) == 0
    assert len(result(out)["sources"]["fake"]["items"]) == 2


def test_nothing_is_written_to_cwd_data_outside_the_configured_path(
    make_env, fixtures_dir, tmp_path, monkeypatch
):
    registry.register(FakeAdapter())
    cfg, data, out = make_env()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    run(cfg, out, fixtures_dir)
    assert list(cwd.iterdir()) == []
    assert (data / "state" / "processed.json").exists()


def test_run_never_prompts(make_env, fixtures_dir, monkeypatch):
    import builtins
    import sys

    def no_prompt(*a, **k):
        raise AssertionError("interactive prompt attempted")

    monkeypatch.setattr(builtins, "input", no_prompt)
    monkeypatch.setattr(sys, "stdin", None)
    registry.register(FakeAdapter())
    cfg, data, out = make_env()
    assert run(cfg, out, fixtures_dir) == 0


def test_shipped_config_runs_and_has_the_spec_seed_values(tmp_path, fixtures_dir, monkeypatch):
    from pathlib import Path

    shipped = Path(__file__).parent.parent / "config" / "daily-pulse.toml"
    monkeypatch.chdir(tmp_path)  # the relative data_dir lands here, not in the repo
    out = tmp_path / "out"
    assert run(shipped, out, fixtures_dir) == 0
    assert (tmp_path / "data" / "logs").exists()
    from daily_pulse.config import load_config

    cfg = load_config(shipped)
    assert cfg.first_run_cap == 10
    assert {"karpathy", "can1357", "langchain-ai"} <= set(cfg.sources["github"]["watch_accounts"])
    assert "Simon Willison" in cfg.sources["x"]["handles"]
    assert cfg.sources["youtube"]["playlist_url"] == ""


def test_bad_date_is_rejected_before_anything_is_written(make_env, fixtures_dir, capsys):
    import pytest

    registry.register(FakeAdapter())
    cfg, data, out = make_env()
    with pytest.raises(SystemExit) as exc:
        main(["run", "--config", str(cfg), "--fixtures", str(fixtures_dir), "--out", str(out),
              "--date", "notadate"])
    assert exc.value.code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err
    assert not data.exists() and not out.exists()


def test_missing_config_is_a_clear_error_not_a_traceback(tmp_path, fixtures_dir, capsys):
    code = main(["run", "--config", str(tmp_path / "nope.toml"), "--out", str(tmp_path / "o")])
    err = capsys.readouterr().err
    assert code == 1
    assert "nope.toml" in err and "Traceback" not in err


def test_corrupt_state_fails_loudly_without_reprocessing_or_overwriting(
    make_env, fixtures_dir, capsys
):
    registry.register(FakeAdapter())
    cfg, data, out = make_env()
    (data / "state").mkdir(parents=True)
    bad = data / "state" / "processed.json"
    bad.write_text("{not json")
    code = run(cfg, out, fixtures_dir)
    err = capsys.readouterr().err
    assert code == 1
    assert "processed.json" in err and "Traceback" not in err
    assert bad.read_text() == "{not json"  # left for the operator to inspect
    assert not out.exists()
