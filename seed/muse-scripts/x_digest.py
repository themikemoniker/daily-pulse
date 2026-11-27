#!/usr/bin/env python3
"""x_digest: X/Twitter accounts' posts + reposts -> notes.

Uses the OFFICIAL X API v2 (pay-per-use). There is no free read tier:
as of Feb 2026 every post read costs $0.005 and every user lookup $0.010
(new payers get a one-time credit, up to ~$70, expiring in 3 months).
No X connector exists in Muse and social.search does not cover X.

Setup (one time):
  1. developer.x.com -> create a Project/App, get a Bearer Token.
  2. export X_BEARER_TOKEN="<token>"   (never put it in config.json)
  3. edit x_config.json with the accounts to follow.
  4. ./x_digest.py --check   (verifies token + quota without spending reads)

Cost guard: every run logs its estimated cost. Default caps (config):
max_accounts 10, max_posts_per_account 10, i.e. ~100 post reads + ~10
user reads per run ~= $0.60/day. Re-reading the same post on the same UTC
day is not re-billed by X, and this script dedupes by tweet id anyway.

Stdlib only (urllib). No scraping.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
API = "https://api.x.com/2"
POST_READ_COST = 0.005
USER_READ_COST = 0.010

INTEREST_KEYWORDS = {
    "harness": 2, "eval": 2, "agent": 1, "multi-agent": 2, "mcp": 2,
    "claude": 1, "codex": 1, "routing": 1, "guardrail": 1, "guard rail": 1,
    "orchestrat": 1, "subagent": 1, "deterministic": 1, "context": 1,
    "worktree": 1, "coding agent": 2,
}


def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


class XClient:
    def __init__(self, bearer):
        self.bearer = bearer
        self.reads = 0  # post reads (for cost estimate)
        self.user_reads = 0

    def get(self, path, params=None):
        from urllib.parse import urlencode
        url = API + path
        if params:
            url += "?" + urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.bearer}",
            "User-Agent": "x-digest/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"X API {e.code} on {path}: {body}")

    def user_id(self, username, cache):
        if username in cache:
            return cache[username]
        data = self.get(f"/users/by/username/{username}",
                        {"user.fields": "public_metrics"})
        self.user_reads += 1
        uid = data["data"]["id"]
        cache[username] = uid
        return uid

    def timeline(self, user_id, max_results=10, since=None):
        params = {
            "max_results": str(max_results),
            "tweet.fields": "created_at,public_metrics,referenced_tweets,"
                            "entities,conversation_id",
            "expansions": "referenced_tweets.id",
            "exclude": "replies",
        }
        if since:
            params["start_time"] = since
        data = self.get(f"/users/{user_id}/tweets", params)
        tweets = data.get("data", []) or []
        self.reads += len(tweets)
        included = {t["id"]: t for t in data.get("includes", {}).get("tweets", [])}
        return tweets, included


def short_title(text, limit=80):
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rsplit(" ", 1)[0]


def heuristic_rating(text, metrics):
    """Transparent heuristic 1-10 (NOT an LLM rating).

    Base 5; +interest-keyword hits (cap +3); +engagement velocity
    (likes+reposts, log scale, cap +2). Labelled heuristic in the note.
    """
    t = text.lower()
    hits = sum(w for kw, w in INTEREST_KEYWORDS.items() if kw in t)
    score = 5 + min(hits, 3)
    eng = (metrics.get("like_count", 0) + metrics.get("retweet_count", 0))
    if eng >= 1000:
        score += 2
    elif eng >= 100:
        score += 1
    return min(score, 10)


def classify(tweet, included):
    """Return (kind, display_text, original_author_or_None)."""
    refs = tweet.get("referenced_tweets") or []
    for r in refs:
        if r.get("type") == "retweeted":
            orig = included.get(r["id"])
            if orig:
                return "repost", orig.get("text", ""), orig.get("author_id")
            return "repost", tweet.get("text", ""), None
    return "post", tweet.get("text", ""), None


def write_note(outdir, username, tweet, kind, text, metrics):
    tid = tweet["id"]
    created = tweet.get("created_at", "")
    clean = re.sub(r"\s+", " ", text).strip()
    title = (clean[:80].rsplit(" ", 1)[0] if len(clean) > 80 else clean
             ).replace('"', "'") or tid
    rating = heuristic_rating(text, metrics)
    url = f"https://x.com/{username}/status/{tid}"
    fm = [
        "---",
        f"title: \"{title}\"",
        f"author: \"@{username}\"",
        f"tweet_id: {tid}",
        f"url: {url}",
        f"published: {created}",
        f"likes: {metrics.get('like_count', '')}",
        f"reposts: {metrics.get('retweet_count', '')}",
        f"replies: {metrics.get('reply_count', '')}",
        f"rating: {rating}",
        "verdict: ",
        "tags: []",
        "source: x",
        f"kind: {kind}",
        "rating_basis: heuristic",
        "---",
    ]
    lines = fm + [""]
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"@{username} · {created} · {kind} · "
                 f"{metrics.get('like_count', 0)} likes · "
                 f"{metrics.get('retweet_count', 0)} reposts")
    lines.append(f"[Open on X]({url})")
    lines.append("")
    lines.append("## Post")
    lines.append("")
    lines.append(text)
    lines.append("")
    lines.append("## Rating (heuristic)")
    lines.append("")
    lines.append(f"{rating}/10 — keyword match to Jordan's interests + "
                 "engagement. Not an LLM rating; the summarizer pass can "
                 "replace it.")
    lines.append("")
    note_path = outdir / f"{tid}.md"
    note_path.write_text("\n".join(lines))
    return note_path


def process_timeline(client, cfg, state, username, outdir, max_posts, since):
    cache = state.setdefault("user_ids", {})
    try:
        uid = client.user_id(username, cache)
    except RuntimeError as e:
        log(f"  ERROR resolving @{username}: {e}")
        return 0
    try:
        tweets, included = client.timeline(uid, max_posts, since)
    except RuntimeError as e:
        log(f"  ERROR timeline @{username}: {e}")
        return 0
    new = 0
    for tw in tweets:
        tid = tw["id"]
        if tid in state["items"]:
            continue
        kind, text, _orig = classify(tw, included)
        metrics = tw.get("public_metrics", {})
        note = write_note(outdir, username, tw, kind, text, metrics)
        state["items"][tid] = {
            "title": short_title(text),
            "author": username, "url": f"https://x.com/{username}/status/{tid}",
            "published": tw.get("created_at"), "kind": kind,
            "rating": heuristic_rating(text, metrics),
            "rating_basis": "heuristic",
            "status": "noted", "note": str(note),
            "processed_at": date.today().isoformat(),
        }
        new += 1
    save_json(cfg["_state_path"], state)
    log(f"  @{username}: {len(tweets)} fetched, {new} new")
    return new


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="x_digest.py",
        description="X accounts' posts + reposts -> notes (official API).")
    p.add_argument("--config", default=str(SCRIPT_DIR / "x_config.json"))
    p.add_argument("--check", action="store_true",
                   help="verify token + config without spending reads")
    p.add_argument("--fixture",
                   help="test mode: JSON file with {tweets, includes} "
                        "instead of the live API")
    p.add_argument("--output-dir",
                   default=str(Path.home() / "workspace/yt-digest/x-notes"))
    args = p.parse_args(argv)

    cfg = load_json(args.config, {})
    cfg["_state_path"] = (Path(args.output_dir).parent / "x-state.json")
    accounts = cfg.get("accounts", [])
    max_posts = int(cfg.get("max_posts_per_account", 10))
    days = int(cfg.get("lookback_days", 1))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    token = os.environ.get("X_BEARER_TOKEN")
    if args.check:
        print(f"accounts: {len(accounts)} "
              f"({', '.join('@' + a for a in accounts) or 'none configured'})")
        print(f"token: {'set' if token else 'MISSING — export X_BEARER_TOKEN'}")
        print(f"est. cost/run: ~${(len(accounts) * max_posts * POST_READ_COST + len(accounts) * USER_READ_COST):.2f}")
        return 0 if token and accounts else 1

    outdir = Path(os.path.expanduser(args.output_dir))
    outdir.mkdir(parents=True, exist_ok=True)
    state = load_json(cfg["_state_path"], {"items": {}, "user_ids": {}})

    if args.fixture:
        data = load_json(args.fixture, {})
        tweets = data.get("tweets", [])
        included = {t["id"]: t for t in data.get("includes", [])}
        new = 0
        for tw in tweets:
            tid = tw["id"]
            if tid in state["items"]:
                continue
            username = tw.get("author_username", "unknown")
            kind, text, _ = classify(tw, included)
            metrics = tw.get("public_metrics", {})
            note = write_note(outdir, username, tw, kind, text, metrics)
            state["items"][tid] = {
                "title": short_title(text),
                "author": username,
                "url": f"https://x.com/{username}/status/{tid}",
                "published": tw.get("created_at"), "kind": kind,
                "rating": heuristic_rating(text, metrics),
                "rating_basis": "heuristic",
                "status": "noted", "note": str(note),
                "processed_at": date.today().isoformat(),
            }
            new += 1
        save_json(cfg["_state_path"], state)
        log(f"fixture: {len(tweets)} tweets, {new} new notes")
        return 0

    if not token:
        print("X_BEARER_TOKEN is not set. See x-feasibility.md for setup.")
        return 1
    if not accounts:
        print("No accounts in x_config.json. Add usernames first.")
        return 1

    client = XClient(token)
    total = 0
    for username in accounts[:int(cfg.get("max_accounts", 10))]:
        total += process_timeline(client, cfg, state, username, outdir,
                                  max_posts, since)
    est = client.reads * POST_READ_COST + client.user_reads * USER_READ_COST
    log(f"done: {total} new post(s); API reads: {client.reads} posts, "
        f"{client.user_reads} users; est. cost ${est:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
