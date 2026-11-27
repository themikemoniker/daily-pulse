"""Digest builder tests. Notes are produced by the real note writer, so the digest is tested
against the actual note format; nothing here touches the network or a subprocess."""
from datetime import datetime, timedelta, timezone

from daily_pulse.digest import build_digest
from daily_pulse.models import Item, Rating
from daily_pulse.notes import write_note

DATE = "2026-01-15"
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
BASE = "https://example.test/pulse/"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def add_note(notes_dir, item_id, title, rating, *, source="github", age_hours=1.0, now=NOW,
             reason="A reason.", verdict="skim", tags=("evals",), url=None, author="someone"):
    """Write one note through the real writer. `age_hours` is how old the item is at `now`."""
    item = Item(
        source=source, id=item_id, url=url or f"https://items.test/{source}/{item_id}",
        title=title, author=author, published=iso(now - timedelta(hours=age_hours)),
    )
    result = Rating(rating=rating, reason=reason, verdict=verdict, tags=list(tags),
                    summary=f"Own words about {title}.")
    return write_note(item, result, notes_dir, DATE)


def digest(notes_dir, sources=None, now=NOW, base_url=BASE):
    return build_digest(notes_dir, DATE, sources=sources or {}, base_url=base_url, now=now)


def order(text, *titles):
    """The titles, sorted by where they first appear in `text`."""
    return sorted(titles, key=text.index)


def test_items_are_ranked_by_rating_descending(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "a", "Low item", 3)
    add_note(notes, "b", "Top item", 8)
    add_note(notes, "c", "Middle item", 5)

    d = digest(notes)

    assert order(d.markdown, "Low item", "Top item", "Middle item") == ["Top item", "Middle item", "Low item"]
    assert order(d.html, "Low item", "Top item", "Middle item") == ["Top item", "Middle item", "Low item"]


def test_equal_ratings_are_ordered_by_recency_newest_first(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "old", "Older item", 6, age_hours=30)
    add_note(notes, "new", "Newer item", 6, age_hours=2)
    add_note(notes, "mid", "Middle item", 6, age_hours=10)

    d = digest(notes)

    assert order(d.markdown, "Older item", "Newer item", "Middle item") == ["Newer item", "Middle item", "Older item"]


def diamonds_md(text):
    """The markdown under the 'Diamonds in the rough' heading, up to the next heading."""
    head = "## Diamonds in the rough"
    assert text.index(head) < text.index("## Ranked"), "diamonds belong above the ranked list"
    after = text.split(head, 1)[1]
    return after.split("\n## ", 1)[0]


def diamonds_html(text):
    after = text.split("Diamonds in the rough", 1)[1]
    return after.split("Ranked", 1)[0]


def test_diamond_requires_rating_9_or_higher(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "eight", "Rating eight", 8, age_hours=1)
    add_note(notes, "nine", "Rating nine", 9, age_hours=1)
    add_note(notes, "ten", "Rating ten", 10, age_hours=1)

    d = digest(notes)

    section = diamonds_md(d.markdown)
    assert "Rating nine" in section and "Rating ten" in section
    assert "Rating eight" not in section
    assert "Rating eight" not in diamonds_html(d.html)
    assert "Rating nine" in diamonds_html(d.html)


def test_diamond_requires_age_under_48_hours(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "young", "Forty seven hours old", 9, age_hours=47)
    add_note(notes, "edge", "Exactly forty eight hours old", 9, age_hours=48)
    add_note(notes, "old", "Forty nine hours old", 9, age_hours=49)

    d = digest(notes)

    section = diamonds_md(d.markdown)
    assert "Forty seven hours old" in section
    assert "Exactly forty eight hours old" not in section
    assert "Forty nine hours old" not in section
    assert "Forty nine hours old" not in diamonds_html(d.html)


def test_items_without_a_usable_publish_time_are_never_diamonds(tmp_path):
    notes = tmp_path / "notes"
    item = Item(source="github", id="x", url="https://items.test/x", title="Undated gem", published="")
    write_note(item, Rating(rating=10, reason="r", verdict="watch", summary="s"), notes, DATE)

    d = digest(notes)

    assert "Undated gem" not in diamonds_md(d.markdown)
    assert "Undated gem" in d.markdown  # still ranked


def test_diamonds_stay_in_the_ranked_list_too(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "gem", "Rising gem", 9, age_hours=3)
    add_note(notes, "other", "Plain item", 4, age_hours=3)

    d = digest(notes)

    ranked = d.markdown.split("## Ranked", 1)[1]
    assert order(ranked, "Plain item", "Rising gem") == ["Rising gem", "Plain item"]


def test_no_diamonds_says_so(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "a", "Ordinary", 5)

    assert "None today" in diamonds_md(digest(notes).markdown)


def status_lines(text):
    """`source: status` lines from the Sources section of the markdown, as a dict."""
    section = text.split("## Sources", 1)[1].split("\n## ", 1)[0]
    pairs = (line[2:].split(": ", 1) for line in section.splitlines() if line.startswith("- "))
    return {name: status for name, status in pairs}


def test_every_source_gets_a_status_line(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "v1", "Video one", 7, source="youtube")
    add_note(notes, "v2", "Video two", 6, source="youtube")
    add_note(notes, "r1", "Repo one", 5, source="github")
    sources = {
        "youtube": {"status": "ok", "items": [{}, {}]},
        "instagram": {"status": "deferred: no CLI configured", "items": []},
        "x": {"status": "deferred: no usable route", "items": []},
        "github": {"status": "ok", "items": [{}]},
    }

    lines = status_lines(digest(notes, sources).markdown)

    assert lines == {
        "youtube": "ok, 2 items",
        "instagram": "deferred: no CLI configured",
        "x": "deferred: no usable route",
        "github": "ok, 1 item",
    }


def test_failed_and_unconfigured_sources_show_status_not_a_gap(tmp_path):
    notes = tmp_path / "notes"
    sources = {
        "youtube": {"status": "not configured", "items": []},
        "github": {"status": "failed", "items": [], "error": "RuntimeError: boom", "consecutive_failures": 1},
    }

    d = digest(notes, sources)

    assert status_lines(d.markdown) == {"youtube": "not configured", "github": "failed"}
    for html_text in (d.html,):
        assert "youtube" in html_text and "not configured" in html_text
        assert "github" in html_text and "failed" in html_text
    assert "RuntimeError" not in d.markdown + d.html  # no internals leaked into a published page


def test_ok_source_with_nothing_new_still_says_so(tmp_path):
    sources = {"github": {"status": "ok", "items": []}}

    assert status_lines(digest(tmp_path / "notes", sources).markdown) == {"github": "ok, 0 items"}


def test_a_day_with_no_notes_still_builds_a_page(tmp_path):
    d = digest(tmp_path / "missing", {"youtube": {"status": "not configured", "items": []}})

    assert "No items today." in d.markdown
    assert "No items today." in d.html
