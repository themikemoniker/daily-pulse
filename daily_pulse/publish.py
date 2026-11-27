"""Publish the built site as the sole content of a git branch (gh-pages).

The site is copied into a throwaway repository, committed as a single orphan commit and force-pushed
with whatever git credentials the host already has: no tokens are stored or read here, and git is told
never to prompt. If the branch already holds exactly this content, nothing is committed or pushed."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

NOJEKYLL = ".nojekyll"  # without it GitHub Pages runs Jekyll, which would render the notes' .md files
GIT_TIMEOUT = 300


# What the site builder writes. Anything else in site/ is refused rather than skipped, so a stray
# state file, transcript or dotfile fails the publish loudly instead of reaching the public branch.
ALLOWED_SUFFIXES = {".html", ".css", ".md"}
RUN_INFO_DIR = "runs"  # the only place .json files may live: sanitized run statuses
SECRET_PATTERNS = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-ant-[A-Za-z0-9_-]{10,}",
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"xox[abprs]-[A-Za-z0-9-]{10,}",
)]
SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIALS?)$|^IG_CLI_CMD$")


class PublishError(Exception):
    """The site could not be published."""


def _secret_env_values() -> list[str]:
    return [v for k, v in os.environ.items() if SECRET_ENV.search(k) and len(v) >= 8]


def check_site(site: Path, forbidden: tuple[Path, ...] = ()) -> None:
    """Raise PublishError unless every file in `site` is plain site content with no secrets in it.
    `forbidden` are directories (the data dir) the site must not live in or contain."""
    real = site.resolve()
    for bad in forbidden:
        bad = bad.resolve()
        if real == bad or bad in real.parents or real in bad.parents:
            raise PublishError(f"{site} overlaps the data directory {bad}; refusing to publish it")
    secrets = _secret_env_values()
    for path in sorted(site.rglob("*")):
        rel = path.relative_to(site)
        if path.is_symlink():
            raise PublishError(f"{rel} is a symlink; refusing to publish it")
        if path.is_dir():
            if any(part.startswith(".") for part in rel.parts):
                raise PublishError(f"{rel} is not site content")
            continue
        if not path.is_file():
            raise PublishError(f"{rel} is not a regular file")
        json_ok = path.suffix == ".json" and rel.parts[0] == RUN_INFO_DIR and len(rel.parts) == 2
        if any(part.startswith(".") for part in rel.parts) or not (path.suffix in ALLOWED_SUFFIXES or json_ok):
            raise PublishError(f"{rel} is not site content")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise PublishError(f"cannot read {rel}: {exc}") from exc
        if any(p.search(text) for p in SECRET_PATTERNS) or any(v in text for v in secrets):
            raise PublishError(f"{rel} looks like a secret; refusing to publish it")


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "SSH_ASKPASS": "true"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args]
    try:
        done = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError(f"git {args[0]} failed: {exc}") from exc
    if done.returncode != 0:
        lines = [ln.strip() for ln in done.stderr.splitlines() if ln.strip() and not ln.startswith("hint:")]
        detail = re.sub(r"://[^/@\s]+@", "://***@", " ".join(lines))[:300] or "no output"  # no URL credentials
        raise PublishError(f"git {args[0]} failed: {detail}")
    return done.stdout.strip()


def publish_site(site: Path, remote: str, branch: str = "gh-pages", forbidden: tuple[Path, ...] = ()) -> bool:
    """Force-push `site` as the only content of `branch` on `remote`. Returns True if the branch was
    updated, False if it already held exactly this content."""
    site = Path(site)
    if not site.is_dir():
        raise PublishError(f"site directory {site} does not exist")
    check_site(site, forbidden)
    with tempfile.TemporaryDirectory(prefix="daily-pulse-publish-") as tmp:
        work = Path(tmp)
        shutil.copytree(site, work, dirs_exist_ok=True)
        (work / NOJEKYLL).write_text("")
        _git(work, "init", "-q")
        _git(work, "add", "-A", "--force")
        tree = _git(work, "write-tree")
        if _remote_tree(work, remote, branch) == tree:
            return False
        ident = ["-c", "user.name=daily-pulse", "-c", "user.email=daily-pulse@localhost"]
        commit = _git(work, *ident, "commit-tree", tree, "-m", "Publish site")
        _git(work, "push", "--force", remote, f"{commit}:refs/heads/{branch}")
        return True


def _remote_tree(work: Path, remote: str, branch: str) -> str | None:
    """Tree id of the remote branch, or None if the branch does not exist yet."""
    if not _git(work, "ls-remote", "--heads", remote, branch):
        return None
    _git(work, "fetch", "-q", "--depth=1", remote, f"refs/heads/{branch}")
    return _git(work, "rev-parse", "FETCH_HEAD^{tree}")
