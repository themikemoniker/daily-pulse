import json
from pathlib import Path

import pytest

from daily_pulse.adapters import registry
from daily_pulse.adapters.instagram import InstagramAdapter
from daily_pulse.adapters.youtube import YouTubeAdapter
from daily_pulse.cli import main
from daily_pulse.config import load_config
from daily_pulse.runner import run_pipeline

from test_youtube import AUTO, DATE, MANUAL, FakeYtDlp, rating_reply

SHIPPED = Path(__file__).parent.parent / "config" / "daily-pulse.toml"
TITLES = {"rice": "How to Cook Perfect Rice Every Time", "es": "Learn Spanish in 30 Days (My Method)",
          "omp": "Claude Code vs OMP: why OMP wins",
          "herdr": "Herdr Desktop Is The Most Customizable Agent Orchestrator"}


def triage_reply(*scores):
    return json.dumps([{"n": n, "score": s, "reason": f"triage reason {n}"} for n, s in enumerate(scores)])


def titled(ids, **per_id):
    return {i: {"title": TITLES.get(i, f"Title {i}"), "duration": 900, "view_count": 50, **per_id.get(i, {})}
            for i in ids}


@pytest.fixture
def env(fake_claude, monkeypatch, tmp_path):
    def build(replies, extra_cfg="", first_run_cap=10, order=""):
        data, out = tmp_path / "state-dir", tmp_path / "out"
        cfg = tmp_path / "config.toml"
        cfg.write_text(f'[run]\nfirst_run_cap = {first_run_cap}\n\n[paths]\ndata_dir = "{data}"\n\n{extra_cfg}\n'
                       '[sources.youtube]\nenabled = true\nplaylist_url = "https://youtube.com/playlist?list=PL1"\n'
                       + (order or 'playlist_order = "newest_first"\n'))
        claude = fake_claude(*replies)
        monkeypatch.setenv("DAILY_PULSE_CLAUDE", str(claude.path))

        class Env:
            pass

        e = Env()
        e.cfg, e.data, e.out, e.claude = cfg, data, out, claude
        e.go = lambda client, date=DATE, **kw: (
            registry.register(YouTubeAdapter(client=client)),
            run_pipeline(load_config(cfg), date, None, out_dir=out, **kw))[1]
        e.notes = lambda: sorted(p.name for p in (out / "notes" / DATE).glob("*.md")) if (out / "notes" / DATE).exists() else []
        e.processed = lambda: json.loads((data / "state" / "processed.json").read_text()).get("youtube", [])
        e.triage_log = lambda: [json.loads(l) for l in (data / "triage.jsonl").read_text().splitlines()]
        return e

    return build


PLAYLIST = ["rice", "es", "omp", "herdr"]
SUBS = {i: {"manual": MANUAL} for i in PLAYLIST}


def test_triaged_out_items_get_no_note_are_marked_processed_and_never_retriaged(env):
    e = env([triage_reply(0, 0, 3, 3), rating_reply()])
    res = e.go(FakeYtDlp(PLAYLIST, subs=SUBS, meta=titled(PLAYLIST)))
    assert res.sources["youtube"]["status"] == "ok"
    assert [i["id"] for i in res.sources["youtube"]["items"]] == ["omp", "herdr"]
    assert e.notes() == ["youtube-herdr.md", "youtube-omp.md"]
    assert {"rice", "es"} <= set(e.processed())
    assert [(d["id"], d["score"]) for d in res.sources["youtube"]["dropped"]] == [("rice", 0), ("es", 0)]
    calls = e.claude.calls
    client = FakeYtDlp(PLAYLIST, subs=SUBS, meta=titled(PLAYLIST))
    again = e.go(client)
    assert again.sources["youtube"]["items"] == [] and e.claude.calls == calls  # no triage, no summaries
    assert [c[0] for c in client.calls] == ["playlist"]


def test_the_triage_prompt_has_no_transcript_or_caption(env):
    e = env([triage_reply(3), rating_reply()])
    subs = {"omp": {"manual": "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nTRANSCRIPT-MARKER words\n"}}
    e.go(FakeYtDlp(["omp"], subs=subs, meta=titled(["omp"])))
    triage_prompt, summary_prompt = e.claude.stdin(0), e.claude.stdin(1)
    assert TITLES["omp"] in triage_prompt and "TRANSCRIPT-MARKER" not in triage_prompt
    assert "TRANSCRIPT-MARKER" in summary_prompt  # the summarizer does get it, after triage


def test_a_malformed_triage_reply_fails_open_and_every_item_continues(env):
    e = env(["this is not json", rating_reply()])
    res = e.go(FakeYtDlp(PLAYLIST, subs=SUBS, meta=titled(PLAYLIST)))
    assert res.sources["youtube"]["status"] == "ok"
    assert len(res.sources["youtube"]["items"]) == 4 and len(e.notes()) == 4
    assert "dropped" not in res.sources["youtube"]
    log = next((e.data / "logs").glob("*.log")) if (e.data / "logs").exists() else None
    assert log is None or "triage" in log.read_text()


