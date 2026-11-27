#!/usr/bin/env python3
"""ig_digest: Instagram saved + recently-liked reels -> summary notes.

Uses the Instagram connector CLI (/opt/hatch/bin/instagram-cli). Read-only.
Lists the user's saved posts and recently-liked posts, dedupes against
ig-state.json, fetches the stored media-understanding narrative for new
reels, and writes one markdown note per item in the same format as
yt_digest.py notes (frontmatter + sections).

What it CANNOT do (see instagram-feasibility.md):
  - No transcript / caption track exists for reels. The
    media-understanding narrative summary is the closest available text;
    real transcripts need the audio downloaded + Whisper (a later step).
  - Liked items are "recently-liked" only -- Instagram exposes no full
    like history through this connector.
  - No per-reel Views/Saves/Shares in list rows (likes + comments only).

Caption text is stored as a short excerpt (<=280 chars) with a link to the
permalink for the full text, per the connector's guidance not to reproduce
raw content verbatim in files.

Stdlib only. External tool: instagram-cli.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "ig_config.json"
DEFAULT_CLI = "/opt/hatch/bin/instagram-cli"
CAPTION_EXCERPT = 280
MAX_UNDERSTANDING_PER_RUN = 20  # connector guidance: <=50 detailed reads/run


def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def cli(cli_path, args, timeout=120):
    """Run instagram-cli; return parsed JSON or raise RuntimeError."""
    try:
        p = subprocess.run([cli_path] + args, capture_output=True,
                           text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"instagram-cli not found at {cli_path}")
    if p.returncode != 0:
        raise RuntimeError(f"instagram-cli failed: {p.stderr.strip()[:300]}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("instagram-cli returned non-JSON output")


def list_items(cli_path, account_id, source, limit, collection_id=None):
    """Page through saved-posts or recently-liked-posts.

    Returns list of raw post dicts (newest first). Stops on a reused
    cursor or when `limit` items have been collected.
    """
    cmd_base = ["saved-posts"] if source == "saved" else ["recently-liked-posts"]
    items, after, seen_cursors = [], None, set()
    while len(items) < limit:
        args = cmd_base + ["--account-id", account_id,
                           "--limit", str(min(limit - len(items), 25))]
        if collection_id:
            args += ["--collection-id", collection_id]
        if after:
            args += ["--after", after]
        data = cli(cli_path, args)
        batch = data.get("posts", [])
        if not batch:
            break
        items.extend(batch)
        if not data.get("has_next_page"):
            break
        after = data.get("end_cursor")
        if not after or after in seen_cursors:
            break
        seen_cursors.add(after)
    return items[:limit]


def fetch_understandings(cli_path, account_id, media_ids):
    """Batch media-understanding lookups. Returns {media_id: {...}}."""
    out = {}
    for i in range(0, len(media_ids), 10):
        batch = media_ids[i:i + 10]
        try:
            data = cli(cli_path, ["media-understanding", "--account-id",
                                  account_id, "--media-ids", ",".join(batch)])
        except RuntimeError as e:
            log(f"  media-understanding failed for batch: {e}")
            continue
        for m in data.get("media", []):
            if m.get("media_id"):
                out[m["media_id"]] = m
    return out


def excerpt(text, limit=CAPTION_EXCERPT):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def title_from(post):
    cap = (post.get("post_caption_text") or "").strip().splitlines()
    first = cap[0].strip() if cap and cap[0].strip() else ""
    if first:
        return first[:80]
    author = (post.get("author_info") or {}).get("username", "unknown")
    return f"Reel by @{author}"


def write_note(outdir, post, kind, understanding):
    mid = post["media_id"]
    author = post.get("author_info") or {}
    username = author.get("username", "unknown")
    unified = post.get("unified_media") or {}
    understanding = understanding or {}
    narrative = (understanding.get("narrative_summary")
                 or (unified.get("understanding") or {}).get("core_insight")
                 or "")
    tags = [f"#{h}" for h in (post.get("hashtags") or [])[:5]]
    title = title_from(post).replace('"', "'")
    fm = [
        "---",
        f"title: \"{title}\"",
        f"author: \"@{username}\"",
        f"media_id: {mid}",
        f"url: {post.get('media_permalink', '')}",
        f"published: {post.get('post_creation_time', '')}",
        f"post_type: {post.get('post_type', '')}",
        f"likes: {post.get('like_count', '')}",
        f"comments: {post.get('comment_count', '')}",
        "rating: ",
        "verdict: ",
        f"tags: [{', '.join(tags)}]",
        "source: instagram",
        f"kind: {kind}",
        "transcript: unavailable",
        "---",
    ]
    lines = fm + [""]
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"@{username} · {post.get('post_creation_time', '')} · "
                 f"{post.get('like_count', 0)} likes · "
                 f"{post.get('comment_count', 0)} comments")
    lines.append(f"[Open on Instagram]({post.get('media_permalink', '')})")
    lines.append("")
    if narrative:
        lines.append("## What it is")
        lines.append("")
        lines.append("_Stored media-understanding summary (not a transcript; "
                     "no caption track exists for reels)._")
        lines.append("")
        lines.append(narrative)
        lines.append("")
    cap = excerpt(post.get("post_caption_text", ""))
    if cap:
        lines.append("## Caption (excerpt)")
        lines.append("")
        lines.append(cap)
        lines.append("")
        lines.append("_Full caption at the permalink above._")
        lines.append("")
    lines.append("## Author")
    lines.append("")
    lines.append(f"{author.get('name', '')} (@{username}) — "
                 f"{author.get('follower_count', '')} followers"
                 + (" · verified" if author.get("is_verified") else ""))
    if author.get("bio"):
        lines.append("")
        lines.append(author["bio"])
    lines.append("")
    lines.append("## Source")
    lines.append("")
    lines.append(f"Saved to your {kind} list on Instagram. "
                 "Transcript: unavailable — reels have no caption track; "
                 "audio → Whisper is a later step (see "
                 "instagram-feasibility.md).")
    lines.append("")
    note_path = outdir / f"{mid}.md"
    note_path.write_text("\n".join(lines))
    return note_path


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="ig_digest.py",
        description="Instagram saved/liked reels -> notes.")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--account-id", help="Instagram user_fbid")
    p.add_argument("--source", choices=["saved", "liked", "both"],
                   default="both")
    p.add_argument("--limit", type=int, default=10,
                   help="max new items per source this run")
    p.add_argument("--no-understanding", action="store_true",
                   help="skip media-understanding lookups")
    p.add_argument("--cli", default=DEFAULT_CLI)
    p.add_argument("--output-dir",
                   default=str(Path.home() / "workspace/yt-digest/ig-notes"))
    args = p.parse_args(argv)

    cfg = load_json(args.config, {})
    account_id = args.account_id or cfg.get("account_id")
    if not account_id:
        print("No Instagram account: pass --account-id or set account_id "
              "in ig_config.json (run `instagram-cli accounts` to find it).")
        return 1
    outdir = Path(os.path.expanduser(args.output_dir))
    outdir.mkdir(parents=True, exist_ok=True)
    state_path = outdir.parent / "ig-state.json"
    state = load_json(state_path, {"items": {}})

    sources = ["saved", "liked"] if args.source == "both" else [args.source]
    total_new = 0
    for kind in sources:
        limit = args.limit
        collections = cfg.get("collections", []) if kind == "saved" else []
        batches = [(None, None)] if not collections else [
            (c.get("id"), c.get("name", c.get("id"))) for c in collections]
        for collection_id, collection_name in batches:
            try:
                items = list_items(args.cli, account_id, kind, limit,
                                   collection_id)
            except RuntimeError as e:
                log(f"ERROR listing {kind} "
                    f"({collection_name or 'all'}): {e}")
                continue
            new = [it for it in items
                   if it.get("media_id") not in state["items"]]
            log(f"{kind} ({collection_name or 'all'}): {len(items)} listed, "
                f"{len(new)} new")
            if not new:
                continue
            understandings = {}
            if not args.no_understanding:
                mids = [it["media_id"]
                        for it in new[:MAX_UNDERSTANDING_PER_RUN]]
                understandings = fetch_understandings(args.cli, account_id,
                                                      mids)
            for it in new:
                mid = it["media_id"]
                note = write_note(outdir, it, kind,
                                  understandings.get(mid))
                state["items"][mid] = {
                    "title": title_from(it),
                    "author": (it.get("author_info") or {}).get("username"),
                    "url": it.get("media_permalink"),
                    "published": it.get("post_creation_time"),
                    "kind": kind,
                    "status": "noted",
                    "note": str(note),
                    "processed_at": date.today().isoformat(),
                }
                total_new += 1
            save_json(state_path, state)
    log(f"done: {total_new} new item(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
