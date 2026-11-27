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
