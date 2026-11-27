import pytest

from daily_pulse.models import Item
from daily_pulse.summarizer import Summarizer


def good(**over):
    reply = {
        "summary": "A tool that wraps agent runs in a maker-checker loop.",
        "takeaways": [],
        "beats": [],
        "rating": 8,
        "reason": "Directly about maker-checker loops for agents.",
        "verdict": "watch",
        "tags": ["maker-checker", "evals"],
        "diamond": True,
        "diamond_reason": "Early, few stars, no hype.",
    }
    reply.update(over)
    return reply


def repo_item(**over):
    base = dict(source="github", id="acme-checker", url="https://github.com/acme/checker",
                title="acme/checker", author="acme", text="Maker-checker loops for agents.")
    base.update(over)
    return Item(**base)


def test_rates_an_item_through_the_claude_binary(fake_claude):
    claude = fake_claude(good())
    rating = Summarizer(claude_cmd=str(claude.path)).rate(repo_item())
    assert rating.rating == 8
    assert rating.verdict == "watch"
    assert rating.reason == "Directly about maker-checker loops for agents."
    assert rating.tags == ["maker-checker", "evals"]
    assert rating.summary.startswith("A tool that wraps")
    assert rating.diamond is True
    assert rating.diamond_reason == "Early, few stars, no hype."
    assert claude.calls == 1
    assert "-p" in claude.argv(0)


RUBRIC_TOPICS = [
    "harness engineering",
    "multi-agent orchestration",
    "evals",
    "maker-checker loops",
    "model routing and cost",
    "guard rails for accounting-firm AI consulting",
]


def test_the_shipped_rubric_names_every_topic():
    from daily_pulse.summarizer import DEFAULT_PROMPTS_DIR

    rubric = (DEFAULT_PROMPTS_DIR / "rubric.md").read_text()
    for topic in RUBRIC_TOPICS:
        assert topic in rubric


def test_rubric_text_is_in_every_call_and_includes_the_item(fake_claude):
    claude = fake_claude("not json", good())
    Summarizer(claude_cmd=str(claude.path)).rate(repo_item(title="acme/checker"))
    from daily_pulse.summarizer import DEFAULT_PROMPTS_DIR

    rubric = (DEFAULT_PROMPTS_DIR / "rubric.md").read_text().strip()
    assert claude.calls == 2
    for n in range(2):
        assert rubric in claude.stdin(n)
        assert "acme/checker" in claude.stdin(n)


def test_prompts_are_read_from_disk_at_call_time(fake_claude, tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "rubric.md").write_text("RUBRIC-V1")
    (prompts / "summarize.md").write_text("DO IT\n{{ITEM}}")
    claude = fake_claude(good())
    s = Summarizer(claude_cmd=str(claude.path), prompts_dir=prompts)
    s.rate(repo_item())
    (prompts / "rubric.md").write_text("RUBRIC-V2")
    s.rate(repo_item())
    assert "RUBRIC-V1" in claude.stdin(0)
    assert "RUBRIC-V2" in claude.stdin(1)


def test_malformed_json_is_retried_once_then_succeeds(fake_claude):
    claude = fake_claude("Sure! here is my rating", good(rating=6))
    rating = Summarizer(claude_cmd=str(claude.path)).rate(repo_item())
    assert rating.rating == 6
    assert claude.calls == 2


def test_malformed_twice_skips_the_item_and_logs(fake_claude, caplog):
    claude = fake_claude("nope", "still nope")
    with caplog.at_level("WARNING", logger="daily_pulse"):
        rating = Summarizer(claude_cmd=str(claude.path)).rate(repo_item())
    assert rating is None
    assert claude.calls == 2  # one retry, no more
    assert "github-acme-checker" in caplog.text


@pytest.mark.parametrize("bad", [0, 11, -3, 7.5, "8", True, None])
def test_ratings_outside_1_to_10_are_rejected(fake_claude, bad):
    claude = fake_claude(good(rating=bad))
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None
    assert claude.calls == 2


@pytest.mark.parametrize("bad", ["maybe", "WATCH", "", None, 1])
def test_verdict_must_be_watch_skim_or_skip(fake_claude, bad):
    claude = fake_claude(good(verdict=bad))
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None


@pytest.mark.parametrize("verdict", ["watch", "skim", "skip"])
def test_each_valid_verdict_is_accepted(fake_claude, verdict):
    claude = fake_claude(good(verdict=verdict))
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()).verdict == verdict


@pytest.mark.parametrize("field,bad", [
    ("reason", ""), ("reason", 5), ("tags", "evals"), ("tags", [1]), ("diamond", "yes"),
    ("diamond", None), ("summary", ""), ("takeaways", "a"), ("beats", [{"timestamp": "x", "text": "t"}]),
    ("beats", ["no"]), ("diamond_reason", 3),
])
def test_other_malformed_fields_are_rejected(fake_claude, field, bad):
    claude = fake_claude(good(**{field: bad}))
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None


