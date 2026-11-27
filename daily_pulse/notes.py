"""Writes one markdown note per item, and keeps transcripts out of the published tree.

Notes go to `<notes_dir>/<date>/<source>-<id>.md` and carry own-words text only. Every free-text
field passes through the quote guard (see quoteguard.py), so a note never copies more than 40
consecutive words of the local transcript/caption and never quotes 50 or more words. Full
transcripts are written only by `write_transcript`, to `<data_dir>/transcripts/`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from daily_pulse.models import VERDICTS, Item, Rating
from daily_pulse.quoteguard import MAX_COPY_RUN, QuoteBudget, guard_fields, guard_text, longest_copied_run

VIDEO_SOURCES = ("youtube",)
MAX_TAKEAWAYS = 8

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# 05:30, 105:12 (up to 3-digit minutes) or 1:05:30 (H:MM:SS)
TIMESTAMP = re.compile(r"[0-9]{1,3}:[0-5][0-9](:[0-5][0-9])?")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
MAX_STEM = 120  # keeps `<stem>-<hash>.md` far below filesystem name limits


class NoteError(ValueError):
    """The item or rating cannot be turned into a safe note."""


def _safe_name(source: str, item_id: str) -> str:
    """A filename stem that cannot escape the notes directory; ids that needed cleaning get a
    short hash so two different ids never share a file."""
    stem = f"{source}-{item_id}"
    clean = _UNSAFE.sub("_", stem)
    if clean != stem or len(clean) > MAX_STEM:
        clean = clean[:MAX_STEM] + "-" + hashlib.sha1(stem.encode()).hexdigest()[:8]
    return clean


def _one_line(text) -> str:
    return " ".join(("" if text is None else str(text)).split())


def _fmt_duration(value) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        h, rest = divmod(value, 3600)
        m, s = divmod(rest, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return str(value)


def _frontmatter(pairs: list[tuple[str, object]]) -> str:
    lines = ["---"]
    for key, value in pairs:
        is_int = isinstance(value, int) and not isinstance(value, bool)
        lines.append(f"{key}: {value if is_int else json.dumps(value, ensure_ascii=False, default=str)}")
    return "\n".join(lines)


def _validate(item: Item, rating: Rating, date: str) -> None:
    if not isinstance(date, str) or not _DATE.fullmatch(date):
        raise NoteError(f"date must be YYYY-MM-DD, got {date!r}")
    if isinstance(rating.rating, bool) or not isinstance(rating.rating, int) or not 1 <= rating.rating <= 10:
        raise NoteError(f"rating must be an integer 1-10, got {rating.rating!r}")
    if rating.verdict not in VERDICTS:
        raise NoteError(f"verdict must be one of {', '.join(VERDICTS)}, got {rating.verdict!r}")
    if not _one_line(item.title):
        raise NoteError(f"{item.source}-{item.id}: item has no title")
    if not _one_line(rating.reason):
        raise NoteError("rating needs a one-line reason")
    if item.source not in VIDEO_SOURCES and not _one_line(rating.summary):
        raise NoteError(f"{item.source}-{item.id}: a {item.source} note needs a summary")
    for beat in rating.beats:
        if not TIMESTAMP.fullmatch(str(beat.timestamp)):
            raise NoteError(f"beat timestamp must look like 05:30 or 1:05:30, got {beat.timestamp!r}")


def _video(item: Item, rating: Rating, gl, head: dict) -> tuple[list, list]:
    metrics = item.metrics or {}
    front = [
        ("title", head["title"]),
        ("channel", head["author"]),
        ("video_id", item.id),
        ("url", item.url),
        ("published", item.published),
        ("duration", _fmt_duration(metrics.get("duration", ""))),
        ("views", metrics.get("views", 0)),
        ("rating", rating.rating),
        ("tags", head["tags"]),
    ]
    takeaways = gl([_one_line(t) for t in rating.takeaways[:MAX_TAKEAWAYS]])
    beats = gl([_one_line(b.text) for b in rating.beats])
    body = ["## Key takeaways", *[f"- {t}" for t in takeaways], "",
            "## The argument, beat by beat", *[f"- [{b.timestamp}] {t}" for b, t in zip(rating.beats, beats)]]
    return front, body


def _generic(item: Item, rating: Rating, gl, head: dict) -> tuple[list, list]:
    front = [
        ("title", head["title"]),
        ("source", item.source),
        ("id", item.id),
        ("url", item.url),
        ("author", head["author"]),
        ("published", item.published),
        ("rating", rating.rating),
        ("verdict", rating.verdict),
        ("tags", head["tags"]),
    ]
    velocity = (item.metrics or {}).get("star_velocity")
    if (item.source == "github" and item.id.startswith(("rising:", "repo:"))
            and isinstance(velocity, (int, float)) and not isinstance(velocity, bool)):
        # New repos only (a lifetime average says nothing about a release of a mature repo): the
        # digest's GitHub diamond rule reads it.
        front.append(("star_velocity", velocity))
    return front, ["## Summary", "", gl([_one_line(rating.summary)])[0]]


def render_note(item: Item, rating: Rating, date: str, transcript: str = "") -> str:
    """The note's text. `transcript` (and the item's own excerpt) is only used to check that
    nothing is copied from it."""
    _validate(item, rating, date)
    reference = "\n".join(part for part in (transcript, item.text) if part)
    budget = QuoteBudget()
    # Explicit quotes spend the budget first, then quoted spans inside the free text.
    quotes = [q for q in (budget.fit(_one_line(q)) for q in rating.quotes) if q]
    quotes = guard_fields(quotes, reference)  # adjacent quotes must not add up to one long copy

    def gl(fields: list[str]) -> list[str]:
        return guard_fields(fields, reference, budget)

    def g(text: str) -> str:
        return gl([_one_line(text)])[0]

    # Title, author and tags are free text too: guard them once and reuse the result.
    # The title appears twice (frontmatter and heading), so it may spend only half the remainder
    # and is charged double.
    title_budget = QuoteBudget(limit=(budget.limit - budget.used) // 2)
    head = {"title": guard_text(_one_line(item.title), reference, title_budget)}
    budget.used += 2 * title_budget.used
    head["author"] = g(item.author)
    head["tags"] = gl([_one_line(t) for t in rating.tags or []])
    front, middle = (_video if item.source in VIDEO_SOURCES else _generic)(item, rating, gl, head)
    body = [f"# {head['title']}", "", *middle, ""]
    if quotes:
        body += ["## Quotes", "", *[f"> {q}" for q in quotes], ""]
    body += ["## Rating", "", f"**{rating.rating}/10** — {g(rating.reason)}", "",
             "## Verdict", "", rating.verdict]
    text = _frontmatter(front) + "\n---\n\n" + "\n".join(body) + "\n"
    if reference and longest_copied_run(text, reference) > MAX_COPY_RUN:  # belt and braces
        raise NoteError(f"{item.source}-{item.id}: note still copies the source text")
    return text


def _write_atomic(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        os.chmod(path, mode)
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_note(item: Item, rating: Rating, notes_dir: Path, date: str, transcript: str = "") -> Path:
    """Write the note for one item; writing the same item twice gives the identical file."""
    text = render_note(item, rating, date, transcript)
    path = Path(notes_dir) / date / f"{_safe_name(item.source, item.id)}.md"
    _write_atomic(path, text, 0o644)  # published, so world-readable
    return path


def write_transcript(data_dir: Path, item: Item, text: str) -> Path:
    """Keep the full transcript/caption locally. The only place one is ever written."""
    path = Path(data_dir) / "transcripts" / f"{_safe_name(item.source, item.id)}.txt"
    _write_atomic(path, text, 0o600)  # private to the host
    return path
