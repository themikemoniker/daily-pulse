"""YouTube source: a public playlist, one note per new video.

`clean_vtt` turns a WebVTT file into plain text lines that keep their start timestamps.
`YouTubeAdapter` lists the playlist, and for each new video fetches metadata and English
captions (manual subtitles first, auto captions second), keeps the cleaned transcript only
under `<data_dir>/transcripts/`, and writes one note through the summarizer. yt-dlp is reached
only through a client object, so tests inject a fake.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
from datetime import date as _date
from pathlib import Path

from daily_pulse.adapters.base import AdapterContext, ItemDeferred, ItemSkipped, SourceAdapter
from daily_pulse.models import Item
from daily_pulse.notes import write_note, write_transcript
from daily_pulse.state import atomic_write_json

log = logging.getLogger("daily_pulse")

WATCH_URL = "https://www.youtube.com/watch?v="
DESCRIPTION_EXCERPT = 500  # characters of the video description given to the rater
UNAVAILABLE_TITLES = ("[private video]", "[deleted video]")  # placeholders that can never be fetched
MAX_ATTEMPTS = 3  # video-specific yt-dlp failures for one video before it is given up on
SOURCE_MAX_ATTEMPTS = 5  # runs in which a video was the only source-level failure before it is given up on
RECENT_DAYS = 2  # a video uploaded this recently may not have captions yet
CAPTION_WAIT_DAYS = 3  # how long, from first sight, a video with no captions is waited for
# yt-dlp errors that say nothing about one video (rate limit, throttling, server trouble, DNS,
# bot check, broken binary): not a strike against the video. They are counted separately (see
# `_source_level_failure`): the run stops for the source only once two different videos fail this
# way, and a video that is the only such failure run after run is given up on. Keep this narrow, and
# leave out wordings that also describe one removed video ("this content isn't available").
SOURCE_LEVEL_ERRORS = re.compile(
    r"cannot run|timed out|http error 429|too many requests|http error 5\d\d|not a bot"
    r"|name or service not known|temporary failure in name resolution|connection reset"
    r"|try again later", re.I)  # the last: how YouTube words throttling
# a premiere or live event that has not started: not a failure, the video is waited for
UPCOMING = re.compile(r"premieres? (?:in |will begin)|live event will begin|will begin (?:in|shortly)", re.I)
# yt-dlp errors that no retry will fix: skip the video at once. Every other failure that is not
# source-level or upcoming is a strike against the video (MAX_ATTEMPTS).
PERMANENT_ERRORS = re.compile(
    r"private video|members-only|members only|join this channel|confirm your age|age-restricted"
    r"|not made this video available in your country|blocked it in your country"
    r"|has been removed|account.*terminated", re.I)
# a subtitle download that failed (as opposed to a video that has no subtitles); it is matched
# against stderr that also holds the video id and a temp path, so keep it to whole phrases
SUBTITLE_FAILURE = re.compile(r"http error 429|too many requests|unable to download video subtitles", re.I)
SOURCE_FAILURES_FILE = "youtube-source-failures.json"
STATE_FILES = ("youtube-attempts.json", "youtube-pending.json", SOURCE_FAILURES_FILE)  # all keyed by video id
NO_CAPTIONS = "No captions available; rated from metadata only."
METADATA_ONLY_TAG = "metadata-only"

_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_CUE_TIME = re.compile(
    r"^(?:(\d+):)?(\d{1,2}):(\d{2})[.,]\d{1,3}\s+-->\s+(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}")
_SOUND_ONLY = re.compile(r"(?:\[[^\]]*\]|\([^)]*\)|[♪♫\s-])+")  # [Music], (applause), ♪
_STAMP = re.compile(r"^\[[\d:]+\] ")
_TAG = re.compile(r"<[^>]*>")  # <c>, <i>, <c.colorE5E5E5>, inline <00:00:01.500> timestamps


def only_sound_tags(cleaned: str) -> bool:
    """True when every caption line is just a sound tag such as `[Music]`: no speech to read."""
    return all(_SOUND_ONLY.fullmatch(_STAMP.sub("", line)) for line in cleaned.splitlines())


def _stamp(hours: str | None, minutes: str, seconds: str) -> str:
    h = int(hours or 0)
    return f"{h}:{minutes}:{seconds}" if h else f"{int(minutes):02d}:{seconds}"


def clean_vtt(vtt: str, auto: bool = False) -> str:
    """One `[mm:ss] text` line per caption line. Markup and entities are dropped. Auto captions
    (`auto=True`) roll: each cue repeats the previous cue's last line, so a line that the previous
    cue already showed (or that equals the last line emitted) is dropped. Manual subtitles are
    never de-duplicated: a repeated line there is real."""
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
            if not auto or (text not in previous and not (out and out[-1].endswith("] " + text))):
                out.append(f"[{stamp}] {text}")
        previous = cue or previous
    return "\n".join(out)


class YtDlpError(Exception):
    """yt-dlp failed, or printed something unusable."""


YTDLP_ENV = "DAILY_PULSE_YTDLP"
YTDLP_TIMEOUT = 180.0  # seconds per yt-dlp call


class ProcessYtDlp:
    """The real client: runs the yt-dlp binary. `cmd` is a command string ("yt-dlp", or the path
    to the copy `scripts/install-ytdlp.sh` put in the virtualenv)."""

    def __init__(self, cmd: str = "yt-dlp", timeout: float = YTDLP_TIMEOUT):
        self.cmd = cmd
        self.timeout = timeout

    def _exec(self, *args: str, warnings: bool = False) -> subprocess.CompletedProcess:
        quiet = [] if warnings else ["--no-warnings"]
        try:
            proc = subprocess.run([*shlex.split(self.cmd), *quiet, *args], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=self.timeout,
                                  stdin=subprocess.DEVNULL)
        except (OSError, ValueError) as exc:
            raise YtDlpError(f"cannot run {self.cmd!r}: {exc}") from None
        except subprocess.TimeoutExpired:
            raise YtDlpError(f"yt-dlp timed out after {self.timeout}s") from None
        if proc.returncode != 0:
            raise YtDlpError(f"yt-dlp exit {proc.returncode}: {proc.stderr.strip()[-300:]}")
        return proc

    def _run(self, *args: str) -> str:
        return self._exec(*args).stdout

    def _json(self, *args: str) -> dict:
        try:
            data = json.loads(self._run(*args))
        except ValueError as exc:
            raise YtDlpError(f"yt-dlp output is not JSON: {exc}") from None
        if not isinstance(data, dict):
            raise YtDlpError("yt-dlp output is not a JSON object")
        return data

    def playlist(self, url: str) -> list[dict]:
        entries = self._json("--flat-playlist", "-J", url).get("entries")
        if not isinstance(entries, list):
            raise YtDlpError("playlist listing has no entries")
        return entries

    def info(self, url: str) -> dict:
        return self._json("--no-playlist", "--skip-download", "-J", url)

    @staticmethod
    def _pick(files: list[Path]) -> Path | None:
        """Several English variants (en, en-US, en-orig) can land in `dest`: plain `.en.vtt` if it is
        there, else the first by name, so the choice never depends on directory order."""
        return next((f for f in files if f.name.endswith(".en.vtt")), files[0] if files else None)

    def subtitles(self, url: str, auto: bool, dest: Path) -> Path | None:
        """English captions as VTT in `dest`: the uploader's own subtitles, or (auto=True) the
        auto-generated ones. None when the video has none of that kind."""
        # Warnings stay on: yt-dlp reports a rate-limited subtitle download only as a warning
        # with exit 0, and that must not be mistaken for a video that has no subtitles.
        proc = self._exec("--no-playlist", "--skip-download", "--write-auto-subs" if auto else "--write-subs",
                          "--sub-langs", "en.*,en", "--sub-format", "vtt",
                          "-o", str(Path(dest) / "%(id)s.%(ext)s"), url, warnings=True)
        found = self._pick(sorted(Path(dest).glob("*.vtt")))
        if found is None and SUBTITLE_FAILURE.search(proc.stderr):
            raise YtDlpError(f"subtitle download failed: {proc.stderr.strip()[-300:]}")
        return found


class FixtureYtDlp:
    """Replays recorded yt-dlp output from `<fixtures>/youtube/`: playlist.json, <id>.info.json,
    <id>.en.vtt (manual subtitles) and <id>.en.auto.vtt (auto captions)."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def playlist(self, url: str) -> list[dict]:
        return json.loads((self.dir / "playlist.json").read_text(encoding="utf-8"))["entries"]

    def info(self, url: str) -> dict:
        try:
            return json.loads((self.dir / f"{url.rsplit('=', 1)[-1]}.info.json").read_text(encoding="utf-8"))
        except OSError as exc:
            raise YtDlpError(f"no recorded metadata: {exc}") from None

    def subtitles(self, url: str, auto: bool, dest: Path) -> Path | None:
        src = self.dir / f"{url.rsplit('=', 1)[-1]}.en{'.auto' if auto else ''}.vtt"
        return src if src.exists() else None


