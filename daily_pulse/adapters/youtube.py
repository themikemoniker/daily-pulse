"""YouTube source: a public playlist, one note per new video.

`clean_vtt` turns a WebVTT file into plain text lines that keep their start timestamps.
"""
from __future__ import annotations

import html
import re

_CUE_TIME = re.compile(
    r"^(?:(\d+):)?(\d{1,2}):(\d{2})[.,]\d{1,3}\s+-->\s+(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}")
_TAG = re.compile(r"<[^>]*>")  # <c>, <i>, <c.colorE5E5E5>, inline <00:00:01.500> timestamps


def _stamp(hours: str | None, minutes: str, seconds: str) -> str:
    h = int(hours or 0)
    return f"{h}:{minutes}:{seconds}" if h else f"{int(minutes):02d}:{seconds}"


def clean_vtt(vtt: str) -> str:
    """One `[mm:ss] text` line per caption line. Markup and entities are dropped. Auto captions
    roll: each cue repeats the previous cue's last line, so a line that the previous cue
    already showed (or that equals the last line emitted) is dropped."""
    cues: list[tuple[str, list[str]]] = []  # (start stamp, raw text lines)
    skipping = True  # header, NOTE/STYLE/REGION blocks and cue identifiers are not cue text
    for line in vtt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = _CUE_TIME.match(line.strip())
        if match:
            cues.append((_stamp(*match.groups()), []))
            skipping = False
        elif line == "":  # only a truly empty line ends a block; auto captions use " " inside cues
            skipping = True
        elif not skipping:
            cues[-1][1].append(line)
    out: list[str] = []
    previous: list[str] = []
    for stamp, raw in cues:
        cue = [t for t in (" ".join(html.unescape(_TAG.sub("", r)).split()) for r in raw) if t]
        for text in cue:
            if text not in previous and not (out and out[-1].endswith("] " + text)):
                out.append(f"[{stamp}] {text}")
        previous = cue or previous
    return "\n".join(out)
