"""Privacy limits on what may be copied from a transcript or caption into a note.

Two rules, enforced in code:
- no more than MAX_COPY_RUN consecutive words may match the local reference text; a longer
  copied run is cut down to a SNIPPET_WORDS snippet followed by an ellipsis;
- the words inside quotation marks (explicit quotes and quoted spans in free text) and any
  lifted run of MIN_COPY_RUN+ words in free text share one per-item budget that stays under 50.

Words are compared after Unicode NFKC normalisation, ignoring case, curly-vs-straight
apostrophes, zero-width characters, markdown underscores, and caption markup (timestamps,
inline tags, SRT cue numbers), so cosmetic changes do not hide a copy.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_COPY_RUN = 40  # more than this many consecutive matching words is rejected
SNIPPET_WORDS = 20  # a copied run is truncated to this many words
MAX_QUOTED_WORDS = 49  # total quoted words per item
MAX_QUOTE_WORDS = 25  # one explicit quote
MIN_COPY_RUN = 8  # a matching run this long counts as lifted text and spends the budget

_APOS = "'’‘ʼ`"
# Scripts written without spaces (kana, CJK, Bopomofo, Thai) count one character per word, so the
# word limits still bite.
_UNSPACED = "\u0e00-\u0e7f\u3040-\u30ff\u3100-\u312f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_WORD = re.compile(rf"[{_UNSPACED}]|[^\W_{_UNSPACED}]+(?:[{_APOS}][^\W_{_UNSPACED}]+)*")
# Timestamps are skipped on both sides, so [00:10] markers inserted into a copy do not break it.
_TIMESTAMP = re.compile(r"\[?\b\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\]?")
# Caption markup is skipped in the reference only (never in the text under test, where a
# "<...>" wrapper could otherwise hide a copy): HTML-ish tags like <c> and SRT cue numbers.
_REF_MARKUP = re.compile(rf"</?[A-Za-z][^<>]*>|{_TIMESTAMP.pattern}|^[ \t]*\d+[ \t]*$", re.M)
_ZERO_WIDTH = re.compile("[​-‏⁠﻿­]")
_QUOTED = re.compile(r'"([^"]*)"|“([^”]*)”|«([^»]*)»|„([^“”]*)[“”]')
ELLIPSIS = "…"
_SEP = "\x00"  # joins fields so a copy spanning adjacent fields is seen as one run


def _clean(text: str) -> str:
    return _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))


def _tokens(text: str, reference: bool = False) -> list[tuple[int, int, str]]:
    """(start, end, normalised word) for each word of cleaned `text`, skipping timestamps
    (and, for a reference, caption markup)."""
    skip = [m.span() for m in (_REF_MARKUP if reference else _TIMESTAMP).finditer(text)]
    out, k = [], 0
    for m in _WORD.finditer(text):
        while k < len(skip) and skip[k][1] <= m.start():
            k += 1
        if k < len(skip) and skip[k][0] <= m.start() < skip[k][1]:
            continue
        out.append((m.start(), m.end(), re.sub(f"[{_APOS}]", "'", m.group().lower())))
    return out


def _ref_words(reference: str) -> list[str]:
    return [t[2] for t in _tokens(_clean(reference), reference=True)]


def _index(ref: list[str]) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for pos, word in enumerate(ref):
        index.setdefault(word, []).append(pos)
    return index


def _match_len(words: list[str], i: int, ref: list[str], index: dict[str, list[int]]) -> int:
    best = 0
    for j in index.get(words[i], ()):
        n = 0
        while i + n < len(words) and j + n < len(ref) and words[i + n] == ref[j + n]:
            n += 1
        best = max(best, n)
    return best


def word_count(text: str) -> int:
    return len(_WORD.findall(_clean(text)))


def _first_words(text: str, n: int) -> str:
    """The prefix of `text` holding its first n words (text is returned whole if shorter)."""
    spans = [m.end() for m in _WORD.finditer(text)]
    return text if len(spans) <= n else text[: spans[n - 1]] if n else ""


def longest_copied_run(text: str, reference: str) -> int:
    """Length in words of the longest run of `text` that appears verbatim in `reference`."""
    ref = _ref_words(reference)
    index = _index(ref)
    words = [t[2] for t in _tokens(_clean(text))]
    return max((_match_len(words, i, ref, index) for i in range(len(words))), default=0)


def copied_word_count(text: str, reference: str, min_run: int = MIN_COPY_RUN) -> int:
    """Words of `text` that sit inside a verbatim run of at least `min_run` words of `reference`."""
    ref = _ref_words(reference)
    index = _index(ref)
    words = [t[2] for t in _tokens(_clean(text))]
    covered, i = 0, 0
    while i < len(words):
        run = _match_len(words, i, ref, index)
        if run >= min_run:
            covered += run
            i += run
        else:
            i += 1
    return covered


@dataclass
class QuoteBudget:
    """Quoted words one item may spend. Share one instance across all of an item's fields."""

    limit: int = MAX_QUOTED_WORDS
    used: int = 0

    def take(self, n: int) -> int:
        granted = max(0, min(n, self.limit - self.used))
        self.used += granted
        return granted

    def fit(self, quote: str, per_quote: int = MAX_QUOTE_WORDS) -> str:
        """Truncate one explicit quote to its cap and the remaining budget; "" if none left."""
        quote = _clean(quote)
        total = word_count(quote)
        granted = self.take(min(total, per_quote))
        if granted == 0:
            return ""
        return _first_words(quote, granted) + (ELLIPSIS if granted < total else "")


