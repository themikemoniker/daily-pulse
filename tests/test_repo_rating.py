"""The GitHub relevance logic ported from the working digest script (see repo_rating.py)."""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from daily_pulse.adapters.base import AdapterContext
from daily_pulse.adapters.github import GitHubAdapter
from daily_pulse.claude import CallFailed
from daily_pulse.digest import Entry, is_diamond
from daily_pulse.repo_rating import (age_days, format_repo_for_rating, is_diamond as relevant_and_fast,
                                     parse_batch_ratings, rate_repos_batch, topic_score, velocity_of)

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class FakeSummarizer:
    """Stands in for Summarizer: records the prompts it was asked to send."""

    def __init__(self, reply="", calls_made=0, max_calls=30, fail=False):
        self.reply, self.calls_made, self.max_calls, self.fail = reply, calls_made, max_calls, fail
        self.prompts_dir = PROMPTS
        self.prompts: list[str] = []

    def _call(self, prompt):
        self.prompts.append(prompt)
        self.calls_made += 1
        if self.fail:
            raise CallFailed("boom")
        return self.reply


def repo(name, stars=10, days=2, topics=(), description="d", **extra):
    created = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return {"full_name": name, "name": name.split("/")[1], "stargazers_count": stars, "created_at": created,
            "topics": list(topics), "description": description, "language": "Python",
            "html_url": f"https://github.com/{name}", "owner": {"login": name.split("/")[0]}, **extra}


# --- the script's pure functions ---

@pytest.mark.parametrize("rating,velocity,expected", [
    (7, 50, True), (7, 49.9, False), (6.9, 500, False), (10, 50, True), (None, 500, False), (8, None, False)])
def test_a_diamond_is_relevance_7_or_more_and_50_or_more_stars_a_day(rating, velocity, expected):
    assert relevant_and_fast(rating, velocity) is expected


def test_topic_score_counts_keywords_and_stops_at_3():
    assert topic_score({"name": "x", "description": "a pretty photo app"}) == 0
    assert topic_score({"name": "eval-harness", "description": "", "topics": []}) == 3  # 2 + 2, capped
    assert topic_score({"name": "x", "description": "an llm toolkit", "topics": ["claude"]}) == 2


def test_velocity_is_stars_per_day_and_never_inflated_for_a_brand_new_repo():
    assert velocity_of(repo("a/b", stars=300, days=3)) == pytest.approx(100, abs=2)
    assert velocity_of({"stargazers_count": 5, "created_at": "nonsense"}) == 5.0  # unreadable date: one day
    assert age_days(datetime.now(timezone.utc).isoformat()) == 0.1  # a floor, so no division by zero


def test_the_rating_line_carries_stars_velocity_topics_and_a_cut_description():
    line = format_repo_for_rating(repo("a/b", stars=40, topics=["mcp"], description="x" * 1000,
                                       _readme_excerpt="intro text"), 12.5)
    assert line.startswith("a/b — Python — ★40 (12.5/day) — Topics: mcp — Description: xxx")
    assert "README excerpt: intro text" in line and len(line) < 500


@pytest.mark.parametrize("text", ["", "no json here", "{broken", "[1, 2]", '{"a/b": "text"}'])
def test_unparseable_batch_output_is_an_empty_dict(text):
    assert parse_batch_ratings(text) == {}


def test_batch_ratings_are_validated_one_by_one():
    text = 'Sure! {"a/b": {"rating": 8, "reason": " good ", "verdict": "READ"}, "c/d": {"rating": 11}, ' \
           '"e/f": {"rating": "x"}, "g/h": {"rating": 0}, "i/j": "no"} trailing'
    assert parse_batch_ratings(text) == {"a/b": {"rating": 8.0, "reason": "good", "verdict": "read"}}


# --- one call, through the repo's wrapper ---

def test_the_batch_is_one_call_listing_every_repo_with_the_rubric_prompt():
    s = FakeSummarizer('{"a/b": {"rating": 8, "reason": "r", "verdict": "read"}}')
    out = rate_repos_batch([repo("a/b"), repo("c/d")], s)
    assert out["a/b"]["rating"] == 8 and len(s.prompts) == 1 and s.calls_made == 1
    prompt = s.prompts[0]
    assert "RELEVANCE" in prompt and "1. a/b" in prompt and "2. c/d" in prompt
    assert "untrusted" in prompt  # the repo list is data, not instructions


@pytest.mark.parametrize("summarizer", [
    None, FakeSummarizer(fail=True), FakeSummarizer("not json"), FakeSummarizer(calls_made=30)])
def test_no_summarizer_a_failed_call_or_a_spent_cap_leaves_repos_unrated(summarizer, caplog):
    with caplog.at_level(logging.WARNING, logger="daily_pulse"):
        assert rate_repos_batch([repo("a/b")], summarizer) == {}
    if summarizer is not None and summarizer.calls_made == 30:
        assert summarizer.prompts == []  # the cap stopped it before any call


