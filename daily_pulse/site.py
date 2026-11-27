"""Builds the static site (plain HTML and CSS, no JavaScript) from notes and digests.

Only `notes/*.md` and `digests/<date>.{md,html}` are read from the output directory; nothing
else (state, transcripts, `data/`) can reach the site. Notes and digests are copied into the site
tree first and every page is built from that tree, so earlier days stay in the archive even when
the output directory no longer holds them. Pages are written only when their content changed.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date as _date
from pathlib import Path

from daily_pulse.digest import (Entry, _default_now, _load, _safe_url, _status_lines, is_diamond,
                                parse_time)
from daily_pulse.notes import _write_atomic

CSS = """\
:root { --bg: #ffffff; --fg: #1f2328; --muted: #57606a; --link: #0550ae; --rule: #d0d7de;
  --gem-bg: #fff8c5; --gem-border: #bf8700; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --link: #58a6ff; --rule: #30363d;
    --gem-bg: #3b2e00; --gem-border: #d29922; }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0 auto; max-width: 42rem; padding: 16px; background: var(--bg); color: var(--fg);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  overflow-wrap: anywhere; }
a { color: var(--link); }
h1 { font-size: 1.5rem; margin: 0 0 8px; }
h2 { font-size: 1.15rem; margin: 24px 0 8px; }
nav { margin-bottom: 16px; color: var(--muted); }
nav a { margin-right: 12px; }
ol.items, ul.plain { list-style: none; padding: 0; margin: 0; }
.item { padding: 12px 0; border-top: 1px solid var(--rule); }
.meta { color: var(--muted); font-size: 0.875rem; }
.badge { font-weight: bold; margin-right: 8px; }
.diamond { background: var(--gem-bg); border-left: 4px solid var(--gem-border); padding: 8px 12px;
  margin: 8px 0; font-weight: bold; }
blockquote { margin: 8px 0; padding-left: 12px; border-left: 3px solid var(--rule); color: var(--muted); }
ul { padding-left: 1.25rem; }
"""


def _h(text: str) -> str:
    return html.escape(str(text), quote=True)


def _page(title: str, root: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_h(title)}</title>"
        f'<link rel="stylesheet" href="{root}style.css"></head>\n'
        f"<body>\n<nav><a href=\"{root}index.html\">Today</a><a href=\"{root}archive/index.html\">Archive</a></nav>\n"
        f"{body}\n</body></html>\n"
    )


def _callout() -> str:
    return '<div class="diamond">Diamond in the rough: high signal, early, low noise.</div>'


def _inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", _h(text))


def _render_body(body: str) -> str:
    """The note's markdown as HTML. Block structure only (headings, bullets, quotes, paragraphs)
    plus **bold**; everything is escaped, so a note can never inject markup."""
    out: list[str] = []
    open_list = False
    for line in body.splitlines():
        item = line[2:] if line.startswith("- ") else None
        if open_list and item is None:
            out.append("</ul>")
            open_list = False
        if not line.strip():
            continue
        if line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            continue  # the page supplies its own title
        elif item is not None:
            if not open_list:
                out.append("<ul>")
                open_list = True
            out.append(f"<li>{_inline(item)}</li>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
        else:
            out.append(f"<p>{_inline(line)}</p>")
    if open_list:
        out.append("</ul>")
    return "\n".join(out)


def _item_html(e: Entry, date: str, root: str, diamond: bool) -> str:
    meta = " · ".join(_h(p) for p in (e.source, e.author, ", ".join(e.tags)) if p)
    note = f"{root}notes/{date}/{e.stem}.html"
    return (
        '<li class="item">'
        + (_callout() if diamond else "")
        + f'<div><span class="badge">{e.rating}/10 · {_h(e.verdict)}</span>'
        f'<a href="{_h(note)}">{_h(e.title)}</a></div>'
        f"<div>{_h(e.reason)}</div>"
        f'<div class="meta">{meta}</div></li>'
    )


def _run_info(out: Path, date: str) -> tuple[dict, object]:
    try:
        recorded = json.loads((out / f"run-{date}.json").read_text(encoding="utf-8"))
        sources = recorded.get("sources", {})
        return (sources if isinstance(sources, dict) else {}), parse_time(recorded.get("generated_at"))
    except (OSError, ValueError, AttributeError):
        return {}, None


def _day_body(site: Path, out: Path, date: str, root: str) -> str:
    entries = _load(site / "notes", date)
    sources, now = _run_info(out, date)
    now = now or _default_now(date)
    rows = "\n".join(_item_html(e, date, root, is_diamond(e, now)) for e in entries)
    ranked = f'<ol class="items">\n{rows}\n</ol>' if rows else "<p>No items today.</p>"
    status = "".join(f"<li><strong>{_h(n)}</strong>: {_h(s)}</li>" for n, s in _status_lines(entries, sources))
    digests = site / "digests"
    links = [f'<a href="{root}digests/{date}.{ext}">digest ({ext})</a>' for ext in ("md", "html")
             if (digests / f"{date}.{ext}").is_file()]
    return (
        f"<h1>Daily Pulse — {_h(date)}</h1>\n<h2>Ranked</h2>\n{ranked}\n"
        f'<h2>Sources</h2>\n<ul class="plain">{status}</ul>\n'
        + (f'<p class="meta">{" · ".join(links)}</p>' if links else "")
    )


def _note_page(site: Path, date: str, e: Entry, now) -> str:
    raw = (site / "notes" / date / f"{e.stem}.md").read_text(encoding="utf-8")
    body = raw.split("\n---\n", 1)[1] if raw.startswith("---\n") and "\n---\n" in raw else raw
    root = "../../"
    link = _safe_url(e.url)
    meta = " · ".join(_h(p) for p in (e.source, e.author, ", ".join(e.tags), date) if p)
    return _page(e.title, root, (
        f"<h1>{_h(e.title)}</h1>\n"
        + (_callout() if is_diamond(e, now) else "")
        + f'\n<p class="meta">{meta}</p>\n'
        + (f'<p><a href="{_h(link)}">Original</a> · ' if link else "<p>")
        + f'<a href="{e.stem}.md">raw note</a> · <a href="{root}days/{date}.html">back to {date}</a></p>\n'
        + _render_body(body)
    ))


def _valid_days(root: Path) -> list[str]:
    days = []
    if root.is_dir():
        for p in root.iterdir():
            try:
                _date.fromisoformat(p.name)
            except ValueError:
                continue
            if p.is_dir():
                days.append(p.name)
    return sorted(days, reverse=True)


def _copy_file(src: Path, dest: Path) -> None:
    """Copy one published input. A file that cannot be read as text, a symlink (which could point
    anywhere, `data/` included) or a non-file is skipped: one bad file must not cost the site."""
    try:
        if src.is_symlink() or not src.is_file():
            return
        _write_atomic(dest, src.read_text(encoding="utf-8"), 0o644)
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return


def _copy_inputs(out: Path, site: Path) -> None:
    for day in _valid_days(out / "notes"):
        for src in sorted((out / "notes" / day).glob("*.md")):
            if not src.name.startswith("."):
                _copy_file(src, site / "notes" / day / src.name)
    digests = out / "digests"
    if digests.is_dir():
        for src in sorted(digests.iterdir()):
            try:
                _date.fromisoformat(src.stem)
            except ValueError:
                continue
            if src.suffix in (".md", ".html"):
                _copy_file(src, site / "digests" / src.name)


def build_site(out: Path, site: Path, date: str) -> list[Path]:
    """Build `site/` from `out/notes`, `out/digests` and `out/run-<date>.json`. `date` is the day
    shown on `index.html`. Returns the pages written (unchanged files are left untouched)."""
    out, site = Path(out), Path(site)
    _copy_inputs(out, site)
    days = sorted(set(_valid_days(site / "notes")) | {date}, reverse=True)
    pages: dict[Path, str] = {Path("style.css"): CSS}
    for day in days:
        now = _run_info(out, day)[1] or _default_now(day)
        entries = _load(site / "notes", day)
        pages[Path("days") / f"{day}.html"] = _page(f"Daily Pulse — {day}", "../", _day_body(site, out, day, "../"))
        for e in entries:
            pages[Path("notes") / day / f"{e.stem}.html"] = _note_page(site, day, e, now)
    pages[Path("index.html")] = _page(f"Daily Pulse — {date}", "", _day_body(site, out, date, ""))
    rows = []
    for day in days:
        n = len(_load(site / "notes", day))
        rows.append(f'<li class="item"><a href="../days/{day}.html">{day}</a> '
                    f'<span class="meta">{n} item{"" if n == 1 else "s"}</span></li>')
    pages[Path("archive") / "index.html"] = _page(
        "Daily Pulse archive", "../", '<h1>Archive</h1>\n<ul class="plain">\n' + "\n".join(rows) + "\n</ul>")
    written = []
    for rel, text in pages.items():
        _write_atomic(site / rel, text, 0o644)
        written.append(site / rel)
    return written
