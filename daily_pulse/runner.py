"""Runs each enabled source adapter in isolation and collects a RunResult."""
from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from daily_pulse.adapters import registry
from daily_pulse.adapters.base import AdapterContext, ItemDeferred, ItemSkipped
from daily_pulse.config import Config
from daily_pulse.quality import QualityGate, Triager, append_triage_log, decision_records
from daily_pulse.state import FailureCounters, StateError, StateStore, serialize_json
from daily_pulse.summarizer import Summarizer, summarize_items

log = logging.getLogger("daily_pulse")


@dataclass
class RunResult:
    date: str
    sources: dict[str, dict] = field(default_factory=dict)
    flagged: list[str] = field(default_factory=list)
    min_rating: int | None = None  # the rating threshold in force; the digest and site rank by it
    error: str | None = None  # set when a state error aborted the run part-way
    generated_at: str = field(  # when the run happened (UTC); the digest measures item ages from it
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        out = {"date": self.date, "generated_at": self.generated_at, "sources": self.sources, "flagged": self.flagged}
        if self.min_rating is not None:
            out["min_rating"] = self.min_rating
        if self.error:
            out["error"] = self.error
        return out


def _mark_backlog(name, state, ids: list[str], cap: int | None) -> None:
    """First run: the older items beyond `first_run_cap` are marked seen, with no note. Logged, so
    no item is ever consumed silently."""
    if ids:
        log.info("%s: first run, %d older item(s) beyond the cap of %s marked seen without notes: %s",
                 name, len(ids), cap, ", ".join(ids))
    state.mark_processed(name, *ids)


def _screen(name, adapter, ctx, state, gate, new, limit, dropped: list) -> list:
    """Run the quality gate over the new items and return the ones to process. Dropped items are
    logged locally, recorded in `dropped` and marked processed (so they are never looked at again,
    and get no note); items past the first-run `limit` are marked seen. A gate that breaks
    never costs the run: the items just carry on unscreened."""
    try:
        screened = gate.screen(new, duration_rules=adapter.quality_duration, triage=ctx.persist,
                               date=ctx.date, limit=limit)
    except Exception:  # noqa: BLE001 - fail open
        log.error("%s: quality gate failed, items continue unscreened\n%s", name, traceback.format_exc())
        if limit is not None:
            _mark_backlog(name, state, [i.id for i in new[limit:]], limit)
        return new if limit is None else new[:limit]
    if ctx.persist and ctx.data_dir is not None:
        try:
            append_triage_log(Path(ctx.data_dir) / "triage.jsonl", decision_records(new, screened, ctx.date))
        except OSError as exc:  # the log is a convenience; losing a line must not stop the run
            log.warning("%s: cannot write the triage log: %s", name, exc)
    titles = {i.id: i.title for i in new}
    for d in screened.dropped:
        log.info("%s: %s dropped (%s): %s", name, d.item_id, "triage" if d.score is not None else "rule", d.reason)
        dropped.append({"id": d.item_id, "title": titles.get(d.item_id, ""), "score": d.score, "reason": d.reason})
    state.mark_processed(name, *(d.item_id for d in screened.dropped))
    _mark_backlog(name, state, [i.id for i in screened.remaining], limit)
    return screened.kept


class RatingFailed(Exception):
    """An item could not be rated or noted; it stays unprocessed and is retried next run."""


def _rate_and_note(name, adapter, item, ctx) -> None:
    """Rate `item` against the rubric and write its note, for sources whose adapter leaves that to
    the runner (`rated_by_runner`). Returns only once the note is on disk, so the caller may then
    mark the item processed. Raises ItemDeferred when the per-run call cap is spent (not a fault:
    the item waits for the next run) and RatingFailed when no valid rating or note came out."""
    summarizer = ctx.summarizer
    if summarizer is None or ctx.notes_dir is None:  # never consume an item without a note
        raise RatingFailed(f"{item.id}: no summarizer or notes directory in the run context")
    if summarizer.calls_made >= summarizer.max_calls:
        raise ItemDeferred(f"{item.id}: claude call cap of {summarizer.max_calls} reached, not rated yet")
    text = adapter.rating_text(item, ctx)
    result = summarize_items(summarizer, [item], ctx.notes_dir, ctx.date, {item.id: text})[0]
    if result.status != "rated":
        if summarizer.calls_made >= summarizer.max_calls:  # the cap, not the item, stopped the retry
            raise ItemDeferred(f"{item.id}: claude call cap of {summarizer.max_calls} reached, not rated yet")
        raise RatingFailed(f"{item.id}: no rating, will retry next run")


def _run_adapter(name, adapter, ctx, state, first_run_cap, items: list, gate=None, dropped=None) -> Exception | None:
    """Process one adapter's new items into `items`. Adapter exceptions are returned (first
    one), never raised. StateError (our own state I/O) propagates: it is not the adapter's fault."""
    try:
        candidates = adapter.list_new(ctx)
        new, seen = [], set()
        for c in candidates:
            if c.id not in seen and not state.is_processed(name, c.id):
                seen.add(c.id)
                new.append(c)
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        log.error("%s: listing failed\n%s", name, traceback.format_exc())
        return exc
    first_run = state.is_first_run(name)
    if adapter.quality_gate and gate is not None:
        # Screened items are dropped before the first-run cap, so the cap takes the newest
        # items that pass, and triage stops once it has found enough of them.
        new = _screen(name, adapter, ctx, state, gate, new, first_run_cap if first_run else None,
                      dropped if dropped is not None else [])
    elif first_run:
        # Don't chew through a backlog: keep the newest few, and mark the rest as seen
        # (in one write) so later runs only handle genuinely new items.
        _mark_backlog(name, state, [c.id for c in new[first_run_cap:]], first_run_cap)
        new = new[:first_run_cap]
    error = None
    for candidate in new:
        try:
            item = adapter.fetch_details(candidate, ctx)
            # Prove the item can be reported *before* it is marked processed, so a bad
            # item is retried next run instead of being consumed and lost.
            serialize_json(item.to_dict())
            if adapter.rated_by_runner and ctx.persist:
                _rate_and_note(name, adapter, item, ctx)
        except ItemDeferred as deferred:
            log.info("%s: item %s deferred to the next run: %s", name, candidate.id, deferred)
            continue
        except ItemSkipped as skipped:
            log.warning("%s: item %s skipped for good: %s", name, candidate.id, skipped)
            state.mark_processed(name, candidate.id)
            continue
        except Exception as exc:  # noqa: BLE001
            log.error("%s: item %s failed\n%s", name, candidate.id, traceback.format_exc())
            error = error or exc
            continue
        state.mark_processed(name, candidate.id)
        items.append(item)
    return error


def _run_source(name, source_cfg, config, ctx, state, failures, result, gate=None) -> None:
    if not source_cfg.get("enabled", True):
        result.sources[name] = {"status": "disabled", "items": []}
        return
    adapter = registry.get(name)
    if adapter is None:
        result.sources[name] = {"status": "not configured: no adapter registered", "items": []}
        return
    items: list = []
    dropped: list = []
    # Keep what was collected even if a StateError aborts the run part-way.
    entry: dict = {"status": "aborted", "items": items}
    result.sources[name] = entry
    try:
        status = adapter.status(ctx)
    except Exception as exc:  # noqa: BLE001
        log.error("%s: status failed\n%s", name, traceback.format_exc())
        status, error = None, exc
    else:
        if status is not None:
            result.sources[name] = {"status": status, "items": []}
            return
        error = _run_adapter(name, adapter, ctx, state, config.first_run_cap, items, gate, dropped)
    entry["items"] = [i.to_dict() for i in items]
    if dropped:
        entry["dropped"] = dropped
    if error is None:
        failures.record_success(name)
        entry["status"] = "ok"
    else:
        failures.record_failure(name, f"{type(error).__name__}: {error}")
        entry["status"] = "failed"
        entry["error"] = f"{type(error).__name__}: {error}"
    entry["consecutive_failures"] = failures.consecutive(name)
    if entry["consecutive_failures"] >= config.alert_after_failures:
        result.flagged.append(name)


def run_pipeline(
    config: Config, date: str, fixtures_dir: Path | None, dry_run: bool = False,
    out_dir: Path | None = None,
) -> RunResult:
    """Run every enabled adapter. StateError while reading state at startup propagates;
    one raised mid-run stops the run but is returned in result.error with what was done."""
    result = RunResult(date=date, min_rating=config.quality.min_rating)
    state = StateStore(config.data_dir, persist=not dry_run)
    failures = FailureCounters(config.data_dir, persist=not dry_run)
    summarizer = Summarizer.from_config(config)  # one per run, so its call cap spans all sources
    gate = QualityGate(config.quality, Triager.from_config(config))  # its triage call cap spans all sources
    notes_dir = None if out_dir is None else Path(out_dir) / "notes"
    for name, source_cfg in config.sources.items():
        ctx = AdapterContext(
            config=source_cfg, fixtures_dir=fixtures_dir, date=date,
            data_dir=config.data_dir, persist=not dry_run,
            notes_dir=notes_dir, summarizer=summarizer,
        )
        try:
            _run_source(name, source_cfg, config, ctx, state, failures, result, gate)
        except StateError as exc:
            log.error("state error, aborting run\n%s", traceback.format_exc())
            partial = result.sources.get(name)
            if partial is not None and partial["status"] == "aborted":
                partial["items"] = [i.to_dict() if hasattr(i, "to_dict") else i for i in partial["items"]]
            result.error = str(exc)
            break
    return result
