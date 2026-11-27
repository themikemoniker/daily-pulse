import json
import re
import sys
from pathlib import Path

import pytest

from daily_pulse.adapters import registry
from daily_pulse.adapters.youtube import (FixtureYtDlp, ProcessYtDlp, YouTubeAdapter, YtDlpError,
                                          clean_vtt, make_client)
from daily_pulse.config import load_config
from daily_pulse.runner import run_pipeline

YT = Path(__file__).parent / "fixtures" / "youtube"


def test_rolling_auto_captions_lose_duplicates_and_markup_but_keep_timestamps():
    cleaned = clean_vtt((YT / "rolling-auto.en.vtt").read_text())
    assert cleaned.splitlines() == [
        "[00:00] welcome to the harness talk",
        "[00:03] today we look at evals",
        "[00:06] a checker & maker loop > single agent",
        "[01:05] routing cheap models",
        "[01:05] [Music]",
    ]


def test_manual_subtitles_skip_headers_notes_and_styles_and_strip_inline_tags():
    cleaned = clean_vtt((YT / "manual.en.vtt").read_text())
    assert cleaned.splitlines() == [
        "[00:01] Hello and welcome.",
        "[00:04] We build a maker-checker loop.",
        "[00:04] It reviews its own work.",
    ]


def test_timestamps_past_an_hour_carry_hours():
    vtt = "WEBVTT\n\n01:02:03.000 --> 01:02:05.000\nlate remark\n"
    assert clean_vtt(vtt) == "[1:02:03] late remark"


def test_a_cue_with_no_text_or_a_file_with_no_cues_cleans_to_empty():
    assert clean_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n \n") == ""
    assert clean_vtt("") == ""


# --- the adapter, driven through the pipeline runner with a fake yt-dlp and a fake claude ---




DATE = "2026-01-15"
MANUAL = (YT / "manual.en.vtt").read_text()
AUTO = (YT / "rolling-auto.en.vtt").read_text()


def rating_reply(**over):
    reply = {
        "summary": "",
        "takeaways": [f"Takeaway {i} in my own words." for i in range(5)],
        "beats": [{"timestamp": "00:01", "text": "Opens with the problem."}],
        "rating": 8,
        "reason": "A clear treatment of maker-checker loops.",
        "verdict": "watch",
        "tags": ["maker-checker"],
        "diamond": False,
        "diamond_reason": "",
    }
    reply.update(over)
    return reply


class FakeYtDlp:
    """Stands in for yt-dlp: canned playlist, per-video info and captions. Records every call."""

    def __init__(self, ids=(), subs=None, fail_info=(), fail_playlist=False, fail_message=None,
                 upload_date="20260112", live_status=None, fail_all=None, meta=None):
        self.meta = meta or {}  # id -> fields for both the playlist entry and the video info
        self.fail_message = fail_message
        self.upload_date = upload_date
        self.live_status = live_status
        self.fail_all = fail_all
        self.ids = list(ids)
        self.subs = subs or {}  # id -> {"manual": vtt, "auto": vtt}
        self.fail_info = set(fail_info)
        self.fail_playlist = fail_playlist
        self.calls: list[tuple] = []

    def playlist(self, url):
        self.calls.append(("playlist", url))
        if self.fail_playlist:
            raise YtDlpError("playlist unavailable")
        return [{"id": i, "title": f"Title {i}", "channel": "Chan", **self.meta.get(i, {})} for i in self.ids]

    def info(self, url):
        vid = url.rsplit("=", 1)[-1]
        self.calls.append(("info", vid))
        if self.fail_all:
            raise YtDlpError(self.fail_all)
        if vid in self.fail_info:
            raise YtDlpError(self.fail_message or f"cannot fetch {vid}")
        return {"id": vid, "title": f"Title {vid}", "channel": "Chan", "duration": 754,
                "upload_date": self.upload_date, "view_count": 4321, "live_status": self.live_status,
                "description": "A talk about loops.\nSecond line.", **self.meta.get(vid, {})}

    def subtitles(self, url, auto, dest):
        vid = url.rsplit("=", 1)[-1]
        self.calls.append(("subs", vid, "auto" if auto else "manual"))
        text = self.subs.get(vid, {}).get("auto" if auto else "manual")
        if text is None:
            return None
        path = dest / f"{vid}.en.vtt"
        path.write_text(text)
        return path


