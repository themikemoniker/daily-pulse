"""Shared data model."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Item:
    """One thing from a source (video, reel, post, repo) flowing through the pipeline."""

    source: str
    id: str
    url: str
    title: str
    author: str = ""
    published: str = ""  # ISO 8601 string
    metrics: dict = field(default_factory=dict)
    text: str = ""  # short excerpt only; never a full transcript or caption

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Beat:
    """One step of a video's argument, with the timestamp it starts at (e.g. "05:30")."""

    timestamp: str
    text: str


VERDICTS = ("watch", "skim", "skip")


@dataclass
class Rating:
    """The rater's result for one item (own-words summary, never source text)."""

    rating: int  # 1-10 against the rubric
    reason: str  # one line
    verdict: str  # one of VERDICTS
    tags: list[str] = field(default_factory=list)
    summary: str = ""  # generic items
    takeaways: list[str] = field(default_factory=list)  # videos: 5-8
    beats: list[Beat] = field(default_factory=list)  # videos: argument with timestamps
    quotes: list[str] = field(default_factory=list)  # short verbatim snippets, budgeted
    diamond: bool = False  # high-signal, early and low-noise
    diamond_reason: str = ""
