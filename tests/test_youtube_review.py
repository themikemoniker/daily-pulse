"""Requirements from the PR #20 review (see the comment on ticket 21)."""
import json
import subprocess
from pathlib import Path

import pytest

from daily_pulse.adapters.youtube import ProcessYtDlp, clean_vtt
from test_youtube import AUTO, MANUAL, FakeYtDlp, env, fake_ytdlp  # noqa: F401 - fixtures

TIMEOUT = "yt-dlp timed out after 180.0s"
GOOD = {"manual": MANUAL}


def state(e, name):
    path = e.data / "state" / name
    return json.loads(path.read_text()) if path.exists() else {}


def processed(e):
    return state(e, "processed.json").get("youtube", [])


def infos(client):
    return [c[1] for c in client.calls if c[0] == "info"]


# --- "content isn't available" is the wording for individually removed videos ---

def test_an_individually_removed_video_does_not_halt_the_source(env):
    e = env()
    gone = "ERROR: [youtube] gone: Video unavailable. This content isn't available."
    res = e.go(FakeYtDlp(["gone", "good"], fail_info={"gone"}, fail_message=gone, subs={"good": GOOD}))
    assert [i["id"] for i in res.sources["youtube"]["items"]] == ["good"]  # the one behind it still ran
    assert state(e, "youtube-attempts.json") == {"gone": 1}  # an ordinary strike, not a source outage


# --- source-level failures are counted per video ---

def test_the_source_halts_once_two_different_videos_fail_source_level_in_one_run(env):
    e = env()
    client = FakeYtDlp(["a", "b", "c"], fail_all=TIMEOUT, subs={"c": GOOD})
    res = e.go(client)
    assert res.sources["youtube"]["status"] == "failed"
    assert infos(client) == ["a", "b"]  # c is not tried: yt-dlp is clearly down
    assert processed(e) == [] and state(e, "youtube-source-failures.json") == {}
    assert state(e, "youtube-attempts.json") == {}


def test_a_single_video_that_always_times_out_does_not_stop_the_others(env):
    e = env()
    client = FakeYtDlp(["stream", "good"], fail_info={"stream"}, fail_message=TIMEOUT, subs={"good": GOOD})
    res = e.go(client)
    assert [i["id"] for i in res.sources["youtube"]["items"]] == ["good"]
    assert infos(client) == ["stream", "good"]


def test_a_video_that_is_the_only_source_level_failure_for_five_runs_is_given_up_on(env):
    e = env()
    statuses = []
    for n in range(5):
        client = FakeYtDlp(["stream", f"g{n}"], fail_info={"stream"}, fail_message=TIMEOUT,
                           subs={f"g{n}": GOOD})
        statuses.append(e.go(client).sources["youtube"]["status"])
        assert "stream" not in processed(e)
    assert statuses == ["failed"] * 5
    assert state(e, "youtube-source-failures.json") == {"stream": 5}
    client = FakeYtDlp(["stream", "g5"], fail_info={"stream"}, fail_message=TIMEOUT, subs={"g5": GOOD})
    res = e.go(client)
    assert res.sources["youtube"]["status"] == "ok"
    assert "stream" in processed(e) and infos(client) == ["g5"]  # not retried
    assert "stream" not in state(e, "youtube-source-failures.json")  # and its counter is pruned
    assert all("stream" not in p.name for p in e.notes())


def test_a_lone_failing_video_is_given_up_on_even_when_nothing_else_is_new(env):
    e = env()  # one new video a day, and it is a stream whose -J call always times out
    for _ in range(5):
        assert e.go(FakeYtDlp(["stream"], fail_info={"stream"}, fail_message=TIMEOUT)).sources["youtube"]["status"] == "failed"
    client = FakeYtDlp(["stream"], fail_info={"stream"}, fail_message=TIMEOUT)
    assert e.go(client).sources["youtube"]["status"] == "ok"
    assert "stream" in processed(e) and infos(client) == []


def test_a_run_that_halts_does_not_count_against_either_video(env):
    e = env()
    e.go(FakeYtDlp(["stream", "g0"], fail_info={"stream"}, fail_message=TIMEOUT, subs={"g0": GOOD}))
    assert state(e, "youtube-source-failures.json") == {"stream": 1}
    e.go(FakeYtDlp(["stream", "other", "g1"], fail_all=TIMEOUT, subs={"g1": GOOD}))  # a real outage
    assert state(e, "youtube-source-failures.json") == {"stream": 1}


def test_an_outage_that_starts_mid_run_never_costs_a_video_its_place(env):
    e = env()  # every run: a new good video, then two videos that time out because yt-dlp got throttled
    for n in range(8):
        client = FakeYtDlp([f"g{n}", "victim", "w"], fail_info={"victim", "w"}, fail_message=TIMEOUT,
                           subs={f"g{n}": GOOD})
        assert e.go(client).sources["youtube"]["status"] == "failed"
        assert state(e, "youtube-source-failures.json") == {}
    assert "victim" not in processed(e) and "w" not in processed(e)