def test_a_failed_triage_call_fails_open(env):
    e = env([{"exit": 1}, rating_reply()])
    res = e.go(FakeYtDlp(PLAYLIST, subs=SUBS, meta=titled(PLAYLIST)))
    assert len(res.sources["youtube"]["items"]) == 4


def test_every_decision_is_appended_to_the_local_triage_log(env):
    e = env([triage_reply(0, 1, 3, 2), rating_reply()],
            extra_cfg='[quality]\nchannel_blocklist = ["Spam"]\n')
    meta = titled(PLAYLIST + ["spam"], spam={"channel": "Spam"})
    e.go(FakeYtDlp(PLAYLIST + ["spam"], subs=SUBS, meta=meta))
    log = e.triage_log()
    by_id = {r["id"]: r for r in log}
    assert set(by_id) == {"rice", "es", "omp", "herdr", "spam"}
    assert by_id["rice"] == {"id": "rice", "source": "youtube", "title": TITLES["rice"], "score": 0,
                             "decision": "dropped", "reason": "triage reason 0", "date": DATE}
    assert by_id["herdr"]["score"] == 2 and by_id["herdr"]["decision"] == "kept"
    assert by_id["spam"]["score"] is None and by_id["spam"]["decision"] == "dropped"
    assert "blocklisted" in by_id["spam"]["reason"]
    assert oct((e.data / "triage.jsonl").stat().st_mode & 0o777) == "0o600"
    e.go(FakeYtDlp(PLAYLIST + ["spam"], meta=meta))  # a rerun adds nothing
    assert len(e.triage_log()) == 5


def test_rule_drops_cost_no_claude_call_and_a_dry_run_triages_nothing(env):
    e = env([triage_reply(3), rating_reply()], extra_cfg='[quality]\ntitle_blocklist = ["rice"]\n')
    res = e.go(FakeYtDlp(["rice"], meta=titled(["rice"])))
    assert e.claude.calls == 0 and res.sources["youtube"]["dropped"][0]["id"] == "rice"
    dry = e.go(FakeYtDlp(["omp"], meta=titled(["omp"])), dry_run=True)
    assert e.claude.calls == 0 and len(dry.sources["youtube"]["items"]) == 1
    assert not (e.data / "triage.jsonl").exists() or "omp" not in (e.data / "triage.jsonl").read_text()


def test_short_videos_are_dropped_by_the_duration_rule(env):
    e = env([triage_reply(3), rating_reply()])
    res = e.go(FakeYtDlp(["short", "omp"], subs=SUBS, meta=titled(["short", "omp"], short={"duration": 59})))
    assert [i["id"] for i in res.sources["youtube"]["items"]] == ["omp"]
    assert "short" in e.processed()


def test_the_first_run_takes_the_ten_newest_by_upload_date_that_pass_triage(env):
    # oldest-first playlist with the shipped config's explicit order; 14 videos, dated by position
    ids = [f"v{i:02d}" for i in range(14)]
    meta = titled(ids, **{i: {"upload_date": f"202601{n + 1:02d}"} for n, i in enumerate(ids)})
    # triage sees newest first: v13 v12 ... ; score the two newest 0, the rest 3
    e = env([triage_reply(0, 0, *[3] * 12), rating_reply()],
            order='playlist_order = "oldest_first"\n')
    res = e.go(FakeYtDlp(ids, subs={i: {"manual": MANUAL} for i in ids}, meta=meta))
    assert [i["id"] for i in res.sources["youtube"]["items"]] == [f"v{n:02d}" for n in range(11, 1, -1)]
    assert {"v13", "v12"} <= set(e.processed()) and len(e.notes()) == 10
    # the backlog older than the 10 is marked seen and never processed later
    later = e.go(FakeYtDlp(ids, subs={i: {"manual": MANUAL} for i in ids}, meta=meta))
    assert later.sources["youtube"]["items"] == []


def test_the_first_run_stops_triaging_once_it_has_enough_survivors(env):
    ids = [f"v{i:03d}" for i in range(120)]
    e = env([triage_reply(*[3] * 40), rating_reply()], first_run_cap=10)
    res = e.go(FakeYtDlp(ids, subs={i: {"manual": MANUAL} for i in ids}, meta=titled(ids)))
    assert len(res.sources["youtube"]["items"]) == 10
    assert e.claude.stdin(0).count('"title"') == 40
    assert e.claude.calls == 1 + 10  # one triage call, ten summaries
    assert set(ids) <= set(e.processed())  # the whole backlog is marked seen


