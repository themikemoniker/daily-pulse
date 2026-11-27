"""End to end: `daily-pulse run`, `digest`, `site` and `publish` on recorded fixtures for all four
sources. yt-dlp output and GitHub HTTP are replayed from fixtures (--fixtures), the Instagram CLI is
replaced by fixture items, and `claude` is a fake executable. Nothing touches the network."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import rating_reply
from daily_pulse.cli import main

DATE = "2026-01-15"
FIXTURE_VTT = Path(__file__).parent / "fixtures" / "youtube" / "manual.en.vtt"
VIDEOS = {"vid1": "Maker-checker loops in practice", "vid2": "Building agent harnesses"}


def reply(rating=8, **over):
    return rating_reply(rating, takeaways=[f"Takeaway {i} in my own words." for i in range(3)],
                        beats=[{"timestamp": "00:01", "text": "Opens with the problem."}], **over)


KEYED_CLAUDE = '''#!{python}
import json, pathlib, sys
d = pathlib.Path({dir!r})
prompt = sys.stdin.read()
log = d / "prompts.jsonl"
with log.open("a") as fh:
    fh.write(json.dumps(prompt) + "\\n")
for needle, answer in json.loads((d / "rules.json").read_text()):
    if needle in prompt:
        if answer is None:
            sys.exit(1)
        sys.stdout.write(answer)
        sys.exit(0)
sys.exit(1)
'''


class KeyedClaude:
    """A fake `claude` that answers by what the prompt contains: the first rule whose needle is in
    the prompt wins (an answer of None makes the call fail). Records every prompt."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.dir = directory
        self.path = directory / "keyed-claude"
        self.path.write_text(KEYED_CLAUDE.format(python=sys.executable, dir=str(directory)))
        self.path.chmod(0o755)
        self.rules([])

    def rules(self, rules):
        (self.dir / "rules.json").write_text(json.dumps(rules))

    @property
    def calls(self) -> int:
        return len(self.prompts)

    @property
    def prompts(self) -> list[str]:
        log = self.dir / "prompts.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def prompts_for(self, needle: str) -> list[str]:
        return [p for p in self.prompts if needle in p.split("# Item", 1)[-1]]


@pytest.fixture
def pipeline(tmp_path, fixtures_dir, monkeypatch):
    yt = fixtures_dir / "youtube"
    (yt / "playlist.json").write_text(json.dumps(
        {"entries": [{"id": v, "title": t, "channel": "Chan", "duration": 900} for v, t in VIDEOS.items()]}))
    for v, t in VIDEOS.items():
        (yt / f"{v}.info.json").write_text(json.dumps(
            {"id": v, "title": t, "channel": "Chan", "duration": 900, "upload_date": "20260112",
             "view_count": 1000, "description": "A talk about loops."}))
        (yt / f"{v}.en.vtt").write_text(FIXTURE_VTT.read_text())
    claude = KeyedClaude(tmp_path / "claude-dir")
    claude.rules([["", reply()]])  # every call gets this reply, until a test adds rules before it
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
        if not kw.get("rerun_only_build"):
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


def long_caption(words=60):
    return " ".join(f"word{i}" for i in range(words)) + " zebracrossing"


def test_the_rater_sees_what_makes_a_github_or_instagram_item_worth_rating(pipeline):
    (pipeline.fixtures / "instagram" / "items.json").write_text(json.dumps([
        {"shortcode": "S1", "url": "https://www.instagram.com/reel/S1/", "author": "saver",
         "caption": long_caption(), "timestamp": "2026-01-13T10:00:00Z", "source": "saved"}]))
    pipeline.go()
    (github,) = pipeline.claude.prompts_for('"id": "rising:newrepo/agent-evals"')
    for fact in ("Tiny eval harness", "evals", '"stars": 300', '"age_days"', '"star_velocity"', "2026-01-12"):
        assert fact in github
    (release,) = pipeline.claude.prompts_for('"id": "release:alice/proj:v1.2.0"')
    assert "Maker-checker loops" in release and "eval runner" in release  # release name and body excerpt
    (reel,) = pipeline.claude.prompts_for('"id": "S1"')
    assert "zebracrossing" in reel  # the rater reads the whole caption, not just the short excerpt


def test_a_note_never_pastes_the_instagram_caption(pipeline):
    caption = long_caption()
    (pipeline.fixtures / "instagram" / "items.json").write_text(json.dumps([
        {"shortcode": "S1", "url": "https://www.instagram.com/reel/S1/", "author": "saver",
         "caption": caption, "timestamp": "2026-01-13T10:00:00Z", "source": "saved"}]))
    pipeline.claude.rules([['"id": "S1"', reply(summary=caption)], ["", reply()]])  # the rater copies it
    pipeline.go()
    note = (pipeline.out / "notes" / DATE / "instagram-S1.md").read_text()
    assert "zebracrossing" not in note
    assert "word30 word31 word32 word33 word34 word35" not in note


