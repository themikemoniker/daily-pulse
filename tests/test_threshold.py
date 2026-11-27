"""The rating threshold in the digest and the site: only items rated at least min_rating and not
"skip" are ranked; the rest are written but shown as one collapsed "Also seen: N below threshold"."""
import json
import re
from datetime import datetime, timezone

import pytest

from daily_pulse.cli import main
from daily_pulse.digest import build_digest
from daily_pulse.models import Item, Rating
from daily_pulse.notes import write_note
from daily_pulse.site import build_site

DAY = "2026-01-15"
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
BASE = "https://example.test/pulse/"


def add(out, item_id, title, rating, verdict="watch", published="2026-01-15T11:00:00Z", source="github"):
    item = Item(source=source, id=item_id, url=f"https://items.test/{item_id}", title=title, author="someone",
                published=published)
    r = Rating(rating=rating, reason=f"Reason for {title}.", verdict=verdict, tags=["evals"], summary=f"About {title}.")
    return write_note(item, r, out / "notes", DAY)


@pytest.fixture
def out(tmp_path):
    out = tmp_path / "out"
    add(out, "four", "Four item", 4)
    add(out, "five", "Five item", 5)
    add(out, "six", "Six item", 6)
    add(out, "skipped", "Skipped but high", 8, verdict="skip")
    return out


def ranked_md(md):
    return md.split("## Ranked", 1)[1].split("\n## ", 1)[0]


def shown(text):
    return [t for t in ("Four item", "Five item", "Six item", "Skipped but high") if t in text]


@pytest.mark.parametrize("threshold,visible,below", [
    (4, ["Four item", "Five item", "Six item"], 1),
    (5, ["Five item", "Six item"], 2),
    (6, ["Six item"], 3),
])
def test_the_digest_ranks_only_items_at_or_above_the_threshold_and_not_skip(out, threshold, visible, below):
    d = build_digest(out / "notes", DAY, base_url=BASE, now=NOW, min_rating=threshold)
    ranked = ranked_md(d.markdown)
    assert shown(ranked.split("Also seen")[0]) == visible
    assert f"Also seen: {below} below threshold" in d.markdown
    assert d.markdown.count("Also seen") == 1
    assert f"Also seen: {below} below threshold" in d.html and d.html.count("Also seen") == 1


def test_below_threshold_items_are_linked_from_the_also_seen_line(out):
    d = build_digest(out / "notes", DAY, base_url=BASE, now=NOW, min_rating=5)
    line = next(l for l in d.markdown.splitlines() if l.startswith("Also seen"))
    assert f"{BASE}notes/{DAY}/github-four.md" in line and f"{BASE}notes/{DAY}/github-skipped.md" in line
    assert "five" not in line
    assert f"{BASE}notes/{DAY}/github-four.md" in d.html


def test_the_default_threshold_is_five(out):
    d = build_digest(out / "notes", DAY, base_url=BASE, now=NOW)
    assert "Also seen: 2 below threshold" in d.markdown


def test_no_also_seen_line_when_nothing_is_below_the_threshold(tmp_path):
    add(tmp_path, "a", "Good item", 7)
    d = build_digest(tmp_path / "notes", DAY, now=NOW, min_rating=5)
    assert "Also seen" not in d.markdown and "Also seen" not in d.html


def test_a_diamond_must_be_shown_in_the_ranked_list(tmp_path):
    add(tmp_path, "g", "Skipped gem", 9, verdict="skip")
    add(tmp_path, "h", "Real gem", 9)
    d = build_digest(tmp_path / "notes", DAY, now=NOW, min_rating=5)
    diamonds = d.markdown.split("## Diamonds in the rough", 1)[1].split("\n## ", 1)[0]
    assert "Real gem" in diamonds and "Skipped gem" not in diamonds


def test_the_notes_themselves_are_still_written_for_below_threshold_items(out):
    assert {p.name for p in (out / "notes" / DAY).glob("*.md")} == {
        "github-four.md", "github-five.md", "github-six.md", "github-skipped.md"}


@pytest.mark.parametrize("threshold,visible,below", [
    (4, ["Four item", "Five item", "Six item"], 1),
    (5, ["Five item", "Six item"], 2),
    (6, ["Six item"], 3),
])
def test_the_site_shows_only_ranked_items_plus_one_collapsed_also_seen_line(out, tmp_path, threshold, visible, below):
    site = tmp_path / "site"
    build_site(out, site, DAY, min_rating=threshold)
    index = (site / "index.html").read_text()
    ranked, _, rest = index.partition("<details")
    assert shown(ranked) == visible
    assert index.count("<details") == 1 and f"Also seen: {below} below threshold" in rest.split("</summary>")[0]
    links = re.findall(r'href="(notes/[^"]+)"', rest.split("</details>")[0])
    assert len(links) == below and all((site / l).is_file() for l in links)  # still has a page each
    assert (site / "days" / f"{DAY}.html").read_text().count("<details") == 1


def test_the_site_threshold_comes_from_the_recorded_run_when_not_given(out, tmp_path):
    (out / f"run-{DAY}.json").write_text(json.dumps({"date": DAY, "generated_at": "2026-01-15T12:00:00+00:00",
                                                    "min_rating": 6, "sources": {}}))
    site = tmp_path / "site"
    build_site(out, site, DAY)
    assert shown(site.joinpath("index.html").read_text().partition("<details")[0]) == ["Six item"]
    out2 = tmp_path / "out2"  # a later run no longer holds the day: the site's own copy keeps its threshold
    out2.mkdir()
    build_site(out2, site, DAY)
    assert shown(site.joinpath("index.html").read_text().partition("<details")[0]) == ["Six item"]


def test_without_a_record_the_site_uses_the_default_threshold(out, tmp_path):
    site = tmp_path / "site"
    build_site(out, site, DAY)
    assert shown(site.joinpath("index.html").read_text().partition("<details")[0]) == ["Five item", "Six item"]


def test_the_archive_counts_ranked_items(out, tmp_path):
    site = tmp_path / "site"
    build_site(out, site, DAY, min_rating=5)
    assert "2 items" in (site / "archive" / "index.html").read_text()


def test_the_run_records_min_rating_and_the_digest_command_uses_it(make_env, fixtures_dir, out, tmp_path):
    from conftest import FakeAdapter
    from daily_pulse.adapters import registry
    registry.register(FakeAdapter())
    cfg, data, run_out = make_env()
    cfg.write_text(cfg.read_text().replace("[quality]\nmax_triage_calls = 0", "[quality]\nmin_rating = 6\nmax_triage_calls = 0"))
    assert main(["run", "--config", str(cfg), "--fixtures", str(fixtures_dir), "--out", str(run_out), "--date", DAY]) == 0
    assert json.loads((run_out / f"run-{DAY}.json").read_text())["min_rating"] == 6
    (run_out / "notes").mkdir(exist_ok=True)
    for src in (out / "notes" / DAY).glob("*.md"):
        (run_out / "notes" / DAY).mkdir(parents=True, exist_ok=True)
        (run_out / "notes" / DAY / src.name).write_text(src.read_text())
    assert main(["digest", "--config", str(cfg), "--out", str(run_out), "--date", DAY]) == 0
    md = (run_out / "digests" / f"{DAY}.md").read_text()
    assert shown(ranked_md(md).split("Also seen")[0]) == ["Six item"] and "Also seen: 3 below threshold" in md
