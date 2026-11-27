"""The source-adapter interface every source plugs into."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from daily_pulse.models import Item


@dataclass
class AdapterContext:
    config: dict = field(default_factory=dict)  # this source's [sources.<name>] table
    fixtures_dir: Path | None = None  # set by --fixtures; adapters read recorded inputs from it
    date: str = ""  # run date, YYYY-MM-DD
    data_dir: Path | None = None  # local-only data (transcripts); never published
    notes_dir: Path | None = None  # where per-item notes are written (<out>/notes); None: write none
    summarizer: object | None = None  # a daily_pulse.summarizer.Summarizer shared by the whole run
    dry_run: bool = False  # write nothing to disk and make no `claude` calls


class SourceAdapter(ABC):
    name: str

    def status(self, ctx: AdapterContext) -> str | None:
        """None when ready to run. Otherwise a status string such as
        "deferred: <reason>" or "not configured"; the runner records it and skips the adapter."""
        return None

    @abstractmethod
    def list_new(self, ctx: AdapterContext) -> list[Item]:
        """Candidate items, newest first. The runner drops ones already processed."""

    @abstractmethod
    def fetch_details(self, item: Item, ctx: AdapterContext) -> Item:
        """Fill in the full details for one new item."""