def processed(pipeline):
    path = pipeline.data / "state" / "processed.json"
    state = json.loads(path.read_text()) if path.exists() else {}  # absent until something is processed
    return {name: set(ids) for name, ids in state.items()}


def noted(pipeline, source):
    """Ids of the source's items that have a note (the note's frontmatter carries the id)."""
    ids = set()
    for path in (pipeline.out / "notes").rglob(f"{source}-*.md"):
        front = path.read_text().split("\n---\n", 1)[0]
        ids |= {json.loads(line.split(": ", 1)[1]) for line in front.splitlines()
                if line.startswith(("id: ", "video_id: "))}
    return ids


def test_an_item_that_fails_rating_is_not_lost_and_is_retried_next_run(pipeline):
    pipeline.claude.rules([['"id": "rising:other/gizmo"', None], ["", reply()]])  # claude fails for this one
    pipeline.go()
    res = pipeline.result()
    assert "rising:other/gizmo" not in processed(pipeline)["github"]
    assert "rising:other/gizmo" not in noted(pipeline, "github")
    assert "rising:other/gizmo" not in {i["id"] for i in res["sources"]["github"]["items"]}
    assert "rising:newrepo/agent-evals" in noted(pipeline, "github")  # the others are unaffected
    assert res["sources"]["instagram"]["status"] == "ok"

    pipeline.claude.rules([["", reply()]])  # claude is back
    pipeline.go()
    res = pipeline.result()
    assert [i["id"] for i in res["sources"]["github"]["items"]] == ["rising:other/gizmo"]
    assert "rising:other/gizmo" in noted(pipeline, "github")
    assert "rising:other/gizmo" in processed(pipeline)["github"]

    before = pipeline.tree(pipeline.out / "notes")
    calls = pipeline.claude.calls
    pipeline.go()  # a second successful run finds nothing new
    assert pipeline.claude.calls == calls
    for name in ("youtube", "github", "instagram"):
        assert pipeline.result()["sources"][name]["items"] == []
    assert pipeline.tree(pipeline.out / "notes") == before


def test_a_malformed_reply_twice_leaves_an_instagram_reel_unprocessed(pipeline):
    pipeline.claude.rules([['"id": "L1"', "this is not json"], ["", reply()]])
    pipeline.go()
    assert "L1" not in processed(pipeline)["instagram"] and "L1" not in noted(pipeline, "instagram")
    assert "S1" in processed(pipeline)["instagram"] and "S1" in noted(pipeline, "instagram")
    assert len(pipeline.claude.prompts_for('"id": "L1"')) == 2  # the reply is retried once, no more


def test_the_call_cap_spans_all_sources_and_unrated_items_wait_for_the_next_run(pipeline):
    cfg = pipeline.cfg
    cfg.write_text(cfg.read_text().replace("[run]", "[summarizer]\nmax_calls = 3\n\n[run]", 1))
    pipeline.go()
    assert pipeline.claude.calls == 3  # retries included, across youtube, instagram and github
    done = noted(pipeline, "youtube") | noted(pipeline, "instagram") | noted(pipeline, "github")
    assert len(done) == 3
    res = pipeline.result()
    assert all(res["sources"][n]["status"] == "ok" for n in ("youtube", "instagram", "github"))  # a cap is not a fault
    assert not any(n in res["flagged"] for n in ("youtube", "instagram", "github"))

    cfg.write_text(cfg.read_text().replace("max_calls = 3", "max_calls = 30"))
    pipeline.go()
    everything = {i["id"] for n in ("youtube", "instagram", "github") for i in pipeline.result()["sources"][n]["items"]}
    assert everything and not everything & done  # the next run rates the rest, nothing twice


def test_a_github_item_rated_9_and_under_48_hours_old_is_a_diamond(pipeline):
    pipeline.claude.rules([['"id": "rising:other/gizmo"', reply(9)], ["", reply(7)]])
    pipeline.go()
    run_file = pipeline.out / f"run-{DATE}.json"
    run = json.loads(run_file.read_text())
    run["generated_at"] = f"{DATE}T00:00:00+00:00"  # fixtures are dated January: age the digest from then
    run_file.write_text(json.dumps(run))
    pipeline.go(rerun_only_build=True)
    digest = (pipeline.out / "digests" / f"{DATE}.md").read_text()
    assert "iamond" in digest
    diamonds = digest.split("iamond", 1)[1].split("\n## ", 1)[0]
    assert "other/gizmo" in diamonds  # created a day before the run
    assert "newrepo/agent-evals" not in diamonds  # rated 7, and three days old