@pytest.fixture
def env(fake_claude, monkeypatch, tmp_path):
    """A configured youtube source + fake claude; `go(client)` runs the pipeline once."""

    def build(first_run_cap=10, claude_replies=None):
        data, out = tmp_path / "state-dir", tmp_path / "out"
        cfg = tmp_path / "config.toml"
        cfg.write_text(f'[run]\nfirst_run_cap = {first_run_cap}\n\n[paths]\ndata_dir = "{data}"\n\n'
                       '[quality]\nmax_triage_calls = 0  # not about triage here: no triage calls\n\n'
                       '[sources.youtube]\nenabled = true\nplaylist_url = "https://youtube.com/playlist?list=PL1"\n'
                       'playlist_order = "newest_first"\n')
        claude = fake_claude(*(claude_replies or [rating_reply()]))
        monkeypatch.setenv("DAILY_PULSE_CLAUDE", str(claude.path))

        class Env:
            pass

        e = Env()
        e.cfg, e.data, e.out, e.claude = cfg, data, out, claude

        def go(client, date=DATE):
            registry.register(YouTubeAdapter(client=client))
            return run_pipeline(load_config(cfg), date, None, out_dir=out)

        e.go = go
        e.notes = lambda: sorted((out / "notes" / DATE).glob("*.md")) if (out / "notes" / DATE).exists() else []
        return e

    return build


def test_a_new_video_becomes_one_note_with_its_metadata(env):
    e = env()
    result = e.go(FakeYtDlp(["abc123"], subs={"abc123": {"manual": MANUAL}}))
    assert result.sources["youtube"]["status"] == "ok"
    [note] = e.notes()
    assert note.name == "youtube-abc123.md"
    text = note.read_text()
    assert 'title: "Title abc123"' in text
    assert 'channel: "Chan"' in text
    assert 'published: "2026-01-12"' in text
    assert 'duration: "12:34"' in text
    assert "views: 4321" in text
    assert "## The argument, beat by beat" in text
    assert "- [00:01] Opens with the problem." in text


def stdin_of(e, n=0):
    return e.claude.stdin(n)


def test_manual_subtitles_win_over_auto_captions(env):
    e = env()
    client = FakeYtDlp(["v1"], subs={"v1": {"manual": MANUAL, "auto": AUTO}})
    e.go(client)
    assert ("subs", "v1", "auto") not in client.calls
    sent = stdin_of(e)
    assert "maker-checker loop" in sent  # manual text reached the rater
    assert "harness talk" not in sent  # auto text did not
    assert (e.data / "transcripts" / "youtube-v1.txt").read_text().startswith("[00:01] Hello and welcome.")


def test_auto_captions_are_the_fallback_when_there_are_no_manual_subtitles(env):
    e = env()
    client = FakeYtDlp(["v1"], subs={"v1": {"auto": AUTO}})
    e.go(client)
    assert [c for c in client.calls if c[0] == "subs"] == [("subs", "v1", "manual"), ("subs", "v1", "auto")]
    assert "[00:03] today we look at evals" in stdin_of(e)
    assert "[00:03] today we look at evals" in (e.data / "transcripts" / "youtube-v1.txt").read_text()


def test_no_captions_falls_back_to_metadata_only_and_the_note_says_so(env):
    e = env()
    result = e.go(FakeYtDlp(["v1"]))
    assert result.sources["youtube"]["status"] == "ok"
    [note] = e.notes()
    text = note.read_text()
    assert "Metadata only (no captions)" in text
    assert "metadata-only" in text.split("---")[1]  # a tag in the frontmatter
    assert "No captions available" in stdin_of(e)
    assert not (e.data / "transcripts").exists()