def test_a_changed_failure_resets_the_source_level_count(env):
    e = env()
    e.go(FakeYtDlp(["v"], fail_info={"v"}, fail_message=TIMEOUT))
    assert state(e, "youtube-source-failures.json") == {"v": 1}
    e.go(FakeYtDlp(["v"], fail_info={"v"}, fail_message="ERROR: [youtube] v: Unable to extract initial data"))
    assert state(e, "youtube-source-failures.json") == {} and state(e, "youtube-attempts.json") == {"v": 1}
    e.go(FakeYtDlp(["v"], fail_info={"v"}, fail_message=TIMEOUT))  # a call that works resets it too
    e.go(FakeYtDlp(["v"], upload_date="20260115"), date="2026-01-15")  # info works, captions pending
    assert state(e, "youtube-source-failures.json") == {}


def test_an_empty_listing_does_not_wipe_the_failure_counts(env):
    e = env()
    odd = "ERROR: [youtube] bad: Unable to extract initial data"
    e.go(FakeYtDlp(["bad"], fail_info={"bad"}, fail_message=odd))
    e.go(FakeYtDlp([]))  # a transient empty listing
    assert state(e, "youtube-attempts.json") == {"bad": 1}


# --- premieres and upcoming/live videos are deferrals ---

PREMIERE = "ERROR: [youtube] soon: Premieres in 2 hours"


@pytest.mark.parametrize("message", [
    PREMIERE, "ERROR: [youtube] soon: Premiere will begin shortly",
    "ERROR: [youtube] soon: This live event will begin in 3 hours.",
])
def test_an_upcoming_video_is_deferred_not_struck(env, message):
    e = env()
    for day in ("2026-01-15", "2026-01-16", "2026-01-17"):
        res = e.go(FakeYtDlp(["good", "soon"], fail_info={"soon"}, fail_message=message,
                             subs={"good": GOOD}), date=day)
        assert res.sources["youtube"]["status"] == "ok"  # waiting is not a failure
    assert state(e, "youtube-attempts.json") == {} and "soon" not in processed(e)


def test_an_upcoming_video_is_processed_once_it_is_out(env):
    e = env()
    e.go(FakeYtDlp(["soon"], fail_info={"soon"}, fail_message=PREMIERE), date="2026-01-15")
    res = e.go(FakeYtDlp(["soon"], subs={"soon": GOOD}), date="2026-01-16")
    assert [i["id"] for i in res.sources["youtube"]["items"]] == ["soon"]
    assert state(e, "youtube-pending.json") == {}  # nothing left to wait for


def test_a_video_still_upcoming_after_three_days_becomes_an_ordinary_strike(env):
    e = env()
    for day in ("2026-01-15", "2026-01-16", "2026-01-17"):
        e.go(FakeYtDlp(["soon"], fail_info={"soon"}, fail_message=PREMIERE), date=day)
    assert state(e, "youtube-attempts.json") == {}
    statuses = [e.go(FakeYtDlp(["soon"], fail_info={"soon"}, fail_message=PREMIERE), date=f"2026-01-{d}")
                .sources["youtube"]["status"] for d in (18, 19, 20)]
    assert statuses == ["failed", "failed", "ok"] and "soon" in processed(e)  # three strikes, then given up


# --- captions ---

class FilesYtDlp(ProcessYtDlp):
    """A yt-dlp whose subtitle download leaves the given files behind."""

    def __init__(self, names):
        super().__init__("unused")
        self.names, self.args = names, None

    def _exec(self, *args, warnings=False):
        self.args = args
        dest = Path(args[args.index("-o") + 1]).parent
        for name in self.names:
            (dest / name).write_text(name)
        return subprocess.CompletedProcess(args, 0, "", "")


def test_all_english_variants_are_requested(tmp_path):
    client = FilesYtDlp([])
    client.subtitles("https://www.youtube.com/watch?v=a", False, tmp_path)
    assert client.args[client.args.index("--sub-langs") + 1] == "en.*,en"


@pytest.mark.parametrize("names,chosen", [
    (["a.en-US.vtt", "a.en.vtt", "a.en-orig.vtt"], "a.en.vtt"),  # plain en wins
    (["a.en-US.vtt", "a.en-GB.vtt", "a.en-orig.vtt"], "a.en-GB.vtt"),  # else the first by name, always
    (["a.en-orig.vtt"], "a.en-orig.vtt"),
])
def test_the_subtitle_file_is_chosen_deterministically_preferring_plain_en(tmp_path, names, chosen):
    path = FilesYtDlp(names).subtitles("https://www.youtube.com/watch?v=a", True, tmp_path)
    assert path.name == chosen


