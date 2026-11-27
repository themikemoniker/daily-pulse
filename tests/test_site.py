"""Site builder tests. Notes come from the real note writer; nothing touches the network."""
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from daily_pulse.models import Beat, Item, Rating
from daily_pulse.notes import write_note
from daily_pulse.site import build_site

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
DAY = "2026-01-15"
PREV = "2026-01-14"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def add_note(out, item_id, title, rating, *, date=DAY, source="youtube", age_hours=1.0, now=NOW, **kw):
    item = Item(source=source, id=item_id, url=f"https://items.test/{item_id}", title=title,
                author="Chan", published=iso(now - timedelta(hours=age_hours)))
    r = Rating(rating=rating, reason=kw.get("reason", "A reason."), verdict="watch", tags=["evals"],
               summary=f"Own words about {title}.", takeaways=["First point"], beats=[Beat("0:10", "beat")],
               quotes=["short quote"])
    return write_note(item, r, out / "notes", date)


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links, self.tags, self.attrs = [], set(), []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.attrs += attrs
        for k, v in attrs:
            if k in ("href", "src") and v:
                self.links.append(v)


def site_files(site):
    return sorted(p for p in site.rglob("*") if p.is_file())


def test_builds_index_archive_day_and_note_pages_from_notes(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "a", "Top video", 8)
    add_note(out, "b", "Low video", 3)

    build_site(out, site, DAY)

    index = (site / "index.html").read_text()
    assert index.index("Top video") < index.index("Low video")
    assert (site / "archive" / "index.html").is_file()
    assert DAY in (site / "archive" / "index.html").read_text()
    assert (site / "days" / f"{DAY}.html").is_file()
    note = (site / "notes" / DAY / "youtube-a.html").read_text()
    assert "Top video" in note and "First point" in note and "[0:10]" in note
    assert (site / "notes" / DAY / "youtube-a.md").is_file()  # raw note copied


def test_every_relative_link_in_the_built_tree_resolves(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "a", "Top video", 9)
    add_note(out, "p", "Yesterday", 5, date=PREV, now=NOW - timedelta(days=1))
    (out / "digests").mkdir()
    (out / "digests" / f"{DAY}.md").write_text("# d\n")
    (out / "digests" / f"{DAY}.html").write_text("<p>d</p>")

    build_site(out, site, DAY)

    checked = 0
    for page in site.rglob("*.html"):
        if page.parts[len(site.parts)] == "digests":
            continue
        for link in Page(page.read_text()).links:
            parts = urlsplit(link)
            if parts.scheme:
                continue
            target = (page.parent / unquote(parts.path)).resolve() if parts.path else page
            if link.endswith("/"):
                target = target / "index.html"
            assert target.is_file(), f"{page.relative_to(site)} -> {link}"
            checked += 1
    assert checked > 8


def test_no_javascript_and_no_external_assets(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "a", "Top video", 9)
    build_site(out, site, DAY)

    for page in site.rglob("*.html"):
        text = page.read_text()
        p = Page(text)
        assert "script" not in p.tags and "iframe" not in p.tags
        assert not [k for k, _ in p.attrs if k.startswith("on")]
        for tag_link in re.findall(r'<link[^>]*>', text):
            assert "http" not in tag_link
    css = (site / "style.css").read_text()
    assert "prefers-color-scheme: dark" in css
    assert "http" not in css and "@import" not in css and "url(" not in css
    assert "16px" in css
    assert 'name="viewport"' in (site / "index.html").read_text()


def test_diamond_items_get_a_visible_callout(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "gem", "Early gem", 9, age_hours=5)
    add_note(out, "old", "Old hit", 9, age_hours=60)
    build_site(out, site, DAY)

    index = (site / "index.html").read_text()
    assert index.count('class="diamond"') == 1
    assert "Diamond in the rough" in index
    assert index.index('class="diamond"') < index.index("Old hit")
    assert 'class="diamond"' not in (site / "notes" / DAY / "youtube-old.html").read_text()
    assert 'class="diamond"' in (site / "notes" / DAY / "youtube-gem.html").read_text()


def test_source_status_lines_come_from_the_run_result(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "a", "Top video", 5)
    (out / f"run-{DAY}.json").write_text(json.dumps(
        {"generated_at": iso(NOW), "sources": {"youtube": {"status": "ok"}, "x": {"status": "deferred: no route"}}}))
    build_site(out, site, DAY)

    index = (site / "index.html").read_text()
    assert "deferred: no route" in index and "ok, 1 item" in index


def test_rebuild_is_deterministic_and_keeps_earlier_days(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "p", "Yesterday", 5, date=PREV, now=NOW - timedelta(days=1))
    build_site(out, site, PREV)
    add_note(out, "a", "Today", 7)
    build_site(out, site, DAY)
    first = {p: p.read_bytes() for p in site_files(site)}
    mtimes = {p: p.stat().st_mtime_ns for p in first}

    build_site(out, site, DAY)

    assert {p: p.read_bytes() for p in site_files(site)} == first
    assert {p: p.stat().st_mtime_ns for p in first} == mtimes
    archive = (site / "archive" / "index.html").read_text()
    assert archive.index(DAY) < archive.index(PREV)  # newest first
    assert "Yesterday" in (site / "days" / f"{PREV}.html").read_text()


def test_earlier_days_survive_when_out_no_longer_holds_them(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "p", "Yesterday", 5, date=PREV, now=NOW - timedelta(days=1))
    build_site(out, site, PREV)
    out2 = tmp_path / "out2"
    add_note(out2, "a", "Today", 7)

    build_site(out2, site, DAY)

    assert PREV in (site / "archive" / "index.html").read_text()
    assert (site / "days" / f"{PREV}.html").is_file()


