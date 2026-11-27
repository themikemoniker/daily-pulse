#!/usr/bin/env python3
"""yt-digest: turn YouTube playlists / videos into summary notes + a digest.

One run:
  1. lists each configured playlist (yt-dlp --flat-playlist -J),
  2. skips videos already in state.json,
  3. fetches metadata + English captions for new videos,
  4. summarizes each transcript (default: `claude -p`, pluggable),
  5. writes one markdown note per video + a cleaned transcript file,
  6. grabs a few frames at key-beat timestamps (ffmpeg, optional),
  7. writes a digest (markdown + email-ready HTML) for the run.

Idempotent and safe to run unattended: per-video errors are logged, never
fatal to the whole run; re-running with nothing new processes zero videos.

Stdlib only. External tools: yt-dlp (required), ffmpeg (optional),
claude CLI (optional -- summaries marked pending without it).
"""
import argparse
import html
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path

VERSION = "1.0.0"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"

# ---------------------------------------------------------------- utilities

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run_cmd(cmd, timeout=300, input_text=None):
    """Run a command. Returns (rc, stdout, stderr); rc=-1 on timeout/error."""
    try:
        p = subprocess.run(cmd, input=input_text, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return -1, "", f"command not found: {e.filename}"
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"


def fmt_ts(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_ts(text):
    """'[04:12]' or '[1:04:12]' -> seconds. Returns None if no match."""
    m = re.search(r"\[(\d+):(\d+)(?::(\d+))?\]", text)
    if not m:
        return None
    if m.group(3) is not None:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return int(m.group(1)) * 60 + int(m.group(2))


def iso_date(yyyymmdd):
    try:
        return datetime.strptime(str(yyyymmdd), "%Y%m%d").date().isoformat()
    except (ValueError, TypeError):
        return ""


def today():
    return date.today().isoformat()


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- config

class Config:
    def __init__(self, path, overrides):
        raw = load_json(path, {})
        self.output_dir = Path(os.path.expanduser(
            overrides.get("output_dir") or raw.get("output_dir", "~/workspace/yt-digest")))
        self.sources = raw.get("sources", [])
        self.max_videos_per_run = int(overrides.get("limit")
                                      or raw.get("max_videos_per_run", 10))
        self.frames_per_video = int(raw.get("frames_per_video", 3))
        self.cookies = overrides.get("cookies") or raw.get("cookies_file")
        self.no_summarize = bool(overrides.get("no_summarize", False))
        self.no_frames = bool(overrides.get("no_frames", False))
        self.yt_dlp = overrides.get("yt_dlp") or raw.get("yt_dlp", "yt-dlp")
        # YouTube bot-check workaround: alternate player clients often get
        # through where the default web client is challenged. Configurable.
        self.extractor_args = (raw.get("extractor_args")
                               or "youtube:player_client=android,web,"
                                  "tv_embedded")
        self.summarizer_cmd = (overrides.get("summarizer_cmd")
                               or raw.get("summarizer", {}).get("command")
                               or ["claude", "-p"])
        if isinstance(self.summarizer_cmd, str):
            self.summarizer_cmd = shlex.split(self.summarizer_cmd)
        prompt_file = raw.get("summarizer", {}).get("prompt_file",
                                                   "prompts/summarize.md")
        self.prompt_file = (SCRIPT_DIR / prompt_file
                            if not Path(prompt_file).is_absolute()
                            else Path(prompt_file))
        for sub in ("notes", "transcripts", "frames", "digests"):
            (self.output_dir / sub).mkdir(parents=True, exist_ok=True)
        self.state_path = self.output_dir / "state.json"
        self.log_path = self.output_dir / "yt-digest.log"

    def ytdlp_base(self, extra=None):
        cmd = [self.yt_dlp, "--no-warnings", "--retries", "3",
               "--retry-sleep", "2"]
        if self.extractor_args:
            cmd += ["--extractor-args", self.extractor_args]
        if self.cookies:
            cmd += ["--cookies", str(self.cookies)]
        return cmd + (extra or [])


# ---------------------------------------------------------------- captions

TS_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+)[.,]\d+\s*-->")


def parse_json3_file(path):
    """Parse a yt-dlp json3 subtitle file into [(start_seconds, text)]."""
    try:
        data = json.loads(Path(path).read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    cues = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs)
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        if text:
            cues.append((int(ev.get("tStartMs", 0)) // 1000, text))
    deduped = []
    for start, text in cues:
        if deduped and deduped[-1][1] == text:
            continue
        deduped.append((start, text))
    return deduped


def parse_subtitle_file(path):
    """Parse a VTT/SRT file into [(start_seconds, clean_text)] cues."""
    cues = []
    lines = Path(path).read_text(errors="replace").splitlines()
    i = 0
    while i < len(lines):
        m = TS_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        start = (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60
                 + int(m.group(3)))
        i += 1
        parts = []
        while i < len(lines) and lines[i].strip() != "":
            parts.append(lines[i].strip())
            i += 1
        text = re.sub(r"<[^>]+>", "", " ".join(parts))
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cues.append((start, text))
    deduped = []
    for start, text in cues:
        if deduped and deduped[-1][1] == text:
            continue  # auto captions repeat the previous cue verbatim
        deduped.append((start, text))
    return deduped


def cues_to_text(cues):
    return "\n".join(f"[{fmt_ts(s)}] {t}" for s, t in cues)


# ---------------------------------------------------------------- yt-dlp

def list_source_videos(cfg, source):
    """Return [{id, title}] for a playlist/channel/video source."""
    url = source["url"]
    cmd = cfg.ytdlp_base(["--flat-playlist", "-J", "--no-check-certificate"])
    if source.get("max_videos"):
        cmd += ["--playlist-end", str(source["max_videos"])]
    cmd.append(url)
    rc, out, err = run_cmd(cmd, timeout=180)
    if rc != 0:
        raise RuntimeError(f"yt-dlp list failed for {url}: {err.strip()[:300]}")
    data = json.loads(out)
    entries = data.get("entries")
    if entries is None:  # single video URL, not a playlist
        entries = [data]
    videos = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        videos.append({"id": e["id"], "title": e.get("title", "")})
    return videos


def fetch_metadata(cfg, video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = cfg.ytdlp_base(["-J", "--no-playlist", url])
    rc, out, err = run_cmd(cmd, timeout=300)
    if rc != 0:
        raise RuntimeError(f"metadata fetch failed for {video_id}: "
                           f"{err.strip()[:300]}")
    d = json.loads(out)
    chapters = []
    for ch in d.get("chapters") or []:
        chapters.append((ch.get("start_time", 0), ch.get("title", "")))
    return {
        "id": video_id,
        "title": d.get("title", ""),
        "channel": d.get("channel") or d.get("uploader", ""),
        "url": url,
        "duration": d.get("duration") or 0,
        "published": iso_date(d.get("upload_date")),
        "views": d.get("view_count") or 0,
        "description": (d.get("description") or "")[:4000],
        "chapters": chapters,
        "formats": d.get("formats") or [],
    }


def fetch_captions(cfg, video_id, workdir):
    """Prefer manual English subs, fall back to auto captions.

    Returns (cues, kind) where kind is 'manual' | 'auto' | None.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    base = cfg.ytdlp_base(["--skip-download", "--sub-lang", "en",
                           "-o", str(workdir / "cap")])
    for flag, kind in (("--write-sub", "manual"),
                       ("--write-auto-sub", "auto")):
        for fmt in ("vtt", "json3"):
            rc, out, err = run_cmd(
                base + ["--sub-format", fmt, flag, url], timeout=300)
            if rc != 0:
                log(f"  captions ({kind}/{fmt}) failed for {video_id}: "
                    f"{err.strip()[:160]}")
                continue
            for cand in sorted(workdir.glob(f"cap*.{fmt}")):
                cues = (parse_subtitle_file(cand) if fmt == "vtt"
                        else parse_json3_file(cand))
                if cues:
                    return cues, kind
    return [], None


# ---------------------------------------------------------------- summarizer

def parse_summary(text):
    """Parse the structured summary into dict. Tolerates missing sections."""
    out = {"takeaways": [], "beats": [], "rating": None,
           "rating_reason": "", "verdict": "", "raw": text}
    parts = re.split(r"^##\s+(.+)$", text, flags=re.M)
    for j in range(1, len(parts) - 1, 2):
        heading = parts[j].strip().lower()
        body = parts[j + 1]
        lines = [ln.strip() for ln in body.strip().splitlines()
                 if ln.strip()]
        if "takeaway" in heading:
            out["takeaways"] = [re.sub(r"^[-*]\s*", "", ln)
                                for ln in lines if ln.startswith(("-", "*"))]
        elif "beat" in heading:
            out["beats"] = lines
        elif "rating" in heading and lines:
            m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", lines[0])
            if m:
                out["rating"] = float(m.group(1))
                out["rating_reason"] = re.sub(
                    r"^\s*\d+(?:\.\d+)?\s*/\s*10\s*[—\-–:]*\s*", "",
                    lines[0]).strip()
        elif "verdict" in heading and lines:
            first = lines[0].lower()
            for v in ("watch", "skim", "skip"):
                if v in first:
                    out["verdict"] = v
                    break
    return out


def summarize_with_cli(cfg, meta, transcript_text):
    """Run the summarizer CLI: instruction as argv, transcript on stdin.

    Returns parsed summary dict, or None if the CLI is unavailable/failed.
    """
    if not cfg.prompt_file.exists():
        log(f"  prompt file missing: {cfg.prompt_file}")
        return None
    instruction = cfg.prompt_file.read_text()
    if transcript_text:
        source_note = "The full transcript follows on stdin."
        body = transcript_text
    else:
        source_note = ("No captions exist for this video. Summarize from "
                       "the title, description and chapters below, and say "
                       "clearly that there is no transcript.")
        body = (f"Title: {meta['title']}\nChannel: {meta['channel']}\n\n"
                f"Description:\n{meta['description']}\n\nChapters:\n"
                + "\n".join(f"[{fmt_ts(s)}] {t}"
                            for s, t in meta["chapters"]))
    prompt = (f"{instruction}\n\nVideo: {meta['title']} "
              f"({meta['channel']}, {meta['url']})\n{source_note}\n")
    rc, out, err = run_cmd(cfg.summarizer_cmd + [prompt], timeout=900,
                           input_text=body)
    if rc == -1 and "command not found" in err:
        log(f"  summarizer not found: {cfg.summarizer_cmd[0]} "
            f"(use --no-summarize or install it)")
        return None
    if rc != 0 or not out.strip():
        log(f"  summarizer failed for {meta['id']}: {err.strip()[:300]}")
        return None
    return parse_summary(out)


def import_summary(path):
    """Read a pre-written summary file in the same section format."""
    return parse_summary(Path(path).read_text())


# ---------------------------------------------------------------- frames

def pick_stream_url(meta):
    """Best small mp4 direct URL from metadata (no full video download)."""
    cands = [f for f in meta.get("formats", [])
             if f.get("ext") == "mp4" and f.get("url")
             and f.get("vcodec") not in (None, "none")]
    if not cands:
        return None
    def score(f):
        h = f.get("height") or 0
        return abs(h - 480)  # prefer ~480p: small, fast, clear enough
    return min(cands, key=score)["url"]


def grab_frames(cfg, meta, beat_seconds):
    """Grab up to N frames at beat timestamps via ffmpeg. Silent skip if
    ffmpeg is missing or no stream URL. Returns list of relative paths."""
    if cfg.no_frames:
        return []
    rc, _, _ = run_cmd(["ffmpeg", "-version"], timeout=10)
    if rc != 0:
        return []
    url = pick_stream_url(meta)
    if not url:
        return []
    duration = meta.get("duration") or 0
    if beat_seconds:
        stamps = beat_seconds[:cfg.frames_per_video]
    elif duration:
        stamps = [int(duration * f)
                  for f in (0.15, 0.5, 0.85)][:cfg.frames_per_video]
    else:
        return []
    outdir = cfg.output_dir / "frames" / meta["id"]
    outdir.mkdir(parents=True, exist_ok=True)
    rels = []
    for i, sec in enumerate(stamps, 1):
        sec = max(0, min(int(sec), max(0, duration - 1))) if duration else sec
        out = outdir / f"frame-{i}.jpg"
        rc, _, err = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error",
                              "-ss", str(sec), "-i", url, "-frames:v", "1",
                              "-q:v", "3", str(out)], timeout=120)
        if rc == 0 and out.exists() and out.stat().st_size > 2000:
            rels.append(f"../frames/{meta['id']}/frame-{i}.jpg")
        else:
            log(f"  frame {i} @ {fmt_ts(sec)} failed: {err.strip()[:150]}")
    return rels


# ---------------------------------------------------------------- notes

TAG_KEYWORDS = {
    "harness": "harness", "multi-agent": "multi-agent",
    "multi agent": "multi-agent", "eval": "evals",
    "maker-checker": "maker-checker", "mcp": "mcp",
    "claude": "claude-code", "codex": "codex",
    "routing": "model-routing", "guard rail": "guard-rails",
    "orchestrat": "orchestration", "subagent": "subagents",
}


def derive_tags(title):
    t = title.lower()
    return sorted({tag for kw, tag in TAG_KEYWORDS.items() if kw in t})


def frontmatter(meta, summary, tags):
    rating = summary.get("rating")
    rating_s = f"{rating:g}" if isinstance(rating, (int, float)) else ""
    fm = [
        "---",
        f"title: \"{meta['title'].replace(chr(34), chr(39))}\"",
        f"channel: \"{meta['channel'].replace(chr(34), chr(39))}\"",
        f"video_id: {meta['id']}",
        f"url: {meta['url']}",
        f"published: {meta['published']}",
        f"duration: {fmt_ts(meta['duration'])}",
        f"views: {meta['views']}",
        f"rating: {rating_s}",
        f"verdict: {summary.get('verdict', '')}",
        f"tags: [{', '.join(tags)}]",
        "---",
    ]
    return "\n".join(fm)


def write_note(cfg, meta, summary, transcript_path, frames, has_captions):
    """Write (or rewrite) the markdown note. Never includes the transcript."""
    tags = derive_tags(meta["title"])
    lines = [frontmatter(meta, summary, tags), ""]
    lines.append(f"# {meta['title']}")
    lines.append("")
    lines.append(f"{meta['channel']} · {meta['published']} · "
                 f"{fmt_ts(meta['duration'])} · {meta['views']} views")
    lines.append(f"[Watch on YouTube]({meta['url']})")
    lines.append("")
    if summary.get("takeaways"):
        lines.append("## Key takeaways")
        lines.append("")
        for t in summary["takeaways"]:
            lines.append(f"- {t}")
        lines.append("")
    if summary.get("beats"):
        lines.append("## Beats")
        lines.append("")
        for b in summary["beats"]:
            lines.append(f"- {b}")
        lines.append("")
    if summary.get("rating") is not None:
        lines.append("## Rating")
        lines.append("")
        lines.append(f"{summary['rating']:g}/10 — "
                     f"{summary.get('rating_reason', '')}".strip())
        lines.append("")
    if summary.get("verdict"):
        lines.append("## Verdict")
        lines.append("")
        lines.append(summary["verdict"])
        lines.append("")
    if not summary.get("takeaways"):
        lines.append("## Summary")
        lines.append("")
        lines.append("_Summary pending — run `yt_digest.py summarize` "
                     "to generate it._")
        lines.append("")
        if meta["description"]:
            lines.append("Description excerpt:")
            lines.append("")
            lines.append(meta["description"][:500])
            lines.append("")
    if frames:
        lines.append("## Frames")
        lines.append("")
        for i, rel in enumerate(frames, 1):
            lines.append(f"![frame {i}]({rel})")
        lines.append("")
    lines.append("## Source")
    lines.append("")
    if has_captions:
        lines.append("Full captions live on YouTube: open the video → "
                     "… → Show transcript.")
    else:
        lines.append("No captions exist for this video; the note above is "
                     "built from the title, description and chapters.")
    lines.append(f"Cleaned transcript (local reference only): "
                 f"`transcripts/{meta['id']}.txt`")
    lines.append("")
    note_path = cfg.output_dir / "notes" / f"{meta['id']}.md"
    note_path.write_text("\n".join(lines))
    return note_path


# ---------------------------------------------------------------- pipeline

def fetch_new_videos(cfg, state):
    """Fetch metadata + captions for videos not yet in state.

    Returns list of (meta, transcript_text, has_captions, workdir_cues).
    """
    fetched = []
    seen_this_run = 0
    for source in cfg.sources:
        name = source.get("name", source["url"])
        try:
            listed = list_source_videos(cfg, source)
        except Exception as e:
            log(f"ERROR listing {name}: {e}")
            continue
        for entry in listed:
            vid = entry["id"]
            prev = state["videos"].get(vid)
            if prev and prev.get("status") in ("fetched", "summarized"):
                continue  # already processed; "failed" is retried
            if prev and prev.get("status") == "failed":
                log(f"  retrying previously failed {vid}")
            if seen_this_run >= cfg.max_videos_per_run:
                log(f"  cap reached ({cfg.max_videos_per_run}); "
                    f"remaining new videos wait for the next run")
                return fetched
            seen_this_run += 1
            log(f"NEW {vid} — {entry['title'][:80]}")
            try:
                meta = fetch_metadata(cfg, vid)
            except Exception as e:
                log(f"  ERROR {e}")
                state["videos"][vid] = {"title": entry["title"],
                                        "status": "failed",
                                        "error": str(e)[:300],
                                        "processed_at": today()}
                save_json(cfg.state_path, state)
                continue
            workdir = Path(f"/tmp/yt-digest-{vid}")
            workdir.mkdir(exist_ok=True)
            try:
                cues, kind = fetch_captions(cfg, vid, workdir)
            except Exception as e:
                log(f"  captions error for {vid}: {e}")
                cues, kind = [], None
            transcript_text = cues_to_text(cues)
            tpath = cfg.output_dir / "transcripts" / f"{vid}.txt"
            tpath.write_text(
                f"# {meta['title']}\n# {meta['url']}\n"
                f"# Captions: {kind or 'none'}\n\n{transcript_text}\n")
            state["videos"][vid] = {
                "title": meta["title"], "channel": meta["channel"],
                "url": meta["url"], "published": meta["published"],
                "duration": meta["duration"], "views": meta["views"],
                "status": "fetched", "has_captions": bool(cues),
                "caption_kind": kind, "transcript": str(tpath),
                "processed_at": today(),
            }
            save_json(cfg.state_path, state)
            fetched.append((meta, transcript_text, bool(cues)))
            log(f"  fetched: {len(cues)} cues ({kind or 'none'})")
    return fetched


def summarize_pending(cfg, state, import_dir=None):
    """Summarize videos in state with status 'fetched'.

    import_dir: optional dir of pre-written <video-id>.md summaries in the
    same section format (used when no summarizer CLI is available).
    Returns list of video ids summarized.
    """
    done = []
    for vid, rec in list(state["videos"].items()):
        if rec.get("status") != "fetched":
            continue
        meta = {"id": vid, "title": rec["title"], "channel": rec["channel"],
                "url": rec["url"], "published": rec["published"],
                "duration": rec["duration"], "views": rec["views"],
                "description": "", "chapters": []}
        summary = None
        if import_dir:
            cand = Path(import_dir) / f"{vid}.md"
            if cand.exists():
                summary = import_summary(cand)
                log(f"  imported summary for {vid}")
        if summary is None:
            if cfg.no_summarize:
                continue
            tpath = Path(rec["transcript"])
            transcript_text = ""
            if tpath.exists():
                raw = tpath.read_text()
                transcript_text = "\n".join(
                    ln for ln in raw.splitlines()
                    if not ln.startswith("#")).strip()
            # No captions: re-fetch description/chapters for the prompt.
            if not rec.get("has_captions"):
                try:
                    full = fetch_metadata(cfg, vid)
                    meta["description"] = full["description"]
                    meta["chapters"] = full["chapters"]
                except Exception as e:
                    log(f"  metadata re-fetch failed for {vid}: {e}")
            summary = summarize_with_cli(cfg, meta, transcript_text)
            if summary is None:
                continue
        beat_secs = [s for s in (parse_ts(b) for b in summary.get("beats", []))
                     if s is not None]
        frames = grab_frames(cfg, meta, beat_secs)
        note_path = write_note(cfg, meta, summary, rec["transcript"],
                               frames, rec.get("has_captions", False))
        rec["status"] = "summarized"
        rec["rating"] = summary.get("rating")
        rec["verdict"] = summary.get("verdict")
        rec["note"] = str(note_path)
        rec["frames"] = frames
        save_json(cfg.state_path, state)
        done.append(vid)
        log(f"  summarized {vid} — "
            f"{summary.get('rating')}/10 {summary.get('verdict', '')}")
    return done


def write_pending_notes(cfg, state, fetched):
    """Write notes for fetched videos that have no summary yet."""
    for meta, _text, has_captions in fetched:
        summary = {"takeaways": [], "beats": [], "rating": None,
                   "rating_reason": "", "verdict": "", "raw": ""}
        write_note(cfg, meta, summary, "", [], has_captions)


def digest_name(cfg, stamp):
    base = cfg.output_dir / "digests" / f"digest-{stamp}.md"
    if not base.exists():
        return base
    return cfg.output_dir / "digests" / (
        f"digest-{stamp}-{datetime.now().strftime('%H%M%S')}.md")


def build_digest(cfg, state, date_str):
    """Write digest md + html for videos processed on date_str.

    If nothing is new, still writes a one-line 'nothing new' digest so a
    scheduled run leaves a trace.
    """
    todays = [(vid, rec) for vid, rec in state["videos"].items()
              if rec.get("processed_at") == date_str
              and rec.get("status") in ("summarized", "fetched")]
    md_path = digest_name(cfg, date_str)
    lines = [f"# YouTube digest — {date_str}", ""]
    if not todays:
        lines.append("_Nothing new in the configured playlists today._")
        lines.append("")
    for vid, rec in todays:
        rating = rec.get("rating")
        rating_s = f"{rating:g}/10" if isinstance(rating, (int, float)) else "—"
        verdict = rec.get("verdict", "summary pending")
        lines.append(f"## {rec['title']}")
        lines.append("")
        lines.append(f"{rec.get('channel', '')} · "
                     f"{fmt_ts(rec.get('duration', 0))} · "
                     f"**{rating_s}** · {verdict}")
        lines.append("")
        note = rec.get("note")
        summary = None
        if note and Path(note).exists():
            summary = parse_summary(Path(note).read_text())
        takeaways = (summary or {}).get("takeaways", [])[:3]
        for t in takeaways:
            lines.append(f"- {t}")
        if takeaways:
            lines.append("")
        lines.append(f"[Full note]({note}) · "
                     f"[Watch on YouTube]({rec['url']})")
        lines.append("")
    md_path.write_text("\n".join(lines))
    html_path = md_path.with_suffix(".html")
    html_path.write_text(digest_html(date_str, todays, state))
    log(f"digest written: {md_path.name} "
        f"({len(todays)} video{'s' if len(todays) != 1 else ''})")
    return md_path, html_path


def digest_html(date_str, todays, state):
    rows = []
    for vid, rec in todays:
        rating = rec.get("rating")
        rating_s = f"{rating:g}/10" if isinstance(rating, (int, float)) else "—"
        verdict = html.escape(rec.get("verdict", "summary pending"))
        note = rec.get("note", "")
        summary = None
        if note and Path(note).exists():
            summary = parse_summary(Path(note).read_text())
        items = "".join(
            f"<li>{html.escape(t)}</li>"
            for t in (summary or {}).get("takeaways", [])[:3])
        rows.append(
            f"<h2>{html.escape(rec['title'])}</h2>"
            f"<p>{html.escape(rec.get('channel', ''))} · "
            f"<b>{rating_s}</b> · {verdict}</p>"
            f"<ul>{items}</ul>"
            f"<p><a href=\"{html.escape(rec['url'])}\">Watch on YouTube</a>"
            + (f" · <a href=\"file://{html.escape(note)}\">Full note</a>"
               if note else "") + "</p>")
    body = ("<p><i>Nothing new in the configured playlists today.</i></p>"
            if not rows else "\n".join(rows))
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>YouTube digest — {date_str}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:24px auto;
padding:0 16px;color:#111}}h1{{font-size:22px}}h2{{font-size:17px;margin-top:28px}}
li{{margin:4px 0}}a{{color:#0b5fff}}</style></head>
<body><h1>YouTube digest — {date_str}</h1>
{body}</body></html>
"""


# ---------------------------------------------------------------- CLI

def build_parser():
    # Global options live in `common` so they work before AND after the
    # subcommand (e.g. both `run --no-summarize` and `--no-summarize run`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(DEFAULT_CONFIG))
    common.add_argument("--output-dir")
    common.add_argument("--cookies", help="cookies.txt for private playlists")
    common.add_argument("--no-summarize", action="store_true",
                        help="skip the summarizer; notes marked pending")
    common.add_argument("--no-frames", action="store_true")
    common.add_argument("--limit", type=int,
                        help="max new videos to process this run")
    common.add_argument("--force", help="reprocess one video id")
    common.add_argument("--import-dir",
                        help="dir of pre-written <video-id>.md summaries")
    common.add_argument("--summarizer-cmd",
                        help="summarizer command, e.g. 'claude -p'")
    common.add_argument("--yt-dlp", help="path to the yt-dlp binary")
    common.add_argument("--version", action="version", version=VERSION)
    p = argparse.ArgumentParser(
        prog="yt_digest.py", parents=[common],
        description="YouTube playlist -> summary notes + digest.")
    sub = p.add_subparsers(dest="command", required=False)
    sub.add_parser("run", parents=[common],
                   help="fetch + summarize + digest (default)")
    sub.add_parser("fetch", parents=[common],
                   help="fetch new videos only")
    sub.add_parser("summarize", parents=[common],
                   help="summarize pending videos")
    sub.add_parser("digest", parents=[common],
                   help="rebuild today's digest from state")
    sub.add_parser("status", parents=[common], help="show state counts")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    cmd = args.command or "run"
    cfg = Config(args.config, {
        "output_dir": args.output_dir, "limit": args.limit,
        "cookies": args.cookies, "no_summarize": args.no_summarize,
        "no_frames": args.no_frames, "yt_dlp": args.yt_dlp,
        "summarizer_cmd": args.summarizer_cmd,
    })
    state = load_json(cfg.state_path, {"videos": {}})

    if args.force and args.force in state["videos"]:
        log(f"forgetting {args.force} for reprocessing")
        del state["videos"][args.force]
        save_json(cfg.state_path, state)

    if cmd == "status":
        counts = {}
        for rec in state["videos"].values():
            counts[rec.get("status", "?")] = counts.get(rec.get("status", "?"), 0) + 1
        print(f"tracked videos: {len(state['videos'])}")
        for k, v in sorted(counts.items()):
            print(f"  {k}: {v}")
        pending = [vid for vid, rec in state["videos"].items()
                   if rec.get("status") == "fetched"]
        if pending:
            print("pending summary:", ", ".join(pending))
        return 0

    if cmd == "digest":
        build_digest(cfg, state, today())
        return 0

    if cmd in ("run", "fetch"):
        fetched = fetch_new_videos(cfg, state)
        if cmd == "fetch":
            write_pending_notes(cfg, state, fetched)
            return 0
        # run: summarize what we just fetched (and anything still pending)
        if not cfg.no_summarize or args.import_dir:
            summarize_pending(cfg, state, import_dir=args.import_dir)
        else:
            write_pending_notes(cfg, state, fetched)
        build_digest(cfg, state, today())
        return 0

    if cmd == "summarize":
        done = summarize_pending(cfg, state, import_dir=args.import_dir)
        log(f"summarized {len(done)} video(s)")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