def test_a_diamond_needs_a_reason(fake_claude):
    claude = fake_claude(good(diamond=True, diamond_reason=""))
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None


def test_a_json_array_or_missing_key_is_malformed(fake_claude):
    claude = fake_claude([1, 2])
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None
    reply = good()
    del reply["verdict"]
    claude = fake_claude(reply)
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None


def test_a_reply_wrapped_in_a_code_fence_is_accepted(fake_claude):
    import json

    claude = fake_claude("```json\n" + json.dumps(good()) + "\n```")
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()).rating == 8
    assert claude.calls == 1


def test_video_beats_and_takeaways_come_through(fake_claude):
    reply = good(summary="", takeaways=["Route cheap tasks to small models."],
                 beats=[{"timestamp": "05:30", "text": "Introduces the router."}])
    claude = fake_claude(reply)
    item = repo_item(source="youtube", id="vid1", title="Routing talk")
    rating = Summarizer(claude_cmd=str(claude.path)).rate(item)
    assert rating.takeaways == ["Route cheap tasks to small models."]
    assert [(b.timestamp, b.text) for b in rating.beats] == [("05:30", "Introduces the router.")]


def test_a_non_video_item_needs_a_summary_but_a_video_does_not(fake_claude):
    claude = fake_claude(good(summary=""))
    video = repo_item(source="youtube", id="v", title="t")
    assert Summarizer(claude_cmd=str(claude.path)).rate(video) is not None


def test_the_call_cap_is_enforced_across_items(fake_claude, caplog):
    claude = fake_claude(good())
    s = Summarizer(claude_cmd=str(claude.path), max_calls=3)
    ratings = [s.rate(repo_item(id=f"r{i}")) for i in range(5)]
    assert [r is not None for r in ratings] == [True, True, True, False, False]
    assert claude.calls == 3
    assert s.calls_made == 3


def test_retries_count_against_the_cap(fake_claude):
    claude = fake_claude("bad", good())
    s = Summarizer(claude_cmd=str(claude.path), max_calls=1)
    assert s.rate(repo_item()) is None  # the retry would be call 2
    assert claude.calls == 1
    assert s.rate(repo_item(id="other")) is None
    assert claude.calls == 1


def test_a_zero_cap_makes_no_calls(fake_claude):
    claude = fake_claude(good())
    assert Summarizer(claude_cmd=str(claude.path), max_calls=0).rate(repo_item()) is None
    assert claude.calls == 0


def test_default_cap_is_30(fake_claude):
    claude = fake_claude(good())
    s = Summarizer(claude_cmd=str(claude.path))
    assert s.max_calls == 30
    for i in range(32):
        s.rate(repo_item(id=f"r{i}"))
    assert claude.calls == 30


def test_a_failing_process_leaves_the_item_unrated_without_raising(fake_claude, caplog):
    claude = fake_claude({"exit": 1}, good())
    with caplog.at_level("WARNING", logger="daily_pulse"):
        rating = Summarizer(claude_cmd=str(claude.path)).rate(repo_item())
    assert rating.rating == 8  # the retry worked
    claude = fake_claude({"exit": 1})
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None


def test_a_missing_binary_leaves_the_item_unrated(tmp_path, caplog):
    s = Summarizer(claude_cmd=str(tmp_path / "no-such-claude"))
    with caplog.at_level("WARNING", logger="daily_pulse"):
        assert s.rate(repo_item()) is None
    assert "github-acme-checker" in caplog.text


def test_a_hung_process_times_out_as_unrated(tmp_path):
    script = tmp_path / "slow"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    s = Summarizer(claude_cmd=str(script), timeout=0.2, max_calls=2)
    assert s.rate(repo_item()) is None


TRANSCRIPT = " ".join(f"word{i}" for i in range(200))


def _all_text(r):
    return "\n".join([r.summary, r.reason, r.diamond_reason, *r.takeaways, *r.tags, *r.quotes,
                      *(b.text for b in r.beats)])


def test_transcript_is_sent_to_claude_but_not_returned(fake_claude):
    from daily_pulse.quoteguard import MAX_COPY_RUN, longest_copied_run

    lifted = " ".join(f"word{i}" for i in range(10, 110))  # 100 words copied verbatim
    claude = fake_claude(good(
        summary=lifted, takeaways=[lifted], beats=[{"timestamp": "00:10", "text": lifted}],
        reason=lifted, quotes=[" ".join(f"word{i}" for i in range(150, 190))],
    ))
    item = repo_item(source="youtube", id="v1", title="Talk")
    rating = Summarizer(claude_cmd=str(claude.path)).rate(item, transcript=TRANSCRIPT)
    assert "word199" in claude.stdin(0)  # the model sees it
    assert longest_copied_run(_all_text(rating), TRANSCRIPT) <= MAX_COPY_RUN
    assert longest_copied_run(rating.summary, TRANSCRIPT) < 25
    for quote in rating.quotes:
        assert len(quote.split()) <= 26  # 25 words plus the ellipsis marker


