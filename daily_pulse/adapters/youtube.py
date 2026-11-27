"""YouTube source: a public playlist, one note per new video.

`clean_vtt` turns a WebVTT file into plain text lines that keep their start timestamps.
`YouTubeAdapter` lists the playlist, and for each new video fetches metadata and English
captions (manual subtitles first, auto captions second), keeps the cleaned transcript only
under `<data_dir>/transcripts/`, and writes one note through the summarizer. yt-dlp is reached
only through a client object, so tests inject a fake.
"""
from __future__ import annotations

import html
import logging
import re
import tempfile
from pathlib import Path

from daily_pulse.adapters.base import AdapterContext, SourceAdapter
from daily_pulse.models import Item
from daily_pulse.notes import write_note, write_transcript

log = logging.getLogger("daily_pulse")

WATCH_URL = "https://www.youtube.com/watch?v="
DESCRIPTION_EXCERPT = 500  # characters of the video description given to the rater
NO_CAPTIONS = "No captions available; rated from metadata only."
METADATA_ONLY_TAG = "metadata-only"

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


class YtDlpError(Exception):
    """yt-dlp failed, or printed something unusable."""


class SummaryFailed(Exception):
    """The summarizer gave no rating; the video is retried on the next run."""


def _iso_date(upload_date) -> str:
    """yt-dlp's YYYYMMDD as YYYY-MM-DD; anything else is passed through as-is."""
    text = str(upload_date or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:]}" if re.fullmatch(r"\d{8}", text) else text


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class YouTubeAdapter(SourceAdapter):
    name = "youtube"

    def __init__(self, client=None):
        self._client = client

    def _yt(self, ctx: AdapterContext):
        return self._client

    def status(self, ctx: AdapterContext) -> str | None:
        if not str(ctx.config.get("playlist_url") or "").strip():
            return "not configured"
        return None

    def list_new(self, ctx: AdapterContext) -> list[Item]:
        """The playlist as yt-dlp lists it (newest first, as YouTube shows it)."""
        entries = self._yt(ctx).playlist(str(ctx.config["playlist_url"]).strip())
        return [
            Item(source=self.name, id=e["id"], url=WATCH_URL + e["id"], title=str(e.get("title") or ""),
                 author=str(e.get("channel") or e.get("uploader") or ""))
            for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"]
        ]

    def _captions(self, client, item: Item) -> str:
        """Cleaned captions: manual subtitles if the video has them, else auto captions, else ''."""
        with tempfile.TemporaryDirectory(prefix="daily-pulse-subs-") as tmp:
            for auto in (False, True):
                path = client.subtitles(item.url, auto, Path(tmp))
                if path is not None:
                    cleaned = clean_vtt(Path(path).read_text(encoding="utf-8", errors="replace"))
                    if cleaned:
                        return cleaned
        return ""

    def fetch_details(self, item: Item, ctx: AdapterContext) -> Item:
        client = self._yt(ctx)
        info = client.info(item.url)
        item.title = str(info.get("title") or item.title)
        item.author = str(info.get("channel") or info.get("uploader") or item.author)
        item.published = _iso_date(info.get("upload_date"))
        item.metrics = {"duration": _count(info.get("duration")), "views": _count(info.get("view_count"))}
        item.text = str(info.get("description") or "").strip()[:DESCRIPTION_EXCERPT]
        if ctx.dry_run or ctx.summarizer is None or ctx.notes_dir is None:
            return item
        transcript = self._captions(client, item)
        if transcript:
            write_transcript(ctx.data_dir, item, transcript)
        else:
            item.text = (item.text + "\n" + NO_CAPTIONS).strip()
        rating = ctx.summarizer.rate(item, transcript)
        if rating is None:
            raise SummaryFailed(f"{item.id}: no rating, will retry next run")
        if not transcript:
            rating.tags = [*rating.tags, METADATA_ONLY_TAG]
            rating.reason = "Metadata only (no captions): " + rating.reason
        write_note(item, rating, ctx.notes_dir, ctx.date, transcript)
        return item
