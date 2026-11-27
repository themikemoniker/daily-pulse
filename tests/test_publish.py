import subprocess
from pathlib import Path

import pytest

from daily_pulse.cli import main
from daily_pulse.models import Item, Rating
from daily_pulse.notes import write_note

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "--git-dir", str(repo), *args], capture_output=True, text=True,
                          check=True, env={**__import__("os").environ, **GIT_ENV})
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def remote(tmp_path) -> Path:
    """A local bare repo standing in for the GitHub remote."""
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


@pytest.fixture
def site(tmp_path) -> Path:
    s = tmp_path / "site"
    (s / "notes" / "2026-01-15").mkdir(parents=True)
    (s / "runs").mkdir()
    (s / "index.html").write_text("<html>today</html>\n")
    (s / "style.css").write_text("body{}\n")
    (s / "notes" / "2026-01-15" / "x-1.md").write_text("---\nid: 1\n---\nbody\n")
    (s / "runs" / "2026-01-15.json").write_text('{"sources": {}}\n')
    return s


@pytest.fixture
def cfg(make_env):
    return make_env(sources=())[0]


def publish(cfg, site, remote, *extra):
    return main(["publish", "--config", str(cfg), "--site", str(site), "--remote", str(remote),
                 "--no-build", *extra])


def branch_files(remote: Path, branch="gh-pages") -> list[str]:
    return git(remote, "ls-tree", "-r", "--name-only", branch).splitlines()


def test_publish_makes_branch_hold_only_site_content(cfg, site, remote):
    assert publish(cfg, site, remote) == 0
    assert branch_files(remote) == sorted([
        ".nojekyll", "index.html", "style.css", "notes/2026-01-15/x-1.md", "runs/2026-01-15.json"])
    assert git(remote, "show", "gh-pages:index.html") == "<html>today</html>"
    assert git(remote, "rev-list", "--count", "gh-pages") == "1"


def test_publishing_twice_without_changes_makes_no_new_commit(cfg, site, remote):
    assert publish(cfg, site, remote) == 0
    first = git(remote, "rev-parse", "gh-pages")
    assert publish(cfg, site, remote) == 0
    assert git(remote, "rev-parse", "gh-pages") == first
    assert git(remote, "rev-list", "--count", "gh-pages") == "1"


def test_a_changed_site_replaces_the_branch_and_drops_removed_files(cfg, site, remote):
    assert publish(cfg, site, remote) == 0
    first = git(remote, "rev-parse", "gh-pages")
    (site / "index.html").write_text("<html>tomorrow</html>\n")
    (site / "style.css").unlink()
    assert publish(cfg, site, remote) == 0
    assert git(remote, "rev-parse", "gh-pages") != first
    assert "style.css" not in branch_files(remote)
    assert git(remote, "show", "gh-pages:index.html") == "<html>tomorrow</html>"


