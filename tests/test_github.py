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


def set_response(fixtures, path, status, body, headers=None):
    p = fixtures / "github" / "responses.json"
    data = json.loads(p.read_text())
    data[path] = {"status": status, "headers": headers or {}, "body": body}
    p.write_text(json.dumps(data))


def test_rerun_skips_items_already_processed(make_env, fixtures_dir):
    run(make_env, fixtures_dir)
    code, res = run(make_env, fixtures_dir)
    assert code == 0
    assert res["sources"]["github"]["status"] == "ok"
    assert res["sources"]["github"]["items"] == []


def test_rising_items_carry_star_velocity(make_env, fixtures_dir):
    _, res = run(make_env, fixtures_dir)
    m = items_by_id(res)["rising:newrepo/agent-evals"]["metrics"]
    assert m["stars"] == 300 and m["age_days"] == 3.0 and m["star_velocity"] == 100.0
    # younger than a day counts as one day, so the proxy is never inflated
    assert items_by_id(res)["rising:other/gizmo"]["metrics"]["star_velocity"] == 40.0


def test_release_item_summary_input_is_short_excerpt(make_env, fixtures_dir):
    _, res = run(make_env, fixtures_dir)
    rel = items_by_id(res)["release:alice/proj:v1.2.0"]
    assert rel["url"] == "https://github.com/alice/proj/releases/tag/v1.2.0"
    assert rel["text"] == "Adds a maker-checker loop and an eval runner."
    assert rel["author"] == "alice"