def test_the_shipped_config_takes_the_ten_newest_from_an_oldest_first_playlist(env, tmp_path, fake_claude, monkeypatch):
    import shutil
    shipped = tmp_path / "config" / "daily-pulse.toml"
    shipped.parent.mkdir()
    shutil.copy(SHIPPED, shipped)
    ids = [f"v{i:02d}" for i in range(13)]
    meta = titled(ids, **{i: {"upload_date": f"202601{n + 1:02d}"} for n, i in enumerate(ids)})
    claude = fake_claude(triage_reply(*[3] * 13), rating_reply())
    monkeypatch.setenv("DAILY_PULSE_CLAUDE", str(claude.path))
    registry.register(YouTubeAdapter(client=FakeYtDlp(ids, subs={i: {"manual": MANUAL} for i in ids}, meta=meta)))
    res = run_pipeline(load_config(shipped), DATE, None, out_dir=tmp_path / "out")
    assert [i["id"] for i in res.sources["youtube"]["items"]] == [f"v{n:02d}" for n in range(12, 2, -1)]


def test_a_rerun_is_idempotent_with_the_gate_on(env, tmp_path):
    e = env([triage_reply(0, 0, 3, 3), rating_reply()])
    client = FakeYtDlp(PLAYLIST, subs=SUBS, meta=titled(PLAYLIST))
    first = e.go(client)
    notes = {p: p.read_text() for p in e.out.rglob("*.md")}
    again = e.go(FakeYtDlp(PLAYLIST, subs=SUBS, meta=titled(PLAYLIST)))
    assert again.sources["youtube"]["items"] == [] and {p: p.read_text() for p in e.out.rglob("*.md")} == notes
    assert len(first.sources["youtube"]["items"]) == 2


def test_instagram_items_are_triaged_but_not_held_to_the_duration_rule(fake_claude, monkeypatch, tmp_path, fixtures_dir):
    items = json.loads((fixtures_dir / "instagram" / "items.json").read_text())
    n = len(items)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndata_dir = "{tmp_path / "data"}"\n\n[sources.instagram]\nenabled = true\n')
    claude = fake_claude(triage_reply(*([0] + [3] * (n - 1))), rating_reply(summary="A reel summary."))
    monkeypatch.setenv("DAILY_PULSE_CLAUDE", str(claude.path))
    registry.register(InstagramAdapter())
    res = run_pipeline(load_config(cfg), DATE, fixtures_dir, out_dir=tmp_path / "out")
    src = res.sources["instagram"]
    assert len(src["dropped"]) == 1 and len(src["items"]) == n - 1
    assert "under the minimum" not in json.dumps(src)


def test_github_items_are_never_triaged(fake_claude, monkeypatch, make_env, fixtures_dir):
    from conftest import FakeAdapter
    claude = fake_claude(triage_reply(0))
    monkeypatch.setenv("DAILY_PULSE_CLAUDE", str(claude.path))
    registry.register(FakeAdapter())
    cfg, data, out = make_env()
    assert main(["run", "--config", str(cfg), "--fixtures", str(fixtures_dir), "--out", str(out), "--date", DATE]) == 0
    assert claude.calls == 0


def test_the_triage_log_is_local_only_and_never_reaches_the_site(env, tmp_path):
    from daily_pulse.site import build_site
    e = env([triage_reply(0, 0, 3, 3), rating_reply()])
    e.go(FakeYtDlp(PLAYLIST, subs=SUBS, meta=titled(PLAYLIST)))
    log = e.data / "triage.jsonl"
    assert log.is_file() and "triage reason 0" in log.read_text()
    # even when a stray copy sits where the builder reads from, nothing of it is published
    (e.out / "triage.jsonl").write_text(log.read_text())
    (e.out / "notes" / DATE / "triage.jsonl").write_text(log.read_text())
    (e.out / "digests").mkdir()
    (e.out / "digests" / "triage.jsonl").write_text(log.read_text())
    site = tmp_path / "site"
    build_site(e.out, site, DATE)
    published = [p for p in site.rglob("*") if p.is_file()]
    assert published and not [p for p in published if "triage" in p.name]
    assert not [p for p in published if "triage reason" in p.read_text(errors="ignore")]
    assert not [p for p in published if TITLES["rice"] in p.read_text(errors="ignore")]  # triaged-out titles too


def test_the_triage_log_is_gitignored():
    import shutil
    import subprocess
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = Path(__file__).parent.parent
    proc = subprocess.run(["git", "check-ignore", "-q", "data/triage.jsonl"], cwd=root)
    assert proc.returncode == 0


def test_a_gate_that_breaks_never_costs_the_run(env, monkeypatch):
    from daily_pulse import quality

    def boom(self, *a, **k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(quality.QualityGate, "screen", boom)
    e = env([rating_reply()], first_run_cap=2)
    res = e.go(FakeYtDlp(PLAYLIST, subs=SUBS, meta=titled(PLAYLIST)))
    assert res.sources["youtube"]["status"] == "ok"
    assert [i["id"] for i in res.sources["youtube"]["items"]] == PLAYLIST[:2]  # the first-run cap still holds
    assert set(PLAYLIST) <= set(e.processed())
