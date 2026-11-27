from pathlib import Path

from daily_pulse.adapters.youtube import clean_vtt

YT = Path(__file__).parent / "fixtures" / "youtube"


def test_rolling_auto_captions_lose_duplicates_and_markup_but_keep_timestamps():
    cleaned = clean_vtt((YT / "rolling-auto.en.vtt").read_text())
    assert cleaned.splitlines() == [
        "[00:00] welcome to the harness talk",
        "[00:03] today we look at evals",
        "[00:06] a checker & maker loop > single agent",
        "[01:05] routing cheap models",
        "[01:05] [Music]",
    ]


def test_manual_subtitles_skip_headers_notes_and_styles_and_strip_inline_tags():
    cleaned = clean_vtt((YT / "manual.en.vtt").read_text())
    assert cleaned.splitlines() == [
        "[00:01] Hello and welcome.",
        "[00:04] We build a maker-checker loop.",
        "[00:04] It reviews its own work.",
    ]


def test_timestamps_past_an_hour_carry_hours():
    vtt = "WEBVTT\n\n01:02:03.000 --> 01:02:05.000\nlate remark\n"
    assert clean_vtt(vtt) == "[1:02:03] late remark"


def test_a_cue_with_no_text_or_a_file_with_no_cues_cleans_to_empty():
    assert clean_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n \n") == ""
    assert clean_vtt("") == ""