def test_no_item_is_marked_processed_without_a_note_or_a_logged_drop(pipeline, caplog):
    cfg = pipeline.cfg
    cfg.write_text(cfg.read_text().replace("[quality]\n", '[quality]\nchannel_blocklist = ["saver"]\n', 1))
    # One item per way a run can end: rated and noted, dropped by a rule, failing rating.
    pipeline.claude.rules([['"id": "L1"', None], ['"id": "vid2"', None], ['"id": "release:bob/tool:v0.3.0"', None],
                           ["", reply()]])
    with caplog.at_level("INFO", logger="daily_pulse"):
        pipeline.go()
    res = pipeline.result()
    for name in ("youtube", "instagram", "github"):
        source = res["sources"][name]
        dropped = {d["id"] for d in source.get("dropped", [])}
        done = processed(pipeline).get(name, set())
        assert done, name
        assert done <= noted(pipeline, name) | dropped, (name, done - noted(pipeline, name) - dropped)
        for gone in dropped:
            assert any(gone in r.getMessage() and "dropped" in r.getMessage() for r in caplog.records), gone
    assert {d["id"] for d in res["sources"]["instagram"]["dropped"]} == {"S1"}
    assert processed(pipeline)["instagram"] == {"S1"}  # L1 failed rating: still to do
    assert "vid2" not in processed(pipeline)["youtube"]
    assert "release:bob/tool:v0.3.0" not in processed(pipeline)["github"]


def test_a_dry_run_rates_nothing_and_writes_nothing(pipeline):
    assert main(["run", "--config", str(pipeline.cfg), "--date", DATE, "--out", str(pipeline.out),
                 "--fixtures", str(pipeline.fixtures), "--dry-run"]) == 0
    assert pipeline.claude.calls == 0
    assert not pipeline.out.exists() and not pipeline.data.exists()


def test_the_first_run_backlog_beyond_the_cap_is_logged_for_every_source(pipeline, caplog):
    cfg = pipeline.cfg
    cfg.write_text(cfg.read_text().replace("first_run_cap = 10", "first_run_cap = 1"))
    with caplog.at_level("INFO", logger="daily_pulse"):
        pipeline.go()
    for name, older in (("instagram", "L1"), ("youtube", "vid2")):  # gated sources and the plain path
        assert older in processed(pipeline)[name] and older not in noted(pipeline, name)
        assert any(older in r.getMessage() and name in r.getMessage() and "first run" in r.getMessage()
                   for r in caplog.records), name
    done = processed(pipeline)["github"]
    backlog = done - noted(pipeline, "github")
    assert backlog
    for gone in backlog:
        assert any(gone in r.getMessage() for r in caplog.records), gone


def test_a_failed_attempt_that_spends_the_last_call_is_a_cap_hit_not_a_source_failure(pipeline):
    cfg = pipeline.cfg
    cfg.write_text(cfg.read_text().replace("[run]", "[summarizer]\nmax_calls = 1\n\n[run]", 1)
                   .replace("[sources.youtube]\nenabled = true", "[sources.youtube]\nenabled = false")
                   .replace("[sources.github]\nenabled = true", "[sources.github]\nenabled = false"))
    pipeline.claude.rules([["", "not json"]])  # the first attempt fails; the retry would exceed the cap
    pipeline.go()
    src = pipeline.result()["sources"]["instagram"]
    assert pipeline.claude.calls == 1
    assert src["status"] == "ok" and "instagram" not in pipeline.result()["flagged"]
    assert not noted(pipeline, "instagram") and not processed(pipeline).get("instagram")  # retried next run


def test_a_creator_added_with_watch_add_reaches_the_digest_and_removing_it_stops_it(pipeline, capsys):
    yt = pipeline.fixtures / "youtube"
    channel = "https://www.youtube.com/@demochannel"
    (yt / "channels.json").write_text(json.dumps({channel: [
        {"id": "chanvid", "title": "A creator's new video", "channel": "Demo", "duration": 900}]}))
    (yt / "chanvid.info.json").write_text(json.dumps(
        {"id": "chanvid", "title": "A creator's new video", "channel": "Demo", "duration": 900,
         "upload_date": "20260112", "view_count": 1000, "description": "About loops."}))
    (yt / "chanvid.en.vtt").write_text(FIXTURE_VTT.read_text())

    assert main(["watch", "add", channel, "--config", str(pipeline.cfg)]) == 0  # nothing but the command
    pipeline.go()
    notes = {p.name for p in (pipeline.out / "notes" / DATE).glob("*.md")}
    assert "youtube-chanvid.md" in notes and {"youtube-vid1.md", "youtube-vid2.md"} <= notes
    digest = (pipeline.out / "digests" / f"{DATE}.md").read_text()
    assert "A creator's new video" in digest

    assert main(["watch", "list", "--config", str(pipeline.cfg)]) == 0
    assert "@demochannel" in capsys.readouterr().out
    assert main(["watch", "remove", channel, "--config", str(pipeline.cfg)]) == 0
    from daily_pulse.config import load_config
    assert "channels" not in load_config(pipeline.cfg).sources["youtube"] or not load_config(pipeline.cfg).sources["youtube"]["channels"]
