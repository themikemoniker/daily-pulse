"""Rates items against the rubric by running `claude -p` as a subprocess.

The prompt (rubric + summarize template, both read from `prompts/` at call time) goes in on
stdin; the reply must be one JSON object, which is validated before it becomes a `Rating`.
A bad reply is retried once; after that the item is left unrated (`rate` returns None).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from daily_pulse.config import Config
from daily_pulse.models import VERDICTS, Beat, Item, Rating
from daily_pulse.notes import NoteError, write_note
from daily_pulse.quoteguard import QuoteBudget, guard_fields

log = logging.getLogger("daily_pulse")

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
VIDEO_SOURCES = ("youtube",)
CLAUDE_ENV = "DAILY_PULSE_CLAUDE"
DEFAULT_MAX_CALLS = 30
DEFAULT_TIMEOUT = 300.0  # seconds per call
ATTEMPTS = 2  # the first call plus one retry

_TIMESTAMP = re.compile(r"[0-9]{1,2}:[0-5][0-9](:[0-5][0-9])?")
_FENCE = re.compile(r"\A```[A-Za-z]*\n(.*)\n```\Z", re.S)


class BadReply(ValueError):
    """The reply from claude is not a valid rating."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_list(data: dict, key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadReply(f"{key} must be a list of strings")
    if key == "tags" and not all(v.strip() for v in value):
        raise BadReply("tags must not be empty")
    return value


def _str(data: dict, key: str, required: bool = True) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (required and not value.strip()):
        raise BadReply(f"{key} must be {'a non-empty ' if required else 'a '}string")
    return value


