"""scripts/ig-export.py against a fake `instagram-cli`: shape, paging, reels only, no id in output."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ig-export.py"
FAKE = '''#!{python}
import json, os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(a) + "\\n")
if os.environ.get("FAKE_FAIL"):
    sys.stderr.write("boom")
    sys.exit(3)
data = json.load(open(os.environ["FAKE_DATA"]))[a[0]]
after = a[a.index("--after") + 1] if "--after" in a else "0"
page = data[after]
print(json.dumps(page))
'''
ACCOUNT = "17841400000000000"


def post(code, kind="reel", **extra):
    return {"media_id": code, "post_type": kind, "media_permalink": f"https://www.instagram.com/reel/{code}/?igsh=x",
            "post_caption_text": f"caption {code}", "post_creation_time": "2026-01-14T10:00:00Z",
            "author_info": {"username": "someone"}, **extra}


@pytest.fixture
def run(tmp_path):
    cli = tmp_path / "instagram-cli"
    cli.write_text(FAKE.format(python=sys.executable))
    cli.chmod(0o755)

    def go(data, extra_env=None, account=ACCOUNT):
        (tmp_path / "data.json").write_text(json.dumps(data))
        env = {**os.environ, "INSTAGRAM_CLI": str(cli), "FAKE_DATA": str(tmp_path / "data.json"),
               "FAKE_LOG": str(tmp_path / "log"), **(extra_env or {})}
        env.pop("IG_ACCOUNT_ID", None)
        if account is not None:
            env["IG_ACCOUNT_ID"] = account
        proc = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        log = [json.loads(l) for l in (tmp_path / "log").read_text().splitlines()] if (tmp_path / "log").exists() else []
        return proc, log
    return go


def test_prints_adapter_shaped_entries_saved_before_liked(run):
    data = {"saved-posts": {"0": {"posts": [post("S1", like_count=7, comment_count=2)], "has_next_page": False}},
            "recently-liked-posts": {"0": {"posts": [post("L1")], "has_next_page": False}}}
    proc, _ = run(data)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert [(e["shortcode"], e["source"]) for e in out] == [("S1", "saved"), ("L1", "liked")]
    assert out[0] == {"shortcode": "S1", "url": "https://www.instagram.com/reel/S1/", "source": "saved",
                      "author": "someone", "caption": "caption S1", "timestamp": "2026-01-14T10:00:00Z",
                      "like_count": 7, "comment_count": 2}


def test_pages_until_the_limit_and_stops_on_a_repeated_cursor(run):
    pages = {"0": {"posts": [post("S1"), post("S2")], "has_next_page": True, "end_cursor": "c1"},
             "c1": {"posts": [post("S3")], "has_next_page": True, "end_cursor": "c1"}}
    proc, log = run({"saved-posts": pages, "recently-liked-posts": {"0": {"posts": []}}})
    assert [e["shortcode"] for e in json.loads(proc.stdout)] == ["S1", "S2", "S3"]
    assert sum(1 for a in log if a[0] == "saved-posts") == 2  # the repeated cursor ended the loop
    proc, _ = run({"saved-posts": pages, "recently-liked-posts": {"0": {"posts": []}}}, {"IG_EXPORT_LIMIT": "2"})
    assert [e["shortcode"] for e in json.loads(proc.stdout)] == ["S1", "S2"]


def test_only_reels_with_a_usable_link_are_kept_and_epoch_times_become_iso(run):
    bad = post("X")
    bad["media_permalink"] = "https://example.com/nope"
    data = {"saved-posts": {"0": {"posts": [post("P1", kind="post"), bad, post("R1", post_creation_time=1768384800)]}},
            "recently-liked-posts": {"0": {"posts": []}}}
    out = json.loads(run(data)[0].stdout)
    assert [e["shortcode"] for e in out] == ["R1"] and out[0]["timestamp"] == "2026-01-14T10:00:00Z"


def test_the_account_id_is_required_and_never_printed(run):
    proc, _ = run({}, account=None)
    assert proc.returncode == 1 and "IG_ACCOUNT_ID is not set" in proc.stderr
    proc, _ = run({"saved-posts": {"0": {"posts": []}}, "recently-liked-posts": {"0": {"posts": []}}})
    assert ACCOUNT not in proc.stdout + proc.stderr


def test_failures_exit_nonzero_with_a_short_message_and_no_account_id(run, tmp_path):
    proc, _ = run({}, {"FAKE_FAIL": "1"})
    assert proc.returncode == 1 and "saved-posts failed (exit 3)" in proc.stderr and ACCOUNT not in proc.stderr
    proc, _ = run({}, {"INSTAGRAM_CLI": str(tmp_path / "missing-cli")})
    assert proc.returncode == 1 and "missing-cli not found" in proc.stderr and str(tmp_path) not in proc.stderr
    proc, _ = run({}, {"IG_EXPORT_LIMIT": "0"})
    assert proc.returncode == 1 and "IG_EXPORT_LIMIT" in proc.stderr


def test_the_output_satisfies_the_adapter(run, tmp_path, monkeypatch):
    from daily_pulse.adapters.base import AdapterContext
    from daily_pulse.adapters.instagram import InstagramAdapter

    data = {"saved-posts": {"0": {"posts": [post("S1")]}}, "recently-liked-posts": {"0": {"posts": [post("L1")]}}}
    proc, _ = run(data)
    shim = tmp_path / "shim"
    shim.write_text(f"#!/bin/sh\ncat <<'EOF'\n{proc.stdout}EOF\n")
    shim.chmod(0o755)
    monkeypatch.setenv("IG_CLI_CMD", str(shim))
    items = InstagramAdapter().list_new(AdapterContext(config={}))
    assert [i.id for i in items] == ["S1", "L1"]


def test_a_reel_both_saved_and_liked_is_exported_once_saved_wins(run):
    data = {"saved-posts": {"0": {"posts": [post("DUP")]}}, "recently-liked-posts": {"0": {"posts": [post("DUP"), post("L2")]}}}
    out = json.loads(run(data)[0].stdout)
    assert [(e["shortcode"], e["source"]) for e in out] == [("DUP", "saved"), ("L2", "liked")]


def test_the_limit_counts_reels_not_photos(run):
    photos = [post(f"P{i}", kind="post") for i in range(25)]
    data = {"saved-posts": {"0": {"posts": photos, "has_next_page": True, "end_cursor": "c1"},
                            "c1": {"posts": [post("R1"), post("R2")], "has_next_page": False}},
            "recently-liked-posts": {"0": {"posts": []}}}
    out = json.loads(run(data, {"IG_EXPORT_LIMIT": "2"})[0].stdout)
    assert [e["shortcode"] for e in out] == ["R1", "R2"]  # twenty-five photos did not use the limit up


def test_a_millisecond_or_absurd_timestamp_never_aborts_the_export(run):
    data = {"saved-posts": {"0": {"posts": [post("R1", post_creation_time=1768384800000),
                                            post("R2", post_creation_time=1e300)]}},
            "recently-liked-posts": {"0": {"posts": []}}}
    proc, _ = run(data)
    out = json.loads(proc.stdout)
    assert out[0]["timestamp"] == "2026-01-14T10:00:00Z" and "timestamp" not in out[1]


def test_a_changed_response_shape_is_an_error_not_an_empty_export(run):
    proc, _ = run({"saved-posts": {"0": {"items": []}}, "recently-liked-posts": {"0": {"posts": []}}})
    assert proc.returncode == 1 and "no posts list" in proc.stderr


def test_a_post_without_a_type_is_not_assumed_to_be_a_reel(run):
    untyped = post("U1")
    del untyped["post_type"]
    data = {"saved-posts": {"0": {"posts": [untyped]}}, "recently-liked-posts": {"0": {"posts": []}}}
    assert json.loads(run(data)[0].stdout) == []


def test_connector_stderr_that_echoes_the_account_id_is_scrubbed(run, tmp_path):
    cli = tmp_path / "instagram-cli"
    cli.write_text(f"#!/bin/sh\necho 'no access to account {ACCOUNT}' >&2\nexit 1\n")
    proc, _ = run({}, {"INSTAGRAM_CLI": str(cli)})
    assert proc.returncode == 1 and ACCOUNT not in proc.stderr and "<account>" in proc.stderr
