"""Rates items against the rubric by running `claude -p` as a subprocess.

The prompt (rubric + summarize template, both read from `prompts/` at call time) goes in on
stdin; the reply must be one JSON object, which is validated before it becomes a `Rating`.
A bad reply is retried once; after that the item is left unrated (`rate` returns None).
"""
from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from daily_pulse.models import VERDICTS, Beat, Item, Rating

log = logging.getLogger("daily_pulse")

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
VIDEO_SOURCES = ("youtube",)
DEFAULT_MAX_CALLS = 30
DEFAULT_TIMEOUT = 300.0  # seconds per call
ATTEMPTS = 2  # the first call plus one retry

_TIMESTAMP = re.compile(r"[0-9]{1,2}(:[0-9]{2}){1,2}")
_FENCE = re.compile(r"\A```[A-Za-z]*\n(.*)\n```\Z", re.S)


class BadReply(ValueError):
    """The reply from claude is not a valid rating."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_list(data: dict, key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadReply(f"{key} must be a list of strings")
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
    except ValueError as exc:
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
        beats=parsed_beats,
        diamond=diamond,
        diamond_reason=diamond_reason,
    )


class CallFailed(Exception):
    """The claude process failed, timed out or could not be started."""


@dataclass
class Summarizer:
    """One per run: `max_calls` bounds the `claude -p` calls (retries included) it will make."""

    claude_cmd: str = "claude"
    max_calls: int = DEFAULT_MAX_CALLS
    timeout: float = DEFAULT_TIMEOUT
    prompts_dir: Path = DEFAULT_PROMPTS_DIR
    calls_made: int = field(default=0, init=False)

    def _prompt(self, item: Item, transcript: str) -> str:
        rubric = (Path(self.prompts_dir) / "rubric.md").read_text(encoding="utf-8")
        template = (Path(self.prompts_dir) / "summarize.md").read_text(encoding="utf-8")
        body = json.dumps(item.to_dict(), ensure_ascii=False, indent=2)
        if transcript:
            body += "\n\nTranscript/caption:\n" + transcript
        return rubric.rstrip() + "\n\n" + template.replace("{{ITEM}}", body)

    def _call(self, prompt: str) -> str:
        self.calls_made += 1
        try:
            proc = subprocess.run([*shlex.split(self.claude_cmd), "-p"], input=prompt, text=True,
                                  capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise CallFailed(f"timed out after {self.timeout}s") from None
        except (OSError, ValueError) as exc:  # not found, not executable, bad command string
            raise CallFailed(f"cannot run {self.claude_cmd!r}: {exc}") from None
        if proc.returncode != 0:
            raise CallFailed(f"exit {proc.returncode}: {proc.stderr.strip()[:200]}")
        return proc.stdout

    def rate(self, item: Item, transcript: str = "") -> Rating | None:
        """The item's rating, or None (logged) when it could not be rated: claude never gave a
        valid reply, the process failed, or the per-run call cap was reached. Never raises."""
        label = f"{item.source}-{item.id}"
        try:
            prompt = self._prompt(item, transcript)
        except OSError as exc:
            log.error("%s: left unrated, cannot read prompts: %s", label, exc)
            return None
        for attempt in range(1, ATTEMPTS + 1):
            if self.calls_made >= self.max_calls:
                log.warning("%s: left unrated, call cap of %d reached", label, self.max_calls)
                return None
            try:
                return parse_reply(self._call(prompt), item)
            except (BadReply, CallFailed) as exc:
                log.warning("%s: attempt %d/%d failed: %s", label, attempt, ATTEMPTS, exc)
        log.warning("%s: left unrated", label)
        return None
