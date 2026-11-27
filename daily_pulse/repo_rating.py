"""Relevance rating for GitHub repos, ported from the working digest script `gh_digest.py`.

The script's first version rated repos by star velocity alone and gave 10/10 to a game-studio agent and
a typing cat. The fix, kept here: relevance 1-10 against the rubric in ONE summarizer call
(`prompts/rate-repos.md`); velocity is a signal and a tiebreaker only; a diamond is relevance >= 7 AND
>= 50 stars a day; with no summarizer a repo is UNRATED, never given a guessed score.

`topic_score`, `age_days`, `velocity_of`, `is_diamond`, `format_repo_for_rating` and
`parse_batch_ratings` are the script's functions. `rate_repos_batch` is adapted to the repo's hardened,
tool-less `claude -p` wrapper (so the per-run call cap and the timeout apply) in place of a raw
subprocess call.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from daily_pulse.claude import CallFailed

log = logging.getLogger("daily_pulse")

TOPIC_KEYWORDS = {
    "agent": 2, "agents": 2, "harness": 2, "mcp": 2, "eval": 2,
    "llm": 1, "claude": 1, "codex": 1, "orchestrat": 1, "multi-agent": 2,
    "coding-agent": 2, "autonomous": 1, "worktree": 1,
}
DIAMOND_MIN_RELEVANCE = 7
DIAMOND_MIN_VELOCITY = 50  # stars per day
DESCRIPTION_CHARS = 300  # an untrusted description is cut to this in the prompt


def age_days(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max(0.1, (datetime.now(timezone.utc) - dt).days
                   + (datetime.now(timezone.utc) - dt).seconds / 86400)
    except (ValueError, TypeError, AttributeError):
        return 1.0


def topic_score(repo):
    text = " ".join([repo.get("name", ""), repo.get("description") or ""] +
                    list(repo.get("topics") or [])).lower()
    return min(sum(w for kw, w in TOPIC_KEYWORDS.items() if kw in text), 3)


def velocity_of(repo):
    """Stars per day since creation."""
    return round(repo.get("stargazers_count", 0)
                 / age_days(repo.get("created_at")), 1)


def is_diamond(rating, velocity):
    """Diamond in the rough: relevant AND fast-growing."""
    return (rating is not None and rating >= DIAMOND_MIN_RELEVANCE
            and velocity is not None and velocity >= DIAMOND_MIN_VELOCITY)


def format_repo_for_rating(repo, velocity):
    topics = ", ".join(repo.get("topics") or []) or "—"
    description = " ".join(str(repo.get("description") or "(none)").split())[:DESCRIPTION_CHARS]
    line = (f"{repo['full_name']} — {repo.get('language') or '—'} — "
            f"★{repo.get('stargazers_count', 0)} ({velocity}/day) — "
            f"Topics: {topics} — "
            f"Description: {description}")
    excerpt = repo.get("_readme_excerpt") or ""
    if excerpt:
        line += f" — README excerpt: {excerpt}"
    return line


def parse_batch_ratings(text):
    """Parse the summarizer's JSON object:
    {full_name: {"rating": 1-10, "reason": "...", "verdict": "..."}}.
    Returns {} on any failure — the caller leaves those repos unrated."""
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, v in data.items():
        if not isinstance(v, dict):
            continue
        try:
            rating = float(v.get("rating"))
        except (TypeError, ValueError):
            continue
        if not 1 <= rating <= 10:
            continue
        out[name] = {"rating": rating,
                     "reason": str(v.get("reason", "")).strip(),
                     "verdict": str(v.get("verdict", "")).strip().lower()}
    return out


def rate_repos_batch(repos, summarizer, prompts_dir=None):
    """One summarizer call rating all candidate repos for RELEVANCE against the rubric
    (prompts/rate-repos.md).

    Returns {full_name: {"rating", "reason", "verdict"}}. An empty dict means there was no
    summarizer, the call cap was spent, or the call failed: callers leave those repos UNRATED
    (they are retried on a later run), never a guessed score.
    """
    if summarizer is None or not repos:
        return {}
    prompt_file = Path(prompts_dir or summarizer.prompts_dir) / "rate-repos.md"
    try:
        template = prompt_file.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("github: rating prompt missing, repos left unrated: %s", exc)
        return {}
    if summarizer.calls_made >= summarizer.max_calls:
        log.warning("github: claude call cap of %d reached, repos left unrated", summarizer.max_calls)
        return {}
    listing = "\n".join(
        f"{i}. {format_repo_for_rating(r, velocity_of(r))}"
        for i, r in enumerate(repos, 1))
    prompt = template + "\n\nRepos to rate (one per line):\n" + listing + "\n"
    try:
        out = summarizer._call(prompt)
    except CallFailed as exc:
        log.warning("github: batch rating failed, repos left unrated: %s", exc)
        return {}
    ratings = parse_batch_ratings(out)
    if not ratings:
        log.warning("github: batch rating output did not parse as JSON, repos left unrated")
    return ratings
