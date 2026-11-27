import json

import pytest

from daily_pulse.adapters import registry
from daily_pulse.adapters.youtube import YouTubeAdapter, YtDlpError
from daily_pulse.config import load_config
from daily_pulse.runner import run_pipeline

from test_youtube import DATE, MANUAL, FakeYtDlp, rating_reply


class UndatedListing(FakeYtDlp):
    """A flat playlist listing carries no upload dates, as yt-dlp's often does; info() does."""

    def __init__(self, ids, dates, **kw):
        super().__init__(ids, meta={i: {"upload_date": d} for i, d in dates.items()}, **kw)

    def playlist(self, url):
        return [{k: v for k, v in e.items() if k != "upload_date"} for e in super().playlist(url)]


@pytest.fixture
def go(fake_claude, monkeypatch, tmp_path):
    monkeypatch.setenv("DAILY_PULSE_CLAUDE", str(fake_claude(rating_reply()).path))

    def run(client, order=None, cap=2, date=DATE):
        cfg = tmp_path / "config.toml"
        cfg.write_text(f'[run]\nfirst_run_cap = {cap}\n\n[paths]\ndata_dir = "{tmp_path / "data"}"\n\n'
                       "[quality]\nmax_triage_calls = 0\n\n"
                       '[sources.youtube]\nenabled = true\nplaylist_url = "https://youtube.com/playlist?list=PL1"\n'
                       + (f'playlist_order = "{order}"\n' if order else ""))
        registry.register(YouTubeAdapter(client=client))
        res = run_pipeline(load_config(cfg), date, None, out_dir=tmp_path / "out")
        return [i["id"] for i in res.sources["youtube"]["items"]], res

    run.data = tmp_path / "data"
    return run


SUBS = {i: {"manual": MANUAL} for i in "abc"}


def test_an_unset_order_is_detected_oldest_first_from_the_first_and_last_upload_dates(go):
    client = UndatedListing("abc", {"a": "20250101", "b": "20250601", "c": "20260101"}, subs=SUBS)
    ids, _ = go(client)  # position order is oldest first, so the first run keeps c and b
    assert ids == ["c", "b"]
    assert [c for c in client.calls if c[0] == "info"][:2] == [("info", "a"), ("info", "c")]


def test_an_unset_order_is_detected_newest_first_too(go):
    ids, _ = go(UndatedListing("abc", {"a": "20260101", "b": "20250601", "c": "20250101"}, subs=SUBS))
    assert ids == ["a", "b"]


def test_a_detected_order_is_remembered_so_it_costs_two_calls_once(go):
    dates = {"a": "20250101", "b": "20250601", "c": "20260101"}
    go(UndatedListing("abc", dates, subs=SUBS), cap=3)
    client = UndatedListing(["a", "b", "c", "d"], {**dates, "d": "20260101"}, subs={**SUBS, "d": {"manual": MANUAL}})
    ids, _ = go(client)
    assert ids == ["d"] and [c for c in client.calls if c[0] == "info"] == [("info", "d")]


def test_an_explicit_order_is_never_detected(go):
    client = UndatedListing("abc", {"a": "20260101", "b": "20250601", "c": "20250101"}, subs=SUBS)
    ids, _ = go(client, order="oldest_first")
    assert ids == ["c", "b"]
    assert [c for c in client.calls if c[0] == "info"] == [("info", "c"), ("info", "b")]  # only the videos


@pytest.mark.parametrize("dates", [{"a": "20260101", "b": "20260101", "c": "20260101"}, {"a": "", "c": ""}])
def test_dates_that_cannot_tell_fall_back_to_newest_first(go, dates):
    ids, res = go(UndatedListing("abc", dates, subs=SUBS))
    assert ids == ["a", "b"] and res.sources["youtube"]["status"] == "ok"


def test_a_failing_detection_call_falls_back_to_newest_first_without_failing_the_source(go):
    client = UndatedListing("abc", {}, subs=SUBS)
    real_info = client.info

    def info(url):
        if url.endswith("=a") and not client.calls_seen:
            client.calls_seen = True
            raise YtDlpError("boom")
        return real_info(url)

    client.calls_seen = False
    client.info = info
    ids, res = go(client)
    assert res.sources["youtube"]["status"] == "ok" and ids == ["a", "b"]


def test_a_single_video_playlist_needs_no_detection(go):
    client = UndatedListing("a", {"a": "20260101"}, subs=SUBS)
    ids, _ = go(client)
    assert ids == ["a"] and [c for c in client.calls if c[0] == "info"] == [("info", "a")]


def test_listing_dates_win_over_the_declared_order(go):
    client = FakeYtDlp("abc", subs=SUBS, meta={"a": {"upload_date": "20250101"}, "b": {"upload_date": "20260301"},
                                               "c": {"upload_date": "20260201"}})
    ids, _ = go(client, order="newest_first", cap=3)
    assert ids == ["b", "c", "a"]


def test_a_bad_order_value_fails_the_source_not_the_run(go):
    ids, res = go(FakeYtDlp("a"), order="sideways")
    assert ids == [] and res.sources["youtube"]["status"] == "failed"
