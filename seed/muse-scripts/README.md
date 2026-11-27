# yt-digest — daily trends digest (YouTube + Instagram + X + GitHub)

One daily run turns four sources into a single skimmable digest
(`~/workspace/yt-digest/digests/daily-YYYY-MM-DD.md` + `.html`),
ranked by rating, readable in ~5 minutes. Per-source notes live in
`~/workspace/yt-digest/<source>-notes/` in a common markdown format
(frontmatter + takeaways + verdict + links).

Built for Jordan: ratings use HIS rubric (harness engineering, multi-agent
orchestration, maker-checker/evals, model routing/cost, guard rails for the
accounting work). A **diamond in the rough** = high signal, early, low
views/stars but fast growth or a strong idea.

## Layout

```
~/workspace/scripts/yt-digest/
  yt_digest.py        YouTube playlists/videos -> notes + digest
  ig_digest.py        Instagram saved/liked reels -> notes
  x_digest.py         X accounts' posts + reposts -> notes
  gh_digest.py        GitHub rising repos + watched accounts -> notes
  digest_all.py       combines all four into one daily digest
  run.sh              yt-digest wrapper (finds yt-dlp)
  run_all.sh          daily runner: all four modules + digest_all
  config.json         YouTube sources (public playlists; see below)
  ig_config.json      Instagram account + sources
  x_config.json       X accounts to follow
  gh_config.json      GitHub topics + watched accounts
  prompts/summarize.md  the summarizer prompt + rating rubric
  instagram-feasibility.md, x-feasibility.md

~/workspace/yt-digest/
  notes/ transcripts/ frames/ digests/   YouTube
  ig-notes/  x-notes/  gh-notes/          other sources
  state.json ig-state.json x-state.json gh-state.json
```

## Quickstart

```sh
cd ~/workspace/scripts/yt-digest
./run_all.sh            # everything, then the unified digest
./run.sh run            # YouTube only
python3 ig_digest.py    # Instagram only
python3 gh_digest.py    # GitHub only
python3 digest_all.py   # rebuild today's unified digest from state
```

Prereqs: Python 3.10+, `yt-dlp` (`brew install yt-dlp` / `pipx install yt-dlp`),
`ffmpeg` (optional, for frames), Claude Code CLI `claude -p` (optional —
without it, YouTube notes are written with the summary marked pending and
`yt_digest.py summarize --import-dir <dir>` can import pre-written ones).

## Scheduling

Linux (deux) cron, daily 07:00:

```
0 7 * * * /home/hatch/workspace/scripts/yt-digest/run_all.sh
```

