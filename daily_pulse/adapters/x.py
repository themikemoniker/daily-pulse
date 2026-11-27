"""X (Twitter): an OPT-IN adapter on the official, PAID X API v2.

Nothing runs, and no request is ever made, unless `[sources.x] paid_api_approved = true` AND the
environment variable named by `token_env` (default X_BEARER_TOKEN) is non-empty. Until then
`status()` says what is missing and the run carries on. With `--fixtures` the adapter reads
recorded responses from <fixtures>/x/responses.json and never touches the network.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from daily_pulse.adapters.base import AdapterContext, SourceAdapter
from daily_pulse.models import Item
from daily_pulse.state import atomic_write_json

log = logging.getLogger("daily_pulse")

API = "https://api.x.com/2"
DEFAULT_TOKEN_ENV = "X_BEARER_TOKEN"
EXCERPT_CHARS = 200
USERNAME = re.compile(r"[A-Za-z0-9_]{1,15}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Prices from the reference script's notes (X pay-per-use, Feb 2026). An estimate, not verified billing.
POST_READ_COST = 0.005
USER_READ_COST = 0.010
STOP_STATUSES = {401: "token rejected", 402: "payment required, X credits exhausted", 429: "rate limited"}


class XStopped(RuntimeError):
    """X refused (402/429) or rejected the token (401): stop the whole module, fail only it."""


class UrllibTransport:
    """Real HTTP. The token is read from the environment on each call and only ever placed in the
    Authorization header: it is not stored, logged, shown in repr or put in an exception."""

    TIMEOUT = 30

    def __init__(self, token_env: str):
        self._token_env = token_env

    def __repr__(self) -> str:
        return "UrllibTransport()"

    def __call__(self, path: str, params: dict):
        url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        token = os.environ.get(self._token_env, "").strip()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "daily-pulse"})
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"X API request failed on {path}: {type(exc).__name__}") from None
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, None


class FixtureTransport:
    """Serves recorded responses from <fixtures>/x/responses.json, keyed by API path."""

    def __init__(self, fixtures_dir):
        self._responses = json.loads((fixtures_dir / "x" / "responses.json").read_text(encoding="utf-8"))

    def __call__(self, path: str, params: dict):
        r = self._responses.get(path, {"status": 404, "body": {"title": "Not Found"}})
        return r["status"], r["body"]


def _int(cfg: dict, key: str, default: int, high: int) -> int:
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
        raise ValueError(f"[sources.x] {key} must be a whole number from 1 to {high}, got {value!r}")
    return value


def settings(cfg: dict) -> dict:
    """The validated [sources.x] settings. Raises ValueError naming the bad key."""
    approved = cfg.get("paid_api_approved", False)
    if not isinstance(approved, bool):
        raise ValueError(f"[sources.x] paid_api_approved must be true or false, got {approved!r}")
    token_env = cfg.get("token_env", DEFAULT_TOKEN_ENV)
    if not isinstance(token_env, str) or not ENV_NAME.fullmatch(token_env):
        raise ValueError(f"[sources.x] token_env must be an environment variable name, got {token_env!r}")
    names = cfg.get("usernames", [])
    if not isinstance(names, list) or not all(isinstance(n, str) and USERNAME.fullmatch(n) for n in names):
        raise ValueError("[sources.x] usernames must be a list of @-less usernames "
                         f"(letters, digits, underscore, 1 to 15 characters), got {names!r}")
    usernames = []
    for n in names:  # case-insensitive dedupe, first spelling wins
        if n.lower() not in (u.lower() for u in usernames):
            usernames.append(n)
    return {"paid_api_approved": approved, "token_env": token_env, "usernames": usernames,
            "max_accounts": _int(cfg, "max_accounts", 10, 100),
            "max_posts_per_account": _int(cfg, "max_posts_per_account", 10, 100),
            "lookback_days": _int(cfg, "lookback_days", 1, 30)}


def _excerpt(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1].rstrip() + "…"


def classify(tweet: dict, included: dict) -> tuple[str, str, dict | None]:
    """(kind, full text, original tweet) for one timeline entry. A repost shows the original's
    text, resolved from `includes.tweets`; if X did not include it, its own (truncated) text."""
    for ref in tweet.get("referenced_tweets") or []:
        if isinstance(ref, dict) and ref.get("type") == "retweeted":
            orig = included.get(ref.get("id"))
            return "repost", (orig or tweet).get("text") or "", orig
    return "post", tweet.get("text") or "", None


class XAdapter(SourceAdapter):
    name = "x"
    rated_by_runner = True  # rated from the post text (held in memory) plus metadata

    def __init__(self, transport=None, clock=lambda: datetime.now(timezone.utc)):
        self._transport = transport  # callable(path, params) -> (status, body)
        self._clock = clock
        self.texts: dict[str, str] = {}  # full post text, in memory, for the summarizer only
        self.last_run: dict = {"post_reads": 0, "user_reads": 0, "estimated_cost": 0.0}

    def status(self, ctx: AdapterContext) -> str | None:
        s = settings(ctx.config)
        missing = []
        if not s["paid_api_approved"]:
            missing.append("[sources.x] paid_api_approved is false (the X API is paid; set it to true to opt in)")
        if not s["usernames"]:
            missing.append("[sources.x] usernames is empty (add @-less usernames; handles are display names and are not used)")
        if ctx.fixtures_dir is not None:
            if not (ctx.fixtures_dir / "x" / "responses.json").is_file():
                missing.append("no recorded X responses in the fixtures (x/responses.json)")
        elif not os.environ.get(s["token_env"], "").strip():
            missing.append(f"environment variable {s['token_env']} is empty or unset")
        return "deferred: " + "; ".join(missing) if missing else None

    # -- API ----------------------------------------------------------------------------

    def _get(self, transport, path: str, params: dict):
        status, body = transport(path, params)
        if status in STOP_STATUSES:
            raise XStopped(f"X API HTTP {status} ({STOP_STATUSES[status]}) on {path}: "
                           "X module stopped, no further requests this run")
        return status, body

    def _user_id(self, transport, name: str, cache: dict) -> str | None:
        if name.lower() in cache:
            return cache[name.lower()]
        status, body = self._get(transport, f"/users/by/username/{name}", {})
        data = body.get("data") if isinstance(body, dict) else None
        uid = data.get("id") if isinstance(data, dict) else None
        if status == 200:
            self.last_run["user_reads"] += 1  # billed whether or not the user exists
        if status != 200 or not isinstance(uid, str) or not uid.isdigit():
            log.warning("x: cannot resolve @%s (HTTP %s), skipped", name, status)
            return None
        cache[name.lower()] = uid
        return uid

    def _timeline(self, transport, uid: str, s: dict) -> tuple[list[dict], dict, dict]:
        since = (self._clock() - timedelta(days=s["lookback_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        status, body = self._get(transport, f"/users/{uid}/tweets", {
            "max_results": str(max(5, s["max_posts_per_account"])),  # the API's minimum is 5
            "tweet.fields": "created_at,public_metrics,referenced_tweets,author_id",
            "expansions": "referenced_tweets.id,referenced_tweets.id.author_id",
            "user.fields": "username", "exclude": "replies", "start_time": since})
        if status != 200 or not isinstance(body, dict):
            log.warning("x: timeline for user %s failed (HTTP %s), skipped", uid, status)
            return [], {}, {}
        tweets = [t for t in body.get("data") or [] if isinstance(t, dict) and isinstance(t.get("id"), str)]
        self.last_run["post_reads"] += len(tweets)
        inc = body.get("includes") if isinstance(body.get("includes"), dict) else {}
        included = {t["id"]: t for t in inc.get("tweets") or [] if isinstance(t, dict) and "id" in t}
        users = {u["id"]: u["username"] for u in inc.get("users") or [] if isinstance(u, dict) and "username" in u}
        return tweets[: s["max_posts_per_account"]], included, users

    # -- id cache (local state: username -> user id, so a lookup is paid for once) --------

    @staticmethod
    def _cache_path(ctx):
        return ctx.data_dir / "state" / "x-user-ids.json" if ctx.data_dir else None

    def _load_cache(self, ctx) -> dict:
        path = self._cache_path(ctx)
        try:
            data = json.loads(path.read_text()).get("user_ids", {}) if path else {}
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_cache(self, ctx, cache: dict) -> None:
        path = self._cache_path(ctx)
        if path and ctx.persist and cache and cache != self._load_cache(ctx):
            atomic_write_json(path, {"user_ids": cache})

    # -- adapter ------------------------------------------------------------------------

    def list_new(self, ctx: AdapterContext) -> list[Item]:
        reason = self.status(ctx)  # defence in depth: never reach the network when not opted in
        if reason:
            log.info("x: %s", reason)
            return []
        s = settings(ctx.config)
        fixtures = ctx.fixtures_dir is not None
        transport = self._transport or (
            FixtureTransport(ctx.fixtures_dir) if fixtures else UrllibTransport(s["token_env"]))
        self.last_run = {"post_reads": 0, "user_reads": 0, "estimated_cost": 0.0}
        cache = self._load_cache(ctx)
        items: dict[str, Item] = {}
        try:
            for name in s["usernames"][: s["max_accounts"]]:
                uid = self._user_id(transport, name, cache)
                if uid is None:
                    continue
                tweets, included, users = self._timeline(transport, uid, s)
                for tw in tweets:
                    item = self._item(name, tw, included, users)
                    items.setdefault(item.id, item)
        finally:
            self._save_cache(ctx, cache)
            r = self.last_run
            r["estimated_cost"] = r["post_reads"] * POST_READ_COST + r["user_reads"] * USER_READ_COST
            log.info("x: %d post read(s), %d user lookup(s); estimated cost $%.3f (an estimate from the "
                     "reference script's notes, $%.3f per post read and $%.3f per user lookup; not verified "
                     "billing)%s", r["post_reads"], r["user_reads"], r["estimated_cost"], POST_READ_COST,
                     USER_READ_COST, "; recorded fixtures, nothing was requested or billed" if fixtures else "")
        return sorted(items.values(), key=lambda i: (i.published, i.id), reverse=True)

    def _item(self, username: str, tw: dict, included: dict, users: dict) -> Item:
        kind, text, orig = classify(tw, included)
        tid = tw["id"]
        self.texts[tid] = text
        m = ((orig or {}).get("public_metrics") or tw.get("public_metrics") or {})  # a repost: the original's
        title = f"{kind.capitalize()} by @{username} ({tid})"
        original_author = users.get((orig or {}).get("author_id"))
        if kind == "repost" and original_author:
            title = f"Repost by @{username} of @{original_author} ({tid})"
        return Item(source=self.name, id=tid, url=f"https://x.com/{username}/status/{tid}", title=title,
                    author=username, published=tw.get("created_at") or "",
                    metrics={"likes": m.get("like_count", 0), "reposts": m.get("retweet_count", 0),
                             "replies": m.get("reply_count", 0), "kind": kind})

    def fetch_details(self, item: Item, ctx: AdapterContext) -> Item:
        item.text = _excerpt(self.texts.get(item.id, ""))
        return item

    def rating_text(self, item: Item, ctx: AdapterContext) -> str:
        return self.texts.get(item.id, "")