def test_the_prompt_carries_no_personal_names():
    text = (PROMPTS / "rate-repos.md").read_text()
    for forbidden in ("Jordan", "Carlile", "Florencio", "herdr"):
        assert forbidden not in text


# --- the adapter: rank_new on the new repos only ---

def new_rising(names_stars, **cfg):
    adapter = GitHubAdapter()
    adapter._rising_cfg = adapter._rising_settings({"rising_topics": [], **cfg})
    items = []
    for name, stars in names_stars:
        raw = repo(name, stars=stars, days=3)
        item = adapter._repo_item("rising", raw, datetime.now(timezone.utc), raw["created_at"])
        adapter._pool_raw[name] = raw
        items.append(item)
    return adapter, items


def ctx(summarizer, persist=True):
    return AdapterContext(config={}, date="2026-01-15", summarizer=summarizer, persist=persist)


def test_rank_new_orders_by_relevance_not_stars_and_weeds_out_low_relevance():
    adapter, items = new_rising([("game/cat", 9000), ("harness/evals", 60), ("util/tool", 500)])
    reply = json.dumps({"game/cat": {"rating": 1, "reason": "a typing cat", "verdict": "skip"},
                        "harness/evals": {"rating": 9, "reason": "evals", "verdict": "read"},
                        "util/tool": {"rating": 5, "reason": "ok", "verdict": "skim"}})
    kept, weeded = adapter.rank_new(items, ctx(FakeSummarizer(reply)))
    assert [i.title for i in kept] == ["harness/evals", "util/tool"]  # not by stars
    assert [i.title for i, _ in weeded] == ["game/cat"] and "typing cat" in weeded[0][1]
    assert kept[0].metrics["relevance"] == 9


def test_rank_new_leaves_repos_unrated_not_scored_when_the_call_fails():
    adapter, items = new_rising([("a/b", 10), ("c/d", 20)])
    kept, weeded = adapter.rank_new(items, ctx(FakeSummarizer(fail=True)))
    assert len(kept) == 2 and weeded == [] and all("relevance" not in i.metrics for i in kept)


def test_rank_new_makes_no_call_when_nothing_is_rising_and_none_on_a_dry_run():
    adapter, items = new_rising([("a/b", 10)])
    s = FakeSummarizer("{}")
    adapter.rank_new([i for i in items if False], ctx(s))
    adapter.rank_new(items, ctx(s, persist=False))
    assert s.prompts == []


def test_rank_new_carries_on_at_most_rising_top_and_other_kinds_pass_through():
    adapter, items = new_rising([(f"o/r{i}", 10 + i) for i in range(5)], rising_top=2)
    other = adapter._repo_item("repo", repo("x/y"), datetime.now(timezone.utc), "2026-01-01T00:00:00Z")
    kept, weeded = adapter.rank_new([other, *items], ctx(FakeSummarizer("{}")))
    assert sum(i.id.startswith("rising:") for i in kept) == 2 and other in kept and weeded == []


@pytest.mark.parametrize("cfg", [
    {"rising_top": 0}, {"min_stars": -1}, {"rising_per_page": 101}, {"min_relevance": 11},
    {"rising_topics": "agent"}, {"rising_topics": ["Bad Topic"]}, {"readme_excerpt": "yes"}, {"rising_top": True}])
def test_malformed_rising_settings_are_rejected_with_a_message(cfg):
    with pytest.raises(ValueError, match=r"sources\.github"):
        GitHubAdapter._rising_settings(cfg)


# --- the digest's diamond rule ---

def entry(source="github", rating=7, velocity=50.0, verdict="read", published=None):
    return Entry(source=source, stem="s", title="t", url="u", author="a", published=published,
                 rating=rating, reason="r", verdict=verdict, tags=[], star_velocity=velocity)


NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)


@pytest.mark.parametrize("e,expected", [
    (entry(rating=7, velocity=50.0), True),
    (entry(rating=7, velocity=49.9), False),
    (entry(rating=6, velocity=500.0), False),
    (entry(rating=9, velocity=500.0, verdict="skip"), False),
    (entry(source="youtube", rating=8, velocity=500.0), False),  # the velocity rule is GitHub's
    (entry(rating=7, velocity=None), False),
    (entry(rating=9, velocity=1.0, published=NOW - timedelta(hours=47)), True),  # the 9+ and early rule stands
    (entry(rating=9, velocity=1.0, published=NOW - timedelta(hours=49)), False),
])
def test_the_github_diamond_rule_sits_beside_the_rating_9_and_under_48h_rule(e, expected):
    assert is_diamond(e, NOW) is expected
