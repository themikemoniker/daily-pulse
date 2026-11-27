"""The quality gate: weeds out low-quality and off-topic items before the expensive summary.

Two layers, cheapest first. Rules (`[quality]` in the config) look only at channel, title and
duration. Items that pass may then be triaged by one batched `claude -p` call per 40 items.
Allowlisted channels bypass both. `QualityGate.screen` is the one entry point the runner uses.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from daily_pulse.claude import CallFailed, ClaudeCaller
from daily_pulse.config import Config, QualityConfig
from daily_pulse.models import Item

log = logging.getLogger("daily_pulse")

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
CLAUDE_ENV = "DAILY_PULSE_CLAUDE"
BATCH_SIZE = 40  # items per triage call
DROP_BELOW = 2  # a triage score under this drops the item (0 and 1)
MAX_SCORE = 3
TRIAGE_TIMEOUT = 300.0
_FENCE = re.compile(r"\A```[A-Za-z]*\n(.*)\n```\Z", re.S)


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
    scores: list[Decision] = field(default_factory=list)  # every triage score, kept or dropped
    remaining: list[Item] = field(default_factory=list)  # not examined (or beyond `limit`): mark seen


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _duration(item: Item) -> int | None:
    value = (item.metrics or {}).get("duration")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _one_line(text, limit: int = 200) -> str:
    return " ".join(str(text).split())[:limit]


@dataclass
class Triager(ClaudeCaller):
    """Scores items 0-3 for relevance to the rubric in batched `claude -p` calls that see only
    title, channel, duration and views. `max_calls` is its own cap, separate from the summarizer's."""

    claude_cmd: str = "claude"
    max_calls: int = 10
    timeout: float = TRIAGE_TIMEOUT
    prompts_dir: Path = DEFAULT_PROMPTS_DIR
    calls_made: int = field(default=0, init=False)

    @classmethod
    def from_config(cls, config: Config, env=None) -> "Triager":
        env = os.environ if env is None else env
        return cls(claude_cmd=(env.get(CLAUDE_ENV) or "").strip() or config.claude_cmd,
                   max_calls=config.quality.max_triage_calls)

    def _prompt(self, batch: list[Item]) -> str:
        rubric = (Path(self.prompts_dir) / "rubric.md").read_text(encoding="utf-8")
        template = (Path(self.prompts_dir) / "triage.md").read_text(encoding="utf-8")
        rows = [{"n": n, "title": i.title, "channel": i.author,
                 "duration_seconds": _duration(i), "views": (i.metrics or {}).get("views")}
                for n, i in enumerate(batch)]
        return rubric.rstrip() + "\n\n" + template.replace(
            "{{ITEMS}}", json.dumps(rows, ensure_ascii=False, indent=2))

    @staticmethod
    def _parse(text: str, batch: list[Item]) -> dict[str, Decision]:
        text = text.strip()
        fenced = _FENCE.match(text)
        try:
            data = json.loads(fenced.group(1) if fenced else text)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"not JSON: {exc}") from None
        if not isinstance(data, list):
            raise ValueError("reply must be a JSON array")
        found: dict[str, Decision] = {}
        seen: set[int] = set()
        for entry in data:
            n = entry.get("n") if isinstance(entry, dict) else None
            score = entry.get("score") if isinstance(entry, dict) else None
            if (isinstance(n, bool) or not isinstance(n, int) or not 0 <= n < len(batch)
                    or n in seen or isinstance(score, bool) or not isinstance(score, int)
                    or not 0 <= score <= MAX_SCORE):
                log.warning("triage: ignoring a bad entry in the reply: %s", _one_line(entry, 80))
                continue
            seen.add(n)
            found[batch[n].id] = Decision(batch[n].id, _one_line(entry.get("reason") or ""), score, "triage")
        return found

    def triage(self, items: list[Item]) -> dict[str, Decision]:
        """Item id -> its score, for the items that got one. An item missing from the result (a bad
        or missing reply entry, a failed call, the call cap) is not triaged: the caller keeps it."""
        scores: dict[str, Decision] = {}
        for start in range(0, len(items), BATCH_SIZE):
            batch = items[start:start + BATCH_SIZE]
            if self.calls_made >= self.max_calls:
                log.warning("triage: call cap of %d reached, %d items kept untriaged",
                            self.max_calls, len(items) - start)
                break
            try:
                scores.update(self._parse(self._call(self._prompt(batch)), batch))
            except (CallFailed, ValueError, OSError) as exc:
                log.warning("triage: batch of %d items kept untriaged: %s", len(batch), exc)
        return scores


class QualityGate:
    def __init__(self, config: QualityConfig, triager: Triager | None = None):
        self.config = config
        self.triager = triager
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

    def screen(self, items: list[Item], *, duration_rules: bool, triage: bool, date: str,
               limit: int | None = None) -> Screened:
        """Rules first (free), then triage for the survivors when `triage` is true and the gate has
        a triager. Allowlisted items skip both. Triage only ever drops on a score it really got.
        With `limit` (the first-run cap) the survivors are the first `limit` items, in the order
        given, that pass; triage stops as soon as it has them, and everything else lands in
        `remaining` (never triaged, to be marked seen by the caller)."""
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
        triager = self.triager if triage else None
        pending, out.kept = out.kept, []
        while pending and (limit is None or len(out.kept) < limit):
            chunk, count = [], 0
            while pending and count < BATCH_SIZE:  # one triage call's worth of untrusted items
                chunk.append(pending.pop(0))
                count += chunk[-1].id not in out.allowlisted
            candidates = [i for i in chunk if i.id not in out.allowlisted]
            scores = triager.triage(candidates) if triager is not None and candidates else {}
            for item in chunk:
                decision = scores.get(item.id)
                if decision is not None:
                    out.scores.append(decision)
                if decision is not None and decision.score < DROP_BELOW:
                    out.dropped.append(decision)
                else:
                    out.kept.append(item)
        if limit is not None:
            out.kept, extra = out.kept[:limit], out.kept[limit:]
            out.remaining = extra + pending
        return out


def decision_records(items: list[Item], screened: Screened, date: str) -> list[dict]:
    """One log line per decision: rule drops, triage scores (kept or dropped) and allowlisted keeps."""
    by_id = {i.id: i for i in items}
    records = []
    for decision, verdict in ([(d, "dropped") for d in screened.dropped]
                              + [(d, "kept") for d in screened.scores if d.score >= DROP_BELOW]
                              + [(Decision(i, "allowlisted channel"), "kept") for i in sorted(screened.allowlisted)]):
        item = by_id.get(decision.item_id)
        if item is not None:
            records.append({"id": item.id, "source": item.source, "title": item.title,
                            "score": decision.score, "decision": verdict, "reason": decision.reason, "date": date})
    return records


def append_triage_log(path: Path, records: list[dict]) -> None:
    """Append to the local decision log (`<data_dir>/triage.jsonl`). It stays on this machine: it
    is private (0600), gitignored with the rest of data/, and nothing in the site build reads it."""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records))
