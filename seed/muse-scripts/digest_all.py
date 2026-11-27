#!/usr/bin/env python3
"""digest_all: one daily digest combining YouTube, Instagram, X, GitHub.

Reads the four modules' state files, takes items processed today,
ranks them by rating (unrated last), and writes:
  digests/daily-YYYY-MM-DD.md  +  .html (email-ready, inline CSS)

Skimmable in ~5 minutes: a "Read first" top 3, then the rest grouped
by source. Per-source notes stay where their module wrote them.

Run after the four modules, e.g. via run_all.sh.
Stdlib only.
"""
import argparse
import html
import json
import re
from datetime import date
from pathlib import Path

BASE = Path.home() / "workspace/yt-digest"
SOURCES = [
    ("youtube", "YouTube", BASE / "state.json", BASE / "notes"),
    ("instagram", "Instagram", BASE / "ig-state.json", BASE / "ig-notes"),
    ("x", "X", BASE / "x-state.json", BASE / "x-notes"),
    ("github", "GitHub", BASE / "gh-state.json", BASE / "gh-notes"),
]


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def parse_frontmatter(note_path):
    """Parse the simple `key: value` frontmatter block of a note."""
    try:
        text = Path(note_path).read_text()
    except OSError:
        return {}, ""
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"')
    return fm, text


def parse_takeaways(note_text, limit=3):
    m = re.search(r"## Key takeaways\n(.*?)(?=\n## |\Z)", note_text, re.S)
    if not m:
        return []
    return [re.sub(r"^[-*]\s*", "", ln).strip()
            for ln in m.group(1).strip().splitlines()
            if ln.strip().startswith(("-", "*"))][:limit]


def parse_rating_reason(note_text):
    m = re.search(r"## Rating[^\n]*\n+(.*?)(?=\n## |\Z)", note_text, re.S)
    if not m:
        return ""
    lines = [ln.strip() for ln in m.group(1).strip().splitlines()
             if ln.strip()]
    if not lines:
        return ""
    reason = re.sub(r"^\d+(?:\.\d+)?\s*/\s*10\s*[—\-–:]*\s*", "",
                    lines[0])
    return reason


def parse_blurb(note_text, limit=200):
    """One-line summary for notes without takeaways: first line of the
    'What it is' section (Instagram) or the first body paragraph."""
    m = re.search(r"## What it is\n(.*?)(?=\n## |\Z)", note_text, re.S)
    if m:
        for ln in m.group(1).strip().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith(("_", "*")):
                return (ln[:limit].rsplit(" ", 1)[0] + "…"
                        if len(ln) > limit else ln)
    return ""


def collect(day):
    items = []
    for key, label, state_path, note_dir in SOURCES:
        state = load_json(state_path, {})
        records = state.get("items") or state.get("videos") or {}
        for _id, rec in records.items():
            if rec.get("processed_at") != day:
                continue
            if rec.get("status") not in ("summarized", "fetched", "noted"):
                continue
            note = rec.get("note")
            fm, text = parse_frontmatter(note) if note else ({}, "")
            try:
                rating = float(fm.get("rating")) if fm.get("rating") else None
            except ValueError:
                rating = None
            items.append({
                "source": key, "source_label": label,
                "title": rec.get("title") or fm.get("title", _id),
                "url": rec.get("url") or fm.get("url", ""),
                "note": note or "",
                "rating": rating,
                "verdict": fm.get("verdict") or rec.get("verdict") or "",
                "kind": fm.get("kind") or rec.get("kind") or "",
                "diamond": fm.get("diamond") == "true"
                           or rec.get("diamond") is True,
                "takeaways": parse_takeaways(text),
                "reason": parse_rating_reason(text)
                          or parse_blurb(text),
                "published": rec.get("published") or fm.get("published", ""),
            })
    items.sort(key=lambda it: (it["rating"] is None,
                               -(it["rating"] or 0)))
    return items


