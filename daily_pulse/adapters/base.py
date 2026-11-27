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
    data_dir: Path | None = None  # local-only state dir; adapters keep private state under it
    persist: bool = True  # False on --dry-run: adapters must not write anything
    notes_dir: Path | None = None  # where per-item notes are written (<out>/notes); None: write none
    summarizer: object | None = None  # a daily_pulse.summarizer.Summarizer shared by the whole run


class ItemSkipped(Exception):
    """Raised by `fetch_details` for an item that can never be processed (or has failed too
    often): the runner marks it processed and carries on, with no error and no item."""


class ItemDeferred(Exception):
    """Raised by `fetch_details` when an item is not ready yet (not an error): the runner leaves
    it unprocessed, so the next run looks at it again, and reports nothing as failed."""


class SourceAdapter(ABC):
    name: str
    quality_gate = False  # True: the runner screens new items (rules, triage) before fetch_details
    quality_duration = False  # True: the gate's min/max duration rules apply to this source's items
    rated_by_runner = False  # True: the runner rates each fetched item and writes its note (see runner.py).
    # False: the adapter does it inside `fetch_details` (YouTube), or the source yields no notes.

    def status(self, ctx: AdapterContext) -> str | None:
        """None when ready to run. Otherwise a status string such as
        "deferred: <reason>" or "not configured"; the runner records it and skips the adapter."""
        return None

    def rating_text(self, item: Item, ctx: AdapterContext) -> str:
        """Extra source text the rater may read for `item` (a caption, say) beyond the item's own
        fields. Used only for rating and the quote guard; it is never stored. Default: none."""
        return ""

    def rank_new(self, items: list[Item], ctx: AdapterContext) -> tuple[list[Item], list[tuple[Item, str]]]:
        """Called by the runner on the NEW items only (already-processed ones are gone), before the
        quality gate and the first-run cap. Returns (the items to carry on with, in the order to take
        them, and [(item, reason)] for items weeded out for good: the runner logs them, lists them as
        dropped and marks them processed, so nothing is consumed silently). An item in neither list
        simply waits for a later run. Default: nothing changes."""
        return items, []

    @abstractmethod
    def list_new(self, ctx: AdapterContext) -> list[Item]:
        """Candidate items, newest first. The runner drops ones already processed."""

    @abstractmethod
    def fetch_details(self, item: Item, ctx: AdapterContext) -> Item:
        """Fill in the full details for one new item."""
