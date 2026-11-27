"""Ticket 14: rank GitHub candidates by kind and drop noisy pushes."""
import json

import pytest

from daily_pulse.adapters.base import AdapterContext
from daily_pulse.adapters.github import KIND_ORDER, GitHubAdapter

CFG = {"enabled": True, "rising_days": 7, "watch_accounts": ["alice"], "watch_repos": ["bob/tool"]}


def listed(fixtures, tmp_path, **cfg):
    ctx = AdapterContext(config={**CFG, **cfg}, fixtures_dir=fixtures, date="2026-01-15",
                         data_dir=tmp_path / "data", persist=False)
    return GitHubAdapter().list_new(ctx)


def edit_responses(fixtures, change):
    path = fixtures / "github" / "responses.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def alice_active(data):
    return next(r for r in data["/users/alice/repos"]["body"] if r["full_name"] == "alice/active")


def kind(item):
    return item.id.split(":", 1)[0]


def test_candidates_are_ordered_by_kind_then_newest_first(fixtures_dir, tmp_path):
    items = listed(fixtures_dir, tmp_path)
    kinds = [kind(i) for i in items]
    assert kinds == sorted(kinds, key=KIND_ORDER.index)
    assert {"release", "repo", "rising", "push"} <= set(kinds)  # the fixture has all four
    for k in set(kinds):
        published = [i.published for i in items if kind(i) == k]
        assert published == sorted(published, reverse=True)


def test_a_push_never_outranks_a_release_or_a_rising_repo_even_when_newer(fixtures_dir, tmp_path):
    edit_responses(fixtures_dir, lambda d: alice_active(d).update(pushed_at="2026-01-15T11:59:00Z"))
    items = listed(fixtures_dir, tmp_path)
    push = next(i for i in items if kind(i) == "push")
    assert push.published > max(i.published for i in items if kind(i) in ("release", "rising"))
    assert items.index(push) > max(items.index(i) for i in items if kind(i) in ("release", "rising"))


def test_a_cap_of_three_keeps_the_release_and_rising_repos_before_any_push(fixtures_dir, tmp_path):
    kept = listed(fixtures_dir, tmp_path)[:3]
    assert "push" not in {kind(i) for i in kept}
    assert {kind(i) for i in kept} <= {"release", "repo", "rising"}


@pytest.mark.parametrize("stars,expected", [(24, False), (25, True), (500, True)])
def test_a_bare_push_is_news_only_for_a_repo_with_enough_stars(fixtures_dir, tmp_path, stars, expected):
    edit_responses(fixtures_dir, lambda d: alice_active(d).update(stargazers_count=stars))
    assert ("push" in {kind(i) for i in listed(fixtures_dir, tmp_path)}) is expected


def test_the_push_star_threshold_is_configurable(fixtures_dir, tmp_path):
    assert "push" not in {kind(i) for i in listed(fixtures_dir, tmp_path, push_min_stars=100)}
    edit_responses(fixtures_dir, lambda d: alice_active(d).update(stargazers_count=0))
    assert "push" in {kind(i) for i in listed(fixtures_dir, tmp_path, push_min_stars=0)}


def test_an_ignored_repo_produces_no_items_at_all(fixtures_dir, tmp_path):
    ids = {i.id for i in listed(fixtures_dir, tmp_path, ignore_repos=["ALICE/act*"])}
    assert not any("alice/active" in i for i in ids)
    assert "repo:alice/brand-new" in ids  # only the matching repo is dropped
    assert any("alice/active" in i.id for i in listed(fixtures_dir, tmp_path))  # and only because of the setting


def test_an_ignored_repo_is_dropped_from_watch_repos_and_from_the_rising_search_too(fixtures_dir, tmp_path):
    base = {i.id for i in listed(fixtures_dir, tmp_path)}
    assert "release:bob/tool:v0.3.0" in base and "rising:newrepo/agent-evals" in base
    ids = {i.id for i in listed(fixtures_dir, tmp_path, ignore_repos=["bob/tool", "newrepo/*"])}
    assert not any("bob/tool" in i or "newrepo/agent-evals" in i for i in ids)
    assert "rising:other/gizmo" in ids  # the rest of the search results stay


def test_a_fresh_adapter_has_default_noise_settings():
    adapter = GitHubAdapter()
    assert adapter._push_min_stars == 25 and adapter._ignore == []


def test_ignore_globs_are_case_insensitive_and_match_whole_names(fixtures_dir, tmp_path):
    assert any("alice/active" in i.id for i in listed(fixtures_dir, tmp_path, ignore_repos=["alice/act"]))
    assert not any("alice/active" in i.id for i in listed(fixtures_dir, tmp_path, ignore_repos=["alice/*"]))


@pytest.mark.parametrize("cfg", [
    {"push_min_stars": -1}, {"push_min_stars": "25"}, {"push_min_stars": True}, {"push_min_stars": 2.5},
    {"ignore_repos": "simonw/*"}, {"ignore_repos": ["ok", 3]},
])
def test_malformed_noise_settings_fail_the_source_with_a_message(fixtures_dir, tmp_path, cfg):
    with pytest.raises(ValueError, match=r"sources\.github"):
        listed(fixtures_dir, tmp_path, **cfg)


def test_the_shipped_config_ignores_the_known_noisy_repos_and_sets_the_threshold():
    import tomllib
    from pathlib import Path

    cfg = tomllib.loads((Path(__file__).resolve().parent.parent / "config" / "daily-pulse.toml").read_text())
    gh = cfg["sources"]["github"]
    assert gh["push_min_stars"] >= 1
    assert {"simonw/simonw", "simonw/scrape-*"} <= set(gh["ignore_repos"])
