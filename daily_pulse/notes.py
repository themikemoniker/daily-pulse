"""Writes one markdown note per item."""
from __future__ import annotations

import json
from pathlib import Path

from daily_pulse.models import Item, Rating


def _fmt_duration(value) -> str:
    if isinstance(value, int):
        h, rest = divmod(value, 3600)
        m, s = divmod(rest, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return str(value)


def _frontmatter(pairs: list[tuple[str, object]]) -> str:
    lines = ["---"]
    for key, value in pairs:
        lines.append(f"{key}: {value if isinstance(value, int) else json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines)


def write_note(item: Item, rating: Rating, notes_dir: Path, date: str) -> Path:
    front = _frontmatter([
        ("title", item.title),
        ("channel", item.author),
        ("video_id", item.id),
        ("url", item.url),
        ("published", item.published),
        ("duration", _fmt_duration(item.metrics.get("duration", ""))),
        ("views", item.metrics.get("views", 0)),
        ("rating", rating.rating),
        ("tags", rating.tags),
    ])
    body = [f"# {item.title}", "", "## Key takeaways", *[f"- {t}" for t in rating.takeaways], "",
            "## The argument, beat by beat", *[f"- [{b.timestamp}] {b.text}" for b in rating.beats], "",
            "## Rating", "", f"**{rating.rating}/10** — {rating.reason}", "",
            "## Verdict", "", rating.verdict]
    path = Path(notes_dir) / date / f"{item.source}-{item.id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(front + "\n---\n\n" + "\n".join(body) + "\n")
    return path
