"""End to end: `daily-pulse run`, `digest`, `site` and `publish` on recorded fixtures for all four
sources. yt-dlp output and GitHub HTTP are replayed from fixtures (--fixtures), the Instagram CLI is
replaced by fixture items, and `claude` is a fake executable. Nothing touches the network."""
import json
import subprocess
from pathlib import Path

import pytest

from daily_pulse.cli import main

DATE = "2026-01-15"
FIXTURE_VTT = Path(__file__).parent / "fixtures" / "youtube" / "manual.en.vtt"
VIDEOS = {"vid1": "Maker-checker loops in practice", "vid2": "Building agent harnesses"}


def reply(rating=8, **over):
    base = {
        "summary": "A short own-words summary of the item.",
        "takeaways": [f"Takeaway {i} in my own words." for i in range(3)],
        "beats": [{"timestamp": "00:01", "text": "Opens with the problem."}],
        "rating": rating,
        "reason": "Relevant to agent loops.",
        "verdict": "watch",
        "tags": ["agents"],
        "diamond": False,
        "diamond_reason": "",
    }
    base.update(over)
    return json.dumps(base)


@pytest.fixture
def pipeline(tmp_path, fixtures_dir, fake_claude, monkeypatch):
    yt = fixtures_dir / "youtube"
    (yt / "playlist.json").write_text(json.dumps(
        {"entries": [{"id": v, "title": t, "channel": "Chan", "duration": 900} for v, t in VIDEOS.items()]}))
    for v, t in VIDEOS.items():
        (yt / f"{v}.info.json").write_text(json.dumps(
            {"id": v, "title": t, "channel": "Chan", "duration": 900, "upload_date": "20260112",
             "view_count": 1000, "description": "A talk about loops."}))
        (yt / f"{v}.en.vtt").write_text(FIXTURE_VTT.read_text())
    claude = fake_claude(reply())  # the last reply repeats for every call
    monkeypatch.setenv("DAILY_PULSE_CLAUDE", str(claude.path))
    monkeypatch.setenv("IG_CLI_CMD", "false")  # would fail if the adapter ran it instead of the fixture
    data, out, site = tmp_path / "data", tmp_path / "out", tmp_path / "site"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[run]\nfirst_run_cap = 10\n\n[paths]\ndata_dir = "{data}"\n\n'
        '[site]\nbase_url = "https://example.test/pulse/"\n\n'
        "[quality]\nmax_triage_calls = 0\n\n"
        '[sources.youtube]\nenabled = true\nplaylist_url = "https://youtube.com/playlist?list=PL1"\n'
        'playlist_order = "newest_first"\n\n'
        "[sources.instagram]\nenabled = true\n\n"
        '[sources.x]\nenabled = true\nhandles = ["someone"]\n\n'
        '[sources.github]\nenabled = true\nrising_days = 7\nwatch_accounts = ["alice"]\n')

    class P:
        pass

    p = P()
    p.cfg, p.data, p.out, p.site, p.fixtures, p.claude = cfg, data, out, site, fixtures_dir, claude

    def go(**kw):
        base = ["--config", str(cfg), "--date", DATE]
        fx = kw.get("fixtures", fixtures_dir)
        assert main(["run", *base, "--out", str(out), "--fixtures", str(fx)]) == 0
        assert main(["digest", *base, "--out", str(out)]) == 0
        assert main(["site", "--date", DATE, "--out", str(out), "--site", str(site)]) == 0

    def tree(root):
        return {str(f.relative_to(root)): f.read_bytes() for f in sorted(root.rglob("*")) if f.is_file()}

    p.go, p.tree = go, tree
    p.result = lambda: json.loads((out / f"run-{DATE}.json").read_text())
    return p


