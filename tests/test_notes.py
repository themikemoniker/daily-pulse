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


import re

from daily_pulse.notes import write_transcript

TRANSCRIPT = " ".join(f"word{i}" for i in range(120))


def quoted_words(text: str) -> int:
    """Words in blockquote lines plus words inside double quotes: what a reader sees as quoted."""
    n = sum(len(line[1:].split()) for line in text.splitlines() if line.startswith(">"))
    n += sum(len(q.split()) for q in re.findall(r'"([^"]*)"|“([^”]*)”', text.split("\n---\n", 1)[1])
             for q in q if q)
    return n


def test_note_never_copies_more_than_forty_words_of_the_transcript(tmp_path):
    copied = " ".join(f"word{i}" for i in range(5, 65))  # 60 words
    rating = video_rating(
        takeaways=[f"Takeaway {i}" for i in range(4)] + [f"He explains {copied} in detail"],
        beats=[Beat("01:00", copied)],
    )
    path = write_note(video_item(), rating, tmp_path / "notes", DATE, transcript=TRANSCRIPT)
    from daily_pulse.quoteguard import longest_copied_run
    assert longest_copied_run(path.read_text(), TRANSCRIPT) <= 40
    assert "word64" not in path.read_text()


def test_quotes_are_capped_well_under_fifty_words_per_note(tmp_path):
    quotes = [" ".join(f"q{k}w{i}" for i in range(30)) for k in range(4)]  # 120 words offered
    rating = video_rating(
        quotes=quotes,
        takeaways=["He says \"" + " ".join(f"t{i}" for i in range(30)) + "\" outright"] * 5,
    )
    text = write_note(video_item(), rating, tmp_path / "notes", DATE, transcript=TRANSCRIPT).read_text()
    assert 0 < quoted_words(text) <= 49


def test_short_quotes_are_kept_as_blockquotes(tmp_path):
    rating = video_rating(quotes=["evals are your unit tests now"])
    text = write_note(video_item(), rating, tmp_path / "notes", DATE).read_text()
    assert "## Quotes\n\n> evals are your unit tests now" in text


def test_transcript_goes_only_under_data_transcripts(tmp_path):
    notes, data = tmp_path / "notes", tmp_path / "data"
    item = video_item()
    path = write_transcript(data, item, TRANSCRIPT)
    write_note(item, video_rating(), notes, DATE, transcript=TRANSCRIPT)

    assert path == data / "transcripts" / "youtube-abc123.txt"
    assert path.read_text() == TRANSCRIPT
    for note in notes.rglob("*"):
        if note.is_file():
            assert "word50 word51 word52" not in note.read_text()
    assert write_transcript(data, item, TRANSCRIPT) == path  # idempotent
    assert path.read_text() == TRANSCRIPT


from daily_pulse.notes import NoteError


@pytest.mark.parametrize("over", [{"rating": 0}, {"rating": 11}, {"rating": "8"}, {"verdict": "binge"},
                                  {"reason": "  "}, {"beats": [Beat("soon", "x")]}])
def test_invalid_ratings_are_refused_and_nothing_is_written(tmp_path, over):
    with pytest.raises(NoteError):
        write_note(video_item(), video_rating(**over), tmp_path / "notes", DATE)
    assert not (tmp_path / "notes").exists()


def test_hostile_ids_cannot_escape_the_notes_directory(tmp_path):
    item = video_item()
    item.id = "../../evil"
    path = write_note(item, video_rating(), tmp_path / "notes", DATE)
    assert path.parent == tmp_path / "notes" / DATE
    assert write_transcript(tmp_path / "data", item, "x").parent == tmp_path / "data" / "transcripts"


def test_instagram_caption_is_guarded_like_a_transcript(tmp_path):
    caption = " ".join(f"cap{i}" for i in range(70))
    item = generic_item("instagram")
    item.text = caption
    rating = generic_rating()
    rating.summary = caption
    text = write_note(item, rating, tmp_path / "notes", DATE).read_text()
    assert "cap69" not in text
    assert "cap0 cap1" in text  # a short snippet survives


def test_note_text_is_collapsed_to_single_lines(tmp_path):
    rating = video_rating(reason="line one\nline two")
    text = write_note(video_item(), rating, tmp_path / "notes", DATE).read_text()
    assert "**8/10** — line one line two" in text
