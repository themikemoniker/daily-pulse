"""GitHub source: rising repos plus watched accounts, via the REST API."""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date as _date
from datetime import datetime, timedelta, timezone

from daily_pulse.adapters.base import AdapterContext, SourceAdapter
from daily_pulse.models import Item
from daily_pulse.state import atomic_write_json

log = logging.getLogger("daily_pulse")

# The order candidates are processed in, which is also what the first-run cap and the per-run
# `claude -p` call budget keep: releases and new repos before rising repos, tags, star jumps and,
# last, bare pushes. Within a kind, newest first.
KIND_ORDER = ("release", "repo", "rising", "tag", "stars", "push")
DEFAULT_PUSH_MIN_STARS = 25  # a bare push (no release or tag) is news only for a repo this popular

API = "https://api.github.com"
EXCERPT_CHARS = 400


class RateLimited(Exception):
    """The API is out of requests (or asked us to stop); the run degrades instead of failing."""


MAX_WAIT_SECONDS = 30  # longest Retry-After / reset wait we will sit through
MIN_REMAINING = 1  # stop issuing requests when this few remain


class UrllibTransport:
    """Real HTTP. The optional token is read from the environment on each call and only ever
    placed in the Authorization header: it is not stored, logged or shown in repr."""

    TOKEN_ENV = "GITHUB_TOKEN"
    TIMEOUT = 30

    def __repr__(self) -> str:
        return "UrllibTransport()"

    def __call__(self, path: str, params: dict):
        url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "daily-pulse",
                   "X-GitHub-Api-Version": "2022-11-28"}
        token = os.environ.get(self.TOKEN_ENV, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                status, resp_headers, raw = resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            status, resp_headers, raw = err.code, dict(err.headers or {}), err.read()
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            body = {"message": raw[:200].decode("utf-8", "replace")}
        return status, resp_headers, body


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
    rated_by_runner = True  # rated from the repo/release metadata the items already carry

    def __init__(self, transport=None, sleep=time.sleep, clock=time.time):
        self._transport = transport  # callable(path, params) -> (status, headers, body)
        self._sleep = sleep
        self._clock = clock
        self._exhausted: dict = {}
        self._errors: list = []
        self._stars: dict = {}
        self._push_min_stars, self._ignore = DEFAULT_PUSH_MIN_STARS, []  # set per run by list_new
        self.last_run: dict = {"degraded": False, "reason": ""}

    def _now(self, ctx: AdapterContext) -> datetime:
        day = _date.fromisoformat(ctx.date) if ctx.date else datetime.now(timezone.utc).date()
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)

    def _get(self, ctx: AdapterContext, path: str, params: dict | None = None):
        """One API call. Raises RateLimited when the budget is spent; waits out a short
        Retry-After (once) first."""
        transport = self._transport or (
            FixtureTransport(ctx.fixtures_dir) if ctx.fixtures_dir else UrllibTransport())
        path_bucket = "search" if path.startswith("/search") else "core"
        for attempt in (1, 2):
            if path_bucket in self._exhausted:
                raise RateLimited(self._exhausted[path_bucket])
            status, headers, body = transport(path, params or {})
            headers = {k.lower(): str(v) for k, v in headers.items()}
            # Search has its own small budget; only the bucket that hit zero is exhausted.
            bucket = headers.get("x-ratelimit-resource", path_bucket)
            remaining = headers.get("x-ratelimit-remaining")
            limited = status == 429 or (status == 403 and (
                remaining == "0" or "rate limit" in str(body.get("message", "") if isinstance(body, dict) else "").lower()))
            if limited:
                wait = self._wait_seconds(headers)
                if attempt == 1 and wait is not None and wait <= MAX_WAIT_SECONDS:
                    log.warning("github: rate limited on %s, waiting %ss", path, wait)
                    self._sleep(wait)
                    continue
                self._exhausted[bucket] = f"rate limit hit on {path} (HTTP {status})"
                raise RateLimited(self._exhausted[bucket])
            if remaining is not None and remaining.isdigit() and int(remaining) < MIN_REMAINING:
                self._exhausted[bucket] = f"rate limit exhausted ({bucket}: X-RateLimit-Remaining=0)"
            if status != 200:
                log.warning("github: HTTP %s on %s", status, path)
                if status in (401, 404, 422) or status >= 500:
                    self._errors.append(f"HTTP {status} on {path}")
            return status, body
        raise RateLimited(self._exhausted.get(path_bucket, "rate limited"))

    def _wait_seconds(self, headers: dict) -> float | None:
        if headers.get("retry-after", "").isdigit():
            return int(headers["retry-after"])
        if headers.get("x-ratelimit-reset", "").isdigit():
            return max(0, int(headers["x-ratelimit-reset"]) - self._clock())
        return None

    def list_new(self, ctx: AdapterContext) -> list[Item]:
        cfg = ctx.config
        now = self._now(ctx)
        self._exhausted = {}
        self._errors = []
        self.last_run = {"degraded": False, "reason": ""}
        self._stars = self._load_stars(ctx)
        self._push_min_stars, self._ignore = self._noise_settings(cfg)
        items: list[Item] = []
        # Watched repos first: they are explicitly required and cheap (one call each), so a
        # tight rate limit must not starve them behind the larger account listings.
        stages = [lambda r=r: items.extend(self._releases(ctx, now, r)) for r in cfg.get("watch_repos", [])]
        stages.append(lambda: items.extend(self._rising(ctx, now, int(cfg.get("rising_days", 7)))))
        stages += [lambda a=a: self._account(ctx, now, a, items) for a in cfg.get("watch_accounts", [])]
        failed = 0
        try:
            for stage in stages:
                errors_before = len(self._errors)
                try:
                    stage()
                except RateLimited as exc:
                    # Only the exhausted bucket is closed (_get refuses it); later stages that
                    # use the other bucket still run.
                    log.warning("github: degraded, rate limited after %d item(s): %s", len(items), exc)
                    self.last_run = {"degraded": True, "reason": str(exc)}
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad call must not lose the rest
                    log.warning("github: skipped one lookup: %s: %s", type(exc).__name__, exc)
                    self._errors.append(f"{type(exc).__name__}: {exc}")
                    failed += 1
                    continue
                if len(self._errors) > errors_before:
                    failed += 1
        finally:
            self._save_stars(ctx)
        if stages and failed == len(stages):
            raise RuntimeError(f"github: every lookup failed, first: {self._errors[0]}")
        if self._exhausted and not self.last_run["degraded"]:
            reason = "; ".join(self._exhausted.values())
            log.warning("github: %s; remaining requests skipped", reason)
            self.last_run = {"degraded": True, "reason": reason}
        if self._errors and not self.last_run["degraded"]:
            reason = f"{len(self._errors)} failed request(s), first: {self._errors[0]}"
            log.warning("github: degraded: %s", reason)
            self.last_run = {"degraded": True, "reason": reason}
        items.sort(key=lambda i: i.published, reverse=True)
        items.sort(key=self._kind_rank)  # stable: newest first inside each kind
        return items

    @staticmethod
    def _kind_rank(item: Item) -> int:
        kind = item.id.split(":", 1)[0]
        return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)

    @staticmethod
    def _noise_settings(cfg: dict) -> tuple[int, list[str]]:
        stars = cfg.get("push_min_stars", DEFAULT_PUSH_MIN_STARS)
        if isinstance(stars, bool) or not isinstance(stars, int) or stars < 0:
            raise ValueError(f"[sources.github] push_min_stars must be a whole number, 0 or more, got {stars!r}")
        ignore = cfg.get("ignore_repos", [])
        if not isinstance(ignore, list) or not all(isinstance(g, str) for g in ignore):
            raise ValueError(f"[sources.github] ignore_repos must be a list of owner/name globs, got {ignore!r}")
        return stars, [g.lower() for g in ignore]

    def _ignored(self, full_name: str) -> bool:
        return any(fnmatch.fnmatchcase(full_name.lower(), g) for g in self._ignore)

    # -- star baseline (local state: star counts as of the last run) -------------------------

    def _stars_path(self, ctx):
        return ctx.data_dir / "state" / "github.json" if ctx.data_dir else None

    def _load_stars(self, ctx) -> dict:
        path = self._stars_path(ctx)
        try:
            data = json.loads(path.read_text()) if path else {}
            stars = data.get("stars", {}) if isinstance(data, dict) else {}
            if not isinstance(stars, dict):
                return {}
            return {k: v for k, v in stars.items() if isinstance(v, int)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_stars(self, ctx) -> None:
        path = self._stars_path(ctx)
        if path and ctx.persist and self._stars:
            atomic_write_json(path, {"stars": self._stars})

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
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("items"), list):
            return []
        return [self._repo_item("rising", r, now, r["created_at"]) for r in body["items"]
                if not self._ignored(str(r.get("full_name", "")))]

    # -- watched accounts ----------------------------------------------------------------

    def _account(self, ctx, now, account, items: list) -> None:
        """Append this account's items to `items` as they are found, so a rate limit or error
        part-way through keeps what was already collected."""
        lookback = int(ctx.config.get("lookback_days", 7))
        push_days = int(ctx.config.get("push_days", 1))
        status, repos = self._get(ctx, f"/users/{account}/repos", {"sort": "pushed", "per_page": 30})
        if status != 200 or not isinstance(repos, list):
            return
        for r in repos:
            try:
                self._repo(ctx, now, r, items, lookback, push_days)
            except RateLimited:
                raise
            except (KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
                log.warning("github: skipped a malformed repo entry from %s: %s", account, exc)

    def _repo(self, ctx, now, r, items, lookback, push_days) -> None:
        if r.get("fork") or self._ignored(r["full_name"]):
            return
        created = _parse(r["created_at"])
        pushed = _parse(r["pushed_at"]) if r.get("pushed_at") else created
        stars_item = self._star_activity(ctx, r, now)
        if stars_item:
            items.append(stars_item)
        if now - created <= timedelta(days=lookback):
            items.append(self._repo_item("repo", r, now, r["created_at"]))
            return
        if now - pushed > timedelta(days=lookback):
            return
        releases = self._releases(ctx, now, r["full_name"])
        items += releases
        if not releases:
            items += self._tags(ctx, r)
            if (now - pushed <= timedelta(days=push_days)
                    and r.get("stargazers_count", 0) >= self._push_min_stars):
                item = self._repo_item("push", r, now, r["pushed_at"])
                item.id = f"push:{r['full_name']}:{pushed.date().isoformat()}"
                items.append(item)

    def _star_activity(self, ctx, r, now) -> Item | None:
        """A notable star gain since the last run. The first sighting only sets the baseline."""
        name, stars = r["full_name"], r.get("stargazers_count", 0)
        before = self._stars.get(name)
        self._stars[name] = stars
        gained = stars - before if before is not None else 0
        if gained < int(ctx.config.get("star_delta", 50)):
            return None
        item = self._repo_item("stars", r, now, now.isoformat().replace("+00:00", "Z"))
        item.id = f"stars:{name}:{now.date().isoformat()}"
        item.metrics["stars_gained"] = gained
        return item

    def _releases(self, ctx, now, full_name) -> list[Item]:
        if self._ignored(full_name):
            return []
        lookback = int(ctx.config.get("lookback_days", 7))
        status, body = self._get(ctx, f"/repos/{full_name}/releases", {"per_page": 5})
        if status != 200 or not isinstance(body, list):
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
        status, body = self._get(ctx, f"/repos/{r['full_name']}/tags", {"per_page": 1})
        if status != 200 or not isinstance(body, list):
            return []
        return [
            Item(source="github", id=f"tag:{r['full_name']}:{t['name']}",
                 url=f"{r['html_url']}/releases/tag/{t['name']}", title=f"{r['full_name']} {t['name']}",
                 author=r["owner"]["login"], published=r["pushed_at"], text=_excerpt(r.get("description")))
            for t in body
        ]