def test_rate_limit_403_degrades_with_log_and_keeps_earlier_items(make_env, fixtures_dir, caplog):
    set_response(fixtures_dir, "/users/alice/repos", 403,
                 {"message": "API rate limit exceeded for 1.2.3.4."},
                 {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4102444800"})
    adapter = GitHubAdapter()
    registry.register(adapter)
    with caplog.at_level("WARNING", logger="daily_pulse"):
        code, res = run(make_env, fixtures_dir)
    assert code == 0
    assert res["sources"]["github"]["status"] == "ok"
    ids = set(items_by_id(res))
    assert "rising:other/gizmo" in ids and "release:bob/tool:v0.3.0" not in ids
    assert adapter.last_run["degraded"] is True
    assert "rate limit" in adapter.last_run["reason"].lower()
    assert "rate limit" in caplog.text.lower()


def test_remaining_zero_on_success_stops_further_requests(make_env, fixtures_dir):
    set_response(fixtures_dir, "/search/repositories", 200, {"items": []},
                 {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4102444800"})
    adapter = GitHubAdapter()
    registry.register(adapter)
    _, res = run(make_env, fixtures_dir)
    assert res["sources"]["github"]["items"] == []
    assert adapter.last_run["degraded"] is True


def test_short_retry_after_is_waited_out_then_retried(make_env, fixtures_dir):
    calls = []

    def transport(path, params):
        calls.append(path)
        if len([c for c in calls if c == "/search/repositories"]) == 1:
            return 429, {"Retry-After": "2", "X-RateLimit-Remaining": "0"}, {"message": "secondary rate limit"}
        return 200, {"X-RateLimit-Remaining": "50"}, {"items": []} if path.startswith("/search") else []

    sleeps = []
    adapter = GitHubAdapter(transport=transport, sleep=sleeps.append)
    registry.register(adapter)
    run(make_env, fixtures_dir)
    assert sleeps == [2]
    assert calls.count("/search/repositories") == 2
    assert adapter.last_run["degraded"] is False


# -- real HTTP transport (urlopen mocked; no network) --------------------------------------

import io
import urllib.error
import urllib.request

from daily_pulse.adapters.github import UrllibTransport


class FakeResponse(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(json.dumps(body).encode())
        self.status, self.headers = status, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def capture_urlopen(monkeypatch, response):
    seen = []

    def fake(req, timeout=None):
        seen.append(req)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def test_token_comes_from_env_and_is_sent_as_bearer(monkeypatch):
    seen = capture_urlopen(monkeypatch, FakeResponse([], headers={"X-RateLimit-Remaining": "4999"}))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_sekrit")
    status, headers, body = UrllibTransport()("/users/alice/repos", {"per_page": 30})
    assert (status, body) == (200, [])
    req = seen[0]
    assert req.full_url == "https://api.github.com/users/alice/repos?per_page=30"
    assert req.get_header("Authorization") == "Bearer ghp_sekrit"
    assert req.get_header("Accept") == "application/vnd.github+json"


def test_no_token_means_no_authorization_header(monkeypatch):
    seen = capture_urlopen(monkeypatch, FakeResponse([]))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    UrllibTransport()("/x", {})
    assert not seen[0].has_header("Authorization")


def test_http_error_status_is_returned_not_raised(monkeypatch):
    err = urllib.error.HTTPError("u", 403, "Forbidden", {"X-RateLimit-Remaining": "0"},
                                 io.BytesIO(b'{"message": "API rate limit exceeded"}'))
    capture_urlopen(monkeypatch, err)
    status, headers, body = UrllibTransport()("/x", {})
    assert status == 403 and body["message"].startswith("API rate limit")
    assert headers["X-RateLimit-Remaining"] == "0"


def test_token_never_appears_in_logs_even_when_rate_limited(monkeypatch, caplog, make_env, fixtures_dir):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_sekrit")
    err = urllib.error.HTTPError("u", 403, "Forbidden", {"X-RateLimit-Remaining": "0"},
                                 io.BytesIO(b'{"message": "API rate limit exceeded"}'))
    capture_urlopen(monkeypatch, err)
    registry.register(GitHubAdapter(transport=UrllibTransport()))
    with caplog.at_level("DEBUG", logger="daily_pulse"):
        code, _ = run(make_env, fixtures_dir)
    assert code == 0
    assert "ghp_sekrit" not in caplog.text
    assert "ghp_sekrit" not in repr(UrllibTransport())


def test_without_fixtures_the_adapter_uses_the_real_transport(monkeypatch, make_env, tmp_path):
    seen = capture_urlopen(monkeypatch, FakeResponse({"items": []}, headers={"X-RateLimit-Remaining": "9"}))
    registry.register(GitHubAdapter(sleep=lambda s: None))
    cfg, data, out = make_env(sources=(), extra=GITHUB_CFG)
    assert main(["run", "--config", str(cfg), "--out", str(out), "--date", "2026-01-15"]) == 0
    assert seen[0].full_url.startswith("https://api.github.com/search/repositories?")
    assert "created%3A%3E2026-01-08" in seen[0].full_url


# -- star activity since the last run -------------------------------------------------------

def set_stars(fixtures, full_name, stars):
    p = fixtures / "github" / "responses.json"
    data = json.loads(p.read_text())
    for r in data["/users/alice/repos"]["body"]:
        if r["full_name"] == full_name:
            r["stargazers_count"] = stars
    p.write_text(json.dumps(data))


def test_notable_star_growth_since_last_run_is_an_item(make_env, fixtures_dir):
    run(make_env, fixtures_dir)  # baseline: alice/proj at 1200
    set_stars(fixtures_dir, "alice/proj", 1300)
    _, res = run(make_env, fixtures_dir)
    items = items_by_id(res)
    assert list(items) == ["stars:alice/proj:2026-01-15"]
    assert items["stars:alice/proj:2026-01-15"]["metrics"]["stars_gained"] == 100
    # and it is not repeated once the new count is the baseline
    _, res = run(make_env, fixtures_dir)
    assert res["sources"]["github"]["items"] == []


def test_small_star_growth_is_not_notable(make_env, fixtures_dir):
    run(make_env, fixtures_dir)
    set_stars(fixtures_dir, "alice/proj", 1210)
    _, res = run(make_env, fixtures_dir)
    assert res["sources"]["github"]["items"] == []


def test_dry_run_does_not_move_the_star_baseline(make_env, fixtures_dir):
    cfg, data, out = make_env(sources=(), extra=GITHUB_CFG)
    args = ["run", "--config", str(cfg), "--fixtures", str(fixtures_dir), "--out", str(out),
            "--date", "2026-01-15"]
    assert main(args + ["--dry-run"]) == 0
    assert not list(data.rglob("github*.json"))


# -- config ---------------------------------------------------------------------------------

def test_shipped_config_watches_the_seed_handles_and_oh_my_pi():
    import tomllib
    from pathlib import Path
    cfg = tomllib.loads((Path(__file__).parent.parent / "config" / "daily-pulse.toml").read_text())
    gh = cfg["sources"]["github"]
    assert {"karpathy", "mattpocock", "can1357", "simonw", "hamelsmu", "shreyashankar", "jxnl",
            "eugeneyan", "hwchase17", "anthropics", "langchain-ai"} <= set(gh["watch_accounts"])
    assert "can1357/oh-my-pi" in gh["watch_repos"]
