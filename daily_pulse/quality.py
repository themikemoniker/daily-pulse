"""The quality gate: weeds out low-quality and off-topic items before the expensive summary.

Two layers, cheapest first. Rules (`[quality]` in the config) look only at channel, title and
duration. Items that pass may then be triaged by one batched `claude -p` call per 40 items.
Allowlisted channels bypass both. `QualityGate.screen` is the one entry point the runner uses.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from daily_pulse.config import QualityConfig
from daily_pulse.models import Item

log = logging.getLogger("daily_pulse")


@dataclass
class Decision:
    """Why one item was dropped (or, for triage, its score)."""

    item_id: str
    reason: str
    score: int | None = None  # the 0-3 triage score; None for rule decisions
    stage: str = "rule"


@dataclass
class Screened:
    kept: list[Item] = field(default_factory=list)  # in the order given, still to be processed
    dropped: list[Decision] = field(default_factory=list)  # to be marked processed, with no note
    allowlisted: set[str] = field(default_factory=set)


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _duration(item: Item) -> int | None:
    value = (item.metrics or {}).get("duration")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class QualityGate:
    def __init__(self, config: QualityConfig):
        self.config = config
        self._allow = {_norm(c) for c in config.channel_allowlist}
        self._block = {_norm(c) for c in config.channel_blocklist}
        self._titles = [_norm(t) for t in config.title_blocklist if _norm(t)]

    def _rule_drop(self, item: Item, duration_rules: bool) -> str | None:
        if _norm(item.author) in self._block:
            return f"channel {item.author!r} is blocklisted"
        title = _norm(item.title)
        for phrase in self._titles:
            if phrase in title:
                return f"title contains blocklisted text {phrase!r}"
        seconds = _duration(item)
        if duration_rules and seconds is not None:
            if seconds < self.config.min_duration_seconds:
                return f"duration {seconds}s is under the minimum of {self.config.min_duration_seconds}s"
            if seconds > self.config.max_duration_seconds:
                return f"duration {seconds}s is over the maximum of {self.config.max_duration_seconds}s"
        return None

    def screen(self, items: list[Item], *, duration_rules: bool, triage: bool, date: str) -> Screened:
        out = Screened()
        for item in items:
            if _norm(item.author) in self._allow:
                out.allowlisted.add(item.id)
                out.kept.append(item)
                continue
            reason = self._rule_drop(item, duration_rules)
            if reason:
                out.dropped.append(Decision(item.id, reason))
            else:
                out.kept.append(item)
        return out