def test_a_video_that_fails_is_skipped_logged_and_retried_but_the_others_are_done(env):
    e = env()
    result = e.go(FakeYtDlp(["good1", "bad", "good2"], fail_info={"bad"},
                            subs={"good1": {"manual": MANUAL}, "good2": {"manual": MANUAL}}))
    entry = result.sources["youtube"]
    assert entry["status"] == "failed"
    assert [i["id"] for i in entry["items"]] == ["good1", "good2"]
    assert [n.name for n in e.notes()] == ["youtube-good1.md", "youtube-good2.md"]
    processed = json.loads((e.data / "state" / "processed.json").read_text())["youtube"]
    assert "bad" not in processed
    # next run: only the failed one is looked at again, and it now works
    client = FakeYtDlp(["good1", "bad", "good2"], subs={"bad": {"manual": MANUAL}})
    again = e.go(client)
    assert again.sources["youtube"]["status"] == "ok"
    assert [i["id"] for i in again.sources["youtube"]["items"]] == ["bad"]
    assert [c for c in client.calls if c[0] == "info"] == [("info", "bad")]


def test_a_video_the_summarizer_cannot_rate_is_not_marked_processed(env):
    e = env(claude_replies=["not json at all"])
    result = e.go(FakeYtDlp(["v1"], subs={"v1": {"manual": MANUAL}}))
    assert result.sources["youtube"]["status"] == "failed"
    assert e.notes() == []
    processed = e.data / "state" / "processed.json"
    assert not processed.exists() or "v1" not in json.loads(processed.read_text()).get("youtube", [])


def test_an_unconfigured_playlist_reports_not_configured_and_does_not_touch_yt_dlp(env):
    e = env()
    e.cfg.write_text(e.cfg.read_text().replace('playlist_url = "https://youtube.com/playlist?list=PL1"', 'playlist_url = ""'))
    client = FakeYtDlp(["v1"])
    result = e.go(client)
    assert result.sources["youtube"] == {"status": "not configured", "items": []}
    assert client.calls == []


def test_a_playlist_listing_failure_fails_only_this_source(env):
    e = env()
    result = e.go(FakeYtDlp(fail_playlist=True))
    assert result.sources["youtube"]["status"] == "failed"
    assert "playlist unavailable" in result.sources["youtube"]["error"]


def test_the_first_run_handles_only_the_ten_newest_and_later_runs_handle_all_new(env):
    e = env()
    ids = [f"v{i:02d}" for i in range(12)]  # newest first
    subs = {i: {"manual": MANUAL} for i in ids + ["w1", "w2", "w3"]}
    first = e.go(FakeYtDlp(ids, subs=subs))
    assert [i["id"] for i in first.sources["youtube"]["items"]] == ids[:10]
    assert len(e.notes()) == 10
    later = e.go(FakeYtDlp(["w1", "w2", "w3", *ids], subs=subs))  # the 2 old ones stay skipped
    assert [i["id"] for i in later.sources["youtube"]["items"]] == ["w1", "w2", "w3"]


def test_a_rerun_with_the_same_playlist_finds_nothing_new(env):
    e = env()
    e.go(FakeYtDlp(["v1", "v2"], subs={"v1": {"manual": MANUAL}, "v2": {"manual": MANUAL}}))
    calls = e.claude.calls
    client = FakeYtDlp(["v1", "v2"])
    again = e.go(client)
    assert again.sources["youtube"]["items"] == []
    assert again.sources["youtube"]["status"] == "ok"
    assert [c[0] for c in client.calls] == ["playlist"]
    assert e.claude.calls == calls