def _truncate_copied_runs(text: str, reference: str, budget: QuoteBudget | None) -> str:
    ref = _ref_words(reference)
    if not ref:
        return text
    index = _index(ref)
    spans = _tokens(text)
    words = [s[2] for s in spans]
    out, pos, i = [], 0, 0
    while i < len(words):
        run = _match_len(words, i, ref, index)
        if run < MIN_COPY_RUN:
            i += 1
            continue
        keep = SNIPPET_WORDS if run > MAX_COPY_RUN else run
        if budget is not None:
            keep = budget.take(keep)
        if keep < run:
            cut = spans[i + keep - 1][1] if keep else spans[i][0]
            end = spans[i + run - 1][1]
            out.append(text[pos:cut])
            # Field separators inside the removed span stay, so the fields still split cleanly.
            out.append(ELLIPSIS + (_SEP + ELLIPSIS) * text[cut:end].count(_SEP))
            pos = end
        i += run
    out.append(text[pos:])
    return "".join(out)


def _limit_quoted_spans(text: str, budget: QuoteBudget) -> str:
    def repl(m: re.Match) -> str:
        inner = next(g for g in m.groups() if g is not None)
        total = word_count(inner)
        granted = budget.take(total)
        if granted == total:
            return m.group(0)
        if granted == 0:
            return ELLIPSIS
        whole = m.group(0)
        return f"{whole[0]}{_first_words(inner, granted)}{ELLIPSIS}{whole[-1]}"

    return _QUOTED.sub(repl, text)


def guard_fields(fields: list[str], reference: str = "", budget: QuoteBudget | None = None) -> list[str]:
    """Apply both rules to fields that sit next to each other in a note (a list of takeaways,
    say). They are checked as one text, so a copy that continues from one field into the next
    is cut instead of slipping under the per-field limit. Returns one string per field."""
    if not fields:
        return []
    joined = _SEP.join(_clean(f).replace(_SEP, " ") for f in fields)
    parts = _truncate_copied_runs(joined, reference, budget).split(_SEP)
    if budget is not None:
        parts = [_limit_quoted_spans(p, budget) for p in parts]
    return parts


def guard_text(text: str, reference: str = "", budget: QuoteBudget | None = None) -> str:
    """Apply both rules to one free-text field of a note."""
    return guard_fields([text], reference, budget)[0]
