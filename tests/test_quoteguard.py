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


# --- review round: bypasses of the copy detector -------------------------------------------

def _plain(n):
    return " ".join(f"word{i}" for i in range(n))


def test_curly_apostrophes_do_not_defeat_the_copy_guard():
    ref = " ".join(f"don't{i} w{i}" for i in range(40))
    copy = ref.replace("'", "’")
    assert longest_copied_run(copy, ref) == 80
    assert longest_copied_run(guard_text(copy, ref), ref) <= 40


def test_markdown_emphasis_and_zero_width_chars_do_not_defeat_the_copy_guard():
    copy = " ".join(f"_word{i}_" if i % 10 == 0 else f"wo​rd{i}" for i in range(60))
    assert longest_copied_run(copy, _plain(80)) >= 60
    assert longest_copied_run(guard_text(copy, _plain(80)), _plain(80)) <= 40


def test_timestamps_inside_the_copy_do_not_break_the_run():
    copy = " ".join((f"[00:{i:02d}] " if i % 10 == 0 else "") + f"word{i}" for i in range(60))
    assert longest_copied_run(copy, _plain(80)) == 60


def test_srt_and_vtt_markup_in_the_reference_does_not_break_the_run():
    srt = "\n".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i+1:02d},000\nword{i}" for i in range(60))
    assert longest_copied_run(_plain(60), srt) == 60
    vtt = " ".join(f"<00:00:01.200><c> word{i}</c>" for i in range(60))
    assert longest_copied_run(_plain(60), vtt) == 60


def test_quoted_words_are_counted_as_words_not_whitespace_tokens():
    hyphenated = " ".join("-".join(f"k{j}p{i}" for i in range(10)) for j in range(25))  # 250 words
    budget = QuoteBudget()
    out = budget.fit(hyphenated)
    assert len(re.findall(r"\w+", out)) <= 49
    text = guard_text(f'He says "{hyphenated}".', "", QuoteBudget())
    assert len(re.findall(r"\w+", text)) <= 49 + 3


def test_guillemet_and_low_double_quotes_count_as_quoted_spans():
    spans = guard_text(f"A «{_plain(60)}» and B „{_plain(60)}“.", "", QuoteBudget())
    assert len(re.findall(r"\bword\d+", spans)) <= 49


def test_short_lifted_runs_spend_the_budget_too():
    ref = _plain(500)
    pieces = [" ".join(f"word{i}" for i in range(s, s + 9)) for s in range(0, 450, 10)]  # 9-word lifts
    budget = QuoteBudget()
    out = " / ".join(guard_text(p, ref, budget) for p in pieces)
    from daily_pulse.quoteguard import copied_word_count
    assert copied_word_count(out, ref, min_run=5) <= 49


# --- PR review: angle brackets, unspaced scripts ---------------------------------------------

def test_copy_wrapped_in_angle_brackets_is_still_caught():
    wrapped = "<" + _plain(60) + ">"
    assert longest_copied_run(wrapped, _plain(80)) == 60
    assert longest_copied_run(guard_text(wrapped, _plain(80)), _plain(80)) <= 40


def test_cjk_text_is_counted_and_limited_per_character():
    ref = "".join(chr(0x4E00 + i) for i in range(200))  # 200 CJK characters, no spaces
    copy = "要点是" + ref[10:110]
    assert longest_copied_run(guard_text(copy, ref), ref) <= 40
    assert len(re.findall(r"[一-鿿]", QuoteBudget().fit(ref))) <= 49