def test_quoted_words_across_the_result_stay_under_50(fake_claude):
    from daily_pulse.quoteguard import word_count

    q = " ".join(f"word{i}" for i in range(0, 20))
    q2 = " ".join(f"word{i}" for i in range(50, 70))
    q3 = " ".join(f"word{i}" for i in range(100, 120))
    claude = fake_claude(good(quotes=[q, q2, q3]))
    rating = Summarizer(claude_cmd=str(claude.path)).rate(repo_item(), transcript=TRANSCRIPT)
    assert sum(word_count(q) for q in rating.quotes) < 50


def test_own_words_pass_through_unchanged(fake_claude):
    claude = fake_claude(good(quotes=["a short quote"]))
    rating = Summarizer(claude_cmd=str(claude.path)).rate(repo_item(), transcript=TRANSCRIPT)
    assert rating.summary == "A tool that wraps agent runs in a maker-checker loop."
    assert rating.quotes == ["a short quote"]


def test_item_excerpt_counts_as_source_text(fake_claude):
    from daily_pulse.quoteguard import MAX_COPY_RUN, longest_copied_run

    excerpt = " ".join(f"tok{i}" for i in range(120))
    claude = fake_claude(good(summary=excerpt))
    rating = Summarizer(claude_cmd=str(claude.path)).rate(repo_item(text=excerpt))
    assert longest_copied_run(rating.summary, excerpt) <= MAX_COPY_RUN


def _cfg(tmp_path, body=""):
    from daily_pulse.config import load_config

    path = tmp_path / "c.toml"
    path.write_text(body)
    return load_config(path)


