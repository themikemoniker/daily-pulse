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