def test_notes_carry_no_transcript_text_beyond_short_quotes(env):
    words = [f"zq{i}word" for i in range(120)]
    transcript_vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:09.000\n" + " ".join(words) + "\n"
    copied = " ".join(words[:60])
    e = env(claude_replies=[rating_reply(takeaways=[copied] + ["Own words."] * 5,
                                         quotes=[" ".join(words[:10])])])
    e.go(FakeYtDlp(["v1"], subs={"v1": {"manual": transcript_vtt}}))
    [note] = e.notes()
    longest = max((len(m.group(0).split()) for m in re.finditer(r"(?:zq\d+word ?)+", note.read_text())), default=0)
    assert longest <= 40
    assert "zq100word" not in note.read_text()
    assert "zq100word" in (e.data / "transcripts" / "youtube-v1.txt").read_text()
    assert not list((e.out).rglob("*.txt"))  # no transcript file anywhere in the output tree


# --- the real yt-dlp client, driven against a fake yt-dlp executable ---



FAKE_YTDLP = '''#!{python}
import json, pathlib, sys
d = pathlib.Path({dir!r})
args = sys.argv[1:]
n = len(list(d.glob("argv-*.json")))
(d / f"argv-{{n}}.json").write_text(json.dumps(args))
if (d / "exit").exists():
    sys.stderr.write("ERROR: boom")
    sys.exit(int((d / "exit").read_text()))
url = args[-1]
if "--flat-playlist" in args:
    sys.stdout.write((d / "playlist.json").read_text())
elif "--skip-download" in args and ("--write-subs" in args or "--write-auto-subs" in args):
    kind = "auto" if "--write-auto-subs" in args else "manual"
    vid = url.rsplit("=", 1)[-1]
    src = d / f"{{vid}}.{{kind}}.vtt"
    if (d / "sub-stderr").exists():
        sys.stderr.write((d / "sub-stderr").read_text())
    if src.exists():
        out = pathlib.Path(args[args.index("-o") + 1].replace("%(id)s", vid).replace("%(ext)s", "vtt"))
        out.with_name(f"{{vid}}.en.vtt").write_text(src.read_text())
else:
    sys.stdout.write((d / "info.json").read_text())
'''


@pytest.fixture
def fake_ytdlp(tmp_path):
    d = tmp_path / "fake-ytdlp"
    d.mkdir()
    exe = d / "yt-dlp"
    exe.write_text(FAKE_YTDLP.format(python=sys.executable, dir=str(d)))
    exe.chmod(0o755)

    class Fake:
        path = exe
        dir = d

        def argv(self, n):
            return json.loads((d / f"argv-{n}.json").read_text())

    return Fake()


def test_the_process_client_lists_a_playlist_with_flat_playlist_json(fake_ytdlp):
    (fake_ytdlp.dir / "playlist.json").write_text(json.dumps({"entries": [{"id": "a"}, {"id": "b"}]}))
    entries = ProcessYtDlp(str(fake_ytdlp.path)).playlist("https://youtube.com/playlist?list=PL1")
    assert [e["id"] for e in entries] == ["a", "b"]
    args = fake_ytdlp.argv(0)
    assert "--flat-playlist" in args and "-J" in args
    assert args[-1] == "https://youtube.com/playlist?list=PL1"


def test_the_process_client_reads_video_metadata_without_downloading(fake_ytdlp):
    (fake_ytdlp.dir / "info.json").write_text(json.dumps({"id": "a", "title": "T"}))
    assert ProcessYtDlp(str(fake_ytdlp.path)).info("https://www.youtube.com/watch?v=a")["title"] == "T"
    assert "--skip-download" in fake_ytdlp.argv(0)