def test_a_diverged_remote_branch_is_overwritten(cfg, site, remote, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    (other / "stray.txt").write_text("hello")
    subprocess.run(["git", "-C", str(other), "add", "."], check=True)
    subprocess.run(["git", "-C", str(other), "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "x"],
                   check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q", "--force", str(remote), "HEAD:refs/heads/gh-pages"],
                   check=True)
    assert publish(cfg, site, remote) == 0
    assert "stray.txt" not in branch_files(remote)


def test_other_branches_on_the_remote_are_untouched(cfg, site, remote, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    (other / "keep.txt").write_text("hello")
    subprocess.run(["git", "-C", str(other), "add", "."], check=True)
    subprocess.run(["git", "-C", str(other), "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "x"],
                   check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    before = git(remote, "rev-parse", "main")
    assert publish(cfg, site, remote) == 0
    assert git(remote, "rev-parse", "main") == before


def test_unreachable_remote_fails_with_one_line_and_never_prompts(cfg, site, tmp_path, capsys):
    assert publish(cfg, site, tmp_path / "nope.git") == 1
    err = capsys.readouterr().err
    assert err.startswith("daily-pulse: cannot publish")
    assert "Traceback" not in err


def test_missing_site_dir_fails(cfg, tmp_path, remote, capsys):
    assert publish(cfg, tmp_path / "absent", remote) == 1
    assert "does not exist" in capsys.readouterr().err


def assert_refused(cfg, site, remote, capsys, fragment):
    assert publish(cfg, site, remote) == 1
    assert fragment in capsys.readouterr().err
    assert git(remote, "branch", "--list") == ""  # nothing reached the remote


def test_a_site_inside_the_data_dir_is_refused(make_env, remote, capsys):
    cfg, data, _ = make_env(sources=())
    inside = data / "site"
    inside.mkdir(parents=True)
    (inside / "index.html").write_text("x")
    assert_refused(cfg, inside, remote, capsys, "data directory")


def test_a_symlink_in_the_site_is_refused(cfg, site, remote, capsys, make_env):
    secret = site.parent / "state-dir" / "processed.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"ids": []}')
    (site / "leak.html").symlink_to(secret)
    assert_refused(cfg, site, remote, capsys, "symlink")


@pytest.mark.parametrize("name", [".env", "state/processed.json", "transcripts/yt-1.txt", ".git/config",
                                  "notes/2026-01-15/x-1.json"])
def test_files_that_are_not_site_content_are_refused(cfg, site, remote, capsys, name):
    stray = site / name
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("data")
    assert_refused(cfg, site, remote, capsys, "not site content")


@pytest.mark.parametrize("secret", [
    "ghp_" + "a" * 36,
    "github_pat_" + "B" * 40,
    "sk-ant-" + "c" * 30,
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "AKIA" + "D" * 16,
])
def test_a_token_in_a_page_is_refused(cfg, site, remote, capsys, secret):
    (site / "index.html").write_text(f"<html>{secret}</html>")
    assert_refused(cfg, site, remote, capsys, "looks like a secret")
    assert secret not in capsys.readouterr().err


def test_the_value_of_a_secret_env_var_is_refused(cfg, site, remote, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "correct-horse-battery-staple")
    monkeypatch.setenv("IG_CLI_CMD", "ig-cli --session abc123def456")
    (site / "notes" / "2026-01-15" / "x-1.md").write_text("see ig-cli --session abc123def456")
    assert_refused(cfg, site, remote, capsys, "looks like a secret")


def test_building_from_an_out_dir_publishes_only_the_site(make_env, remote):
    cfg, data, out = make_env(sources=())
    item = Item(source="github", id="1", url="https://items.test/1", title="T", author="a")
    write_note(item, Rating(rating=7, reason="r", verdict="skim", tags=["x"], summary="Own words."),
               out / "notes", "2026-01-15")
    (out / "transcripts").mkdir()
    (out / "transcripts" / "yt-1.txt").write_text("FULL TRANSCRIPT")
    (out / "run-2026-01-15.json").write_text('{"generated_at": "2026-01-15T07:00:00+00:00", "sources": {}}')
    (data / "state").mkdir(parents=True)
    (data / "state" / "processed.json").write_text("{}")
    assert main(["publish", "--config", str(cfg), "--out", str(out), "--date", "2026-01-15",
                 "--remote", str(remote)]) == 0
    files = branch_files(remote)
    assert "index.html" in files and "notes/2026-01-15/github-1.html" in files and "notes/2026-01-15/github-1.md" in files
    assert not [f for f in files if "transcript" in f or "state" in f or f.endswith(".txt")]
    for f in files:
        assert "FULL TRANSCRIPT" not in git(remote, "show", f"gh-pages:{f}")


def test_a_git_failure_reports_the_whole_reason_not_just_its_last_line(cfg, site, tmp_path, capsys):
    assert publish(cfg, site, tmp_path / "nope.git") == 1
    err = capsys.readouterr().err
    assert "nope.git" in err and "does not appear to be a git repository" in err


def test_credentials_in_a_remote_url_are_not_echoed_in_errors(cfg, site, capsys):
    assert main(["publish", "--config", str(cfg), "--site", str(site), "--no-build",
                 "--remote", "ssh://user:hunter2pass@127.0.0.1:1/x.git"]) == 1
    assert "hunter2pass" not in capsys.readouterr().err
