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
from urllib.parse import urlsplit

from daily_pulse.notes import _write_atomic

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


def _try_parse(path: Path) -> Entry | None:
    """A note that cannot be read or understood is skipped: one bad file must not cost the day's digest."""
    try:
        return _parse_note(path)
    except (OSError, ValueError, TypeError):  # UnicodeDecodeError is a ValueError
        return None


def _load(notes_dir: Path, date: str) -> list[Entry]:
    day = Path(notes_dir) / date
    entries = [e for p in sorted(day.glob("*.md")) if (e := _try_parse(p))] if day.is_dir() else []
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
        if name not in (sources or {}):
            status = "ok"  # notes exist for a source the run result does not mention
        else:
            entry = sources[name]
            status = entry.get("status") if isinstance(entry, dict) else None
            status = " ".join(status.split()) if isinstance(status, str) and status.strip() else "unknown"
        if status == "ok":
            n = counts.get(name, 0)
            status = f"ok, {n} item{'' if n == 1 else 's'}"
        lines.append((name, status))
    return lines


def _safe_url(url: str) -> str:
    """The URL if it is an absolute http(s) link, else "" (never link javascript: and the like)."""
    parts = urlsplit(url.strip())
    return url.strip() if parts.scheme in ("http", "https") and parts.netloc else ""


def _join(base_url: str, *parts: str) -> str:
    """An absolute URL under base_url, or "" when no usable base is configured."""
    base = _safe_url(base_url)
    return base.rstrip("/") + "/" + "/".join(parts) if base else ""


def _note_url(base_url: str, date: str, entry: Entry) -> str:
    return _join(base_url, "notes", date, f"{entry.stem}.md")


def _md_text(text: str) -> str:
    return re.sub(r"([\\\[\]*_`<>])", r"\\\1", text)


def _md_link(text: str, url: str) -> str:
    url = _safe_url(url)
    if not url:
        return _md_text(text)
    return f"[{_md_text(text)}]({url.replace(' ', '%20').replace('(', '%28').replace(')', '%29')})"


def _meta(e: Entry) -> list[str]:
    return [p for p in (e.source, e.author, ", ".join(e.tags)) if p]


def _render_markdown(date, entries, diamonds, statuses, base_url) -> str:
    out = [f"# Daily Pulse — {date}", "", "## Diamonds in the rough", ""]
    out += [f"- **{e.rating}/10** {_md_link(e.title, e.url)} — {_md_text(e.reason)}" for e in diamonds] or ["None today."]
    out += ["", "## Ranked", ""]
    for i, e in enumerate(entries, 1):
        out.append(f"{i}. **{e.rating}/10 · {e.verdict}** {_md_link(e.title, e.url)} — {_md_text(e.reason)}")
        meta = [_md_text(p) for p in _meta(e)]
        note = _note_url(base_url, date, e)
        if note:
            meta.append(_md_link("note", note))
        if meta:
            out.append("   " + " · ".join(meta))
    if not entries:
        out.append("No items today.")
    out += ["", "## Sources", ""] + [f"- {_md_text(name)}: {_md_text(status)}" for name, status in statuses]
    archive = _join(base_url, "archive/")
    if archive:
        out += ["", _md_link("Archive", archive)]
    return "\n".join(out) + "\n"


_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_TEXT = f"font-family:{_FONT};font-size:15px;line-height:1.5;color:#1f2328;"
_MUTED = "color:#57606a;font-size:13px;"
_LINK = "color:#0550ae;"


def _h(text: str) -> str:
    return html.escape(text, quote=True)


def _a(text: str, url: str) -> str:
    url = _safe_url(url)
    return f'<a href="{_h(url)}" style="{_LINK}">{_h(text)}</a>' if url else _h(text)


def _html_item(e: Entry, date: str, base_url: str, badge: str) -> str:
    meta = _h(" · ".join(_meta(e)))
    note = _note_url(base_url, date, e)
    if note:
        meta += (" · " if meta else "") + _a("note", note)
    return (
        '<tr><td style="padding:10px 0;border-top:1px solid #d0d7de;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="64" valign="top" style="{_TEXT}font-weight:bold;">{badge}</td>'
        f'<td valign="top" style="{_TEXT}">'
        f'<div style="font-weight:bold;">{_a(e.title, e.url)}</div>'
        f'<div>{_h(e.reason)}</div>'
        f'<div style="{_MUTED}">{meta}</div>'
        "</td></tr></table></td></tr>"
    )


def _html_section(title: str, rows: list[str], empty: str) -> str:
    body = "".join(rows) or f'<tr><td style="{_TEXT}padding:10px 0;">{_h(empty)}</td></tr>'
    return (
        f'<tr><td style="{_TEXT}padding:20px 0 4px 0;font-size:18px;font-weight:bold;">{_h(title)}</td></tr>'
        + body
    )


def _render_html(date, entries, diamonds, statuses, base_url) -> str:
    diamond_rows = [_html_item(e, date, base_url, f"{e.rating}/10") for e in diamonds]
    ranked_rows = [_html_item(e, date, base_url, f"{e.rating}/10<br>{_h(e.verdict)}") for e in entries]
    status_rows = [
        f'<tr><td style="{_TEXT}padding:4px 0;border-top:1px solid #d0d7de;">'
        f"<strong>{_h(name)}</strong>: {_h(status)}</td></tr>"
        for name, status in statuses
    ]
    archive = _join(base_url, "archive/")
    footer = f'<tr><td style="{_TEXT}padding:16px 0 0 0;">{_a("Archive", archive)}</td></tr>' if archive else ""
    sections = (
        _html_section("Diamonds in the rough", diamond_rows, "None today.")
        + _html_section("Ranked", ranked_rows, "No items today.")
        + _html_section("Sources", status_rows, "No sources.")
        + footer
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Daily Pulse — {_h(date)}</title></head>\n"
        '<body style="margin:0;padding:0;background:#f6f8fa;">\n'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f6f8fa;"><tr><td align="center" style="padding:16px;">\n'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:640px;background:#ffffff;padding:16px 20px;">\n'
        f'<tr><td style="{_TEXT}font-size:24px;font-weight:bold;">Daily Pulse — {_h(date)}</td></tr>\n'
        f"{sections}\n"
        "</table>\n</td></tr></table>\n</body></html>\n"
    )


def build_digest(notes_dir: Path, date: str, sources: dict | None = None,
                 base_url: str = "", now: datetime | None = None) -> Digest:
    """Rank the day's notes and render them. `sources` is RunResult.sources (name -> {"status": ...});
    `now` defaults to the end of `date`, so the same inputs always give the same digest."""
    entries = _load(notes_dir, date)
    now = now or _default_now(date)
    diamonds = [e for e in entries if is_diamond(e, now)]
    statuses = _status_lines(entries, sources)
    return Digest(
        _render_markdown(date, entries, diamonds, statuses, base_url),
        _render_html(date, entries, diamonds, statuses, base_url),
    )


def write_digest(notes_dir: Path, digests_dir: Path, date: str, sources: dict | None = None,
                 base_url: str = "", now: datetime | None = None) -> tuple[Path, Path]:
    """Write `<digests_dir>/<date>.md` and `.html`. Files whose content is unchanged are left
    untouched, so rerunning a day is a no-op."""
    built = build_digest(notes_dir, date, sources=sources, base_url=base_url, now=now)
    md_path = Path(digests_dir) / f"{date}.md"
    html_path = Path(digests_dir) / f"{date}.html"
    _write_atomic(md_path, built.markdown, 0o644)  # published, so world-readable
    _write_atomic(html_path, built.html, 0o644)
    return md_path, html_path
