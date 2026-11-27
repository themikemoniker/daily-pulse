"""GitHub source: rising repos plus watched accounts, via the REST API."""
from __future__ import annotations

import json
import logging
from datetime import date as _date
from datetime import datetime, timedelta, timezone

from daily_pulse.adapters import registry
from daily_pulse.adapters.base import AdapterContext, SourceAdapter
from daily_pulse.models import Item

log = logging.getLogger("daily_pulse")

API = "https://api.github.com"
EXCERPT_CHARS = 400


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

    def __init__(self, transport=None):
        self._transport = transport  # callable(path, params) -> (status, headers, body)

    def _now(self, ctx: AdapterContext) -> datetime:
        day = _date.fromisoformat(ctx.date) if ctx.date else datetime.now(timezone.utc).date()
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)

    def _get(self, ctx: AdapterContext, path: str, params: dict | None = None):
        transport = self._transport or FixtureTransport(ctx.fixtures_dir)
        status, _headers, body = transport(path, params or {})
        return status, body

    def list_new(self, ctx: AdapterContext) -> list[Item]:
        cfg = ctx.config
        now = self._now(ctx)
        items: list[Item] = []
        items += self._rising(ctx, now, int(cfg.get("rising_days", 7)))
        for account in cfg.get("watch_accounts", []):
            items += self._account(ctx, now, account)
        for repo in cfg.get("watch_repos", []):
            items += self._releases(ctx, now, repo)
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