def render_md(day, items):
    lines = [f"# Daily digest — {day}", "",
             f"{len(items)} new item(s) across YouTube, Instagram, X and "
             "GitHub. Ranked by rating; unrated items at the bottom.", ""]
    if not items:
        lines.append("_Nothing new today._")
        lines.append("")
        return "\n".join(lines)
    top = [it for it in items if it["rating"] is not None][:3]
    if top:
        lines.append("## Read first")
        lines.append("")
        for it in top:
            lines.append(f"- **{it['title']}** ({it['source_label']}, "
                         f"{it['rating']:g}/10)"
                         + (" **[diamond]**" if it["diamond"] else "")
                         + f" — {it['reason'] or it['verdict'] or ''}")
        lines.append("")
    for label in ("YouTube", "Instagram", "X", "GitHub"):
        group = [it for it in items if it["source_label"] == label]
        if not group:
            continue
        lines.append(f"## {label}")
        lines.append("")
        for it in group:
            rating = f"{it['rating']:g}/10" if it["rating"] is not None else "—"
            lines.append(f"### {it['title']}")
            lines.append("")
            lines.append(f"{rating}"
                         + (" **[diamond]**" if it["diamond"] else "")
                         + (f" · {it['verdict']}" if it["verdict"] else "")
                         + (f" · {it['kind']}" if it["kind"] else ""))
            lines.append("")
            for t in it["takeaways"]:
                lines.append(f"- {t}")
            if it["takeaways"]:
                lines.append("")
            elif it["reason"]:
                lines.append(f"{it['reason']}")
                lines.append("")
            links = f"[Open]({it['url']})" if it["url"] else ""
            if it["note"]:
                links += (f" · [Full note]({it['note']})" if links
                          else f"[Full note]({it['note']})")
            if links:
                lines.append(links)
                lines.append("")
    return "\n".join(lines)


def render_html(day, items):
    rows = []
    for it in items:
        rating = (f"{it['rating']:g}/10"
                  if it["rating"] is not None else "—")
        lis = "".join(f"<li>{html.escape(t)}</li>"
                      for t in it["takeaways"])
        reason = (f"<p>{html.escape(it['reason'])}</p>"
                  if it["reason"] and not lis else "")
        rows.append(
            f"<h2>{html.escape(it['title'])}</h2>"
            f"<p>{html.escape(it['source_label'])} · <b>{rating}</b>"
            + (" · <b>[diamond]</b>" if it["diamond"] else "")
            + (f" · {html.escape(it['verdict'])}" if it["verdict"] else "")
            + "</p>"
            + (f"<ul>{lis}</ul>" if lis else "")
            + reason
            + (f"<p><a href=\"{html.escape(it['url'])}\">Open</a></p>"
               if it["url"] else ""))
    body = ("<p><i>Nothing new today.</i></p>" if not rows
            else "\n".join(rows))
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Daily digest — {day}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:24px auto;
padding:0 16px;color:#111}}h1{{font-size:22px}}h2{{font-size:17px;margin-top:28px}}
li{{margin:4px 0}}a{{color:#0b5fff}}</style></head>
<body><h1>Daily digest — {day}</h1>
<p>{len(items)} new item(s) across YouTube, Instagram, X and GitHub.</p>
{body}</body></html>
"""


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="digest_all.py",
        description="Combine all four sources into one daily digest.")
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--output-dir", default=str(BASE / "digests"))
    args = p.parse_args(argv)

    items = collect(args.date)
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    md_path = outdir / f"daily-{args.date}.md"
    md_path.write_text(render_md(args.date, items))
    html_path = md_path.with_suffix(".html")
    html_path.write_text(render_html(args.date, items))
    by_source = {}
    for it in items:
        by_source[it["source_label"]] = by_source.get(it["source_label"], 0) + 1
    print(f"daily digest: {len(items)} item(s) {by_source} -> {md_path.name}")
    return 0


if __name__ == "__main__":
    main()
