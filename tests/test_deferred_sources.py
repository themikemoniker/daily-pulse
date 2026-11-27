import json
import logging
import sys

from daily_pulse.adapters import registry
from daily_pulse.adapters.instagram import InstagramAdapter
from daily_pulse.adapters.x import XAdapter
from daily_pulse.cli import main
from daily_pulse.models import Item
from daily_pulse.summarizer import Summarizer, summarize_items

DEFAULT_SOURCES = ("x", "instagram")


def run(cfg, out, fixtures):
    return main(["run", "--config", str(cfg), "--fixtures", str(fixtures), "--out", str(out),
                 "--date", "2026-01-15"])


def result(out):
    return json.loads((out / "run-2026-01-15.json").read_text())


def fake_ig_cmd(tmp_path, stdout, exit_code=0):
    """A stand-in IG CLI that prints canned stdout and records its argv."""
    script = tmp_path / "fake-ig"
    script.write_text(
        f"#!{sys.executable}\nimport sys, pathlib\n"
        f"pathlib.Path({str(tmp_path / 'ig-argv')!r}).write_text(' '.join(sys.argv[1:]))\n"
        f"sys.stdout.write({stdout!r})\nsys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def entry(code, kind, caption="a caption"):
    return {"shortcode": code, "url": f"https://www.instagram.com/reel/{code}/", "author": "au",
            "caption": caption, "timestamp": "2026-01-14T10:00:00Z", "source": kind}


def ig_env(make_env, monkeypatch, cmd=None, extra=""):
    if cmd is None:
        monkeypatch.delenv("IG_CLI_CMD", raising=False)
    else:
        monkeypatch.setenv("IG_CLI_CMD", str(cmd))
    registry.register(InstagramAdapter())
    return make_env(sources=(), extra=extra or "[sources.instagram]\nenabled = true\n")


def test_x_reports_deferred_and_never_fails_the_run(make_env, fixtures_dir):
    registry.register(XAdapter())
    cfg, data, out = make_env(sources=("x",))
    assert run(cfg, out, fixtures_dir) == 0
    src = result(out)["sources"]["x"]
    assert src["status"] == "deferred: no usable read route"
    assert src["items"] == []


def test_instagram_unset_reports_deferred_no_cli(make_env, fixtures_dir, monkeypatch):
    (fixtures_dir / "instagram" / "items.json").unlink()
    cfg, data, out = ig_env(make_env, monkeypatch)
    assert run(cfg, out, fixtures_dir) == 0
    src = result(out)["sources"]["instagram"]
    assert src["status"] == "deferred: no CLI configured"
    assert src["items"] == []


def test_both_modules_appear_in_one_run_result(make_env, fixtures_dir, monkeypatch):
    monkeypatch.delenv("IG_CLI_CMD", raising=False)
    (fixtures_dir / "instagram" / "items.json").unlink()
    registry.register(XAdapter())
    registry.register(InstagramAdapter())
    cfg, data, out = make_env(sources=DEFAULT_SOURCES)
    assert run(cfg, out, fixtures_dir) == 0
    res = result(out)
    assert res["sources"]["x"]["status"].startswith("deferred")
    assert res["sources"]["instagram"]["status"].startswith("deferred")
    assert res["flagged"] == []


def test_instagram_command_items_put_saved_before_liked(make_env, fixtures_dir, tmp_path, monkeypatch):
    payload = [entry("L1", "liked"), entry("S1", "saved"), entry("L2", "liked"), entry("S2", "saved")]
    cmd = fake_ig_cmd(tmp_path, json.dumps(payload))
    cfg, data, out = ig_env(make_env, monkeypatch, cmd)
    (fixtures_dir / "instagram" / "items.json").unlink()
    assert run(cfg, out, fixtures_dir) == 0
    src = result(out)["sources"]["instagram"]
    assert src["status"] == "ok"
    assert [i["id"] for i in src["items"]] == ["S1", "S2", "L1", "L2"]
    first = src["items"][0]
    assert first["url"] == "https://www.instagram.com/reel/S1/"
    assert first["author"] == "au"
    assert first["metrics"]["source"] == "saved"


def test_instagram_passes_configured_args_to_the_command(make_env, fixtures_dir, tmp_path, monkeypatch):
    cmd = fake_ig_cmd(tmp_path, "[]")
    cfg, data, out = ig_env(make_env, monkeypatch, cmd, extra='[sources.instagram]\nenabled = true\nargs = ["--json", "--limit", "5"]\n')
    (fixtures_dir / "instagram" / "items.json").unlink()
    assert run(cfg, out, fixtures_dir) == 0
    assert (tmp_path / "ig-argv").read_text() == "--json --limit 5"


def test_malformed_json_is_logged_and_skipped_without_failing(make_env, fixtures_dir, tmp_path, monkeypatch):
    cmd = fake_ig_cmd(tmp_path, "not json {")
    cfg, data, out = ig_env(make_env, monkeypatch, cmd)
    (fixtures_dir / "instagram" / "items.json").unlink()
    assert run(cfg, out, fixtures_dir) == 0
    src = result(out)["sources"]["instagram"]
    assert src["status"] == "ok"
    assert src["items"] == []
    assert "malformed" in (data / "logs" / "run-2026-01-15.log").read_text().lower()


def test_malformed_entries_are_skipped_but_good_ones_kept(make_env, fixtures_dir, tmp_path, monkeypatch):
    bad = [{"shortcode": "X1"}, "nope", entry("S1", "saved"), {**entry("B2", "saved"), "source": "pinned"}]
    cmd = fake_ig_cmd(tmp_path, json.dumps(bad))
    cfg, data, out = ig_env(make_env, monkeypatch, cmd)
    (fixtures_dir / "instagram" / "items.json").unlink()
    assert run(cfg, out, fixtures_dir) == 0
    src = result(out)["sources"]["instagram"]
    assert [i["id"] for i in src["items"]] == ["S1"]
    assert (data / "logs" / "run-2026-01-15.log").read_text().lower().count("malformed") >= 3


def test_failing_command_fails_only_this_module(make_env, fixtures_dir, tmp_path, monkeypatch):
    cmd = fake_ig_cmd(tmp_path, "", exit_code=3)
    cfg, data, out = ig_env(make_env, monkeypatch, cmd)
    (fixtures_dir / "instagram" / "items.json").unlink()
    assert run(cfg, out, fixtures_dir) == 0
    assert result(out)["sources"]["instagram"]["status"] == "failed"


def test_fixture_items_are_read_in_fixtures_mode_without_running_a_command(make_env, fixtures_dir, monkeypatch):
    cfg, data, out = ig_env(make_env, monkeypatch)  # IG_CLI_CMD unset
    assert run(cfg, out, fixtures_dir) == 0
    src = result(out)["sources"]["instagram"]
    assert [i["id"] for i in src["items"]] == ["S1", "L1"]


def test_run_result_keeps_only_a_short_caption_excerpt(make_env, fixtures_dir, tmp_path, monkeypatch):
    long_caption = " ".join(f"word{i}" for i in range(200))
    cmd = fake_ig_cmd(tmp_path, json.dumps([entry("S1", "saved", long_caption)]))
    cfg, data, out = ig_env(make_env, monkeypatch, cmd)
    (fixtures_dir / "instagram" / "items.json").unlink()
    run(cfg, out, fixtures_dir)
    item = result(out)["sources"]["instagram"]["items"][0]
    assert 0 < len(item["text"].split()) <= 30
    assert "word150" not in json.dumps(result(out))


def test_full_caption_is_available_for_the_summarizer_only(tmp_path, monkeypatch):
    long_caption = " ".join(f"word{i}" for i in range(200))
    cmd = fake_ig_cmd(tmp_path, json.dumps([entry("S1", "saved", long_caption)]))
    monkeypatch.setenv("IG_CLI_CMD", str(cmd))
    adapter = InstagramAdapter()
    from daily_pulse.adapters.base import AdapterContext
    ctx = AdapterContext(config={"enabled": True})
    (cand,) = adapter.list_new(ctx)
    adapter.fetch_details(cand, ctx)
    assert adapter.captions["S1"] == long_caption


def test_instagram_items_are_rated_through_the_summarizer_saved_first(tmp_path, fake_claude):
    reply = {"summary": "a reel", "takeaways": [], "beats": [], "rating": 6, "reason": "ok",
             "verdict": "skim", "tags": ["ai"], "diamond": False, "diamond_reason": ""}
    claude = fake_claude(reply)
    items = [Item(source="instagram", id=c, url="u", title="t", text="x") for c in ("S1", "L1")]
    s = Summarizer(claude_cmd=str(claude.path))
    results = summarize_items(s, items, tmp_path / "notes", "2026-01-15", {"S1": "saved caption"})
    assert [r.status for r in results] == ["rated", "rated"]
    assert "saved caption" in claude.stdin(0)
    assert (tmp_path / "notes/2026-01-15/instagram-S1.md").exists()


def test_x_handles_stay_in_config_for_later():
    adapter = XAdapter()
    from daily_pulse.adapters.base import AdapterContext
    ctx = AdapterContext(config={"enabled": True, "handles": ["swyx"]})
    assert adapter.status(ctx) == "deferred: no usable read route"
    assert adapter.list_new(ctx) == []


def test_shipped_config_reports_x_and_instagram_deferred(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.delenv("IG_CLI_CMD", raising=False)
    cfg = Path(__file__).parent.parent / "config" / "daily-pulse.toml"
    out = tmp_path / "out"
    empty = tmp_path / "fx"
    empty.mkdir()
    assert main(["run", "--config", str(cfg), "--fixtures", str(empty), "--out", str(out),
                 "--date", "2026-01-15", "--dry-run"]) == 0
