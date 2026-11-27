"""Privacy limits on what may be copied from a transcript or caption into a note.

Two rules, enforced in code:
- no more than MAX_COPY_RUN consecutive words may match the local reference text; a longer
  copied run is cut down to a SNIPPET_WORDS snippet followed by an ellipsis;
- the words inside quotation marks (explicit quotes and quoted spans in free text) share one
  per-item budget that stays under 50.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_COPY_RUN = 40  # more than this many consecutive matching words is rejected
SNIPPET_WORDS = 20  # a copied run is truncated to this many words
MAX_QUOTED_WORDS = 49  # total quoted words per item
MAX_QUOTE_WORDS = 25  # one explicit quote

_WORD = re.compile(r"\w+(?:'\w+)*")
_TIMESTAMP = re.compile(r"\[?\b\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\]?")
_QUOTED = re.compile(r'"([^"]*)"|“([^”]*)”')
ELLIPSIS = "…"


def _ref_words(reference: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(_TIMESTAMP.sub(" ", reference))]


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


def longest_copied_run(text: str, reference: str) -> int:
    """Length in words of the longest run of `text` that appears verbatim in `reference`."""
    ref = _ref_words(reference)
    index = _index(ref)
    words = [w.lower() for w in _WORD.findall(text)]
    return max((_match_len(words, i, ref, index) for i in range(len(words))), default=0)


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
        words = quote.split()
        granted = self.take(min(len(words), per_quote))
        if granted == 0:
            return ""
        return " ".join(words[:granted]) + (ELLIPSIS if granted < len(words) else "")


def _truncate_copied_runs(text: str, reference: str) -> str:
    ref = _ref_words(reference)
    if not ref:
        return text
    index = _index(ref)
    spans = [(m.start(), m.end(), m.group().lower()) for m in _WORD.finditer(text)]
    words = [s[2] for s in spans]
    out, pos, i = [], 0, 0
    while i < len(words):
        run = _match_len(words, i, ref, index)
        if run > MAX_COPY_RUN:
            out.append(text[pos:spans[i + SNIPPET_WORDS - 1][1]] + ELLIPSIS)
            pos = spans[i + run - 1][1]
            i += run
        else:
            i += 1
    out.append(text[pos:])
    return "".join(out)


def _limit_quoted_spans(text: str, budget: QuoteBudget) -> str:
    def repl(m: re.Match) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        words = inner.split()
        granted = budget.take(len(words))
        if granted == len(words):
            return m.group(0)
        if granted == 0:
            return ELLIPSIS
        return f'"{" ".join(words[:granted])}{ELLIPSIS}"'

    return _QUOTED.sub(repl, text)


def guard_text(text: str, reference: str = "", budget: QuoteBudget | None = None) -> str:
    """Apply both rules to one free-text field of a note."""
    text = _truncate_copied_runs(text, reference)
    if budget is not None:
        text = _limit_quoted_spans(text, budget)
    return text
