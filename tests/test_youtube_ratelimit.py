"""Ticket 13: pace yt-dlp requests and recover from YouTube 429s."""
import json
import sys
import tomllib
from pathlib import Path

import pytest

from daily_pulse.adapters.youtube import ProcessYtDlp, YtDlpError, YtDlpRateLimited, make_client
from test_youtube import MANUAL, FakeYtDlp, env, fake_ytdlp  # noqa: F401 - fixtures

RATE = "ERROR: unable to download video data: HTTP Error 429: Too Many Requests"
GOOD = {"manual": MANUAL}
ROOT = Path(__file__).resolve().parent.parent

# Answers 429 for the first `limit` calls (a counter file in its directory), then a canned video.
FLAKY = '''#!{python}
import pathlib, sys
d = pathlib.Path({dir!r})
n = len(list(d.glob("call-*")))
(d / f"call-{{n}}").write_text("")
if n < int((d / "limit").read_text()):
    sys.stderr.write({message!r})
    sys.exit(1)
sys.stdout.write('{{"id": "a", "title": "T"}}')
'''


def flaky(tmp_path, limit):
    d = tmp_path / "flaky"
    d.mkdir()
    exe = d / "yt-dlp"
    exe.write_text(FLAKY.format(python=sys.executable, dir=str(d), message=RATE))
    exe.chmod(0o755)
    (d / "limit").write_text(str(limit))
    return exe, d


def state(e, name):
    path = e.data / "state" / name
    return json.loads(path.read_text()) if path.exists() else {}