Mac launchd (`~/Library/LaunchAgents/com.jordan.ytdigest.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jordan.ytdigest</string>
  <key>ProgramArguments</key><array>
    <string>/Users/jordan/workspace/scripts/yt-digest/run_all.sh</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>7</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/jordan/workspace/yt-digest/digest-run.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/jordan/workspace/yt-digest/digest-run.log</string>
</dict></plist>
```

Then `launchctl load ~/Library/LaunchAgents/com.jordan.ytdigest.plist`.
Every module is idempotent: re-running with nothing new processes zero
items. Per-item errors are logged, never fatal to the run.

## YouTube — public playlists (no auth needed)

`config.json` lists public playlists/channels/videos. Watch Later and other
private playlists need auth — keep `--cookies` for that, but it is NOT the
primary path:

```sh
# one-time: export cookies from your browser (yt-dlp reads the browser profile)
yt-dlp --cookies-from-browser chrome --cookies ~/cookies.txt \
  "https://www.youtube.com/playlist?list=WL"
./run.sh run --cookies ~/cookies.txt
```

Never commit `cookies.txt` or put it in config. `yt_digest.py` also accepts
`--no-summarize` (transcript + metadata only) and `--force <video-id>`.

Known limitation: YouTube bot-checks yt-dlp intermittently, worse from
shared/cloud IPs. The script retries with alternate player clients
(android, web, tv_embedded), retries failed videos on the next run, and
still emits a note from title/description/chapters when captions are
unavailable — it never invents transcript content. On Jordan's own network
(Mac/deux) this should be far more reliable than on this shared VM, where
captions were blocked for the whole test window on 2026-11-27. If the
block persists, `yt-dlp --cookies-from-browser` is the reliable fix.

## Instagram — saved + recently-liked reels

`ig_digest.py` reads via the Instagram connector CLI (account
`@evolving_vlog`, already linked). See `instagram-feasibility.md` for the
full findings. v1: metadata + caption excerpt + stored media-understanding
narrative. **No transcript exists for reels** — audio → Whisper is a later,
unverified step. Liked items are "recently-liked" only (no full history).

## X — official API, pay-per-use (needs setup)

No X connector exists and there is no free read tier (see
`x-feasibility.md`). `x_digest.py` uses the official X API v2:
`developer.x.com` → Project/App → Bearer Token, then
`export X_BEARER_TOKEN=...` and list accounts in `x_config.json`.
~$0.60/day for 10 accounts × 10 posts. `--check` verifies setup without
spending reads. Without a token the module skips itself in `run_all.sh`.

## GitHub — unauthenticated works, token recommended

`gh_digest.py` calls api.github.com directly. Unauthenticated = 60 req/hr
per IP, which a shared IP can exhaust (seen live 2026-11-27); a token
(`export GITHUB_TOKEN=...`, classic, no scopes) raises it to 5000/hr.
On a 403 the run stops cleanly, records the reset time, and the next run
resumes. Rising = repos created in the last 7 days for the configured
topics, rated for RELEVANCE in one batched `claude -p` call
(`prompts/rate-repos.md`) and ranked by that rating, with star velocity
(stars/day) only as a tiebreaker. Diamond = relevance >= 7 AND >= 50
stars/day. `--rerate` forgets today's rising items and re-rates them.
Without a summarizer (`--no-summarize`, or no `claude` CLI on the
machine) repos are left unrated (status "unrated", shown without a
score, and re-rated on the next run that has a summarizer) — a keyword
heuristic is never ranked next to LLM scores. The keyword match is
shown as a signal in the note.
Optional `readme_excerpt: true` in `gh_config.json` adds a short README
excerpt to the rating prompt, but only when `GITHUB_TOKEN` is set (1
extra API call per repo).

## Ratings

- YouTube: LLM (`claude -p` + `prompts/summarize.md`), 1–10, verdict
  watch/skim/skip. Diamond in the rough is part of the rubric.
- Instagram: pending (no summarizer pass yet — the understanding narrative
  is the content; a `claude -p` pass can be added like YouTube's).
- X: transparent heuristics, labelled `rating_basis: heuristic` in the
  note. Keyword match to Jordan's interests + engagement.
- GitHub: relevance 1–10 from one batched `claude -p` call per run
  (`prompts/rate-repos.md`), labelled `rating_basis: llm`. Star velocity
  is a signal and a tiebreaker only — never the score. Without a
  summarizer repos are left unrated (never a fake score) and re-rated
  on the next run with a summarizer.

## Email

The digest `.html` is a single self-contained file with inline CSS —
attach it or paste it into any mail client. No sending is wired up yet:
that needs Jordan's call on which mailbox/sender to use (Gmail connector
is available; a `send_digest.sh` can be added once he picks the recipient).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| YouTube: "Sign in to confirm you're not a bot" | Bot check on this IP. Wait for the next run; if persistent, `--cookies-from-browser`. |
| GitHub: "RATE LIMITED" | Shared-IP quota spent. Set `GITHUB_TOKEN`; the run resumes next time. |
| X: "X_BEARER_TOKEN is not set" | Expected until setup — see `x-feasibility.md`. |
| Instagram: "not connected" | Re-link in Meta Accounts Center; `instagram-cli accounts` to verify. |
| Notes say "Summary pending" | No `claude` CLI on that machine. Install Claude Code, or import summaries. |
