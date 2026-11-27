import pytest

from daily_pulse.config import QualityConfig
from daily_pulse.models import Item
from daily_pulse.quality import QualityGate


def video(id="v", title="A talk", author="Chan", duration=600, views=10):
    metrics = {} if duration is None else {"duration": duration}
    metrics["views"] = views
    return Item(source="youtube", id=id, url=f"https://y/{id}", title=title, author=author, metrics=metrics)


def screen(items, duration_rules=True, **quality):
    gate = QualityGate(QualityConfig(**quality))
    return gate.screen(items, duration_rules=duration_rules, triage=False, date="2026-01-15")


@pytest.mark.parametrize("duration,kept", [(239, False), (240, True), (241, True), (7199, True), (7200, True), (7201, False)])
def test_duration_limits_are_inclusive_at_the_boundaries(duration, kept):
    result = screen([video(duration=duration)])
    assert [i.id for i in result.kept] == (["v"] if kept else [])
    assert [d.item_id for d in result.dropped] == ([] if kept else ["v"])


def test_a_dropped_duration_says_why():
    [d] = screen([video(duration=90)]).dropped
    assert "90" in d.reason and "240" in d.reason and d.score is None


def test_an_unknown_duration_is_never_dropped_for_its_length():
    assert [i.id for i in screen([video(duration=None)]).kept] == ["v"]
    assert [i.id for i in screen([video(duration=0)]).kept] == ["v"]  # yt-dlp reports 0 for unknown


def test_sources_without_duration_rules_ignore_the_limits():
    assert [i.id for i in screen([video(duration=30)], duration_rules=False).kept] == ["v"]


@pytest.mark.parametrize("title", ["Highest-Paying Careers in 2026", "the HIGHEST-PAYING careers", "x highest-paying careers"])
def test_title_blocklist_matches_case_insensitive_substrings(title):
    result = screen([video(title=title)], title_blocklist=["Highest-Paying"])
    assert result.kept == [] and "highest-paying" in result.dropped[0].reason.lower()


def test_a_title_that_only_nearly_matches_the_blocklist_is_kept():
    assert screen([video(title="Highest Paying careers")], title_blocklist=["Highest-Paying"]).dropped == []


def test_channel_blocklist_matches_the_whole_channel_name_ignoring_case_and_padding():
    items = [video("a", author="Spam Channel"), video("b", author="Spam Channel Two"), video("c", author="  spam channel ")]
    result = screen(items, channel_blocklist=["SPAM CHANNEL"])
    assert [i.id for i in result.kept] == ["b"]
    assert [d.item_id for d in result.dropped] == ["a", "c"]


def test_the_allowlist_keeps_a_channel_whatever_else_would_drop_it():
    short_clickbait = video(author="Good Chan", title="Highest-Paying Careers", duration=30)
    result = screen([short_clickbait], channel_allowlist=["good chan"], channel_blocklist=["Good Chan"],
                    title_blocklist=["highest-paying"])
    assert [i.id for i in result.kept] == ["v"] and result.dropped == []
    assert result.allowlisted == {"v"}


def test_an_empty_blocklist_entry_matches_nothing():
    assert screen([video(title="anything")], title_blocklist=[""]).dropped == []


def test_the_first_drop_reason_wins_and_each_item_is_decided_once():
    result = screen([video(title="Highest-Paying", author="Spam", duration=10)],
                    title_blocklist=["highest"], channel_blocklist=["spam"])
    assert len(result.dropped) == 1
