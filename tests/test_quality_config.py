from pathlib import Path

import pytest

from daily_pulse.config import ConfigError, load_config

SHIPPED = Path(__file__).parent.parent / "config" / "daily-pulse.toml"


def write(tmp_path, quality=""):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndata_dir = "{tmp_path / "data"}"\n\n{quality}')
    return cfg


def test_quality_defaults_when_the_table_is_missing(tmp_path):
    q = load_config(write(tmp_path)).quality
    assert (q.min_rating, q.min_duration_seconds, q.max_duration_seconds) == (5, 240, 7200)
    assert q.channel_allowlist == [] and q.channel_blocklist == [] and q.title_blocklist == []
    assert q.max_triage_calls == 10


def test_quality_values_are_read_from_the_config(tmp_path):
    cfg = write(tmp_path, '[quality]\nmin_rating = 7\nmin_duration_seconds = 60\nmax_duration_seconds = 600\n'
                          'channel_allowlist = ["Good"]\nchannel_blocklist = ["Bad"]\n'
                          'title_blocklist = ["Clickbait"]\nmax_triage_calls = 3\n')
    q = load_config(cfg).quality
    assert (q.min_rating, q.min_duration_seconds, q.max_duration_seconds) == (7, 60, 600)
    assert (q.channel_allowlist, q.channel_blocklist, q.title_blocklist) == (["Good"], ["Bad"], ["Clickbait"])
    assert q.max_triage_calls == 3


@pytest.mark.parametrize("body", [
    "min_rating = 11", "min_rating = -1", 'min_rating = "5"', "min_duration_seconds = -1",
    "min_duration_seconds = 500\nmax_duration_seconds = 100", 'channel_allowlist = "x"',
    "title_blocklist = [1]", "max_triage_calls = -1",
])
def test_bad_quality_values_are_config_errors(tmp_path, body):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, f"[quality]\n{body}\n"))


def test_the_shipped_config_has_the_documented_quality_defaults_and_the_playlist():
    cfg = load_config(SHIPPED)
    q = cfg.quality
    assert (q.min_rating, q.min_duration_seconds, q.max_duration_seconds) == (5, 240, 7200)
    yt = cfg.sources["youtube"]
    assert yt["playlist_url"] == "https://www.youtube.com/playlist?list=PLOsrHd23kLUo"
    assert yt["playlist_order"] == "oldest_first"
