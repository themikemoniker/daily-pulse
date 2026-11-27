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
