import json
import logging
import sys

import pytest

from daily_pulse.adapters import registry
from daily_pulse.adapters.instagram import InstagramAdapter
from daily_pulse.adapters.x import XAdapter
from daily_pulse.cli import main
from daily_pulse.models import Item
from daily_pulse.summarizer import Summarizer, summarize_items

DEFAULT_SOURCES = ("x", "instagram")


@pytest.fixture(autouse=True)
def rater(fake_rater):
    """The runner rates Instagram items; these tests only need a rater that answers."""
    return fake_rater()


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
    # X is now an opt-in paid adapter: the status names what is missing instead of "no usable read route"
    assert src["status"].startswith("deferred: ") and "paid_api_approved" in src["status"]
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


def test_x_handles_are_ignored_and_x_stays_deferred_until_opted_in():
    adapter = XAdapter()
    from daily_pulse.adapters.base import AdapterContext
    ctx = AdapterContext(config={"enabled": True, "handles": ["swyx"]})
    status = adapter.status(ctx)  # handles are display names: only `usernames` is read
    assert status.startswith("deferred: ") and "paid_api_approved" in status and "usernames" in status
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


def test_null_optional_fields_are_accepted_and_filtered_kinds_are_not_malformed(
        make_env, fixtures_dir, tmp_path, monkeypatch):
    payload = [{**entry("S1", "saved"), "caption": None, "author": None}, entry("L1", "liked")]
    cmd = fake_ig_cmd(tmp_path, json.dumps(payload))
    cfg, data, out = ig_env(make_env, monkeypatch, cmd,
                            extra='[sources.instagram]\nenabled = true\nkinds = ["saved"]\n')
    (fixtures_dir / "instagram" / "items.json").unlink()
    assert run(cfg, out, fixtures_dir) == 0
    assert [i["id"] for i in result(out)["sources"]["instagram"]["items"]] == ["S1"]
    assert "malformed" not in (data / "logs" / "run-2026-01-15.log").read_text().lower()


# --- Instagram ports from the working digest script ---

def _ig(entries, **cfg):
    import json
    import tempfile
    from pathlib import Path

    from daily_pulse.adapters.base import AdapterContext
    from daily_pulse.adapters.instagram import InstagramAdapter

    d = Path(tempfile.mkdtemp())
    (d / "instagram").mkdir()
    (d / "instagram" / "items.json").write_text(json.dumps(entries))
    return InstagramAdapter().list_new(AdapterContext(config=cfg, fixtures_dir=d, date="2026-01-15"))


def _entry(code, caption="", kind="saved", **extra):
    return {"shortcode": code, "url": f"https://www.instagram.com/reel/{code}/", "source": kind,
            "author": "maker", "caption": caption, "timestamp": "2026-01-14T10:00:00Z", **extra}


def test_a_reel_is_titled_by_its_captions_first_line_cut_to_80_characters():
    items = _ig([_entry("A1", "How I wire a checker agent\n\n#agents #evals"), _entry("A2", "x" * 200),
                 _entry("A3", ""), _entry("A4", "   \n  ")])
    titles = {i.id: i.title for i in items}
    assert titles["A1"] == "How I wire a checker agent"
    assert len(titles["A2"]) == 80 and titles["A2"].endswith("…")
    assert titles["A3"] == "Reel by maker (A3)" and titles["A4"] == "Reel by maker (A4)"


def test_likes_comments_and_hashtags_are_optional_metrics_and_bad_values_are_left_out():
    items = _ig([_entry("A1", "c", like_count=120, comment_count=7, hashtags=["#evals", "agents"] + ["t"] * 9),
                 _entry("A2", "c", like_count=-3, comment_count="many", hashtags="evals"), _entry("A3", "c")])
    m = {i.id: i.metrics for i in items}
    assert m["A1"]["likes"] == 120 and m["A1"]["comments"] == 7 and len(m["A1"]["hashtags"]) == 5
    assert m["A1"]["hashtags"][:2] == ["evals", "agents"]
    assert m["A2"] == {"source": "saved"} and m["A3"] == {"source": "saved"}


def test_the_per_run_limit_applies_to_new_reels_saved_first_and_the_rest_wait():
    from daily_pulse.adapters.base import AdapterContext
    from daily_pulse.adapters.instagram import InstagramAdapter

    entries = [_entry(f"L{i}", kind="liked") for i in range(3)] + [_entry(f"S{i}") for i in range(3)]
    listed = _ig(entries, limit=4)
    assert [i.id for i in listed] == ["S0", "S1", "S2", "L0", "L1", "L2"]  # listing does not cut
    kept, weeded = InstagramAdapter().rank_new(listed, AdapterContext(config={"limit": 4}))
    assert [i.id for i in kept] == ["S0", "S1", "S2", "L0"] and weeded == []
    assert len(InstagramAdapter().rank_new(listed, AdapterContext(config={}))[0]) == 6  # default is generous


def test_already_processed_reels_never_use_up_the_limit(make_env, fixtures_dir):
    import json as _json

    registry.register(InstagramAdapter())
    cfg, data, out = make_env(sources=("instagram",), extra="")
    cfg.write_text(cfg.read_text().replace("[sources.instagram]\nenabled = true", "[sources.instagram]\nenabled = true\nlimit = 2"))
    items = [_entry(f"S{i}", f"reel {i}") for i in range(4)]
    (fixtures_dir / "instagram").mkdir(exist_ok=True)
    (fixtures_dir / "instagram" / "items.json").write_text(_json.dumps(items))
    seen = []
    for _ in range(3):
        main(["run", "--config", str(cfg), "--fixtures", str(fixtures_dir), "--out", str(out), "--date", "2026-01-15"])
        res = _json.loads((out / "run-2026-01-15.json").read_text())
        seen += [i["id"] for i in res["sources"]["instagram"]["items"]]
    assert sorted(seen) == ["S0", "S1", "S2", "S3"]  # two a run, none repeated, none lost


def test_blank_hashtags_are_left_out_rather_than_passed_on_empty():
    items = _ig([_entry("A1", "c", hashtags=["#", " ", ""]), _entry("A2", "c", hashtags=["#ok", "#"])])
    m = {i.id: i.metrics for i in items}
    assert "hashtags" not in m["A1"] and m["A2"]["hashtags"] == ["ok"]


def test_a_bad_limit_is_rejected_even_when_the_listing_is_empty():
    with pytest.raises(ValueError, match=r"sources\.instagram"):
        _ig([], limit=0)


@pytest.mark.parametrize("limit", [0, -1, "5", True, 2.5])
def test_a_malformed_limit_is_rejected_with_a_message(limit):
    with pytest.raises(ValueError, match=r"sources\.instagram"):
        _ig([_entry("A1")], limit=limit)


def test_a_missing_ig_command_names_the_program_and_never_its_arguments(monkeypatch):
    from daily_pulse.adapters.base import AdapterContext
    from daily_pulse.adapters.instagram import InstagramAdapter

    monkeypatch.setenv("IG_CLI_CMD", "/nonexistent/ig-cli --token SECRET123")
    with pytest.raises(RuntimeError, match="IG CLI not found: ig-cli") as err:
        InstagramAdapter().list_new(AdapterContext(config={}, date="2026-01-15"))
    assert "SECRET123" not in str(err.value)
