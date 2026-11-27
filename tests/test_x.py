import json
import urllib.request
from pathlib import Path

import pytest
from conftest import FakeAdapter

from daily_pulse.adapters import registry
from daily_pulse.adapters.base import AdapterContext
from daily_pulse.adapters.x import FixtureTransport, UrllibTransport, XAdapter, XStopped, settings
from daily_pulse.cli import main

DATE = "2026-01-15"
LONG_ID, REPOST_ID, SHORT_ID = "9000000000000000003", "9000000000000000002", "9000000000000000001"
X_CFG = """
[sources.x]
enabled = true
paid_api_approved = true
usernames = ["demo_alpha", "demo_beta"]
"""
FIXTURE_FILE = Path(__file__).parent / "fixtures" / "x" / "responses.json"


@pytest.fixture(autouse=True)
def rater(fake_rater):
    return fake_rater()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing in this module may reach urllib: any attempt fails the test."""
    def boom(*a, **k):
        raise AssertionError("urllib.request.urlopen was called")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def register_x():
    registry.register(XAdapter())


def run(make_env, fixtures, extra=X_CFG, flags=()):
    cfg, data, out = make_env(sources=(), extra=extra)
    code = main(["run", "--config", str(cfg), "--fixtures", str(fixtures), "--out", str(out),
                 "--date", DATE, *flags])
    res = json.loads((out / f"run-{DATE}.json").read_text()) if (out / f"run-{DATE}.json").exists() else None
    return code, res, data, out


def x_items(res):
    return {i["id"]: i for i in res["sources"]["x"]["items"]}


def set_response(fixtures, path, status, body=None):
    p = fixtures / "x" / "responses.json"
    data = json.loads(p.read_text())
    data[path] = {"status": status, "body": body or {}}
    p.write_text(json.dumps(data))


def ctx(cfg, fixtures=None, **kw):
    return AdapterContext(config=cfg, fixtures_dir=fixtures, date=DATE, **kw)


# -- opt-in and the no-network guarantee ---------------------------------------------------


def test_default_config_is_deferred_and_makes_no_network_call(make_env, fixtures_dir, monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "tok-secret-value")  # even with a token present
    code, res, _, _ = run(make_env, fixtures_dir, extra="[sources.x]\nenabled = true\nusernames = []\n")
    assert code == 0
    src = res["sources"]["x"]
    assert src["status"].startswith("deferred: ") and "paid_api_approved" in src["status"]
    assert src["items"] == []
    assert "tok-secret-value" not in json.dumps(res)


def test_shipped_config_is_deferred_with_no_network_and_x_keys_are_valid(monkeypatch):
    import tomllib
    cfg = tomllib.loads((Path(__file__).parent.parent / "config" / "daily-pulse.toml").read_text())["sources"]["x"]
    assert cfg["paid_api_approved"] is False and cfg["usernames"] == []
    assert settings(cfg)["max_accounts"] == 10
    monkeypatch.setenv("X_BEARER_TOKEN", "tok")
    adapter = XAdapter()
    assert adapter.status(ctx(cfg)).startswith("deferred: ")
    assert adapter.list_new(ctx(cfg)) == []  # even called directly, it makes no request


def test_not_approved_never_calls_transport_even_with_token(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "tok")
    calls = []
    adapter = XAdapter(transport=lambda *a: calls.append(a))
    assert adapter.list_new(ctx({"usernames": ["demo_alpha"]})) == []
    assert calls == []


def test_missing_token_is_deferred_naming_the_variable(monkeypatch):
    cfg = {"paid_api_approved": True, "usernames": ["demo_alpha"]}
    msg = XAdapter().status(ctx(cfg))
    assert msg.startswith("deferred: ") and "X_BEARER_TOKEN" in msg
    monkeypatch.setenv("MY_X_TOKEN", "   ")  # whitespace only counts as empty
    msg = XAdapter().status(ctx({**cfg, "token_env": "MY_X_TOKEN"}))
    assert "MY_X_TOKEN" in msg


def test_token_value_never_appears_in_status(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "tok-secret-value")
    assert "tok-secret-value" not in XAdapter().status(ctx({"usernames": []}))
    assert XAdapter().status(ctx({"paid_api_approved": True, "usernames": ["a"]})) is None


def test_approved_but_no_usernames_is_deferred(make_env, fixtures_dir):
    code, res, _, _ = run(make_env, fixtures_dir, extra="[sources.x]\nenabled = true\npaid_api_approved = true\n")
    assert "usernames is empty" in res["sources"]["x"]["status"]


def test_live_transport_sends_token_only_in_header_with_30s_timeout(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "tok-secret-value")
    seen = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"data": []}'

    def fake_urlopen(req, timeout=None):
        seen["req"], seen["timeout"] = req, timeout
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport = UrllibTransport("X_BEARER_TOKEN")
    assert transport("/users/1/tweets", {"exclude": "replies"}) == (200, {"data": []})
    assert seen["timeout"] == 30
    assert seen["req"].get_header("Authorization") == "Bearer tok-secret-value"
    assert "tok-secret-value" not in seen["req"].full_url and "tok-secret-value" not in repr(transport)

    def failing(req, timeout=None):
        raise OSError(f"cannot reach {req.get_header('Authorization')}")

    monkeypatch.setattr(urllib.request, "urlopen", failing)
    with pytest.raises(RuntimeError) as err:
        transport("/users/1/tweets", {})
    assert "tok-secret-value" not in str(err.value)


# -- enabled runs from fixtures -------------------------------------------------------------


def test_enabled_run_from_fixtures_produces_items_without_a_token(make_env, fixtures_dir):
    code, res, _, _ = run(make_env, fixtures_dir)
    assert code == 0 and res["sources"]["x"]["status"] == "ok"
    items = x_items(res)
    assert set(items) == {LONG_ID, REPOST_ID, SHORT_ID, "9000000000000000200", "9000000000000000201"}
    item = items[SHORT_ID]
    assert item["url"] == f"https://x.com/demo_alpha/status/{SHORT_ID}"
    assert item["author"] == "demo_alpha" and item["published"] == "2026-01-14T20:00:00.000Z"
    assert item["metrics"] == {"likes": 5, "reposts": 1, "replies": 0, "kind": "post"}
    assert item["text"] == "Invented note: a fixture is just a recorded answer."
    ids = [i["id"] for i in res["sources"]["x"]["items"]]
    assert ids == sorted(ids, key=lambda i: items[i]["published"], reverse=True)


def test_rerun_finds_nothing_new(make_env, fixtures_dir):
    cfg, data, out = make_env(sources=(), extra=X_CFG)
    args = ["run", "--config", str(cfg), "--fixtures", str(fixtures_dir), "--out", str(out), "--date", DATE]
    assert main(args) == 0
    first = json.loads((out / f"run-{DATE}.json").read_text())
    assert len(x_items(first)) == 5
    assert main(args) == 0
    second = json.loads((out / f"run-{DATE}.json").read_text())  # the first run is kept as .1
    assert second["sources"]["x"]["status"] == "ok" and second["sources"]["x"]["items"] == []
    assert len(list((out / "notes" / DATE).glob("x-*.md"))) == 5


def test_repost_is_labelled_and_resolved_from_includes_tweets(make_env, fixtures_dir):
    _, res, _, _ = run(make_env, fixtures_dir)
    repost = x_items(res)[REPOST_ID]
    assert repost["metrics"]["kind"] == "repost"
    assert repost["title"] == f"Repost by @demo_alpha of @demo_beta ({REPOST_ID})"
    assert repost["text"].startswith("Invented original post about tiny eval harnesses")  # not "RT @..."
    assert repost["metrics"]["likes"] == 250  # the original's metrics
    assert repost["url"] == f"https://x.com/demo_alpha/status/{REPOST_ID}"
    assert x_items(res)[LONG_ID]["metrics"]["kind"] == "post"


def test_repost_without_the_original_in_includes_falls_back_to_its_own_text(fixtures_dir):
    path = "/users/1001/tweets"
    data = json.loads((fixtures_dir / "x" / "responses.json").read_text())
    data[path]["body"]["includes"] = {}
    (fixtures_dir / "x" / "responses.json").write_text(json.dumps(data))
    adapter = XAdapter()
    cfg = {"paid_api_approved": True, "usernames": ["demo_alpha"]}
    by_id = {i.id: i for i in adapter.list_new(ctx(cfg, fixtures_dir))}
    assert by_id[REPOST_ID].metrics["kind"] == "repost"
    assert adapter.rating_text(by_id[REPOST_ID], ctx(cfg)).startswith("RT @demo_beta")


def test_excerpt_is_at_most_200_chars_and_full_text_only_via_rating_text(fixtures_dir):
    adapter = XAdapter()
    c = ctx({"paid_api_approved": True, "usernames": ["demo_alpha"]}, fixtures_dir)
    by_id = {i.id: i for i in adapter.list_new(c)}
    item = adapter.fetch_details(by_id[LONG_ID], c)
    assert 0 < len(item.text) <= 200
    assert "FULLTEXT-MARKER" not in json.dumps(item.to_dict())
    assert "FULLTEXT-MARKER" in adapter.rating_text(item, c)


# -- cost controls ------------------------------------------------------------------------------


def test_caps_are_enforced(make_env, fixtures_dir):
    extra = X_CFG.replace('"demo_beta"]', '"demo_beta", "demo_gamma"]') + "max_accounts = 2\nmax_posts_per_account = 1\n"
    _, res, _, _ = run(make_env, fixtures_dir, extra=extra)
    # two accounts only (gamma is cut), one post each: the newest from each
    assert set(x_items(res)) == {LONG_ID, "9000000000000000200"}


def test_request_params_ask_for_no_replies_and_at_least_five_results(fixtures_dir):
    seen = []
    fx = FixtureTransport(fixtures_dir)

    def spy(path, params):
        seen.append((path, params))
        return fx(path, params)

    adapter = XAdapter(transport=spy)
    adapter.list_new(ctx({"paid_api_approved": True, "usernames": ["demo_alpha"], "max_posts_per_account": 2,
                          "lookback_days": 3}, fixtures_dir))
    (path, params), = [s for s in seen if s[0].endswith("/tweets")]
    assert params["exclude"] == "replies" and params["max_results"] == "5" and "start_time" in params
    assert "referenced_tweets.id" in params["expansions"]
    assert adapter.last_run["post_reads"] == 3 and adapter.last_run["user_reads"] == 1
    assert adapter.last_run["estimated_cost"] == pytest.approx(3 * 0.005 + 0.010)


def test_lookback_days_sets_start_time():
    from datetime import datetime, timezone
    seen = []

    def transport(path, params):
        seen.append(params)
        return (200, {"data": {"id": "1"}}) if "by/username" in path else (200, {"data": []})

    adapter = XAdapter(transport=transport, clock=lambda: datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc))
    adapter.list_new(_fx_ctx())
    assert seen[-1]["start_time"] == "2026-01-12T12:00:00Z"


def _fx_ctx(**kw):
    # a fixtures dir that exists only so status() passes; the injected transport is what answers
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "x").mkdir()
    (d / "x" / "responses.json").write_text("{}")
    return ctx({"paid_api_approved": True, "usernames": ["a"], "lookback_days": 3}, d, **kw)


def test_cost_estimate_is_logged_as_an_estimate(make_env, fixtures_dir):
    _, res, data, _ = run(make_env, fixtures_dir)
    log = (data / "logs" / f"run-{DATE}.log").read_text()
    line = next(ln for ln in log.splitlines() if "estimated cost" in ln)
    assert "estimate" in line and "not verified billing" in line


def test_user_id_cache_is_written_only_when_persist_is_true(make_env, fixtures_dir):
    _, _, data, _ = run(make_env, fixtures_dir, flags=["--dry-run"])
    assert not (data / "state" / "x-user-ids.json").exists()
    _, _, data, _ = run(make_env, fixtures_dir)
    cache = json.loads((data / "state" / "x-user-ids.json").read_text())
    assert cache == {"user_ids": {"demo_alpha": "1001", "demo_beta": "1002"}}


def test_cached_ids_skip_the_user_lookup(tmp_path, fixtures_dir):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "x-user-ids.json").write_text(json.dumps({"user_ids": {"demo_alpha": "1001"}}))
    adapter = XAdapter()
    adapter.list_new(ctx({"paid_api_approved": True, "usernames": ["Demo_Alpha"]}, fixtures_dir,
                         data_dir=tmp_path, persist=False))
    assert adapter.last_run["user_reads"] == 0


@pytest.mark.parametrize("code", [402, 429])
def test_payment_or_rate_limit_stops_only_the_x_module(make_env, fixtures_dir, code, caplog):
    set_response(fixtures_dir, "/users/1001/tweets", code, {"title": "nope"})
    (fixtures_dir / "fake").mkdir(exist_ok=True)
    registry.register(FakeAdapter())
    cfg, data, out = make_env(sources=("fake",), extra=X_CFG)
    assert main(["run", "--config", str(cfg), "--fixtures", str(fixtures_dir), "--out", str(out),
                 "--date", DATE]) == 0
    res = json.loads((out / f"run-{DATE}.json").read_text())
    assert res["sources"]["x"]["status"] == "failed"
    assert f"HTTP {code}" in res["sources"]["x"]["error"] and "stopped" in res["sources"]["x"]["error"]
    assert res["sources"]["x"]["items"] == []
    assert res["sources"]["fake"]["status"] == "ok"
    log = (data / "logs" / f"run-{DATE}.log").read_text()
    assert "demo_beta" not in log  # the second account was never requested


def test_stop_raises_xstopped_for_402():
    def transport(path, params):
        return (200, {"data": {"id": "1"}}) if "by/username" in path else (402, {})
    with pytest.raises(XStopped, match="402"):
        XAdapter(transport=transport).list_new(_fx_ctx())


def test_other_http_errors_skip_only_that_account(fixtures_dir):
    set_response(fixtures_dir, "/users/by/username/demo_alpha", 404, {"errors": [{"title": "Not Found"}]})
    adapter = XAdapter()
    items = adapter.list_new(ctx({"paid_api_approved": True, "usernames": ["demo_alpha", "demo_beta"]}, fixtures_dir))
    assert {i.author for i in items} == {"demo_beta"}


# -- settings -----------------------------------------------------------------------------------


@pytest.mark.parametrize("bad, key", [
    ({"paid_api_approved": "yes"}, "paid_api_approved"),
    ({"paid_api_approved": 1}, "paid_api_approved"),
    ({"token_env": ""}, "token_env"),
    ({"token_env": "has space"}, "token_env"),
    ({"token_env": 5}, "token_env"),
    ({"usernames": "demo_alpha"}, "usernames"),
    ({"usernames": ["@demo_alpha"]}, "usernames"),
    ({"usernames": ["Andrej Karpathy"]}, "usernames"),
    ({"usernames": ["a" * 16]}, "usernames"),
    ({"usernames": [""]}, "usernames"),
    ({"usernames": [5]}, "usernames"),
    ({"max_accounts": 0}, "max_accounts"),
    ({"max_accounts": True}, "max_accounts"),
    ({"max_posts_per_account": "10"}, "max_posts_per_account"),
    ({"max_posts_per_account": 101}, "max_posts_per_account"),
    ({"lookback_days": -1}, "lookback_days"),
    ({"lookback_days": 1.5}, "lookback_days"),
])
def test_malformed_settings_are_rejected_naming_the_key(bad, key):
    with pytest.raises(ValueError, match=rf"\[sources\.x\] {key} "):
        XAdapter().status(ctx(bad))


def test_malformed_settings_fail_only_the_x_module(make_env, fixtures_dir):
    code, res, _, _ = run(make_env, fixtures_dir, extra=X_CFG + "max_accounts = 0\n")
    assert code == 0
    assert res["sources"]["x"]["status"] == "failed" and "[sources.x] max_accounts" in res["sources"]["x"]["error"]


def test_usernames_are_deduplicated_case_insensitively():
    assert settings({"usernames": ["Demo_Alpha", "demo_beta", "demo_alpha"]})["usernames"] == ["Demo_Alpha", "demo_beta"]


# -- privacy ------------------------------------------------------------------------------------


def test_full_post_text_never_reaches_any_file_written_by_a_run(make_env, fixtures_dir, rater):
    _, res, data, out = run(make_env, fixtures_dir)
    assert len(x_items(res)) == 5
    written = [p for root in (data, out) for p in root.rglob("*") if p.is_file()]
    assert any(p.parent.name == DATE and p.name.startswith("x-") for p in written)  # notes were written
    for p in written:
        assert "FULLTEXT-MARKER" not in p.read_text(errors="ignore"), p
    assert "FULLTEXT-MARKER" in rater.stdin(0) + "".join(rater.stdin(n) for n in range(1, rater.calls))