def parse_reply(text: str, item: Item) -> Rating:
    """Turn claude's stdout into a Rating, or raise BadReply."""
    text = text.strip()
    fenced = _FENCE.match(text)
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except (ValueError, RecursionError) as exc:
        raise BadReply(f"not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise BadReply("reply must be a JSON object")
    rating = data.get("rating")
    if not _is_int(rating) or not 1 <= rating <= 10:
        raise BadReply(f"rating must be an integer 1-10, got {rating!r}")
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise BadReply(f"verdict must be one of {', '.join(VERDICTS)}, got {verdict!r}")
    diamond = data.get("diamond")
    if not isinstance(diamond, bool):
        raise BadReply("diamond must be true or false")
    diamond_reason = _str(data, "diamond_reason", required=diamond)
    beats = data.get("beats")
    if not isinstance(beats, list):
        raise BadReply("beats must be a list")
    parsed_beats = []
    for beat in beats:
        if not (isinstance(beat, dict) and isinstance(beat.get("text"), str)
                and beat["text"].strip()
                and isinstance(beat.get("timestamp"), str) and _TIMESTAMP.fullmatch(beat["timestamp"])):
            raise BadReply(f"bad beat {beat!r}")
        parsed_beats.append(Beat(beat["timestamp"], beat["text"]))
    is_video = item.source in VIDEO_SOURCES
    return Rating(
        rating=rating,
        reason=_str(data, "reason"),
        verdict=verdict,
        tags=_str_list(data, "tags"),
        summary=_str(data, "summary", required=not is_video),
        takeaways=_str_list(data, "takeaways"),
        quotes=_str_list(data, "quotes") if "quotes" in data else [],
        beats=parsed_beats,
        diamond=diamond,
        diamond_reason=diamond_reason,
    )


class CallFailed(Exception):
    """The claude process failed or timed out; a retry may help."""


class CannotRun(CallFailed):
    """The claude process could not be started at all; a retry cannot help."""


def _one_line(text: str) -> str:
    return " ".join(text.split())


def guard_rating(rating: Rating, reference: str) -> Rating:
    """Keep source text out of a rating: quotes are capped and share one budget with quoted spans
    and lifted runs in the free text, and no run of more than 40 words matches `reference`."""
    budget = QuoteBudget()
    quotes = [q for q in (budget.fit(_one_line(q)) for q in rating.quotes) if q]
    rating.quotes = guard_fields(quotes, reference)  # adjacent quotes must not add up to a long copy

    def guarded(fields: list[str]) -> list[str]:
        return guard_fields(fields, reference, budget)

    rating.summary, rating.reason, rating.diamond_reason = guarded(
        [rating.summary, rating.reason, rating.diamond_reason])
    rating.takeaways = guarded(rating.takeaways)
    for beat, text in zip(rating.beats, guarded([b.text for b in rating.beats])):
        beat.text = text
    rating.tags = guarded(rating.tags)
    return rating


@dataclass
class Summarizer:
    """One per run: `max_calls` bounds the `claude -p` calls (retries included) it will make."""

    claude_cmd: str = "claude"
    max_calls: int = DEFAULT_MAX_CALLS
    timeout: float = DEFAULT_TIMEOUT
    prompts_dir: Path = DEFAULT_PROMPTS_DIR
    calls_made: int = field(default=0, init=False)

    @classmethod
    def from_config(cls, config: Config, env=None) -> "Summarizer":
        """The binary comes from $DAILY_PULSE_CLAUDE if set (tests point it at a fake script),
        else `[summarizer].claude_cmd`."""
        env = os.environ if env is None else env
        return cls(claude_cmd=(env.get(CLAUDE_ENV) or "").strip() or config.claude_cmd,
                   max_calls=config.max_claude_calls)

    def _prompt(self, item: Item, transcript: str) -> str:
        rubric = (Path(self.prompts_dir) / "rubric.md").read_text(encoding="utf-8")
        template = (Path(self.prompts_dir) / "summarize.md").read_text(encoding="utf-8")
        body = json.dumps(item.to_dict(), ensure_ascii=False, indent=2)
        if transcript:
            body += "\n\nTranscript/caption:\n" + transcript
        return rubric.rstrip() + "\n\n" + template.replace("{{ITEM}}", body)

    def _argv(self) -> list[str]:
        # Item text and transcripts are untrusted, so claude gets no tools (nothing for injected
        # instructions to run), no skills, no MCP servers and no saved session.
        return [*shlex.split(self.claude_cmd), "-p", "--tools", "", "--disable-slash-commands",
                "--strict-mcp-config", "--no-session-persistence"]

    def _call(self, prompt: str) -> str:
        """One `claude -p` run, in its own process group and an empty scratch directory (so it
        loads none of this repo's CLAUDE.md/AGENTS.md). On timeout the whole group is killed:
        helper processes (node, MCP servers) would otherwise hold the pipes open."""
        try:
            with tempfile.TemporaryDirectory(prefix="daily-pulse-claude-") as scratch:
                proc = subprocess.Popen(
                    self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=scratch, start_new_session=True, encoding="utf-8", errors="replace")
                try:
                    stdout, stderr = proc.communicate(prompt, timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self.calls_made += 1
                    raise CallFailed(f"timed out after {self.timeout}s") from None
                finally:
                    self._kill_group(proc)  # helpers that outlive a finished claude
        except (OSError, ValueError) as exc:  # not found, not executable, bad command string
            raise CannotRun(f"cannot run {self.claude_cmd!r}: {exc}") from None
        self.calls_made += 1
        if proc.returncode != 0:
            raise CallFailed(f"exit {proc.returncode}: {stderr.strip()[:200]}")
        return stdout

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=5)  # reap and drain; never wait on a stray pipe holder
        except (subprocess.TimeoutExpired, ValueError):
            pass

    def rate(self, item: Item, transcript: str = "") -> Rating | None:
        """The item's rating, or None (logged) when it could not be rated: claude never gave a
        valid reply, the process failed, or the per-run call cap was reached. Bad replies and process
        failures never raise; `summarize_items` also guards against anything unexpected."""
        label = f"{item.source}-{item.id}"
        try:
            prompt = self._prompt(item, transcript)
        except (OSError, ValueError, TypeError) as exc:  # unreadable prompts, unserializable item
            log.error("%s: left unrated, cannot build the prompt: %s", label, exc)
            return None
        for attempt in range(1, ATTEMPTS + 1):
            if self.calls_made >= self.max_calls:
                log.warning("%s: left unrated, call cap of %d reached", label, self.max_calls)
                return None
            try:
                rating = parse_reply(self._call(prompt), item)
                return guard_rating(rating, "\n".join(p for p in (transcript, item.text) if p))
            except (BadReply, CallFailed) as exc:
                log.warning("%s: attempt %d/%d failed: %s", label, attempt, ATTEMPTS, exc)
                if isinstance(exc, CannotRun):
                    break
        log.warning("%s: left unrated", label)
        return None


@dataclass
class ItemResult:
    item: Item
    status: str  # "rated" or "unrated"
    rating: Rating | None = None
    path: Path | None = None  # the written note, when rated


def summarize_items(summarizer: Summarizer, items: list[Item], notes_dir: Path, date: str,
                    transcripts: dict[str, str] | None = None) -> list[ItemResult]:
    """Rate each item and hand the rating to the note writer. One item failing (no valid reply,
    cap reached, note refused) marks only that item "unrated"; the rest carry on."""
    transcripts = transcripts or {}
    results = []
    for item in items:
        transcript = transcripts.get(item.id, "")
        try:
            rating = summarizer.rate(item, transcript)
        except Exception:  # noqa: BLE001 - one item must never kill the batch
            log.exception("%s-%s: left unrated, unexpected error", item.source, item.id)
            rating = None
        if rating is not None:
            try:
                path = write_note(item, rating, notes_dir, date, transcript)
            except (NoteError, OSError) as exc:
                log.warning("%s-%s: left unrated, note not written: %s", item.source, item.id, exc)
            else:
                results.append(ItemResult(item, "rated", rating, path))
                continue
        results.append(ItemResult(item, "unrated"))
    return results