def make_client(config: dict, fixtures_dir: Path | None, env=None):
    """Recorded fixtures when `--fixtures` is given, else the yt-dlp binary: $DAILY_PULSE_YTDLP,
    then `ytdlp_cmd` in [sources.youtube], then `yt-dlp` on PATH."""
    if fixtures_dir is not None:
        return FixtureYtDlp(Path(fixtures_dir) / "youtube")
    env = os.environ if env is None else env
    cmd = (env.get(YTDLP_ENV) or "").strip() or str(config.get("ytdlp_cmd") or "").strip() or "yt-dlp"
    return ProcessYtDlp(cmd)


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
    quality_gate = True  # rules and triage run on the listing, before any per-video yt-dlp call
    quality_duration = True  # Shorts and clips, and very long streams, are dropped by length

    def __init__(self, client=None):
        self._client = client
        self._halted: str | None = None  # set when two videos fail source-level in a run; cleared by list_new
        self._source_failed: set[str] = set()  # videos that failed source-level in this run
        self._counted: set[str] = set()  # ...and whose failure was added to the persistent count

    def _yt(self, ctx: AdapterContext):
        return self._client or make_client(ctx.config, ctx.fixtures_dir)

    def status(self, ctx: AdapterContext) -> str | None:
        if not str(ctx.config.get("playlist_url") or "").strip():
            return "not configured"
        return None

    def list_new(self, ctx: AdapterContext) -> list[Item]:
        """Newest first. Entries that carry an upload date are ordered by it. Otherwise
        `playlist_order` says how the playlist is ordered: "newest_first" (as YouTube shows
        "Watch later" and most saved lists) or "oldest_first" (a plain list new videos are appended
        to). When it is unset the order is detected from the upload dates of the first and last
        entries. Private and deleted placeholders are left out. Duration and view count from the
        listing go to the quality gate, which runs before any per-video call."""
        self._halted = None
        self._source_failed, self._counted = set(), set()
        order = str(ctx.config.get("playlist_order") or "").strip()
        if order not in ("", "newest_first", "oldest_first"):
            raise ValueError(f"playlist_order must be newest_first or oldest_first, got {order!r}")
        client, url = self._yt(ctx), str(ctx.config["playlist_url"]).strip()
        entries = client.playlist(url)
        items = [self._listed(e) for e in entries
                 if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"]
                 and str(e.get("title") or "").strip().lower() not in UNAVAILABLE_TITLES]
        if items:  # an empty listing is more likely a hiccup than a playlist with nothing left
            self._prune_state(ctx, {i.id for i in items})
        if items and all(i.published for i in items):  # real dates beat any declared order
            return sorted(items, key=lambda i: i.published, reverse=True)
        order = order or self._detect_order(client, url, items, ctx)
        return items[::-1] if order == "oldest_first" else items

    def _listed(self, entry: dict) -> Item:
        metrics = {}
        for key, field_name in (("duration", "duration"), ("views", "view_count")):
            value = entry.get(field_name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                metrics[key] = int(value)
        return Item(source=self.name, id=entry["id"], url=WATCH_URL + entry["id"],
                    title=str(entry.get("title") or ""),
                    author=str(entry.get("channel") or entry.get("uploader") or ""),
                    published=_iso_date(entry.get("upload_date")), metrics=metrics)

    def _detect_order(self, client, url: str, items: list[Item], ctx: AdapterContext) -> str:
        """The order of a playlist whose config does not say: compare the upload dates of its first
        and last entries. The answer is remembered per playlist, so it costs two yt-dlp calls once.
        When the dates cannot tell (fewer than two videos, equal dates, a failed call) it is
        newest first, as YouTube lists most playlists, and nothing is remembered."""
        if len(items) < 2:
            return "newest_first"
        path = self._state_file(ctx, "youtube-order.json")
        known = self._load_map(path).get(url)
        if known in ("newest_first", "oldest_first"):
            return known
        try:
            first, last = (_iso_date(client.info(i.url).get("upload_date")) for i in (items[0], items[-1]))
        except YtDlpError as exc:
            log.warning("youtube: cannot detect the playlist order (%s); assuming newest first", exc)
            return "newest_first"
        if not (_DATE_ONLY.fullmatch(first) and _DATE_ONLY.fullmatch(last)) or first == last:
            log.warning("youtube: upload dates of the first and last entries (%r, %r) do not show the "
                        "playlist order; assuming newest first. Set playlist_order to be sure.", first, last)
            return "newest_first"
        order = "oldest_first" if first < last else "newest_first"
        log.info("youtube: detected playlist order %s (first %s, last %s)", order, first, last)
        if path is not None:
            try:
                atomic_write_json(path, {**self._load_map(path), url: order})
            except OSError as exc:
                log.warning("youtube: cannot remember the playlist order: %s", exc)
        return order

    def _captions(self, client, item: Item) -> str:
        """Cleaned captions: manual subtitles if the video has real speech in them, else auto
        captions, else ''. Captions that are only sound tags like [Music] count as none."""
        for auto in (False, True):
            with tempfile.TemporaryDirectory(prefix="daily-pulse-subs-") as tmp:
                path = client.subtitles(item.url, auto, Path(tmp))
                cleaned = clean_vtt(Path(path).read_text(encoding="utf-8", errors="replace"), auto=auto) if path else ""
            if cleaned and not only_sound_tags(cleaned):
                return cleaned
        return ""

    @staticmethod
    def _state_file(ctx: AdapterContext, name: str) -> Path | None:
        return None if ctx.data_dir is None or not ctx.persist else Path(ctx.data_dir) / "state" / name

    @staticmethod
    def _load_map(path: Path | None) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _drop_ids(self, ctx: AdapterContext, keep=None, remove=()) -> None:
        """Prune the per-video state files (attempts, pending, source-level failures): drop the
        ids in `remove`, and, when `keep` is given, every id not in it. Nothing else lives there."""
        for name in STATE_FILES:
            path = self._state_file(ctx, name)
            data = self._load_map(path)
            left = {k: v for k, v in data.items() if k not in remove and (keep is None or k in keep)}
            if path is not None and left != data:
                try:
                    atomic_write_json(path, left)
                except OSError as exc:  # pruning is housekeeping: never fail a video over it
                    log.warning("youtube: cannot prune %s: %s", name, exc)

    def _prune_state(self, ctx: AdapterContext, listed: set[str]) -> None:
        """Entries for videos that left the playlist can never be looked at again."""
        self._drop_ids(ctx, keep=listed)

    def _bump(self, ctx: AdapterContext, name: str, item_id: str) -> int | None:
        """Add one to a per-video counter file; the new count, or None when it cannot be kept."""
        path = self._state_file(ctx, name)
        if path is None:
            return None
        counts = self._load_map(path)
        counts[item_id] = n = (counts.get(item_id) if isinstance(counts.get(item_id), int) else 0) + 1
        try:
            atomic_write_json(path, counts)
        except OSError as exc:  # counting must never hide the real failure
            log.warning("youtube: cannot record a failure for %s: %s", item_id, exc)
            return None
        return n

    def _source_level_failure(self, item: Item, message: str, ctx: AdapterContext) -> None:
        """A yt-dlp failure that says nothing about this video (timeout, rate limit, network, bot
        check, broken binary). One such failure does not stop the source: one odd video (say a very
        long stream whose `-J` times out) must not block the others, so it is counted for that video
        (the playlist listing already worked this run). Two different videos failing this way in one
        run means yt-dlp itself is down: the source stops and this run's counts are taken back, so an
        outage never costs a video its place. A video that is the only such failure run after run is
        given up on after SOURCE_MAX_ATTEMPTS runs (see `fetch_details`)."""
        self._source_failed.add(item.id)
        if len(self._source_failed) >= 2:
            self._halted = message
            self._uncount(ctx)
        elif self._bump(ctx, SOURCE_FAILURES_FILE, item.id) is not None:
            self._counted.add(item.id)

    def _uncount(self, ctx: AdapterContext) -> None:
        """Take back the source-level counts added in this run."""
        path = self._state_file(ctx, SOURCE_FAILURES_FILE)
        counts = self._load_map(path)
        for item_id in self._counted:
            if isinstance(counts.get(item_id), int):
                counts[item_id] -= 1
                if counts[item_id] <= 0:
                    del counts[item_id]
        self._counted.clear()
        if path is not None:
            try:
                atomic_write_json(path, counts)
            except OSError as exc:
                log.warning("youtube: cannot take back this run's failure counts: %s", exc)

    def _clear_source_count(self, ctx: AdapterContext, item_id: str) -> None:
        """"Consecutive" runs: once a video fails some other way, or yt-dlp answers for it, its
        source-level count starts again."""
        path = self._state_file(ctx, SOURCE_FAILURES_FILE)
        counts = self._load_map(path)
        if path is not None and counts.pop(item_id, None) is not None:
            try:
                atomic_write_json(path, counts)
            except OSError as exc:
                log.warning("youtube: cannot reset the failure count for %s: %s", item_id, exc)

    def _record_failure(self, item: Item, exc: YtDlpError, ctx: AdapterContext) -> None:
        """Handle a yt-dlp failure for one video. Source-level errors are counted per video, not as
        strikes (`_source_level_failure`). A premiere or live event that has not started is deferred
        (for up to CAPTION_WAIT_DAYS from first sight, then it is an ordinary strike). A permanent
        error skips the video at once. Any other failure is a strike, and the MAX_ATTEMPTS-th raises
        ItemSkipped (the runner marks the video processed, so it stops failing the source)."""
        message = str(exc)
        if SOURCE_LEVEL_ERRORS.search(message):
            self._source_level_failure(item, message, ctx)
            return
        self._clear_source_count(ctx, item.id)
        if UPCOMING.search(message) and self._within_wait(item, ctx):
            raise ItemDeferred(f"{item.id}: not out yet, will look again next run: {message}") from exc
        if PERMANENT_ERRORS.search(message):
            self._drop_ids(ctx, remove={item.id})
            raise ItemSkipped(f"{item.id}: {message}") from exc
        n = self._bump(ctx, "youtube-attempts.json", item.id)
        if n is not None and n >= MAX_ATTEMPTS:
            self._drop_ids(ctx, remove={item.id})
            raise ItemSkipped(f"{item.id}: failed {n} times, giving up: {message}") from exc

    @staticmethod
    def _days_between(later: str, earlier: str) -> int | None:
        try:
            return (_date.fromisoformat(_iso_date(later)) - _date.fromisoformat(_iso_date(earlier))).days
        except ValueError:
            return None

    def _within_wait(self, item: Item, ctx: AdapterContext) -> bool:
        """True while a video that is not ready (no captions yet, or not out yet) has been waited for
        less than CAPTION_WAIT_DAYS. The wait is counted from the first run that saw it, separately
        from failure strikes."""
        path = self._state_file(ctx, "youtube-pending.json")
        first_seen = self._load_map(path)
        waited = self._days_between(ctx.date, str(first_seen.get(item.id) or ctx.date)) or 0
        if item.id not in first_seen and path is not None:
            first_seen[item.id] = ctx.date
            try:
                atomic_write_json(path, first_seen)
            except OSError as exc:
                log.warning("youtube: cannot record when %s was first seen: %s", item.id, exc)
        return waited < CAPTION_WAIT_DAYS

    def _awaiting_captions(self, item: Item, info: dict, ctx: AdapterContext) -> bool:
        """True while a video that has no captions is recent or live and has been waited for less
        than CAPTION_WAIT_DAYS: its auto-captions probably just are not there yet."""
        age = self._days_between(ctx.date, str(info.get("upload_date") or ""))
        live = info.get("live_status") in ("is_live", "is_upcoming", "post_live") or info.get("is_live") is True
        if not (live or (age is not None and age <= RECENT_DAYS)):
            return False
        return self._within_wait(item, ctx)

    def fetch_details(self, item: Item, ctx: AdapterContext) -> Item:
        counted = self._load_map(self._state_file(ctx, SOURCE_FAILURES_FILE)).get(item.id)
        if isinstance(counted, int) and counted >= SOURCE_MAX_ATTEMPTS:
            self._drop_ids(ctx, remove={item.id})
            raise ItemSkipped(f"{item.id}: the only source-level failure in {counted} runs while other "
                              "videos worked, giving up")
        if self._halted:  # yt-dlp is unusable right now: don't call it again this run
            raise YtDlpError(f"not tried, yt-dlp failed earlier in this run: {self._halted}")
        try:
            return self._fetch(item, ctx)
        except YtDlpError as exc:
            self._record_failure(item, exc, ctx)
            raise

    def _fetch(self, item: Item, ctx: AdapterContext) -> Item:
        client = self._yt(ctx)
        info = client.info(item.url)
        self._clear_source_count(ctx, item.id)
        item.title = str(info.get("title") or item.title)
        item.author = str(info.get("channel") or info.get("uploader") or item.author)
        item.published = _iso_date(info.get("upload_date"))
        item.metrics = {"duration": _count(info.get("duration")), "views": _count(info.get("view_count"))}
        item.text = str(info.get("description") or "").strip()[:DESCRIPTION_EXCERPT]
        if not ctx.persist:
            return item
        if ctx.summarizer is None or ctx.notes_dir is None:  # never consume a video without a note
            raise SummaryFailed(f"{item.id}: no summarizer or notes directory in the run context")
        transcript = self._captions(client, item)
        if not transcript and self._awaiting_captions(item, info, ctx):
            raise ItemDeferred(f"{item.id}: no captions yet, will look again next run")
        if transcript:
            write_transcript(ctx.data_dir, item, transcript)
        else:
            item.text = (item.text + "\n" + NO_CAPTIONS).strip()
        rating = ctx.summarizer.rate(item, transcript)
        if rating is None:
            raise SummaryFailed(f"{item.id}: no rating, will retry next run")
        if not transcript:
            rating.tags = [*rating.tags, METADATA_ONLY_TAG]
            rating.beats = []  # timestamps would be invented: there is no transcript to ground them
            rating.reason = "Metadata only (no captions): " + rating.reason
        write_note(item, rating, ctx.notes_dir, ctx.date, transcript)
        self._drop_ids(ctx, remove={item.id})  # done: nothing left to wait for or count
        return item
