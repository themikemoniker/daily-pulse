"""GitHub source: rising repos plus watched accounts, via the REST API."""
from __future__ import annotations

import json
import logging
import time
from datetime import date as _date
from datetime import datetime, timedelta, timezone

from daily_pulse.adapters import registry
from daily_pulse.adapters.base import AdapterContext, SourceAdapter
from daily_pulse.models import Item

log = logging.getLogger("daily_pulse")

API = "https://api.github.com"
EXCERPT_CHARS = 400


class RateLimited(Exception):
    """The API is out of requests (or asked us to stop); the run degrades instead of failing."""


MAX_WAIT_SECONDS = 30  # longest Retry-After / reset wait we will sit through
MIN_REMAINING = 1  # stop issuing requests when this few remain


class FixtureTransport:
    """Serves recorded responses from <fixtures>/github/responses.json, keyed by URL path."""

    def __init__(self, fixtures_dir):
        self._responses = json.loads((fixtures_dir / "github" / "responses.json").read_text())

    def __call__(self, path: str, params: dict):
        r = self._responses.get(path, {"status": 404, "headers": {}, "body": {"message": "Not Found"}})
        return r["status"], r.get("headers", {}), r["body"]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _excerpt(text) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1].rstrip() + "…"


class GitHubAdapter(SourceAdapter):
    name = "github"

    def __init__(self, transport=None, sleep=time.sleep, clock=time.time):
        self._transport = transport  # callable(path, params) -> (status, headers, body)
        self._sleep = sleep
        self._clock = clock
        self._exhausted = ""
        self.last_run: dict = {"degraded": False, "reason": ""}

    def _now(self, ctx: AdapterContext) -> datetime:
        day = _date.fromisoformat(ctx.date) if ctx.date else datetime.now(timezone.utc).date()
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)

    def _get(self, ctx: AdapterContext, path: str, params: dict | None = None):
        """One API call. Raises RateLimited when the budget is spent; waits out a short
        Retry-After (once) first."""
        transport = self._transport or FixtureTransport(ctx.fixtures_dir)
        for attempt in (1, 2):
            if self._exhausted:
                raise RateLimited(self._exhausted)
            status, headers, body = transport(path, params or {})
            headers = {k.lower(): str(v) for k, v in headers.items()}
            remaining = headers.get("x-ratelimit-remaining")
            limited = status == 429 or (status == 403 and (
                remaining == "0" or "rate limit" in str(body.get("message", "") if isinstance(body, dict) else "").lower()))
            if limited:
                wait = self._wait_seconds(headers)
                if attempt == 1 and wait is not None and wait <= MAX_WAIT_SECONDS:
                    log.warning("github: rate limited on %s, waiting %ss", path, wait)
                    self._sleep(wait)
                    continue
                self._exhausted = f"rate limit hit on {path} (HTTP {status})"
                raise RateLimited(self._exhausted)
            if remaining is not None and remaining.isdigit() and int(remaining) < MIN_REMAINING:
                self._exhausted = "rate limit exhausted (X-RateLimit-Remaining=0)"
            return status, body
        raise RateLimited(self._exhausted)

    def _wait_seconds(self, headers: dict) -> float | None:
        if headers.get("retry-after", "").isdigit():
            return int(headers["retry-after"])
        if headers.get("x-ratelimit-reset", "").isdigit():
            return max(0, int(headers["x-ratelimit-reset"]) - self._clock())
        return None

    def list_new(self, ctx: AdapterContext) -> list[Item]:
        cfg = ctx.config
        now = self._now(ctx)
        self._exhausted = ""
        self.last_run = {"degraded": False, "reason": ""}
        items: list[Item] = []
        stages = [lambda: self._rising(ctx, now, int(cfg.get("rising_days", 7)))]
        stages += [lambda a=a: self._account(ctx, now, a) for a in cfg.get("watch_accounts", [])]
        stages += [lambda r=r: self._releases(ctx, now, r) for r in cfg.get("watch_repos", [])]
        for stage in stages:
            try:
                items += stage()
            except RateLimited as exc:
                log.warning("github: degraded, returning %d item(s) found so far: %s", len(items), exc)
                self.last_run = {"degraded": True, "reason": str(exc)}
                break
        if self._exhausted and not self.last_run["degraded"]:
            log.warning("github: %s; remaining requests skipped", self._exhausted)
            self.last_run = {"degraded": True, "reason": self._exhausted}
        items.sort(key=lambda i: i.published, reverse=True)
        return items

    def fetch_details(self, item: Item, ctx: AdapterContext) -> Item:
        return item

    # -- rising -------------------------------------------------------------------------

    def _repo_item(self, kind: str, r: dict, now: datetime, published: str) -> Item:
        created = _parse(r["created_at"])
        age_days = max((now - created).total_seconds() / 86400, 1.0)
        stars = r.get("stargazers_count", 0)
        text = _excerpt(r.get("description"))
        if r.get("topics"):
            text = (text + " Topics: " + ", ".join(r["topics"])).strip()
        return Item(
            source="github", id=f"{kind}:{r['full_name']}", url=r["html_url"], title=r["full_name"],
            author=r["owner"]["login"], published=published, text=text,
            metrics={"stars": stars, "forks": r.get("forks_count", 0), "language": r.get("language"),
                     "age_days": round(age_days, 1), "star_velocity": round(stars / age_days, 2)},
        )

    def _rising(self, ctx, now, days) -> list[Item]:
        since = (now - timedelta(days=days)).date().isoformat()
        status, body = self._get(ctx, "/search/repositories", {
            "q": f"created:>{since}", "sort": "stars", "order": "desc", "per_page": 10})
        if status != 200:
            return []
        return [self._repo_item("rising", r, now, r["created_at"]) for r in body["items"]]

    # -- watched accounts ----------------------------------------------------------------

    def _account(self, ctx, now, account) -> list[Item]:
        lookback = int(ctx.config.get("lookback_days", 7))
        push_days = int(ctx.config.get("push_days", 1))
        status, repos = self._get(ctx, f"/users/{account}/repos", {"sort": "pushed", "per_page": 30})
        if status != 200:
            return []
        items: list[Item] = []
        for r in repos:
            if r.get("fork"):
                continue
            created, pushed = _parse(r["created_at"]), _parse(r["pushed_at"])
            if now - created <= timedelta(days=lookback):
                items.append(self._repo_item("repo", r, now, r["created_at"]))
                continue
            if now - pushed > timedelta(days=lookback):
                continue
            found = self._releases(ctx, now, r["full_name"])
            if not found:
                found = self._tags(ctx, r)
            items += found
            if not found and now - pushed <= timedelta(days=push_days):
                day = pushed.date().isoformat()
                item = self._repo_item("push", r, now, r["pushed_at"])
                item.id = f"push:{r['full_name']}:{day}"
                items.append(item)
        return items

    def _releases(self, ctx, now, full_name) -> list[Item]:
        lookback = int(ctx.config.get("lookback_days", 7))
        status, body = self._get(ctx, f"/repos/{full_name}/releases", {"per_page": 5})
        if status != 200:
            return []
        out = []
        for rel in body:
            if rel.get("draft") or not rel.get("published_at"):
                continue
            if now - _parse(rel["published_at"]) > timedelta(days=lookback):
                continue
            title = rel.get("name") or rel["tag_name"]
            out.append(Item(
                source="github", id=f"release:{full_name}:{rel['tag_name']}", url=rel["html_url"],
                title=f"{full_name} {rel['tag_name']}: {title}" if title != rel["tag_name"]
                else f"{full_name} {rel['tag_name']}",
                author=full_name.split("/")[0], published=rel["published_at"],
                text=_excerpt(rel.get("body")), metrics={"prerelease": bool(rel.get("prerelease"))},
            ))
        return out

    def _tags(self, ctx, r) -> list[Item]:
        status, body = self._get(ctx, f"/repos/{r['full_name']}/tags", {"per_page": 3})
        if status != 200:
            return []
        return [
            Item(source="github", id=f"tag:{r['full_name']}:{t['name']}",
                 url=f"{r['html_url']}/releases/tag/{t['name']}", title=f"{r['full_name']} {t['name']}",
                 author=r["owner"]["login"], published=r["pushed_at"], text=_excerpt(r.get("description")))
            for t in body
        ]


registry.register(GitHubAdapter())
