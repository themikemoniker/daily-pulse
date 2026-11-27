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
