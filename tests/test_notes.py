from daily_pulse.models import Beat, Item, Rating
from daily_pulse.notes import write_note

DATE = "2026-01-15"


def video_item():
    return Item(
        source="youtube", id="abc123", url="https://youtu.be/abc123",
        title="Maker-checker loops in practice", author="Some Channel",
        published="2026-01-14T10:00:00Z", metrics={"duration": 754, "views": 12345},
    )


def video_rating(**over):
    base = dict(
        rating=8, reason="Concrete maker-checker design with eval numbers.", verdict="watch",
        tags=["evals", "maker-checker"],
        takeaways=[f"Takeaway number {i}" for i in range(1, 6)],
        beats=[Beat("00:00", "Sets up the problem"), Beat("05:30", "Shows the checker prompt")],
    )
    base.update(over)
    return Rating(**base)


def test_video_note_has_frontmatter_and_structured_body(tmp_path):
    path = write_note(video_item(), video_rating(), tmp_path / "notes", DATE)

    assert path == tmp_path / "notes" / DATE / "youtube-abc123.md"
    text = path.read_text()
    front, body = text.split("\n---\n", 1)
    assert front.splitlines() == [
        "---",
        'title: "Maker-checker loops in practice"',
        'channel: "Some Channel"',
        'video_id: "abc123"',
        'url: "https://youtu.be/abc123"',
        'published: "2026-01-14T10:00:00Z"',
        'duration: "12:34"',
        "views: 12345",
        "rating: 8",
        'tags: ["evals", "maker-checker"]',
    ]
    assert "## Key takeaways\n- Takeaway number 1\n" in body
    assert "- [00:00] Sets up the problem\n- [05:30] Shows the checker prompt" in body
    assert "**8/10** — Concrete maker-checker design with eval numbers." in body
    assert body.rstrip().endswith("## Verdict\n\nwatch")


import pytest


def generic_item(source):
    return Item(
        source=source, id="9001", url=f"https://example.test/{source}/9001",
        title=f"A {source} thing", author="someone", published="2026-01-14T08:00:00Z",
        metrics={"stars": 321}, text="short excerpt",
    )


def generic_rating():
    return Rating(rating=6, reason="Useful but narrow.", verdict="skim", tags=["evals"],
                  summary="Explains a small eval harness in plain words.")


@pytest.mark.parametrize("source", ["github", "instagram", "x"])
def test_generic_note_has_summary_rating_reason_verdict_and_tags(tmp_path, source):
    path = write_note(generic_item(source), generic_rating(), tmp_path / "notes", DATE)

    assert path == tmp_path / "notes" / DATE / f"{source}-9001.md"
    front, body = path.read_text().split("\n---\n", 1)
    assert f'source: "{source}"' in front
    assert 'title: "A %s thing"' % source in front
    assert "rating: 6" in front
    assert 'verdict: "skim"' in front
    assert 'tags: ["evals"]' in front
    assert "## Summary\n\nExplains a small eval harness in plain words." in body
    assert "**6/10** — Useful but narrow." in body
    assert "## Verdict\n\nskim" in body
    assert "Key takeaways" not in body


def test_writing_the_same_item_twice_yields_the_identical_file(tmp_path):
    first = write_note(video_item(), video_rating(), tmp_path / "notes", DATE)
    before = first.read_bytes()
    second = write_note(video_item(), video_rating(), tmp_path / "notes", DATE)
    assert second == first
    assert second.read_bytes() == before
    assert sorted(p.name for p in first.parent.iterdir()) == ["youtube-abc123.md"]
