import pytest

from daily_pulse.models import Item
from daily_pulse.summarizer import Summarizer


def good(**over):
    reply = {
        "summary": "A tool that wraps agent runs in a maker-checker loop.",
        "takeaways": [],
        "beats": [],
        "rating": 8,
        "reason": "Directly about maker-checker loops for agents.",
        "verdict": "watch",
        "tags": ["maker-checker", "evals"],
        "diamond": True,
        "diamond_reason": "Early, few stars, no hype.",
    }
    reply.update(over)
    return reply


def repo_item(**over):
    base = dict(source="github", id="acme-checker", url="https://github.com/acme/checker",
                title="acme/checker", author="acme", text="Maker-checker loops for agents.")
    base.update(over)
    return Item(**base)


def test_rates_an_item_through_the_claude_binary(fake_claude):
    claude = fake_claude(good())
    rating = Summarizer(claude_cmd=str(claude.path)).rate(repo_item())
    assert rating.rating == 8
    assert rating.verdict == "watch"
    assert rating.reason == "Directly about maker-checker loops for agents."
    assert rating.tags == ["maker-checker", "evals"]
    assert rating.summary.startswith("A tool that wraps")
    assert rating.diamond is True
    assert rating.diamond_reason == "Early, few stars, no hype."
    assert claude.calls == 1
    assert "-p" in claude.argv(0)


RUBRIC_TOPICS = [
    "harness engineering",
    "multi-agent orchestration",
    "evals",
    "maker-checker loops",
    "model routing and cost",
    "guard rails for accounting-firm AI consulting",
]


def test_the_shipped_rubric_names_every_topic():
    from daily_pulse.summarizer import DEFAULT_PROMPTS_DIR

    rubric = (DEFAULT_PROMPTS_DIR / "rubric.md").read_text()
    for topic in RUBRIC_TOPICS:
        assert topic in rubric


def test_rubric_text_is_in_every_call_and_includes_the_item(fake_claude):
    claude = fake_claude("not json", good())
    Summarizer(claude_cmd=str(claude.path)).rate(repo_item(title="acme/checker"))
    from daily_pulse.summarizer import DEFAULT_PROMPTS_DIR

    rubric = (DEFAULT_PROMPTS_DIR / "rubric.md").read_text().strip()
    assert claude.calls == 2
    for n in range(2):
        assert rubric in claude.stdin(n)
        assert "acme/checker" in claude.stdin(n)


def test_prompts_are_read_from_disk_at_call_time(fake_claude, tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "rubric.md").write_text("RUBRIC-V1")
    (prompts / "summarize.md").write_text("DO IT\n{{ITEM}}")
    claude = fake_claude(good())
    s = Summarizer(claude_cmd=str(claude.path), prompts_dir=prompts)
    s.rate(repo_item())
    (prompts / "rubric.md").write_text("RUBRIC-V2")
    s.rate(repo_item())
    assert "RUBRIC-V1" in claude.stdin(0)
    assert "RUBRIC-V2" in claude.stdin(1)


def test_malformed_json_is_retried_once_then_succeeds(fake_claude):
    claude = fake_claude("Sure! here is my rating", good(rating=6))
    rating = Summarizer(claude_cmd=str(claude.path)).rate(repo_item())
    assert rating.rating == 6
    assert claude.calls == 2


def test_malformed_twice_skips_the_item_and_logs(fake_claude, caplog):
    claude = fake_claude("nope", "still nope")
    with caplog.at_level("WARNING", logger="daily_pulse"):
        rating = Summarizer(claude_cmd=str(claude.path)).rate(repo_item())
    assert rating is None
    assert claude.calls == 2  # one retry, no more
    assert "github-acme-checker" in caplog.text


@pytest.mark.parametrize("bad", [0, 11, -3, 7.5, "8", True, None])
def test_ratings_outside_1_to_10_are_rejected(fake_claude, bad):
    claude = fake_claude(good(rating=bad))
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None
    assert claude.calls == 2


@pytest.mark.parametrize("bad", ["maybe", "WATCH", "", None, 1])
def test_verdict_must_be_watch_skim_or_skip(fake_claude, bad):
    claude = fake_claude(good(verdict=bad))
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None


@pytest.mark.parametrize("verdict", ["watch", "skim", "skip"])
def test_each_valid_verdict_is_accepted(fake_claude, verdict):
    claude = fake_claude(good(verdict=verdict))
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()).verdict == verdict


@pytest.mark.parametrize("field,bad", [
    ("reason", ""), ("reason", 5), ("tags", "evals"), ("tags", [1]), ("diamond", "yes"),
    ("diamond", None), ("summary", ""), ("takeaways", "a"), ("beats", [{"timestamp": "x", "text": "t"}]),
    ("beats", ["no"]), ("diamond_reason", 3),
])
def test_other_malformed_fields_are_rejected(fake_claude, field, bad):
    claude = fake_claude(good(**{field: bad}))
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None


def test_a_diamond_needs_a_reason(fake_claude):
    claude = fake_claude(good(diamond=True, diamond_reason=""))
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None


def test_a_json_array_or_missing_key_is_malformed(fake_claude):
    claude = fake_claude([1, 2])
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None
    reply = good()
    del reply["verdict"]
    claude = fake_claude(reply)
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None


def test_a_reply_wrapped_in_a_code_fence_is_accepted(fake_claude):
    import json

    claude = fake_claude("```json\n" + json.dumps(good()) + "\n```")
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()).rating == 8
    assert claude.calls == 1


def test_video_beats_and_takeaways_come_through(fake_claude):
    reply = good(summary="", takeaways=["Route cheap tasks to small models."],
                 beats=[{"timestamp": "05:30", "text": "Introduces the router."}])
    claude = fake_claude(reply)
    item = repo_item(source="youtube", id="vid1", title="Routing talk")
    rating = Summarizer(claude_cmd=str(claude.path)).rate(item)
    assert rating.takeaways == ["Route cheap tasks to small models."]
    assert [(b.timestamp, b.text) for b in rating.beats] == [("05:30", "Introduces the router.")]


def test_a_non_video_item_needs_a_summary_but_a_video_does_not(fake_claude):
    claude = fake_claude(good(summary=""))
    video = repo_item(source="youtube", id="v", title="t")
    assert Summarizer(claude_cmd=str(claude.path)).rate(video) is not None
