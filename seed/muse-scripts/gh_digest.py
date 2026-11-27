#!/usr/bin/env python3
"""gh_digest: rising GitHub repos + watched accounts' updates -> notes.

Uses api.github.com directly (urllib, stdlib only). Unauthenticated rate
limit is 60 requests/hour per IP -- on shared infrastructure that can
already be spent, so a GITHUB_TOKEN is strongly recommended (5000/hr):
export GITHUB_TOKEN="<classic token, no scopes needed for public data>".
Never put the token in config.json.

Two feeds:
  1. Rising: repos created in the last N days, sorted by stars; re-ranked
     in code by star velocity (stars / age in days). Ratings are
     RELEVANCE scores (1-10) from one batched summarizer call;
     velocity is a signal and tiebreaker only. A "diamond in the
     rough" = relevance >= 7 AND >= 50 stars/day.
  2. Watched accounts: new repos (last 30 days) + releases (last 7 days)
     for a configurable account list.

Without a summarizer (`--no-summarize`, or no `claude` CLI on the
machine) repos are left UNRATED (status "unrated", re-rated on the
next run that has a summarizer) — a keyword heuristic is not a
relevance score and is never ranked next to LLM scores. The keyword
match is shown as a signal in the note.

On a 403 rate limit the run stops cleanly, records the reset time in
state, and exits 0 -- the next scheduled run picks up where it left off.
"""
import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
API = "https://api.github.com"
TOPIC_KEYWORDS = {
    "agent": 2, "agents": 2, "harness": 2, "mcp": 2, "eval": 2,
    "llm": 1, "claude": 1, "codex": 1, "orchestrat": 1, "multi-agent": 2,
    "coding-agent": 2, "autonomous": 1, "worktree": 1,
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


class RateLimited(Exception):
    def __init__(self, reset_ts):
        self.reset_ts = reset_ts
        super().__init__(f"rate limited until {reset_ts}")


class GH:
    def __init__(self, token=None):
        self.token = token
        self.calls = 0

    def get(self, path, params=None):
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "gh-digest/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self.calls += 1
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            reset = e.headers.get("X-RateLimit-Reset")
            if e.code == 403 and reset:
                raise RateLimited(int(reset))
            body = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"GitHub {e.code} on {path}: {body}")

    def rate_remaining(self):
        try:
            d = self.get("/rate_limit")
            return d["resources"]["core"]["remaining"]
        except Exception:
            return None


