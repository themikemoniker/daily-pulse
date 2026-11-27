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
from daily_pulse.quoteguard import MAX_COPY_RUN, QuoteBudget, guard_text, longest_copied_run

VIDEO_SOURCES = ("youtube",)
MAX_TAKEAWAYS = 8

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIMESTAMP = re.compile(r"\d{1,2}(:\d{2}){1,2}")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


class NoteError(ValueError):
    """The item or rating cannot be turned into a safe note."""


def _safe_name(source: str, item_id: str) -> str:
    """A filename stem that cannot escape the notes directory; ids that needed cleaning get a
    short hash so two different ids never share a file."""
    stem = f"{source}-{item_id}"
    clean = _UNSAFE.sub("_", stem)
    if clean != stem:
        clean += "-" + hashlib.sha1(stem.encode()).hexdigest()[:8]
    return clean


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


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
        lines.append(f"{key}: {value if is_int else json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines)


def _validate(rating: Rating, date: str) -> None:
    if not _DATE.fullmatch(date):
        raise NoteError(f"date must be YYYY-MM-DD, got {date!r}")
    if isinstance(rating.rating, bool) or not isinstance(rating.rating, int) or not 1 <= rating.rating <= 10:
        raise NoteError(f"rating must be an integer 1-10, got {rating.rating!r}")
    if rating.verdict not in VERDICTS:
        raise NoteError(f"verdict must be one of {', '.join(VERDICTS)}, got {rating.verdict!r}")
    if not _one_line(rating.reason):
        raise NoteError("rating needs a one-line reason")
    for beat in rating.beats:
        if not _TIMESTAMP.fullmatch(str(beat.timestamp)):
            raise NoteError(f"beat timestamp must look like 05:30 or 1:05:30, got {beat.timestamp!r}")


def _video(item: Item, rating: Rating, g) -> tuple[list, list]:
    front = [
        ("title", _one_line(item.title)),
        ("channel", _one_line(item.author)),
        ("video_id", item.id),
        ("url", item.url),
        ("published", item.published),
        ("duration", _fmt_duration(item.metrics.get("duration", ""))),
        ("views", item.metrics.get("views", 0)),
        ("rating", rating.rating),
        ("tags", [_one_line(t) for t in rating.tags]),
    ]
    body = ["## Key takeaways", *[f"- {g(t)}" for t in rating.takeaways[:MAX_TAKEAWAYS]], "",
            "## The argument, beat by beat", *[f"- [{b.timestamp}] {g(b.text)}" for b in rating.beats]]
    return front, body


def _generic(item: Item, rating: Rating, g) -> tuple[list, list]:
    front = [
        ("title", _one_line(item.title)),
        ("source", item.source),
        ("id", item.id),
        ("url", item.url),
        ("author", _one_line(item.author)),
        ("published", item.published),
        ("rating", rating.rating),
        ("verdict", rating.verdict),
        ("tags", [_one_line(t) for t in rating.tags]),
    ]
    return front, ["## Summary", "", g(rating.summary)]


def render_note(item: Item, rating: Rating, date: str, transcript: str = "") -> str:
    """The note's text. `transcript` (and the item's own excerpt) is only used to check that
    nothing is copied from it."""
    _validate(rating, date)
    reference = "\n".join(part for part in (transcript, item.text) if part)
    budget = QuoteBudget()
    # Explicit quotes spend the budget first, then quoted spans inside the free text.
    quotes = [q for q in (budget.fit(_one_line(q)) for q in rating.quotes) if q]

    def g(text: str) -> str:
        return guard_text(_one_line(text), reference, budget)

    front, middle = (_video if item.source in VIDEO_SOURCES else _generic)(item, rating, g)
    body = [f"# {_one_line(item.title)}", "", *middle, ""]
    if quotes:
        body += ["## Quotes", "", *[f"> {q}" for q in quotes], ""]
    body += ["## Rating", "", f"**{rating.rating}/10** — {g(rating.reason)}", "",
             "## Verdict", "", rating.verdict]
    text = _frontmatter(front) + "\n---\n\n" + "\n".join(body) + "\n"
    if reference and longest_copied_run(text, reference) > MAX_COPY_RUN:  # belt and braces
        raise NoteError(f"{item.source}-{item.id}: note still copies the source text")
    return text


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text:
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_note(item: Item, rating: Rating, notes_dir: Path, date: str, transcript: str = "") -> Path:
    """Write the note for one item; writing the same item twice gives the identical file."""
    text = render_note(item, rating, date, transcript)
    path = Path(notes_dir) / date / f"{_safe_name(item.source, item.id)}.md"
    _write_atomic(path, text)
    return path


def write_transcript(data_dir: Path, item: Item, text: str) -> Path:
    """Keep the full transcript/caption locally. The only place one is ever written."""
    path = Path(data_dir) / "transcripts" / f"{_safe_name(item.source, item.id)}.txt"
    _write_atomic(path, text)
    return path
