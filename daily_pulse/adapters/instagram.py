"""Instagram: runs a configured command that prints JSON (contract: docs/instagram-adapter.md)."""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess

from daily_pulse.adapters.base import AdapterContext, SourceAdapter
from daily_pulse.models import Item

log = logging.getLogger("daily_pulse.instagram")

DEFAULT_ENV = "IG_CLI_CMD"
KINDS = ("saved", "liked")
EXCERPT_WORDS = 25
TIMEOUT = 120
TITLE_CHARS = 80  # a reel's title is its caption's first line, cut to this
DEFAULT_LIMIT = 20  # reels per run, saved first
MAX_HASHTAGS = 5


def _excerpt(caption: str) -> str:
    words = caption.split()
    return " ".join(words[:EXCERPT_WORDS]) + (" …" if len(words) > EXCERPT_WORDS else "")


def title_from(caption: str, author: str, code: str) -> str:
    """The caption's first line (at most TITLE_CHARS), else "Reel by <author> (<code>)". A real title
    gives the quality gate's title rules and the rater something to work with."""
    lines = [ln.strip() for ln in (caption or "").splitlines() if ln.strip()]
    first = " ".join(lines[0].split()) if lines else ""
    if first:
        return first if len(first) <= TITLE_CHARS else first[: TITLE_CHARS - 1].rstrip() + "…"
    return f"Reel by {author or 'unknown'} ({code})"


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_entries(text: str) -> list[dict]:
    """Valid entries from the command's JSON. Anything malformed is logged and skipped."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        log.warning("instagram: malformed JSON from command, skipped: %s", exc)
        return []
    if not isinstance(raw, list):
        log.warning("instagram: malformed output (expected a JSON list), skipped")
        return []
    good = []
    for i, e in enumerate(raw):
        ok = (
            isinstance(e, dict)
            and isinstance(e.get("shortcode"), str) and e["shortcode"].strip()
            and isinstance(e.get("url"), str) and e["url"]
            and e.get("source") in KINDS
            and all(e.get(k) is None or isinstance(e[k], str) for k in ("author", "caption", "timestamp"))
        )
        if ok:
            good.append(e)
        else:
            log.warning("instagram: malformed entry #%d skipped", i)
    return good


class InstagramAdapter(SourceAdapter):
    name = "instagram"
    quality_gate = True  # channel/title rules and triage; reels are all short, so no duration rule
    rated_by_runner = True  # rated from the caption and metadata, like any item the runner rates

    def __init__(self) -> None:
        self.captions: dict[str, str] = {}  # full captions, for the summarizer only

    @staticmethod
    def _command(ctx: AdapterContext) -> list[str]:
        env_name = ctx.config.get("cli_cmd_env", DEFAULT_ENV)
        return shlex.split((os.environ.get(env_name) or "").strip())

    @staticmethod
    def _fixture(ctx: AdapterContext):
        if ctx.fixtures_dir is None:
            return None
        path = ctx.fixtures_dir / "instagram" / "items.json"
        return path if path.is_file() else None

    def status(self, ctx: AdapterContext) -> str | None:
        if self._fixture(ctx) is None and not self._command(ctx):
            return "deferred: no CLI configured"
        return None

    def _read(self, ctx: AdapterContext) -> str:
        fixture = self._fixture(ctx)
        if fixture is not None:
            return fixture.read_text(encoding="utf-8")
        argv = self._command(ctx) + [str(a) for a in ctx.config.get("args", [])]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT,
                                  stdin=subprocess.DEVNULL)
        except FileNotFoundError:  # name the program only: the rest of the command may hold a token
            raise RuntimeError(f"IG CLI not found: {os.path.basename(argv[0])} "
                               f"(the command in ${ctx.config.get('cli_cmd_env', DEFAULT_ENV)})") from None
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"IG CLI could not run: {type(exc).__name__}") from None
        if proc.returncode != 0:
            raise RuntimeError(f"IG CLI exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return proc.stdout

    def list_new(self, ctx: AdapterContext) -> list[Item]:
        self._limit(ctx)  # validate up front, even for an empty listing
        kinds = [k for k in ctx.config.get("kinds", KINDS) if k in KINDS] or list(KINDS)
        entries = [e for e in parse_entries(self._read(ctx)) if e["source"] in kinds]
        entries.sort(key=lambda e: kinds.index(e["source"]))  # stable: saved first, then liked
        items, seen = [], set()
        for e in entries:
            code = e["shortcode"].strip()
            if code in seen:
                continue
            seen.add(code)
            author = e.get("author") or ""
            self.captions[code] = e.get("caption") or ""
            metrics = {"source": e["source"]}
            for key, field in (("likes", "like_count"), ("comments", "comment_count")):
                if _count(e.get(field)) is not None:  # optional: a malformed value is just left out
                    metrics[key] = e[field]
            tags = e.get("hashtags")
            if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
                cleaned = [t.strip().lstrip("#").strip()[:40] for t in tags]
                cleaned = [t for t in cleaned if t][:MAX_HASHTAGS]
                if cleaned:
                    metrics["hashtags"] = cleaned
            items.append(Item(source=self.name, id=code, url=e["url"],
                              title=title_from(e.get("caption") or "", author, code), author=author,
                              published=e.get("timestamp") or "", metrics=metrics))
        return items

    def rank_new(self, items, ctx):
        """The per-run `limit` applies to NEW reels (saved first, as listed), so reels already
        processed never use up the window. The rest wait for a later run."""
        limit = self._limit(ctx)
        if len(items) > limit:
            log.info("instagram: %d new reel(s) beyond limit=%d wait for a later run", len(items) - limit, limit)
        return items[:limit], []

    @staticmethod
    def _limit(ctx: AdapterContext) -> int:
        limit = ctx.config.get("limit", DEFAULT_LIMIT)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"[sources.instagram] limit must be a whole number, 1 or more, got {limit!r}")
        return limit

    def fetch_details(self, item: Item, ctx: AdapterContext) -> Item:
        item.text = _excerpt(self.captions.get(item.id, ""))
        return item

    def rating_text(self, item: Item, ctx: AdapterContext) -> str:
        return self.captions.get(item.id, "")
