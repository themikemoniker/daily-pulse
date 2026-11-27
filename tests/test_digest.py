"""Digest builder tests. Notes are produced by the real note writer, so the digest is tested
against the actual note format; nothing here touches the network or a subprocess."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from daily_pulse.digest import build_digest, write_digest
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


class Page(HTMLParser):
    """Every tag and attribute in a page, for asserting what the HTML may contain."""

    def __init__(self, text):
        super().__init__()
        self.tags, self.attrs = [], []  # attrs: (tag, name, value)
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [(tag, k, v) for k, v in attrs]

    def values(self, name):
        return [v for _, k, v in self.attrs if k == name]


def full_digest(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "v1", "A video", 9, source="youtube", url="https://youtu.be/v1")
    add_note(notes, "r1", "A repo", 5, url="https://github.com/o/r")
    return digest(notes, {"youtube": {"status": "ok", "items": [{}]}, "x": {"status": "deferred: none", "items": []}})


def test_html_is_email_ready_with_no_external_resources(tmp_path):
    page = Page(full_digest(tmp_path).html)

    assert not {"link", "script", "style", "img", "iframe", "form", "object", "embed"} & set(page.tags)
    assert not page.values("src") and not page.values("srcset")
    assert "table" in page.tags
    assert all("url(" not in v and "@import" not in v for v in page.values("style"))
    assert page.values("style"), "styles must be inline"


def test_html_is_a_complete_standalone_document(tmp_path):
    text = full_digest(tmp_path).html

    assert text.lstrip().lower().startswith("<!doctype html>")
    page = Page(text)
    assert {"html", "head", "body", "title"} <= set(page.tags)
    assert "2026-01-15" in text


def test_every_link_is_an_item_url_or_built_from_the_base_url(tmp_path):
    page = Page(full_digest(tmp_path).html)

    hrefs = page.values("href")
    assert "https://youtu.be/v1" in hrefs and "https://github.com/o/r" in hrefs
    for href in hrefs:
        assert href in ("https://youtu.be/v1", "https://github.com/o/r") or href.startswith(BASE), href
    assert f"{BASE}notes/{DATE}/youtube-v1.md" in hrefs
    assert f"{BASE}archive/" in hrefs


def test_base_url_is_normalised_and_optional(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "r1", "A repo", 5, url="https://github.com/o/r")

    no_slash = Page(digest(notes, base_url="https://example.test/pulse").html).values("href")
    assert f"https://example.test/pulse/notes/{DATE}/github-r1.md" in no_slash

    none = Page(digest(notes, base_url="").html).values("href")
    assert none == ["https://github.com/o/r"]  # nothing relative is invented


def test_titles_are_escaped_and_unsafe_urls_are_not_linked(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "evil", "<script>alert(1)</script> & <b>bold</b>", 5, url="javascript:alert(1)")

    d = digest(notes)

    page = Page(d.html)
    assert "script" not in page.tags and "b" not in page.tags
    assert "&lt;script&gt;" in d.html
    assert not any(h.lower().startswith("javascript:") for h in page.values("href"))
    assert "javascript:" not in d.markdown


def test_markdown_link_text_survives_brackets_in_titles(tmp_path):
    notes = tmp_path / "notes"
    add_note(notes, "b", "Why [evals] (still) matter", 5, url="https://items.test/a_(b)")

    line = next(l for l in digest(notes).markdown.splitlines() if l.startswith("1. "))

    assert "[Why \\[evals\\] (still) matter](https://items.test/a_%28b%29)" in line


def test_write_digest_creates_markdown_and_html_files(tmp_path):
    notes, out = tmp_path / "notes", tmp_path / "digests"
    add_note(notes, "r1", "A repo", 5)

    md_path, html_path = write_digest(notes, out, DATE, sources={}, base_url=BASE, now=NOW)

    assert md_path == out / f"{DATE}.md" and html_path == out / f"{DATE}.html"
    built = digest(notes)
    assert md_path.read_text(encoding="utf-8") == built.markdown
    assert html_path.read_text(encoding="utf-8") == built.html


def test_rerunning_for_the_same_date_gives_identical_output(tmp_path):
    notes, out = tmp_path / "notes", tmp_path / "digests"
    add_note(notes, "a", "Item a", 9, age_hours=5)
    add_note(notes, "b", "Item b", 4)
    sources = {"github": {"status": "ok", "items": [{}, {}]}, "x": {"status": "deferred: no route", "items": []}}

    first = write_digest(notes, out, DATE, sources=sources, base_url=BASE, now=NOW)
    before = [p.read_bytes() for p in first]
    mtimes = [p.stat().st_mtime_ns for p in first]
    second = write_digest(notes, out, DATE, sources=sources, base_url=BASE, now=NOW)

    assert second == first
    assert [p.read_bytes() for p in second] == before
    assert [p.stat().st_mtime_ns for p in second] == mtimes  # unchanged content is not rewritten


def test_default_reference_time_comes_from_the_date_not_the_clock(tmp_path):
    notes = tmp_path / "notes"
    # Published 40 hours before the end of 2026-01-15 UTC: a diamond today, and still on a rebuild next year.
    item = Item(source="github", id="d", url="https://items.test/d", title="Dated gem",
                published="2026-01-14T08:00:00Z")
    write_note(item, Rating(rating=9, reason="r", verdict="watch", summary="s"), notes, DATE)

    d = build_digest(notes, DATE, sources={}, base_url=BASE)  # no `now`

    assert "Dated gem" in diamonds_md(d.markdown)
    assert d == build_digest(notes, DATE, sources={}, base_url=BASE)


def test_output_does_not_depend_on_the_order_notes_were_written(tmp_path):
    specs = [("a", "Alpha", 6), ("b", "Beta", 6), ("c", "Gamma", 6), ("d", "Delta", 7)]
    results = []
    for name, ordering in (("one", specs), ("two", list(reversed(specs)))):
        notes = tmp_path / name / "notes"
        for item_id, title, rating in ordering:
            add_note(notes, item_id, title, rating, age_hours=3)  # same age: ties all the way down
        results.append(digest(notes))

    assert results[0] == results[1]


def test_digest_carries_no_quotes_or_transcript_text(tmp_path):
    notes = tmp_path / "notes"
    transcript = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
    item = Item(source="youtube", id="q", url="https://youtu.be/q", title="Quoted talk",
                published=iso(NOW - timedelta(hours=2)), metrics={"duration": 60, "views": 1})
    rating = Rating(rating=7, reason="Fine.", verdict="watch", takeaways=["One", "Two", "Three", "Four", "Five"],
                    quotes=["alpha bravo charlie delta echo"], summary="")
    write_note(item, rating, notes, DATE, transcript=transcript)
    assert "alpha bravo" in (notes / DATE / "youtube-q.md").read_text()  # the note itself quotes it

    d = digest(notes)

    assert "alpha bravo" not in d.markdown and "alpha bravo" not in d.html
    assert "Quoted talk" in d.markdown
