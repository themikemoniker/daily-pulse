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


def _excerpt(caption: str) -> str:
    words = caption.split()
    return " ".join(words[:EXCERPT_WORDS]) + (" …" if len(words) > EXCERPT_WORDS else "")


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
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT,
                              stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise RuntimeError(f"IG CLI exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return proc.stdout

    def list_new(self, ctx: AdapterContext) -> list[Item]:
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
            items.append(Item(source=self.name, id=code, url=e["url"],
                              title=f"Reel by {author or 'unknown'} ({code})", author=author,
                              published=e.get("timestamp") or "", metrics={"source": e["source"]}))
        return items

    def fetch_details(self, item: Item, ctx: AdapterContext) -> Item:
        item.text = _excerpt(self.captions.get(item.id, ""))
        return item
