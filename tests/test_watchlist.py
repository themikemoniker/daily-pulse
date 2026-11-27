"""`daily-pulse watch add|list|remove`, the watchlist file and its merge into the config."""
import pytest

from daily_pulse import watchlist
from daily_pulse.cli import main
from daily_pulse.config import ConfigError, load_config
from daily_pulse.watchlist import WatchError, parse_target


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise AssertionError("watch must not touch the network")
    monkeypatch.setattr(urllib.request, "urlopen", boom)


@pytest.mark.parametrize("text,source,want", [
    ("https://www.youtube.com/@aiDotEngineer", None, ("youtube", "channels", "https://www.youtube.com/@aiDotEngineer")),
    ("youtube.com/channel/UCabcdefghijklmnopqrstuv", None,
     ("youtube", "channels", "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv")),
    ("https://www.youtube.com/c/SomeName/videos", None, ("youtube", "channels", "https://www.youtube.com/c/SomeName")),
    ("https://github.com/karpathy", None, ("github", "accounts", "karpathy")),
    ("https://github.com/can1357/oh-my-pi/issues/3", None, ("github", "repos", "can1357/oh-my-pi")),
    ("can1357/oh-my-pi", None, ("github", "repos", "can1357/oh-my-pi")),
    ("https://x.com/karpathy/status/1", None, ("x", "usernames", "karpathy")),
    ("https://twitter.com/swyx", None, ("x", "usernames", "swyx")),
    ("https://www.instagram.com/some.one/", None, ("instagram", "accounts", "some.one")),
    ("@swyx", "x", ("x", "usernames", "swyx")),
    ("karpathy", "github", ("github", "accounts", "karpathy")),
    ("aiDotEngineer", "youtube", ("youtube", "channels", "https://www.youtube.com/@aiDotEngineer")),
])
def test_parse_target(text, source, want):
    assert parse_target(text, source) == want


@pytest.mark.parametrize("text,source,msg", [
    ("", None, "give a URL"),
    ("karpathy", None, "--source"),
    ("https://www.youtube.com/watch?v=abc", None, "video or playlist"),
    ("https://youtu.be/abc", None, "cannot tell the platform"),
    ("https://example.com/x", None, "cannot tell the platform"),
    ("https://github.com/karpathy", "x", "does not match"),
    ("bad name!", "x", "not an X handle"),
    ("-bad", "github", "not a GitHub username"),
    ("x", "mastodon", "unknown source"),
    ("https://github.com/", None, "no account"),
])
def test_parse_target_rejects(text, source, msg):
    with pytest.raises(WatchError, match=msg):
        parse_target(text, source)


def run(tmp_path, *argv):
    return main(["watch", *argv[:1], "--watchlist", str(tmp_path / "w.toml"), *argv[1:]])


def test_add_list_remove_roundtrip(tmp_path, capsys):
    assert run(tmp_path, "add", "https://www.youtube.com/@aiDotEngineer") == 0
    assert run(tmp_path, "add", "swyx", "--source", "x") == 0
    assert run(tmp_path, "add", "can1357/oh-my-pi") == 0
    out = capsys.readouterr().out
    assert "watching youtube" in out and "paid and off" in out
    assert run(tmp_path, "list") == 0
    listed = capsys.readouterr().out
    assert "swyx" in listed and "oh-my-pi  [repo]" in listed and "3 watched" in listed
    assert run(tmp_path, "remove", "swyx", "--source", "x") == 0
    assert run(tmp_path, "remove", "swyx", "--source", "x") == 1  # no longer there
    assert "swyx" not in watchlist.load(tmp_path / "w.toml")["x"]["usernames"]


def test_adding_twice_is_idempotent_and_case_insensitive(tmp_path, capsys):
    assert run(tmp_path, "add", "Karpathy", "--source", "github") == 0
    assert run(tmp_path, "add", "karpathy", "--source", "github") == 0
    assert "already watched" in capsys.readouterr().out
    assert watchlist.load(tmp_path / "w.toml")["github"]["accounts"] == ["Karpathy"]


def test_bad_input_exits_two_and_writes_nothing(tmp_path, capsys):
    assert run(tmp_path, "add", "karpathy") == 2
    assert "--source" in capsys.readouterr().err
    assert not (tmp_path / "w.toml").exists()


def test_file_is_deterministic_and_round_trips(tmp_path):
    p = tmp_path / "w.toml"
    watchlist.add(p, "github", "accounts", "a")
    watchlist.add(p, "youtube", "channels", "https://www.youtube.com/@x_y")
    first = p.read_text()
    watchlist.save(p, watchlist.load(p))
    assert p.read_text() == first and first.startswith("# Creators to watch")
    assert not list(tmp_path.glob("*.tmp"))


def test_malformed_watchlist_is_a_clear_error(tmp_path):
    p = tmp_path / "w.toml"
    p.write_text('[github]\naccounts = "nope"\n')
    with pytest.raises(WatchError, match="list of strings"):
        watchlist.load(p)
    p.write_text("[bogus]\n")
    with pytest.raises(WatchError, match="unknown table"):
        watchlist.load(p)


def write_config(tmp_path, extra=""):
    cfg = tmp_path / "daily-pulse.toml"
    cfg.write_text('[sources.github]\nenabled = true\nwatch_accounts = ["Karpathy", "simonw"]\n' + extra)
    return cfg


def test_load_config_merges_the_watchlist_without_duplicates(tmp_path):
    cfg = write_config(tmp_path)
    (tmp_path / "watchlist.toml").write_text(
        '[github]\naccounts = ["karpathy", "mattpocock"]\nrepos = ["a/b"]\n'
        '[youtube]\nchannels = ["https://www.youtube.com/@x_y"]\n[x]\nusernames = ["swyx"]\n')
    s = load_config(cfg).sources
    assert s["github"]["watch_accounts"] == ["Karpathy", "simonw", "mattpocock"]
    assert s["github"]["watch_repos"] == ["a/b"]
    assert s["youtube"]["channels"] == ["https://www.youtube.com/@x_y"]
    assert s["x"]["usernames"] == ["swyx"] and s["x"]["enabled"] is False  # a watched handle never turns a source on
    assert s["youtube"]["enabled"] is False


def test_missing_watchlist_changes_nothing_and_malformed_is_a_config_error(tmp_path):
    cfg = write_config(tmp_path)
    assert load_config(cfg).sources["github"]["watch_accounts"] == ["Karpathy", "simonw"]
    (tmp_path / "watchlist.toml").write_text("[github\n")
    with pytest.raises(ConfigError, match="watchlist.toml"):
        load_config(cfg)
