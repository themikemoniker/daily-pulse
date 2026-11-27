"""Builds the daily digest (markdown and email-ready HTML) from the day's notes.

The digest is derived only from fields the note writer already guarded (title, author, rating,
reason, verdict, tags, summary), so it never carries transcript text or quotes.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIAMOND_MIN_RATING = 9
DIAMOND_MAX_AGE = timedelta(hours=48)  # strictly younger than this

_BOLD_RATING = re.compile(r"^\*\*(\d+)/10\*\* — (.*)$", re.M)


@dataclass
class Entry:
    source: str
    stem: str  # note filename without extension
    title: str
    url: str
    author: str
    published: datetime | None
    rating: int
    reason: str
    verdict: str
    tags: list[str]


@dataclass
class Digest:
    markdown: str
    html: str


def _parse_time(text: str) -> datetime | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_note(path: Path) -> Entry | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text:
        return None
    front_text, body = text[4:].split("\n---\n", 1)
    front: dict = {}
    for line in front_text.splitlines():
        key, sep, raw = line.partition(": ")
        if not sep:
            continue
        try:
            front[key] = json.loads(raw)
        except ValueError:
            front[key] = raw
    rated = _BOLD_RATING.search(body)
    if rated is None:
        return None
    verdict = front.get("verdict")
    if verdict is None:
        m = re.search(r"^## Verdict\n\n(\S+)", body, re.M)
        verdict = m.group(1) if m else ""
    return Entry(
        source=path.stem.split("-", 1)[0],
        stem=path.stem,
        title=str(front.get("title", path.stem)),
        url=str(front.get("url", "")),
        author=str(front.get("author", front.get("channel", ""))),
        published=_parse_time(front.get("published", "")),
        rating=int(rated.group(1)),
        reason=rated.group(2),
        verdict=verdict,
        tags=[str(t) for t in front.get("tags", [])],
    )


def _load(notes_dir: Path, date: str) -> list[Entry]:
    day = Path(notes_dir) / date
    entries = [e for p in sorted(day.glob("*.md")) if (e := _parse_note(p))] if day.is_dir() else []
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    entries.sort(key=lambda e: (-e.rating, -(e.published or epoch).timestamp(), e.source, e.stem))
    return entries


def _default_now(date: str) -> datetime:
    """End of the run date (UTC). Derived from the date, never the wall clock, so rebuilding a
    day's digest later gives the identical file."""
    return datetime.fromisoformat(date).replace(tzinfo=timezone.utc) + timedelta(days=1)


def is_diamond(entry: Entry, now: datetime) -> bool:
    """High signal (rating 9+) and early (published less than 48 hours before `now`).
    An item with no usable publish time cannot be shown to be early, so it is never one."""
    if entry.published is None or entry.rating < DIAMOND_MIN_RATING:
        return False
    return now - entry.published < DIAMOND_MAX_AGE


def _status_lines(entries: list[Entry], sources: dict | None) -> list[tuple[str, str]]:
    """(source, status) per source. Item counts come from the notes on disk rather than the run
    result, so rebuilding after a rerun that found nothing new still reports what is shown."""
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.source] = counts.get(e.source, 0) + 1
    names = list(sources or {}) + sorted(n for n in counts if n not in (sources or {}))
    lines = []
    for name in names:
        status = str((sources or {}).get(name, {}).get("status", "ok"))
        if status == "ok":
            n = counts.get(name, 0)
            status = f"ok, {n} item{'' if n == 1 else 's'}"
        lines.append((name, status))
    return lines


def build_digest(notes_dir: Path, date: str, sources: dict | None = None,
                 base_url: str = "", now: datetime | None = None) -> Digest:
    entries = _load(notes_dir, date)
    now = now or _default_now(date)
    diamonds = [e for e in entries if is_diamond(e, now)]
    statuses = _status_lines(entries, sources)
    md = [f"# Daily Pulse — {date}", "", "## Diamonds in the rough", ""]
    md += [f"- **{e.rating}/10** [{e.title}]({e.url}) — {e.reason}" for e in diamonds] or ["None today."]
    md += ["", "## Ranked", ""]
    md += [f"{i}. **{e.rating}/10 · {e.verdict}** [{e.title}]({e.url}) — {e.reason}"
           for i, e in enumerate(entries, 1)] or ["No items today."]
    md += ["", "## Sources", ""] + [f"- {name}: {status}" for name, status in statuses]

    def rows(items):
        return "".join(f"<tr><td>{e.rating}/10</td><td>{html.escape(e.title)}</td></tr>" for e in items)

    status_rows = "".join(f"<tr><td>{html.escape(n)}</td><td>{html.escape(s)}</td></tr>" for n, s in statuses)
    page = (f"<h2>Diamonds in the rough</h2><table>{rows(diamonds)}</table>"
            f"<h2>Ranked</h2><table>{rows(entries) or '<tr><td>No items today.</td></tr>'}</table>"
            f"<h2>Sources</h2><table>{status_rows}</table>")
    return Digest("\n".join(md) + "\n", page)