def test_the_process_client_asks_for_manual_then_auto_english_subs(fake_ytdlp, tmp_path):
    (fake_ytdlp.dir / "a.auto.vtt").write_text(MANUAL)
    client = ProcessYtDlp(str(fake_ytdlp.path))
    url = "https://www.youtube.com/watch?v=a"
    dest = tmp_path / "subs"
    dest.mkdir()
    assert client.subtitles(url, False, dest) is None  # nothing manual exists
    manual_args = fake_ytdlp.argv(0)
    assert "--write-subs" in manual_args and "--write-auto-subs" not in manual_args
    path = client.subtitles(url, True, dest)
    assert path is not None and path.name == "a.en.vtt" and path.read_text() == MANUAL
    auto_args = fake_ytdlp.argv(1)
    assert "--write-auto-subs" in auto_args and "--skip-download" in auto_args
    for args in (manual_args, auto_args):
        assert args[args.index("--sub-langs") + 1] == "en"


@pytest.mark.parametrize("call", [
    lambda c: c.playlist("u"),
    lambda c: c.info("u"),
    lambda c: c.subtitles("u", False, Path(".")),
])
def test_the_process_client_turns_a_failing_yt_dlp_into_ytdlperror(fake_ytdlp, call):
    (fake_ytdlp.dir / "exit").write_text("1")
    with pytest.raises(YtDlpError, match="boom"):
        call(ProcessYtDlp(str(fake_ytdlp.path)))


def test_the_process_client_reports_a_missing_binary_and_unparseable_output(fake_ytdlp, tmp_path):
    with pytest.raises(YtDlpError, match="cannot run"):
        ProcessYtDlp(str(tmp_path / "nope")).playlist("u")
    (fake_ytdlp.dir / "playlist.json").write_text("not json")
    with pytest.raises(YtDlpError, match="not JSON"):
        ProcessYtDlp(str(fake_ytdlp.path)).playlist("u")


def test_the_binary_comes_from_the_env_then_the_config_then_the_path():
    assert make_client({"ytdlp_cmd": "/opt/yt-dlp"}, None, env={}).cmd == "/opt/yt-dlp"
    assert make_client({"ytdlp_cmd": "/opt/yt-dlp"}, None, env={"DAILY_PULSE_YTDLP": "/x/yt"}).cmd == "/x/yt"
    assert make_client({}, None, env={}).cmd == "yt-dlp"


def test_with_fixtures_the_adapter_reads_recorded_inputs_and_never_runs_yt_dlp(env, fixtures_dir):
    d = fixtures_dir / "youtube"
    d.mkdir(exist_ok=True)
    (d / "playlist.json").write_text(json.dumps({"entries": [{"id": "fx1", "title": "Fixture"}]}))
    (d / "fx1.info.json").write_text(json.dumps({"id": "fx1", "title": "Fixture", "channel": "C",
                                                  "duration": 60, "upload_date": "20260110", "view_count": 5}))
    (d / "fx1.en.vtt").write_text(MANUAL)
    e = env()
    registry.register(YouTubeAdapter())
    cfg = load_config(e.cfg)
    cfg.sources["youtube"]["ytdlp_cmd"] = "/definitely/not/there"
    result = run_pipeline(cfg, DATE, fixtures_dir, out_dir=e.out)
    assert result.sources["youtube"]["status"] == "ok"
    assert [n.name for n in e.notes()] == ["youtube-fx1.md"]
    assert isinstance(make_client({}, fixtures_dir), FixtureYtDlp)


def test_the_install_script_is_executable_and_valid_bash():
    import os
    import subprocess

    script = Path(__file__).parent.parent / "scripts" / "install-ytdlp.sh"
    assert os.access(script, os.X_OK)
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    assert "sudo" not in script.read_text().replace("no sudo", "")


def test_the_cli_registers_the_youtube_adapter(make_env, fixtures_dir):
    from daily_pulse.cli import main

    cfg, data, out = make_env(sources=("youtube",))  # no playlist_url: reports "not configured"
    assert main(["run", "--config", str(cfg), "--fixtures", str(fixtures_dir), "--out", str(out),
                 "--date", DATE]) == 0
    res = json.loads((out / f"run-{DATE}.json").read_text())
    assert res["sources"]["youtube"]["status"] == "not configured"