class Throttled(FakeYtDlp):
    """Like FakeYtDlp, but YouTube answers 429 (after the client's own retries) for some calls."""

    def __init__(self, *args, limit_info=(), limit_subs=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.limit_info, self.limit_subs = set(limit_info), set(limit_subs)

    def info(self, url):
        if url.rsplit("=", 1)[-1] in self.limit_info:
            self.calls.append(("info", url.rsplit("=", 1)[-1]))
            raise YtDlpRateLimited(f"yt-dlp exit 1: {RATE}")
        return super().info(url)

    def subtitles(self, url, auto, dest):
        if url.rsplit("=", 1)[-1] in self.limit_subs:
            self.calls.append(("subs", url.rsplit("=", 1)[-1], "auto" if auto else "manual"))
            raise YtDlpRateLimited(f"subtitle download failed: {RATE}")
        return super().subtitles(url, auto, dest)


# --- the process client ---

def test_a_429_is_retried_after_each_backoff_wait_and_then_succeeds(tmp_path):
    exe, d = flaky(tmp_path, 2)
    waits = []
    client = ProcessYtDlp(str(exe), backoff=(30.0, 90.0), sleep=waits.append)
    assert client.info("https://www.youtube.com/watch?v=a")["title"] == "T"
    assert waits == [30.0, 90.0]
    assert len(list(d.glob("call-*"))) == 3


def test_a_429_that_never_clears_raises_ytdlpratelimited_after_the_last_wait(tmp_path):
    exe, d = flaky(tmp_path, 99)
    waits = []
    client = ProcessYtDlp(str(exe), backoff=(30.0, 90.0), sleep=waits.append)
    with pytest.raises(YtDlpRateLimited, match="429"):
        client.info("https://www.youtube.com/watch?v=a")
    assert waits == [30.0, 90.0] and len(list(d.glob("call-*"))) == 3


def test_a_client_without_backoff_does_not_retry_a_429(tmp_path):
    exe, d = flaky(tmp_path, 99)
    waits = []
    with pytest.raises(YtDlpRateLimited):
        ProcessYtDlp(str(exe), sleep=waits.append).info("u")
    assert waits == [] and len(list(d.glob("call-*"))) == 1


def test_other_failures_are_not_retried_and_are_not_rate_limits(fake_ytdlp):
    (fake_ytdlp.dir / "exit").write_text("1")
    waits = []
    with pytest.raises(YtDlpError, match="boom") as excinfo:
        ProcessYtDlp(str(fake_ytdlp.path), backoff=(30.0,), sleep=waits.append).info("u")
    assert not isinstance(excinfo.value, YtDlpRateLimited) and waits == []


def test_a_rate_limited_subtitle_download_reported_with_exit_0_is_a_rate_limit(fake_ytdlp, tmp_path):
    (fake_ytdlp.dir / "sub-stderr").write_text(
        "WARNING: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests")
    waits = []
    dest = tmp_path / "subs"
    dest.mkdir()
    client = ProcessYtDlp(str(fake_ytdlp.path), backoff=(5.0,), sleep=waits.append)
    with pytest.raises(YtDlpRateLimited):
        client.subtitles("https://www.youtube.com/watch?v=a", True, dest)
    assert waits == []  # exit 0 with a warning is reported, not retried here


def test_calls_are_paced_but_the_first_one_is_not_delayed(fake_ytdlp):
    (fake_ytdlp.dir / "playlist.json").write_text(json.dumps({"entries": []}))
    (fake_ytdlp.dir / "info.json").write_text(json.dumps({"id": "a"}))
    waits = []
    client = ProcessYtDlp(str(fake_ytdlp.path), pace=2.5, sleep=waits.append)
    client.info("https://www.youtube.com/watch?v=a")
    client.info("https://www.youtube.com/watch?v=a")
    client.info("https://www.youtube.com/watch?v=a")
    assert waits == [2.5, 2.5]


def test_extra_args_go_on_every_call_before_the_others(fake_ytdlp):
    (fake_ytdlp.dir / "info.json").write_text(json.dumps({"id": "a"}))
    client = ProcessYtDlp(str(fake_ytdlp.path), extra_args=["--js-runtimes", "node"])
    client.info("https://www.youtube.com/watch?v=a")
    client.info("https://www.youtube.com/watch?v=a")
    for n in (0, 1):
        args = fake_ytdlp.argv(n)
        assert args[:2] == ["--js-runtimes", "node"] and "--skip-download" in args


# --- configuration ---

def test_make_client_defaults_are_a_pause_and_two_backoff_waits():
    client = make_client({}, None, env={})
    assert client.pace == 3.0 and client.backoff == (30.0, 90.0) and client.extra_args == []


def test_make_client_reads_the_config_keys():
    cfg = {"pace_seconds": 0, "rate_limit_backoff_seconds": [10, 20, 40], "ytdlp_extra_args": ["--js-runtimes", "node"]}
    client = make_client(cfg, None, env={})
    assert client.pace == 0 and client.backoff == (10.0, 20.0, 40.0)
    assert client.extra_args == ["--js-runtimes", "node"]


@pytest.mark.parametrize("cfg", [
    {"pace_seconds": -1}, {"pace_seconds": "3"}, {"pace_seconds": True},
    {"rate_limit_backoff_seconds": 30}, {"rate_limit_backoff_seconds": [30, "x"]},
    {"ytdlp_extra_args": "--js-runtimes node"}, {"ytdlp_extra_args": ["ok", 3]},
])
def test_make_client_rejects_a_malformed_setting(cfg):
    with pytest.raises(ValueError, match="sources.youtube"):
        make_client(cfg, None, env={})


def test_the_shipped_config_sets_the_pacing_keys_and_they_are_valid():
    cfg = tomllib.loads((ROOT / "config" / "daily-pulse.toml").read_text())["sources"]["youtube"]
    assert {"pace_seconds", "rate_limit_backoff_seconds", "ytdlp_extra_args"} <= set(cfg)
    client = make_client(cfg, None, env={})
    assert client.pace > 0 and client.backoff


# --- the adapter: a 429 is the source's problem, never a video's ---

def test_a_rate_limited_first_video_stops_the_source_at_once_and_counts_nothing(env):
    e = env()
    client = Throttled(["a", "b", "c"], limit_info={"a"}, subs={"b": GOOD, "c": GOOD})
    res = e.go(client)
    assert res.sources["youtube"]["status"] == "failed" and "429" in res.sources["youtube"]["error"]
    assert [c for c in client.calls if c[0] == "info"] == [("info", "a")]  # nothing else is tried
    assert state(e, "youtube-source-failures.json") == {} and state(e, "youtube-attempts.json") == {}
    assert state(e, "processed.json").get("youtube", []) == []


def test_many_rate_limited_runs_never_cost_the_video_its_place(env):
    e = env()
    for _ in range(8):  # more than SOURCE_MAX_ATTEMPTS
        res = e.go(Throttled(["only"], limit_info={"only"}, subs={"only": GOOD}))
        assert res.sources["youtube"]["status"] == "failed"
    assert state(e, "youtube-source-failures.json") == {}
    assert state(e, "processed.json").get("youtube", []) == []
    ok = e.go(FakeYtDlp(["only"], subs={"only": GOOD}))
    assert [i["id"] for i in ok.sources["youtube"]["items"]] == ["only"]


def test_a_429_at_the_caption_stage_keeps_the_finished_video_and_leaves_the_rest(env):
    e = env()
    client = Throttled(["a", "b", "c"], limit_subs={"b"}, subs={"a": GOOD, "b": GOOD, "c": GOOD})
    res = e.go(client)
    yt = res.sources["youtube"]
    assert yt["status"] == "failed" and [i["id"] for i in yt["items"]] == ["a"]
    assert [c for c in client.calls if c[0] == "info"] == [("info", "a"), ("info", "b")]  # c is not tried
    assert state(e, "processed.json")["youtube"] == ["a"]
    assert state(e, "youtube-source-failures.json") == {} and state(e, "youtube-attempts.json") == {}
    later = e.go(FakeYtDlp(["a", "b", "c"], subs={"a": GOOD, "b": GOOD, "c": GOOD}))  # the rest next run
    assert [i["id"] for i in later.sources["youtube"]["items"]] == ["b", "c"]