def age_days(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max(0.1, (datetime.now(timezone.utc) - dt).days
                   + (datetime.now(timezone.utc) - dt).seconds / 86400)
    except (ValueError, TypeError):
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
    return rating is not None and rating >= 7 and velocity >= 50


def run_cmd(cmd, timeout=300, input_text=None):
    """Run a command. Returns (rc, stdout, stderr); rc=-1 on timeout/error."""
    try:
        p = subprocess.run(cmd, input=input_text, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return -1, "", f"command not found: {e.filename}"
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"


def format_repo_for_rating(repo, velocity):
    topics = ", ".join(repo.get("topics") or []) or "—"
    line = (f"{repo['full_name']} — {repo.get('language') or '—'} — "
            f"★{repo.get('stargazers_count', 0)} ({velocity}/day) — "
            f"Topics: {topics} — "
            f"Description: {repo.get('description') or '(none)'}")
    excerpt = repo.get("_readme_excerpt") or ""
    if excerpt:
        line += f" — README excerpt: {excerpt}"
    return line


def parse_batch_ratings(text):
    """Parse the summarizer's JSON object:
    {full_name: {"rating": 1-10, "reason": "...", "verdict": "..."}}.
    Returns {} on any failure — the caller falls back per repo."""
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


def rate_repos_batch(repos, cfg):
    """One summarizer call rating all candidate repos for RELEVANCE
    against Jordan's rubric (prompts/rate-repos.md).

    Returns {full_name: {"rating", "reason", "verdict"}}. An empty
    dict means the summarizer is unavailable — callers fall back to
    the keyword heuristic per repo, never a fake score.
    """
    if cfg.get("_no_summarize"):
        return {}
    prompt_file = cfg.get("_rate_prompt_file")
    if not prompt_file or not prompt_file.exists():
        log(f"  rating prompt missing: {prompt_file}")
        return {}
    listing = "\n".join(
        f"{i}. {format_repo_for_rating(r, velocity_of(r))}"
        for i, r in enumerate(repos, 1))
    prompt = (prompt_file.read_text()
              + "\n\nRepos to rate (one per line):\n" + listing + "\n")
    rc, out, err = run_cmd(cfg["_summarizer_cmd"] + [prompt], timeout=600)
    if rc == -1 and "command not found" in err:
        log(f"  summarizer not found: {cfg['_summarizer_cmd'][0]} "
            f"(ratings fall back to the keyword heuristic)")
        return {}
    if rc != 0 or not out.strip():
        log(f"  summarizer failed: {err.strip()[:200]} "
            f"(ratings fall back to the keyword heuristic)")
        return {}
    ratings = parse_batch_ratings(out)
    if not ratings:
        log("  summarizer output did not parse as JSON "
            "(ratings fall back to the keyword heuristic)")
    return ratings


def fetch_readme_excerpt(gh, full_name, limit=400):
    """First ~400 chars of the README, for the rating prompt.

    Only when a token is present: 1 extra API call per repo, too
    expensive on the unauthenticated 60/hr budget. Opt-in via
    config `readme_excerpt: true`. Never pasted into notes.
    """
    if not gh.token:
        return ""
    try:
        data = gh.get(f"/repos/{full_name}/readme")
        raw = base64.b64decode(data.get("content", "")).decode(
            "utf-8", "replace")
        return re.sub(r"\s+", " ", raw).strip()[:limit]
    except Exception:
        return ""


def write_note(outdir, item):
    """item: dict with title/author/url/published/kind/rating/reason/diamond/
    stars/body_lines (list of markdown lines)."""
    title = item["title"].replace('"', "'")
    fm = [
        "---",
        f"title: \"{title}\"",
        f"author: \"{item['author']}\"",
        f"url: {item['url']}",
        f"published: {item['published']}",
        f"stars: {item.get('stars', '')}",
        "rating: " + (f"{item['rating']:g}"
                      if isinstance(item.get("rating"), (int, float))
                      else ""),
        "verdict: ",
        "tags: []",
        "source: github",
        f"kind: {item['kind']}",
        f"rating_basis: {item.get('rating_basis', 'heuristic')}",
        f"diamond: {str(item.get('diamond', False)).lower()}",
        "---",
    ]
    lines = fm + ["", f"# {title}", "",
                  f"{item['author']} · {item['published']} · "
                  f"★ {item.get('stars', '')}",
                  f"[Open on GitHub]({item['url']})", ""]
    lines += item["body_lines"]
    lines.append("")
    basis = item.get("rating_basis", "heuristic")
    lines.append("## Rating (LLM relevance)" if basis == "llm"
                 else "## Rating (heuristic)")
    lines.append("")
    if isinstance(item.get("rating"), (int, float)):
        lines.append(f"{item['rating']:g}/10 — {item['reason']}")
    else:
        lines.append(f"Not rated — {item['reason']}")
    lines.append("")
    note_path = outdir / f"{item['key']}.md"
    note_path.write_text("\n".join(lines))
    return note_path


def rising_feed(gh, cfg, state, outdir):
    """One search per configured topic (GitHub ANDs multiple topic:
    qualifiers, so separate queries are the only way to OR them).
    Candidates are rated for RELEVANCE in one batched summarizer call;
    star velocity is a signal and a tiebreaker only, never the score."""
    since = (date.today() - timedelta(days=int(cfg.get("rising_days", 7)))
             ).isoformat()
    topics = cfg.get("topics") or []
    queries = [f"created:>{since} topic:{t}" for t in topics] or \
        [f"created:>{since}"]
    by_name = {}
    for q in queries:
        data = gh.get("/search/repositories",
                      {"q": q, "sort": "stars", "order": "desc",
                       "per_page": str(cfg.get("rising_per_page", 30))})
        for r in data.get("items", []):
            by_name.setdefault(r["full_name"], r)
    repos = [r for r in by_name.values()
             if not r.get("fork") and r.get("stargazers_count", 0)
             >= int(cfg.get("min_stars", 5))]
    # Rating pool: fastest growers plus strongest topic matches, so a
    # relevant-but-slow repo is not crowded out before it is rated.
    top_n = int(cfg.get("rising_top", 10))
    by_vel = sorted(repos, key=velocity_of, reverse=True)
    by_topic = sorted(repos, key=topic_score, reverse=True)
    rate_pool, seen = [], set()
    for r in by_vel[:20] + by_topic[:5]:
        if r["full_name"] not in seen:
            seen.add(r["full_name"])
            rate_pool.append(r)
    if cfg.get("readme_excerpt"):
        for r in rate_pool:
            r["_readme_excerpt"] = fetch_readme_excerpt(gh, r["full_name"])
    llm_ratings = rate_repos_batch(rate_pool, cfg)
    scored = []
    for r in rate_pool:
        velocity = velocity_of(r)
        name = r["full_name"]
        if name in llm_ratings:
            rating = llm_ratings[name]["rating"]
            reason = llm_ratings[name]["reason"]
            basis = "llm"
            status = "noted"
        else:
            # No summarizer: leave the repo UNRATED. A keyword heuristic
            # is not a relevance score and must never be ranked next to
            # LLM scores — the topic match is shown as a signal instead.
            # The entry is re-rated on the next run that has a summarizer.
            rating = None
            reason = (f"not rated — summarizer unavailable "
                      f"(keyword match {topic_score(r)}/3)")
            basis = "heuristic"
            status = "unrated"
        scored.append({"rating": rating, "velocity": velocity,
                       "topic": topic_score(r),
                       "diamond": is_diamond(rating, velocity),
                       "repo": r, "reason": reason, "basis": basis,
                       "status": status})
    # Rated first by relevance, then velocity; unrated last by topic
    # match, then velocity. Diamonds never outrank a higher relevance.
    scored.sort(key=lambda s: (s["rating"] is None,
                               -(s["rating"] or 0),
                               -s["topic"], -s["velocity"]))
    # Eligible = not already LLM-rated. Filter BEFORE slicing: otherwise
    # already-rated repos occupy top slots and repos below the cut are
    # never reached on summarizer-less runs. On a run WITH a summarizer,
    # unrated entries stay eligible so they get upgraded to LLM ratings;
    # on a run WITHOUT one, already-unrated entries are skipped so each
    # run notes the next batch instead of rewriting the same notes.
    have_llm = bool(llm_ratings)
    eligible = []
    for s in scored:
        st = state["items"].get(
            "rising-" + s["repo"]["full_name"].replace("/", "-"),
            {}).get("status")
        if st == "noted":
            continue
        if not have_llm and st == "unrated":
            continue
        eligible.append(s)
    top = eligible[:top_n]
    top_names = {s["repo"]["full_name"] for s in top}
    for d in [s for s in eligible
              if s["diamond"]
              and s["repo"]["full_name"] not in top_names][:3]:
        non_diamond = [i for i, s in enumerate(top) if not s["diamond"]]
        if not non_diamond:
            break
        top[non_diamond[-1]] = d
    top.sort(key=lambda s: (s["rating"] is None,
                             -(s["rating"] or 0),
                             -s["topic"], -s["velocity"]))
    new = 0
    for s in top:
        r = s["repo"]
        velocity = s["velocity"]
        key = "rising-" + r["full_name"].replace("/", "-")
        prev = state["items"].get(key)
        if prev and prev.get("status") == "noted":
            continue  # LLM-rated already; "unrated" entries get upgraded
        reason = s["reason"]
        if s["diamond"]:
            reason += " — diamond in the rough: relevant and fast-growing"
        body = []
        if r.get("description"):
            body += ["## About", "", r["description"], ""]
        body += ["## Signals", "",
                 f"- Stars: {r.get('stargazers_count', 0)} "
                 f"({velocity}/day over "
                 f"{age_days(r.get('created_at')):.1f} days)",
                 f"- Language: {r.get('language') or '—'}",
                 f"- Topics: {', '.join(r.get('topics') or []) or '—'}",
                 f"- Created: {r.get('created_at', '')[:10]}",
                 ""]
        note = write_note(outdir, {
            "key": key, "title": r["full_name"], "author": r["owner"]["login"],
            "url": r["html_url"], "published": r.get("created_at", "")[:10],
            "stars": r.get("stargazers_count", 0), "rating": s["rating"],
            "reason": reason, "diamond": s["diamond"], "kind": "rising-repo",
            "rating_basis": s["basis"], "body_lines": body})
        state["items"][key] = {
            "title": r["full_name"], "author": r["owner"]["login"],
            "url": r["html_url"], "published": r.get("created_at", "")[:10],
            "stars": r.get("stargazers_count", 0), "rating": s["rating"],
            "velocity": velocity, "rating_basis": s["basis"],
            "diamond": s["diamond"], "kind": "rising-repo",
            "status": s["status"],
            "note": str(note), "processed_at": date.today().isoformat()}
        new += 1
    save_json(cfg["_state_path"], state)
    llm_n = sum(1 for s in scored if s["basis"] == "llm")
    log(f"  rising: {len(repos)} candidates, {len(rate_pool)} rated "
        f"({llm_n} by LLM, {len(rate_pool) - llm_n} unrated), "
        f"{new} new notes")
    return new


def watched_feed(gh, cfg, state, outdir, accounts):
    new = 0
    cutoff_repos = date.today() - timedelta(
        days=int(cfg.get("watched_lookback_days", 30)))
    cutoff_rel = date.today() - timedelta(days=7)
    for user in accounts:
        try:
            repos = gh.get(f"/users/{user}/repos",
                           {"sort": "created", "per_page": "10"})
        except RuntimeError as e:
            log(f"  ERROR repos @{user}: {e}")
            continue
        for r in repos:
            created = (r.get("created_at") or "")[:10]
            if not created or date.fromisoformat(created) < cutoff_repos:
                continue
            key = "newrepo-" + r["full_name"].replace("/", "-")
            if key in state["items"]:
                continue
            rating = min(6 + topic_score(r), 10)
            body = (["## About", "", r.get("description") or "_No description._", "",
                     "## Signals", "",
                     f"- Stars: {r.get('stargazers_count', 0)}",
                     f"- Language: {r.get('language') or '—'}",
                     f"- Created: {created}", ""])
            note = write_note(outdir, {
                "key": key, "title": f"New repo: {r['full_name']}",
                "author": user, "url": r["html_url"], "published": created,
                "stars": r.get("stargazers_count", 0), "rating": rating,
                "reason": "new repo from a watched account"
                          + (" with a strong topic match"
                             if topic_score(r) >= 2 else ""),
                "diamond": topic_score(r) >= 2
                           and r.get("stargazers_count", 0) < 200,
                "kind": "new-repo", "body_lines": body})
            state["items"][key] = {
                "title": f"New repo: {r['full_name']}", "author": user,
                "url": r["html_url"], "published": created,
                "stars": r.get("stargazers_count", 0), "rating": rating,
                "kind": "new-repo", "status": "noted", "note": str(note),
                "processed_at": date.today().isoformat()}
            new += 1
        try:
            events = gh.get(f"/users/{user}/events", {"per_page": "20"})
        except RuntimeError as e:
            log(f"  ERROR events @{user}: {e}")
            continue
        for ev in events:
            if ev.get("type") != "ReleaseEvent":
                continue
            created = (ev.get("created_at") or "")[:10]
            if not created or date.fromisoformat(created) < cutoff_rel:
                continue
            repo = (ev.get("repo") or {}).get("name", "")
            tag = ((ev.get("payload") or {}).get("release") or {}).get(
                "tag_name", "")
            rel = (ev.get("payload") or {}).get("release") or {}
            key = f"release-{repo.replace('/', '-')}-{tag}"
            if key in state["items"]:
                continue
            body = ["## Release", "",
                    f"- Repo: {repo}", f"- Tag: {tag}",
                    f"- Release: {rel.get('name') or tag}", ""]
            if rel.get("body"):
                body += [rel["body"][:800], ""]
            note = write_note(outdir, {
                "key": key, "title": f"Release: {repo} {tag}",
                "author": user, "url": rel.get("html_url")
                or f"https://github.com/{repo}/releases/tag/{tag}",
                "published": created, "stars": "",
                "rating": 6, "reason": "new release from a watched account",
                "diamond": False, "kind": "release", "body_lines": body})
            state["items"][key] = {
                "title": f"Release: {repo} {tag}", "author": user,
                "url": rel.get("html_url")
                or f"https://github.com/{repo}/releases/tag/{tag}",
                "published": created, "rating": 6, "kind": "release",
                "status": "noted", "note": str(note),
                "processed_at": date.today().isoformat()}
            new += 1
        save_json(cfg["_state_path"], state)
        log(f"  @{user}: watched feed scanned")
    return new


class _FixtureGH:
    """Test double: serves repos/events from a fixture file, no network."""

    def __init__(self, data):
        self.data = data
        self.calls = 0

    def get(self, path, params=None):
        self.calls += 1
        if path.endswith("/repos"):
            return self.data.get("repos", [])
        if path.endswith("/events"):
            return self.data.get("events", [])
        return {}


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="gh_digest.py",
        description="Rising GitHub repos + watched accounts -> notes.")
    p.add_argument("--config", default=str(SCRIPT_DIR / "gh_config.json"))
    p.add_argument("--rising-only", action="store_true")
    p.add_argument("--watched-only", action="store_true")
    p.add_argument("--watch", help="comma-separated accounts (overrides config)")
    p.add_argument("--fixture",
                   help="test mode: JSON file with {repos, events} for one "
                        "account instead of the live API")
    p.add_argument("--no-summarize", action="store_true",
                   help="skip the summarizer; ratings fall back to the "
                        "keyword heuristic (max 6, never a diamond)")
    p.add_argument("--summarizer-cmd",
                   help="summarizer command, e.g. 'claude -p'")
    p.add_argument("--rerate", action="store_true",
                   help="forget today's rising items and re-rate them")
    p.add_argument("--output-dir",
                   default=str(Path.home() / "workspace/yt-digest/gh-notes"))
    args = p.parse_args(argv)

    cfg = load_json(args.config, {})
    summ = cfg.get("summarizer", {}) or {}
    cmd = (args.summarizer_cmd or summ.get("command") or ["claude", "-p"])
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    cfg["_summarizer_cmd"] = cmd
    prompt_file = summ.get("prompt_file", "prompts/rate-repos.md")
    cfg["_rate_prompt_file"] = (SCRIPT_DIR / prompt_file
                                if not Path(prompt_file).is_absolute()
                                else Path(prompt_file))
    cfg["_no_summarize"] = bool(args.no_summarize)
    cfg["_state_path"] = Path(args.output_dir).parent / "gh-state.json"
    outdir = Path(os.path.expanduser(args.output_dir))
    outdir.mkdir(parents=True, exist_ok=True)
    state = load_json(cfg["_state_path"], {"items": {}})
    if args.rerate:
        today_s = date.today().isoformat()
        dropped = [k for k, v in state["items"].items()
                   if v.get("kind") == "rising-repo"
                   and v.get("processed_at") == today_s]
        for k in dropped:
            old = state["items"][k]
            del state["items"][k]
            note = old.get("note")
            if note:
                try:
                    Path(note).unlink(missing_ok=True)
                except OSError:
                    pass
        save_json(cfg["_state_path"], state)
        log(f"rerate: forgot {len(dropped)} rising item(s) from today")
    gh = GH(token=os.environ.get("GITHUB_TOKEN"))

    if args.fixture:
        data = load_json(args.fixture, {})
        user = data.get("user", "fixture-user")
        fake = _FixtureGH(data)
        total = watched_feed(fake, cfg, state, outdir, [user])
        log(f"fixture: {total} new note(s)")
        return 0

    try:
        total = 0
        if not args.watched_only:
            total += rising_feed(gh, cfg, state, outdir)
        if not args.rising_only:
            accounts = ([a.strip() for a in args.watch.split(",") if a.strip()]
                        if args.watch else cfg.get("watched_accounts", []))
            if accounts:
                total += watched_feed(gh, cfg, state, outdir, accounts)
            else:
                log("  no watched accounts configured (see gh_config.json)")
        log(f"done: {total} new note(s); API calls this run: {gh.calls}")
        return 0
    except RateLimited as e:
        reset = datetime.fromtimestamp(e.reset_ts, tz=timezone.utc)
        state["rate_limited_until"] = reset.isoformat()
        save_json(cfg["_state_path"], state)
        log(f"RATE LIMITED: GitHub core quota spent; resets {reset:%H:%M} UTC. "
            f"Partial progress saved. Set GITHUB_TOKEN to avoid this.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