def test_an_oldest_first_playlist_still_keeps_the_newest_ten_on_the_first_run(env):
    e = env()
    e.cfg.write_text(e.cfg.read_text().replace("newest_first", "oldest_first"))
    ids = [f"v{i:02d}" for i in range(12)]  # position order: oldest first
    subs = {i: {"manual": MANUAL} for i in ids}
    res = e.go(FakeYtDlp(ids, subs=subs))
    assert [i["id"] for i in res.sources["youtube"]["items"]] == list(reversed(ids))[:10]


def test_private_and_deleted_placeholders_are_never_listed(env):
    e = env()
    client = FakeYtDlp(["pub", "gone", "priv"], subs={"pub": {"manual": MANUAL}})
    plain = client.playlist

    def listing(url):
        entries = plain(url)
        entries[1]["title"] = "[Deleted video]"
        entries[2]["title"] = "[Private video]"
        return entries

    client.playlist = listing
    res = e.go(client)
    assert res.sources["youtube"]["status"] == "ok"
    assert [i["id"] for i in res.sources["youtube"]["items"]] == ["pub"]
    assert [c for c in client.calls if c[0] == "info"] == [("info", "pub")]


def test_a_metadata_only_note_has_no_invented_timestamped_beats(env):
    e = env()
    e.go(FakeYtDlp(["v1"]))  # replies include a beat at 00:01, but there is no transcript to ground it
    text = e.notes()[0].read_text()
    assert "- [00:01]" not in text


def test_a_real_run_without_a_notes_dir_fails_the_video_instead_of_consuming_it(env):
    e = env()
    registry.register(YouTubeAdapter(client=FakeYtDlp(["v1"], subs={"v1": {"manual": MANUAL}})))
    res = run_pipeline(load_config(e.cfg), DATE, None)  # no out_dir: nowhere to write the note
    assert res.sources["youtube"]["status"] == "failed"
    assert not (e.data / "state" / "processed.json").exists()


# --- review round 1 ---

RATE_LIMITED = "WARNING: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests\n"


def test_a_rate_limited_subtitle_download_is_an_error_not_an_absence(fake_ytdlp, tmp_path):
    (fake_ytdlp.dir / "sub-stderr").write_text(RATE_LIMITED)
    with pytest.raises(YtDlpError, match="429"):
        ProcessYtDlp(str(fake_ytdlp.path)).subtitles("https://www.youtube.com/watch?v=a", False, tmp_path)
    assert "--no-warnings" not in fake_ytdlp.argv(0)  # the warning is what tells us it failed
    (fake_ytdlp.dir / "sub-stderr").write_text("[info] There are no subtitles for the requested languages\n")
    assert ProcessYtDlp(str(fake_ytdlp.path)).subtitles("https://www.youtube.com/watch?v=a", False, tmp_path) is None


def test_a_video_whose_captions_were_rate_limited_is_not_rated_or_marked_processed(env, fake_ytdlp):
    (fake_ytdlp.dir / "playlist.json").write_text(json.dumps({"entries": [{"id": "v1", "title": "T"}]}))
    (fake_ytdlp.dir / "info.json").write_text(json.dumps({"id": "v1", "title": "T", "channel": "C"}))
    (fake_ytdlp.dir / "sub-stderr").write_text(RATE_LIMITED)
    e = env()
    res = e.go(ProcessYtDlp(str(fake_ytdlp.path)))
    assert res.sources["youtube"]["status"] == "failed"
    assert e.notes() == [] and e.claude.calls == 0
    processed = e.data / "state" / "processed.json"
    assert not processed.exists() or "v1" not in json.loads(processed.read_text()).get("youtube", [])