REPEATS = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nYeah\n\n00:00:02.000 --> 00:00:03.000\nYeah\n"


def test_roll_up_deduplication_applies_to_auto_captions_only():
    assert clean_vtt(REPEATS).splitlines() == ["[00:01] Yeah", "[00:02] Yeah"]  # manual: both lines are real
    assert clean_vtt(REPEATS, auto=True).splitlines() == ["[00:01] Yeah"]


def test_captions_that_are_only_sound_tags_count_as_no_captions(env):
    e = env()
    music = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n[Music]\n\n00:00:03.000 --> 00:00:04.000\n[Applause]\n"
    res = e.go(FakeYtDlp(["v1"], subs={"v1": {"manual": music, "auto": music}}))
    assert res.sources["youtube"]["status"] == "ok"
    [note] = e.notes()
    assert "Metadata only (no captions)" in note.read_text()
    assert not (e.data / "transcripts").exists()


def test_sound_tag_only_manual_subtitles_fall_back_to_auto_captions(env):
    e = env()
    music = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n[Music]\n"
    e.go(FakeYtDlp(["v1"], subs={"v1": {"manual": music, "auto": AUTO}}))
    assert "today we look at evals" in (e.data / "transcripts" / "youtube-v1.txt").read_text()


def test_real_speech_with_a_music_tag_is_still_a_transcript(env):
    e = env()
    e.go(FakeYtDlp(["v1"], subs={"v1": {"manual": "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n[Music] hello there\n"}}))
    assert "hello there" in (e.data / "transcripts" / "youtube-v1.txt").read_text()


def test_auto_captions_are_deduplicated_and_manual_ones_are_not(env):
    e = env()
    e.go(FakeYtDlp(["m"], subs={"m": {"manual": REPEATS}}))
    assert (e.data / "transcripts" / "youtube-m.txt").read_text().splitlines() == ["[00:01] Yeah", "[00:02] Yeah"]
    e.go(FakeYtDlp(["a"], subs={"a": {"auto": REPEATS}}))
    assert (e.data / "transcripts" / "youtube-a.txt").read_text().splitlines() == ["[00:01] Yeah"]


# --- the attempts and pending state files do not grow forever ---

def test_finished_videos_leave_the_attempts_and_pending_files(env):
    e = env()
    odd = "ERROR: [youtube] flaky: Unable to extract initial data"
    e.go(FakeYtDlp(["flaky", "fresh"], fail_info={"flaky"}, fail_message=odd, upload_date="20260115"), date="2026-01-15")
    assert state(e, "youtube-attempts.json") == {"flaky": 1}
    assert set(state(e, "youtube-pending.json")) == {"fresh"}  # no captions yet, waited for
    e.go(FakeYtDlp(["flaky", "fresh"], subs={"flaky": GOOD, "fresh": GOOD}), date="2026-01-16")
    assert state(e, "youtube-attempts.json") == {} and state(e, "youtube-pending.json") == {}


def test_a_given_up_video_leaves_the_attempts_file(env):
    e = env()
    odd = "ERROR: [youtube] bad: Unable to extract initial data"
    for _ in range(3):
        e.go(FakeYtDlp(["bad"], fail_info={"bad"}, fail_message=odd))
    assert "bad" in processed(e) and state(e, "youtube-attempts.json") == {}


def test_entries_for_videos_no_longer_in_the_playlist_are_pruned(env):
    e = env()
    odd = "ERROR: [youtube] bad: Unable to extract initial data"
    e.go(FakeYtDlp(["bad", "fresh"], fail_info={"bad"}, fail_message=odd, upload_date="20260115"), date="2026-01-15")
    assert set(state(e, "youtube-attempts.json")) == {"bad"} and set(state(e, "youtube-pending.json")) == {"fresh"}
    e.go(FakeYtDlp(["other"], subs={"other": GOOD}), date="2026-01-16")  # both left the playlist
    assert state(e, "youtube-attempts.json") == {} and state(e, "youtube-pending.json") == {}


def test_a_dry_run_leaves_the_state_files_alone(env):
    e = env()
    odd = "ERROR: [youtube] bad: Unable to extract initial data"
    e.go(FakeYtDlp(["bad"], fail_info={"bad"}, fail_message=odd))
    e.go(FakeYtDlp(["other"], subs={"other": GOOD}), dry_run=True)
    assert state(e, "youtube-attempts.json") == {"bad": 1}


# --- the install script ---

def test_the_install_script_tries_python_venv_before_virtualenv():
    text = (Path(__file__).parent.parent / "scripts" / "install-ytdlp.sh").read_text()
    assert "python3 -m venv" in text and text.index("python3 -m venv") < text.index("python3 -m virtualenv")
