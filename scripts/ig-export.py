#!/usr/bin/env python3
"""Print the owner's saved and recently-liked Instagram reels as the JSON list the Instagram adapter
reads (see docs/instagram-adapter.md). It wraps the Meta-connector CLI the owner already has linked
(`instagram-cli saved-posts` and `recently-liked-posts`) and reads nothing else.

Set it as the adapter's command: IG_CLI_CMD="python3 /path/to/scripts/ig-export.py"

Environment (nothing here is ever committed or logged):
  IG_ACCOUNT_ID    the linked account's id, from `instagram-cli accounts`        (required)
  INSTAGRAM_CLI    the connector executable                                       (default: instagram-cli)
  IG_EXPORT_LIMIT  most items per kind, newest first                              (default: 20)
Metadata and caption only. The connector's AI "media understanding" narratives are not requested.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

PAGE = 25
SHORTCODE = re.compile(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")


def fail(message: str) -> None:
    print(f"ig-export: {message}", file=sys.stderr)
    sys.exit(1)


def call(cli: str, args: list[str]) -> dict:
    try:
        proc = subprocess.run([cli, *args], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        fail(f"{os.path.basename(cli)} not found: set INSTAGRAM_CLI to the connector executable")
    except (OSError, subprocess.TimeoutExpired):
        fail(f"{os.path.basename(cli)} could not be run or timed out")
    if proc.returncode != 0:
        fail(f"{os.path.basename(cli)} {args[0]} failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        fail(f"{os.path.basename(cli)} {args[0]} printed non-JSON output")
    if not isinstance(data, dict):
        fail(f"{os.path.basename(cli)} {args[0]} printed an unexpected shape")
    return data


def list_posts(cli: str, account: str, command: str, limit: int) -> list[dict]:
    """Page through one list, newest first, stopping at `limit`, a repeated cursor or the last page."""
    posts, after, seen = [], None, set()
    while len(posts) < limit:
        args = [command, "--account-id", account, "--limit", str(min(limit - len(posts), PAGE))]
        if after:
            args += ["--after", after]
        data = call(cli, args)
        batch = data.get("posts")
        if not isinstance(batch, list) or not batch:
            break
        posts.extend(p for p in batch if isinstance(p, dict))
        after = data.get("end_cursor")
        if not data.get("has_next_page") or not isinstance(after, str) or not after or after in seen:
            break
        seen.add(after)
    return posts[:limit]


def timestamp(value) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value.strip() if isinstance(value, str) and value.strip() else None


def entry(post: dict, source: str) -> dict | None:
    """One adapter entry, or None for anything that is not a reel with a usable link."""
    if str(post.get("post_type") or "reel").lower() != "reel":
        return None
    url = post.get("media_permalink")
    match = SHORTCODE.search(url) if isinstance(url, str) else None
    if not match:
        return None
    out = {"shortcode": match.group(1), "url": url.split("?", 1)[0], "source": source}
    author = post.get("author_info")
    if isinstance(author, dict) and isinstance(author.get("username"), str) and author["username"]:
        out["author"] = author["username"]
    if isinstance(post.get("post_caption_text"), str):
        out["caption"] = post["post_caption_text"]
    stamp = timestamp(post.get("post_creation_time"))
    if stamp:
        out["timestamp"] = stamp
    for key in ("like_count", "comment_count"):
        value = post.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    return out


def main() -> int:
    account = os.environ.get("IG_ACCOUNT_ID", "").strip()
    if not account:
        fail("IG_ACCOUNT_ID is not set (find it with `instagram-cli accounts`)")
    cli = os.environ.get("INSTAGRAM_CLI", "").strip() or "instagram-cli"
    raw = os.environ.get("IG_EXPORT_LIMIT", "").strip() or "20"
    if not raw.isdigit() or int(raw) < 1:
        fail("IG_EXPORT_LIMIT must be a whole number >= 1")
    limit, entries = int(raw), []
    for source, command in (("saved", "saved-posts"), ("liked", "recently-liked-posts")):
        entries += [e for e in (entry(p, source) for p in list_posts(cli, account, command, limit)) if e]
    json.dump(entries, sys.stdout, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