def test_a_video_that_keeps_failing_is_given_up_on_after_three_attempts(env):
    e = env()
    soon = "ERROR: [youtube] bad: Premieres in 2 hours"  # video-specific, may work later
    statuses = [e.go(FakeYtDlp(["good", "bad"], fail_info={"bad"}, fail_message=soon, subs={"good": {"manual": MANUAL}}))
                .sources["youtube"]["status"] for _ in range(3)]
    assert statuses == ["failed", "failed", "ok"]
    processed = json.loads((e.data / "state" / "processed.json").read_text())["youtube"]
    assert "bad" in processed
    client = FakeYtDlp(["good", "bad"], fail_info={"bad"})
    res = e.go(client)
    assert res.sources["youtube"]["status"] == "ok"
    assert [c[0] for c in client.calls] == ["playlist"]  # not retried
    assert [n.name for n in e.notes()] == ["youtube-good.md"]  # and no note was made for it


@pytest.mark.parametrize("message", [
    "ERROR: [youtube] x: Private video. Sign in if you've been granted access to this video",
    "ERROR: [youtube] x: Join this channel to get access to members-only content",
    "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users",
    "ERROR: [youtube] x: The uploader has not made this video available in your country",
])
def test_a_permanently_unavailable_video_is_skipped_at_once(env, message):
    e = env()
    res = e.go(FakeYtDlp(["good", "locked"], fail_info={"locked"}, fail_message=message,
                         subs={"good": {"manual": MANUAL}}))
    assert res.sources["youtube"]["status"] == "ok"
    assert "locked" in json.loads((e.data / "state" / "processed.json").read_text())["youtube"]


def test_beats_in_a_two_hour_video_may_use_h_mm_ss_and_three_digit_minutes(env):
    long_vtt = "WEBVTT\n\n01:45:12.000 --> 01:45:20.000\nnear the end\n\n00:05:00.000 --> 00:05:04.000\nearlier\n"
    beats = [{"timestamp": "5:00", "text": "Sets up."}, {"timestamp": "105:12", "text": "Wraps up."},
             {"timestamp": "1:45:12", "text": "Final remarks."}]
    e = env(claude_replies=[rating_reply(beats=beats)])
    res = e.go(FakeYtDlp(["long"], subs={"long": {"manual": long_vtt}}))
    assert res.sources["youtube"]["status"] == "ok"
    text = e.notes()[0].read_text()
    assert "- [105:12] Wraps up." in text and "- [1:45:12] Final remarks." in text
    assert "[1:45:12] near the end" in (e.data / "transcripts" / "youtube-long.txt").read_text()


def test_the_prompt_tells_the_model_to_use_h_mm_ss_past_one_hour():
    from daily_pulse.summarizer import DEFAULT_PROMPTS_DIR

    assert "H:MM:SS" in (DEFAULT_PROMPTS_DIR / "summarize.md").read_text()


# --- review round 2 ---

@pytest.mark.parametrize("message", [
    "ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
    "yt-dlp timed out after 180.0s",
    "ERROR: Sign in to confirm you're not a bot. Use --cookies-from-browser",
    "cannot run 'yt-dlp': [Errno 2] No such file or directory",
])
def test_source_level_yt_dlp_failures_never_count_toward_giving_up_a_video(env, message):
    e = env()
    for _ in range(4):
        client = FakeYtDlp(["a", "b"], fail_all=message)
        res = e.go(client)
        assert res.sources["youtube"]["status"] == "failed"
        assert [c for c in client.calls if c[0] == "info"] == [("info", "a")]  # the run stops for the source
    assert not (e.data / "state" / "youtube-attempts.json").exists()
    processed = e.data / "state" / "processed.json"
    assert not processed.exists() or json.loads(processed.read_text()).get("youtube", []) == []
    ok = e.go(FakeYtDlp(["a", "b"], subs={"a": {"manual": MANUAL}, "b": {"manual": MANUAL}}))
    assert [i["id"] for i in ok.sources["youtube"]["items"]] == ["a", "b"]


