import re

from daily_pulse.quoteguard import QuoteBudget, guard_text, longest_copied_run

# 80 distinct words so no accidental repeats.
TRANSCRIPT = " ".join(f"word{i}" for i in range(80))
# VTT-cleaned transcripts keep timestamps between lines.
TIMESTAMPED = "[00:00:01] " + " ".join(f"word{i}" for i in range(40)) + "\n[00:00:20] " + \
    " ".join(f"word{i}" for i in range(40, 80))


def test_summary_copying_sixty_words_is_truncated_below_the_limit():
    copied = " ".join(f"word{i}" for i in range(10, 70))
    summary = f"The speaker argues {copied} and then moves on."

    out = guard_text(summary, TRANSCRIPT)

    assert longest_copied_run(out, TRANSCRIPT) <= 40
    assert out.startswith("The speaker argues")
    assert out.endswith("and then moves on.")
    assert "word69" not in out


def test_copying_is_caught_even_when_transcript_has_timestamps_and_odd_casing():
    copied = " ".join(f"WORD{i}," for i in range(0, 60))
    out = guard_text(copied, TIMESTAMPED)
    assert longest_copied_run(out, TIMESTAMPED) <= 40


def test_short_overlap_with_the_transcript_is_left_alone():
    summary = "He says word3 word4 word5 is the key idea, in my words."
    assert guard_text(summary, TRANSCRIPT) == summary


def test_own_words_text_is_unchanged_and_no_reference_is_a_no_op():
    assert guard_text("Totally original summary.", TRANSCRIPT) == "Totally original summary."
    assert guard_text("anything at all " * 30, "") == "anything at all " * 30


def test_quoted_spans_in_free_text_draw_on_the_shared_budget():
    budget = QuoteBudget(limit=10)
    first = guard_text('He says "one two three four five six" early on.', "", budget)
    second = guard_text('Later: "seven eight nine ten eleven twelve".', "", budget)

    quoted = [len(q.split()) for q in re.findall(r'"([^"]*)"', first + " " + second)]
    assert sum(quoted) <= 10
    assert budget.used <= 10