def test_nothing_from_data_dir_or_unknown_files_reaches_the_site(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "a", "Top video", 5)
    (out / "data" / "transcripts").mkdir(parents=True)
    (out / "data" / "transcripts" / "t.txt").write_text("SECRET TRANSCRIPT")
    (out / "notes" / DAY / "notes.txt").write_text("SECRET TRANSCRIPT")
    (out / "state.json").write_text("SECRET TRANSCRIPT")
    build_site(out, site, DAY)

    assert not (site / "data").exists()
    assert all("SECRET" not in p.read_text() for p in site_files(site))


def test_html_in_titles_is_escaped(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "a", "<script>alert(1)</script>", 5)
    build_site(out, site, DAY)

    for page in site.rglob("*.html"):
        assert "<script" not in page.read_text()


def test_empty_day_still_builds(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    out.mkdir()
    build_site(out, site, DAY)
    assert "No items" in (site / "index.html").read_text()


def test_cli_site_command_builds_site_under_out(tmp_path):
    from daily_pulse.cli import main

    out = tmp_path / "out"
    add_note(out, "a", "Top video", 5)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndata_dir = "{tmp_path / "state"}"\n')

    assert main(["site", "--config", str(cfg), "--date", DAY, "--out", str(out)]) == 0

    assert "Top video" in (out / "site" / "index.html").read_text()
    assert main(["site", "--config", str(cfg), "--date", DAY, "--out", str(out), "--site", str(tmp_path / "s2")]) == 0
    assert (tmp_path / "s2" / "archive" / "index.html").is_file()


def test_unreadable_or_symlinked_inputs_are_skipped_not_fatal(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "a", "Top video", 5)
    day = out / "notes" / DAY
    (day / "bad.md").write_bytes(b"\xff\xfe\x00")
    (day / "dir.md").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET")
    (day / "link.md").symlink_to(secret)
    (out / "digests").mkdir()
    (out / "digests" / f"{DAY}.md").write_bytes(b"\xff\xfe")

    build_site(out, site, DAY)

    assert "Top video" in (site / "index.html").read_text()
    assert all("SECRET" not in p.read_text(errors="replace") for p in site_files(site))
    assert not (site / "notes" / DAY / "link.md").exists()


def test_cli_site_command_needs_no_config_file(tmp_path, monkeypatch):
    from daily_pulse.cli import main

    out = tmp_path / "out"
    add_note(out, "a", "Top video", 5)
    monkeypatch.chdir(tmp_path)  # no config/daily-pulse.toml here
    assert main(["site", "--date", DAY, "--out", str(out)]) == 0


def test_symlinked_day_directory_publishes_nothing(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    add_note(out, "a", "Top video", 5)
    private = tmp_path / "private"
    add_note(tmp_path / "priv-out", "t", "PRIVATE TRANSCRIPT", 5, date="2026-01-10")
    (out / "notes" / "2026-01-10").symlink_to(tmp_path / "priv-out" / "notes" / "2026-01-10")
    other = tmp_path / "outB"
    (other).mkdir()
    (other / "notes").symlink_to(out / "notes")

    build_site(out, site, DAY)
    build_site(other, tmp_path / "siteB", DAY)

    assert not (site / "notes" / "2026-01-10").exists()
    assert all("PRIVATE" not in p.read_text() for p in site_files(site))
    assert not (tmp_path / "siteB" / "notes").exists()
    assert not private.exists()


def test_past_day_keeps_callout_and_statuses_when_run_file_is_gone(tmp_path):
    out1, out2, site = tmp_path / "out1", tmp_path / "out2", tmp_path / "site"
    add_note(out1, "gem", "Early gem", 9, age_hours=5, date=PREV, now=NOW - timedelta(days=1))
    (out1 / f"run-{PREV}.json").write_text(json.dumps(
        {"generated_at": iso(NOW - timedelta(days=1)), "sources": {"x": {"status": "deferred: no route"}}}))
    build_site(out1, site, PREV)
    before = (site / "days" / f"{PREV}.html").read_text()
    note_before = (site / "notes" / PREV / "youtube-gem.html").read_text()
    assert 'class="diamond"' in before and "deferred: no route" in before

    add_note(out2, "a", "Today", 7)
    build_site(out2, site, DAY)

    assert (site / "days" / f"{PREV}.html").read_text() == before
    assert (site / "notes" / PREV / "youtube-gem.html").read_text() == note_before


def test_write_failure_fails_the_build(tmp_path):
    import pytest

    out = tmp_path / "out"
    add_note(out, "a", "Top video", 5)
    blocked = tmp_path / "site"
    blocked.write_text("a file, not a directory")

    with pytest.raises(OSError):
        build_site(out, blocked, DAY)

    from daily_pulse.cli import main
    assert main(["site", "--date", DAY, "--out", str(out), "--site", str(blocked)]) == 1


def test_deleted_note_disappears_from_the_site(tmp_path):
    out, site = tmp_path / "out", tmp_path / "site"
    gone = add_note(out, "a", "Doomed", 5)
    add_note(out, "b", "Kept", 4)
    build_site(out, site, DAY)
    assert (site / "notes" / DAY / "youtube-a.html").is_file()

    gone.unlink()
    build_site(out, site, DAY)

    assert not (site / "notes" / DAY / "youtube-a.md").exists()
    assert not (site / "notes" / DAY / "youtube-a.html").exists()
    assert "Doomed" not in (site / "index.html").read_text()
    assert "1 item" in (site / "archive" / "index.html").read_text()
    assert (site / "notes" / DAY / "youtube-b.html").is_file()