def test_every_source_is_run_and_youtube_reaches_notes_digest_and_site(pipeline):
    pipeline.go()
    res = pipeline.result()
    assert res["sources"]["youtube"]["status"] == "ok"
    assert res["sources"]["github"]["status"] == "ok"
    assert res["sources"]["instagram"]["status"] == "ok"
    assert res["sources"]["x"]["status"].startswith("deferred")

    notes = {p.name for p in (pipeline.out / "notes" / DATE).glob("*.md")}
    assert {"youtube-vid1.md", "youtube-vid2.md"} <= notes
    # GitHub and Instagram items are rated and noted too, so all three live sources reach the digest.
    assert {i["id"] for i in res["sources"]["instagram"]["items"]} == {"S1", "L1"}
    github_ids = {i["id"] for i in res["sources"]["github"]["items"]}
    assert "rising:newrepo/agent-evals" in github_ids
    assert {"instagram-S1.md", "instagram-L1.md"} <= notes
    assert len(notes) == 2 + 2 + len(github_ids)  # every reported item has exactly one note

    digest = (pipeline.out / "digests" / f"{DATE}.md").read_text()
    assert VIDEOS["vid1"] in digest
    assert "newrepo/agent-evals" in digest
    assert "Reel by saver (S1)" in digest
    assert "deferred" in digest.lower()  # the digest says X is deferred
    assert (pipeline.out / "digests" / f"{DATE}.html").read_text().startswith(("<!", "<html", "<table", "<div"))
    index = (pipeline.site / "index.html").read_text()
    assert VIDEOS["vid2"] in index
    assert "newrepo/agent-evals" in index and "Reel by saver (S1)" in index
    assert (pipeline.site / "notes" / DATE / "youtube-vid1.html").exists()
    assert (pipeline.site / "archive" / "index.html").exists()


def test_notes_and_site_never_contain_the_transcript(pipeline):
    pipeline.go()
    transcript_line = "It reviews its own work."  # a caption line from the fixture
    scanned = [f for root in (pipeline.out, pipeline.site) for f in root.rglob("*")
               if f.is_file() and f.suffix in {".md", ".html"}]
    assert len(scanned) >= 6  # notes, digests and site pages were really checked
    for f in scanned:
        assert transcript_line not in f.read_text(), f
    assert not list(pipeline.site.rglob("transcripts"))
    assert (pipeline.data / "transcripts").exists()  # kept locally only


def test_a_second_run_finds_nothing_new_and_changes_no_output(pipeline):
    pipeline.go()
    calls = pipeline.claude.calls
    before = {"out": pipeline.tree(pipeline.out / "notes"), "digests": pipeline.tree(pipeline.out / "digests"),
              "site": pipeline.tree(pipeline.site)}
    pipeline.go()
    assert pipeline.claude.calls == calls  # nothing new, so nothing to summarize
    for name in ("youtube", "github", "instagram"):
        src = pipeline.result()["sources"][name]
        assert src["status"] == "ok" and src["items"] == []
    after = {"out": pipeline.tree(pipeline.out / "notes"), "digests": pipeline.tree(pipeline.out / "digests"),
             "site": pipeline.tree(pipeline.site)}
    assert after == before


def test_one_failing_source_does_not_hide_the_others(pipeline):
    (pipeline.fixtures / "github" / "responses.json").unlink()  # the GitHub adapter now raises
    pipeline.go()
    res = pipeline.result()
    assert res["sources"]["github"]["status"] == "failed"
    assert res["sources"]["youtube"]["status"] == "ok"
    digest = (pipeline.out / "digests" / f"{DATE}.md").read_text()
    assert VIDEOS["vid1"] in digest
    assert "github: failed" in digest  # the reader can see the source failed
    assert VIDEOS["vid2"] in (pipeline.site / "index.html").read_text()


def test_publish_pushes_only_the_site(pipeline, tmp_path):
    pipeline.go()
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    rc = main(["publish", "--config", str(pipeline.cfg), "--date", DATE, "--no-build",
               "--site", str(pipeline.site), "--remote", str(remote)])
    assert rc == 0
    files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "gh-pages"],
                           capture_output=True, text=True, check=True).stdout.split()
    assert "index.html" in files and ".nojekyll" in files
    assert not any(f.startswith(("data/", "transcripts/")) for f in files)
    assert main(["publish", "--config", str(pipeline.cfg), "--date", DATE, "--no-build",
                 "--site", str(pipeline.site), "--remote", str(remote)]) == 0
    log = subprocess.run(["git", "--git-dir", str(remote), "rev-list", "--count", "gh-pages"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert log == "1"  # the identical second publish made no new commit
