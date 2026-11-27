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