def test_config_defaults_for_the_summarizer(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.max_claude_calls == 30
    assert cfg.claude_cmd == "claude"


def test_config_sets_the_binary_and_the_cap(tmp_path):
    cfg = _cfg(tmp_path, '[summarizer]\nclaude_cmd = "/opt/claude"\nmax_calls = 5\n')
    assert (cfg.claude_cmd, cfg.max_claude_calls) == ("/opt/claude", 5)


@pytest.mark.parametrize("body", [
    "[summarizer]\nmax_calls = -1\n", '[summarizer]\nmax_calls = "ten"\n',
    "[summarizer]\nclaude_cmd = 3\n", '[summarizer]\nclaude_cmd = ""\n', "summarizer = 3\n",
])
def test_bad_summarizer_config_is_rejected(tmp_path, body):
    from daily_pulse.config import ConfigError

    with pytest.raises(ConfigError):
        _cfg(tmp_path, body)


def test_summarizer_built_from_config_and_env_override(tmp_path, fake_claude):
    cfg = _cfg(tmp_path, '[summarizer]\nclaude_cmd = "/opt/claude"\nmax_calls = 5\n')
    s = Summarizer.from_config(cfg, env={})
    assert (s.claude_cmd, s.max_calls) == ("/opt/claude", 5)
    claude = fake_claude(good())
    s = Summarizer.from_config(cfg, env={"DAILY_PULSE_CLAUDE": str(claude.path)})
    assert s.claude_cmd == str(claude.path)
    assert s.rate(repo_item()) is not None
    assert Summarizer.from_config(cfg, env={"DAILY_PULSE_CLAUDE": "  "}).claude_cmd == "/opt/claude"


def test_summarize_items_writes_notes_and_carries_on_past_failures(fake_claude, tmp_path):
    from daily_pulse.summarizer import summarize_items

    claude = fake_claude(good(), "garbage", "garbage", good(rating=3, verdict="skip", diamond=False,
                                                          diamond_reason=""))
    items = [repo_item(id="a"), repo_item(id="b"), repo_item(id="c")]
    results = summarize_items(Summarizer(claude_cmd=str(claude.path)), items, tmp_path / "notes",
                              "2026-01-02")
    assert [r.item.id for r in results] == ["a", "b", "c"]
    assert [r.status for r in results] == ["rated", "unrated", "rated"]
    assert results[1].rating is None and results[1].path is None
    assert results[0].path == tmp_path / "notes" / "2026-01-02" / "github-a.md"
    assert "**8/10**" in results[0].path.read_text()
    assert "**3/10**" in results[2].path.read_text()
    assert not (tmp_path / "notes" / "2026-01-02" / "github-b.md").exists()


def test_summarize_items_passes_transcripts_but_notes_never_contain_them(fake_claude, tmp_path):
    from daily_pulse.summarizer import summarize_items

    lifted = " ".join(f"word{i}" for i in range(10, 110))
    claude = fake_claude(good(summary="", takeaways=[lifted], reason="Routing, in brief."))
    item = repo_item(source="youtube", id="v1", title="Talk")
    [res] = summarize_items(Summarizer(claude_cmd=str(claude.path)), [item], tmp_path, "2026-01-02",
                            transcripts={"v1": TRANSCRIPT})
    assert "word199" in claude.stdin(0)
    from daily_pulse.quoteguard import MAX_COPY_RUN, longest_copied_run

    assert longest_copied_run(res.path.read_text(), TRANSCRIPT) <= MAX_COPY_RUN


def test_summarize_items_marks_an_item_unrated_when_its_note_cannot_be_written(fake_claude, tmp_path):
    from daily_pulse.summarizer import summarize_items

    claude = fake_claude(good())
    items = [repo_item(id="a", title="  "), repo_item(id="b")]  # the first has no title: NoteError
    results = summarize_items(Summarizer(claude_cmd=str(claude.path)), items, tmp_path, "2026-01-02")
    assert [r.status for r in results] == ["unrated", "rated"]


def test_summarize_items_is_idempotent(fake_claude, tmp_path):
    from daily_pulse.summarizer import summarize_items

    claude = fake_claude(good())
    s = lambda: summarize_items(Summarizer(claude_cmd=str(claude.path)), [repo_item()], tmp_path, "2026-01-02")
    first = s()[0].path.read_text()
    assert s()[0].path.read_text() == first


def test_once_the_cap_is_hit_remaining_items_are_unrated_not_crashes(fake_claude, tmp_path):
    from daily_pulse.summarizer import summarize_items

    claude = fake_claude(good())
    items = [repo_item(id=f"r{i}") for i in range(4)]
    results = summarize_items(Summarizer(claude_cmd=str(claude.path), max_calls=2), items, tmp_path,
                              "2026-01-02")
    assert [r.status for r in results] == ["rated", "rated", "unrated", "unrated"]
    assert claude.calls == 2


def test_a_binary_that_cannot_start_is_not_retried_or_counted(tmp_path):
    s = Summarizer(claude_cmd=str(tmp_path / "no-such-claude"), max_calls=5)
    assert s.rate(repo_item()) is None
    assert s.calls_made == 0


def test_non_utf8_output_counts_against_the_cap_and_is_retried(tmp_path):
    script = tmp_path / "binary-out"
    script.write_text("#!/bin/sh\ncat >/dev/null\nprintf '\\377\\376'\n")
    script.chmod(0o755)
    s = Summarizer(claude_cmd=str(script), max_calls=5)
    assert s.rate(repo_item()) is None
    assert s.calls_made == 2


def test_a_timeout_counts_as_a_call(tmp_path):
    script = tmp_path / "slow"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    s = Summarizer(claude_cmd=str(script), timeout=0.2, max_calls=5)
    s.rate(repo_item())
    assert s.calls_made == 2


def test_absurdly_nested_json_is_just_a_bad_reply(fake_claude):
    claude = fake_claude("[" * 200000)
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is None
    assert claude.calls == 2


def test_an_unserializable_item_does_not_kill_the_batch(fake_claude, tmp_path):
    import datetime

    from daily_pulse.summarizer import summarize_items

    claude = fake_claude(good())
    items = [repo_item(id="a", metrics={"when": datetime.datetime.now()}), repo_item(id="b")]
    results = summarize_items(Summarizer(claude_cmd=str(claude.path)), items, tmp_path, "2026-01-02")
    assert [r.status for r in results] == ["unrated", "rated"]


def test_an_unreadable_prompt_file_leaves_items_unrated(fake_claude, tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "rubric.md").write_bytes(b"\xff\xfe")
    (prompts / "summarize.md").write_text("{{ITEM}}")
    claude = fake_claude(good())
    s = Summarizer(claude_cmd=str(claude.path), prompts_dir=prompts)
    assert s.rate(repo_item()) is None
    assert claude.calls == 0


@pytest.mark.parametrize("field,bad", [
    ("beats", [{"timestamp": "99:99", "text": "t"}]),
    ("beats", [{"timestamp": "05:30", "text": "  "}]),
    ("tags", [""]),
    ("tags", ["  "]),
])
def test_beats_and_tags_are_checked_so_bad_ones_get_the_retry(fake_claude, field, bad):
    claude = fake_claude(good(**{field: bad}), good())
    assert Summarizer(claude_cmd=str(claude.path)).rate(repo_item()) is not None
    assert claude.calls == 2
