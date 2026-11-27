from pathlib import Path

from daily_pulse.adapters.youtube import clean_vtt

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

import json  # noqa: E402
import re  # noqa: E402

import pytest  # noqa: E402

from daily_pulse.adapters import registry  # noqa: E402
from daily_pulse.adapters.youtube import YouTubeAdapter, YtDlpError  # noqa: E402
from daily_pulse.config import load_config  # noqa: E402
from daily_pulse.runner import run_pipeline  # noqa: E402

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

    def __init__(self, ids=(), subs=None, fail_info=(), fail_playlist=False):
        self.ids = list(ids)
        self.subs = subs or {}  # id -> {"manual": vtt, "auto": vtt}
        self.fail_info = set(fail_info)
        self.fail_playlist = fail_playlist
        self.calls: list[tuple] = []

    def playlist(self, url):
        self.calls.append(("playlist", url))
        if self.fail_playlist:
            raise YtDlpError("playlist unavailable")
        return [{"id": i, "title": f"Title {i}", "channel": "Chan"} for i in self.ids]

    def info(self, url):
        vid = url.rsplit("=", 1)[-1]
        self.calls.append(("info", vid))
        if vid in self.fail_info:
            raise YtDlpError(f"cannot fetch {vid}")
        return {"id": vid, "title": f"Title {vid}", "channel": "Chan", "duration": 754,
                "upload_date": "20260112", "view_count": 4321,
                "description": "A talk about loops.\nSecond line."}

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
                       '[sources.youtube]\nenabled = true\nplaylist_url = "https://youtube.com/playlist?list=PL1"\n')
        claude = fake_claude(*(claude_replies or [rating_reply()]))
        monkeypatch.setenv("DAILY_PULSE_CLAUDE", str(claude.path))

        class Env:
            pass

        e = Env()
        e.cfg, e.data, e.out, e.claude = cfg, data, out, claude

        def go(client):
            registry.register(YouTubeAdapter(client=client))
            return run_pipeline(load_config(cfg), DATE, None, out_dir=out)

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