def test_a_fresh_video_with_no_captions_yet_is_retried_then_gets_a_flagged_metadata_only_note(env):
    e = env()
    fresh = FakeYtDlp(["new"], upload_date="20260115")  # uploaded on the run date, no captions
    res = e.go(fresh, date="2026-01-15")
    assert res.sources["youtube"]["status"] == "ok"  # waiting is not a failure
    assert res.sources["youtube"]["items"] == []
    assert e.claude.calls == 0 and list(e.out.rglob("*.md")) == []
    processed = e.data / "state" / "processed.json"
    assert not processed.exists() or "new" not in json.loads(processed.read_text()).get("youtube", [])
    later = e.go(FakeYtDlp(["new"], upload_date="20260115"), date="2026-01-19")  # 4 days on, still none
    [note] = list(e.out.rglob("*.md"))
    assert "Metadata only (no captions)" in note.read_text()
    assert [i["id"] for i in later.sources["youtube"]["items"]] == ["new"]


def test_a_live_video_without_captions_is_only_waited_for_three_days(env):
    e = env()
    for day in ("2026-01-15", "2026-01-16", "2026-01-17"):
        client = FakeYtDlp(["stream"], upload_date="20260101", live_status="is_live")
        assert e.go(client, date=day).sources["youtube"]["items"] == []
    assert list(e.out.rglob("*.md")) == []
    res = e.go(FakeYtDlp(["stream"], upload_date="20260101", live_status="is_live"), date="2026-01-18")
    assert [i["id"] for i in res.sources["youtube"]["items"]] == ["stream"]


# --- review round 3: error-text patterns ---

def test_a_throttled_ip_answering_video_unavailable_leaves_the_video_unprocessed(env):
    e = env()
    throttle = "ERROR: [youtube] a: Video unavailable. This content isn't available, try again later."
    for _ in range(4):
        res = e.go(FakeYtDlp(["a", "b"], fail_all=throttle))
        assert res.sources["youtube"]["status"] == "failed"
    processed = e.data / "state" / "processed.json"
    assert not processed.exists() or json.loads(processed.read_text()).get("youtube", []) == []
    assert not (e.data / "state" / "youtube-attempts.json").exists()


def test_one_video_that_always_fails_to_extract_is_given_up_on_without_halting_the_source(env):
    e = env()
    odd = "ERROR: [youtube] bad: Unable to extract initial data; please report this issue"
    subs = {i: {"manual": MANUAL} for i in ("bad", "g1", "g2")}
    first = e.go(FakeYtDlp(["bad", "g1", "g2"], fail_info={"bad"}, fail_message=odd, subs=subs))
    assert [i["id"] for i in first.sources["youtube"]["items"]] == ["g1", "g2"]  # the ones behind it ran
    assert e.go(FakeYtDlp(["bad"], fail_info={"bad"}, fail_message=odd)).sources["youtube"]["status"] == "failed"
    assert e.go(FakeYtDlp(["bad"], fail_info={"bad"}, fail_message=odd)).sources["youtube"]["status"] == "ok"
    client = FakeYtDlp(["bad"], fail_info={"bad"}, fail_message=odd)
    e.go(client)
    assert [c[0] for c in client.calls] == ["playlist"]  # given up on after 3 strikes


def test_a_video_id_containing_429_with_no_subtitles_gets_a_metadata_only_note(env, fake_ytdlp):
    vid = "ab429xyz123"
    (fake_ytdlp.dir / "playlist.json").write_text(json.dumps({"entries": [{"id": vid, "title": "T"}]}))
    (fake_ytdlp.dir / "info.json").write_text(json.dumps({"id": vid, "title": "T", "channel": "C",
                                                           "upload_date": "20260101"}))
    (fake_ytdlp.dir / "sub-stderr").write_text(
        f"[youtube] {vid}: Downloading webpage\n[info] There are no subtitles for the requested languages\n")
    e = env()
    res = e.go(ProcessYtDlp(str(fake_ytdlp.path)))
    assert res.sources["youtube"]["status"] == "ok"
    [note] = list(e.out.rglob("*.md"))
    assert "Metadata only (no captions)" in note.read_text()
