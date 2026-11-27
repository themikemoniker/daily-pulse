"""Watched YouTube channels: newest N per channel, merged with the playlist, failures isolated."""
import pytest

from daily_pulse.adapters.base import AdapterContext
from daily_pulse.adapters.youtube import YouTubeAdapter, YtDlpError, YtDlpRateLimited

CH_A, CH_B = "https://www.youtube.com/@alpha", "https://www.youtube.com/@bravo"


def e(vid, title=None, **extra):
    return {"id": vid, "title": title or f"title {vid}", **extra}


class Client:
    def __init__(self, playlist=(), channels=None):
        self._playlist, self.channels, self.asked = list(playlist), channels or {}, []

    def playlist(self, url):
        return self._playlist

    def channel_videos(self, url, limit):
        self.asked.append((url, limit))
        out = self.channels[url.removesuffix("/videos")]
        if isinstance(out, Exception):
            raise out
        return out[:limit]


def ids(client, **config):
    ctx = AdapterContext(config=config, persist=False)
    return [i.id for i in YouTubeAdapter(client=client).list_new(ctx)]


def test_not_configured_only_without_playlist_and_channels():
    a = YouTubeAdapter(client=Client())
    assert a.status(AdapterContext(config={})) == "not configured"
    assert a.status(AdapterContext(config={"channels": [CH_A]})) is None
    assert a.status(AdapterContext(config={"playlist_url": "https://x/p"})) is None


def test_channels_alone_list_their_newest_five_by_default():
    c = Client(channels={CH_A: [e(f"a{i}") for i in range(9)]})
    assert ids(c, channels=[CH_A]) == ["a0", "a1", "a2", "a3", "a4"]
    assert c.asked == [(CH_A + "/videos", 5)]


def test_channel_limit_is_configurable_and_validated():
    c = Client(channels={CH_A: [e(f"a{i}") for i in range(9)]})
    assert ids(c, channels=[CH_A], channel_limit=2) == ["a0", "a1"]
    for bad in (0, -1, True, "5"):
        with pytest.raises(ValueError, match="channel_limit"):
            ids(c, channels=[CH_A], channel_limit=bad)


def test_playlist_and_channels_are_interleaved_and_deduplicated():
    c = Client(playlist=[e("p0", upload_date="20260105"), e("p1", upload_date="20260104"),
                         e("shared", upload_date="20260103")],
               channels={CH_A: [e("a0"), e("shared"), e("a2")], CH_B: [e("b0")]})
    got = ids(c, playlist_url="https://x/p", channels=[CH_A, CH_B])
    assert got == ["p0", "a0", "b0", "p1", "shared", "a2"]  # round robin, "shared" kept once


def test_one_failing_channel_is_skipped_and_the_rest_still_run(caplog):
    c = Client(playlist=[e("p0")], channels={CH_A: YtDlpError("boom"), CH_B: [e("b0")]})
    with caplog.at_level("WARNING"):
        assert ids(c, playlist_url="https://x/p", channels=[CH_A, CH_B]) == ["p0", "b0"]
    assert "cannot list channel" in caplog.text and "boom" in caplog.text


def test_a_rate_limit_stops_listing_further_channels_but_keeps_what_was_listed(caplog):
    c = Client(channels={CH_A: [e("a0")], CH_B: YtDlpRateLimited("429")})
    with caplog.at_level("WARNING"):
        assert ids(c, channels=[CH_A, CH_B, "https://www.youtube.com/@never"]) == ["a0"]
    assert [u for u, _ in c.asked] == [CH_A + "/videos", CH_B + "/videos"]  # the third was never asked


def test_private_placeholders_are_left_out_of_channel_listings():
    c = Client(channels={CH_A: [e("a0", "[Private video]"), e("a1")]})
    assert ids(c, channels=[CH_A]) == ["a1"]


def test_playlist_only_behaviour_is_unchanged():
    c = Client(playlist=[e("p0", upload_date="20260102"), e("p1", upload_date="20260105")])
    assert ids(c, playlist_url="https://x/p") == ["p1", "p0"]  # dated entries sort newest first


def test_channels_must_be_a_list_of_strings():
    for bad in ("https://www.youtube.com/@x", [1], {"a": 1}):
        with pytest.raises(ValueError, match="channels must be a list"):
            ids(Client(), channels=bad)


def test_a_trailing_videos_segment_is_not_doubled():
    c = Client(channels={CH_A: [e("a0")]})
    assert ids(c, channels=[CH_A + "/videos"]) == ["a0"]
    assert c.asked == [(CH_A + "/videos", 5)]


def test_a_failing_playlist_does_not_stop_the_channels_but_alone_it_still_fails():
    class Broken(Client):
        def playlist(self, url):
            raise YtDlpError("playlist down")
    assert ids(Broken(channels={CH_A: [e("a0")]}), playlist_url="https://x/p", channels=[CH_A]) == ["a0"]
    with pytest.raises(YtDlpError, match="playlist down"):
        ids(Broken(), playlist_url="https://x/p")


def test_a_failed_channel_keeps_its_per_video_state(tmp_path):
    import json
    state = tmp_path / "state"
    state.mkdir()
    attempts = state / "youtube-attempts.json"
    attempts.write_text(json.dumps({"a0": 1, "gone": 1}))
    ctx = AdapterContext(config={"playlist_url": "https://x/p", "channels": [CH_A]}, data_dir=tmp_path, persist=True)
    YouTubeAdapter(client=Client(playlist=[e("p0")], channels={CH_A: YtDlpError("boom")})).list_new(ctx)
    assert json.loads(attempts.read_text()) == {"a0": 1, "gone": 1}  # nothing pruned while a list is unreadable
    YouTubeAdapter(client=Client(playlist=[e("p0")], channels={CH_A: [e("a0")]})).list_new(ctx)
    assert json.loads(attempts.read_text()) == {"a0": 1}  # all lists read: the video that left is pruned


def test_a_rate_limited_channel_listing_halts_per_video_yt_dlp_calls():
    a = YouTubeAdapter(client=Client(channels={CH_A: YtDlpRateLimited("429")}))
    a.list_new(AdapterContext(config={"channels": [CH_A]}, persist=False))
    assert a._halted and "rate limited" in a._halted
