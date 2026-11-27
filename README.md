# daily-pulse

A daily trends digest for one reader. Once a day it collects new items from YouTube, Instagram, X and GitHub,
rates each against a rubric (`prompts/rubric.md`) with `claude -p`, writes one note per item, builds a ranked
digest (markdown and email-ready HTML) and a static site, and publishes the site to the `gh-pages` branch.
Live site: https://themikemoniker.github.io/daily-pulse/. Python 3.11+, standard library only at runtime.

Source status in v1: **YouTube** is collected, rated and noted end to end. **GitHub** and **Instagram** items are
collected and appear in the run result (`run-<date>.json`), but the runner does not rate or write notes for them
yet, so they do not reach the digest or the site. **X** is deferred (see [Deferred sources](#deferred-sources)).

## Setup

    python3 -m virtualenv .venv && .venv/bin/pip install -e '.[dev]'    # or: python3 -m venv .venv
    scripts/install-ytdlp.sh                                           # yt-dlp into .venv/bin/yt-dlp, no sudo

1. **Install yt-dlp** with `scripts/install-ytdlp.sh`, then set `ytdlp_cmd = "/path/to/repo/.venv/bin/yt-dlp"` under
   `[sources.youtube]` (cron has a short PATH) or export `DAILY_PULSE_YTDLP`, which wins over the config.
2. **Configure sources and accounts** in `config/daily-pulse.toml`: `[sources.youtube] playlist_url`;
   `[sources.github] watch_accounts` / `watch_repos` / `rising_days`; `[sources.x] handles` (stored for later);
   `[sources.instagram] kinds`. Each source has `enabled`. Adding an account is a one-line edit. Other tables:
   `[run]` (`first_run_cap`, `alert_after_failures`), `[paths] data_dir`, `[site] base_url`, `[summarizer]`
   (`claude_cmd`, `max_calls`) and `[quality]` (see [Quality gate](#quality-gate)).
3. **Environment** (never in the config or the repo): `GITHUB_TOKEN` (optional, higher GitHub rate limits),
   `IG_CLI_CMD` (the Instagram command), `DAILY_PULSE_CLAUDE` (overrides `claude_cmd`), `DAILY_PULSE_YTDLP`.
4. `claude` must be on the PATH of whatever runs the pipeline; it uses your Claude subscription, no API key.

## Privacy rules

- Transcripts, captions, processed-ID state, triage logs and run logs stay under `data/` (gitignored, never
  pushed). `publish` refuses a site that contains the data directory, a dotfile, a symlink, an unexpected file type
  or anything that looks like a secret.
- Notes, digests and the site carry own-words summaries, ratings, verdicts, tags and links only. Quotes are short:
  under 50 quoted words per item in total, enforced by `daily_pulse/quoteguard.py`.
- `main` holds code, prompts and docs only; the cron job writes only the `gh-pages` branch.

## Tests

    .venv/bin/pytest -q

`tests/test_e2e.py` runs `run`, `digest`, `site` and `publish` through the real CLI on recorded fixtures (no
network, a fake `claude`, a local bare git remote) and checks that a second run finds nothing new and changes no
output, and that one failing source does not hide the others. Tests never call the network, `yt-dlp` or `claude`.

## Backlog (v2, not built)

Frame grabs with ffmpeg at key beats; thought-leader and account discovery; thumbs up/down curation; an X read
route; a real Instagram CLI satisfying `docs/instagram-adapter.md`; Whisper transcription for reels; rating and
noting GitHub and Instagram items in the runner. Tracked in the "v2 backlog" issue.

## Running

    .venv/bin/daily-pulse run --out DIR [--config FILE] [--date YYYY-MM-DD] [--fixtures DIR] [--dry-run]
    .venv/bin/daily-pulse digest --out DIR [--config FILE] [--date YYYY-MM-DD] [--base-url URL]
    .venv/bin/daily-pulse site --out DIR [--date YYYY-MM-DD] [--site DIR]
    .venv/bin/daily-pulse publish [--out DIR] [--site DIR] [--remote URL] [--branch gh-pages] [--no-build]

`scripts/run-daily.sh` chains `run`, `digest` and `publish`; see [Publishing and the daily cron job](#publishing-and-the-daily-cron-job)
for the cron line. A dry run against recorded inputs:

    .venv/bin/daily-pulse run --fixtures tests/fixtures --out /tmp/dp-out --dry-run

    .venv/bin/daily-pulse run --fixtures tests/fixtures --out /tmp/dp-out [--date YYYY-MM-DD] [--dry-run] [--config config/daily-pulse.toml]

- Config lives in `config/daily-pulse.toml` (sources, account lists, first-run cap, `data_dir`). The default `--config` is `config/daily-pulse.toml` relative to the cwd, so cron should pass an absolute `--config` (or `cd` to the repo first). A relative `data_dir` is resolved against the config file's directory (the shipped `../data` is `<repo>/data`), so state and the lock are the same whatever the cwd. Requires Python 3.11+.
- Local state, logs and the run lock live under `data_dir` (default `data/`, gitignored): processed IDs, per-module failure counters (`state/`), run logs (`logs/`), `run.lock`.
- Each source adapter runs in isolation. An exception is logged with its traceback and counted; a module with 2 consecutive failures appears in `flagged` in `<out>/run-<date>.json`. A same-date rerun keeps the earlier file as `run-<date>.<n>.json`.
- A source's first run (until it has processed at least one item) processes only its newest `first_run_cap` items; the older backlog is marked seen and never processed.
- A second run started while one is in progress prints a message and exits 0 without touching state.
- `--dry-run` runs the adapters and prints the result, writing no state, logs, lock or output.
- Adapters register with `daily_pulse.adapters.registry.register(...)` and implement `status`, `list_new`, `fetch_details` (see `daily_pulse/adapters/base.py`).
- Errors are one-line messages with exit 1: unreadable or invalid config, unreadable or wrong-shape state files, a state file that cannot be written (the run stops, the source is not blamed, the partial result is still written), an unusable lock location, or an unwritable `--out` (checked with a real test write before any item is consumed). If the result file cannot be written after state is committed, the result is saved to `<data_dir>/logs/run-<date>.unsaved.json` (a numbered name if one already exists).
- Known trade-off: processed IDs are saved per item as the run goes (so a crash never reprocesses finished items), while the result file is written at the end. A hard crash (SIGKILL, power loss) between the two can leave items marked processed without a result; clean failures are covered by the preflight write test and the `unsaved.json` fallback. Downstream tickets that write per-item notes should write them before calling `mark_processed`.

## Notes and privacy guards

- `daily_pulse.notes.write_note(item, rating, notes_dir, date, transcript="")` writes `<notes_dir>/<date>/<source>-<id>.md` (frontmatter plus body; videos get takeaways, a timestamped argument, rating, verdict; github/instagram/x get summary, rating, reason, verdict, tags). Rewriting the same item gives the identical file. An invalid rating (not 1-10, unknown verdict, no reason, bad beat timestamp) raises `NoteError` and writes nothing. The runner calls it for YouTube items only so far.
- Full transcripts and captions are written only by `write_transcript(data_dir, item, text)` to `<data_dir>/transcripts/`, never to `notes/`. Title, author and tags are guarded too, and matching ignores case, curly apostrophes, markdown underscores, zero-width characters and caption markup.
- `daily_pulse.quoteguard` enforces the limits: a run of more than 40 consecutive words matching the local transcript/caption (or the item's excerpt) is cut to a 20-word snippet, and all quoted words in a note (the `quotes` list, shown as blockquotes, plus quoted spans and any lifted run of 8+ words in free text) share a budget of 49.

## Daily digest

`daily-pulse digest --out <dir> [--date YYYY-MM-DD] [--base-url URL]` builds `<dir>/digests/<date>.md` and
`<date>.html` from the notes in `<dir>/notes/<date>/` and the source statuses in `<dir>/run-<date>.json`.
Items are ranked by rating (newest first on ties); items rated 9+ and under 48 hours old are repeated in a
"Diamonds in the rough" section at the top. The HTML is email-ready (inline styles, table layout, no external
CSS/JS); links are absolute, built from `site.base_url` in the config. Item ages are measured from the run's
`generated_at` (recorded in `run-<date>.json`; falls back to the end of the date, UTC), so rebuilding a date gives
identical files.

## Summarizer

- `daily_pulse.summarizer.Summarizer` rates one item by running `claude -p` with the prompt on stdin: `prompts/rubric.md` (the rubric, in every call) plus `prompts/summarize.md` (the task and the strict-JSON reply format). Both files are read on every call, so editing them needs no code change. `summarize_items(summarizer, items, notes_dir, date, transcripts=None)` rates each item and passes the result to `write_note`.
- The reply must be one JSON object: summary, takeaways, beats (with timestamps), rating (integer 1-10), reason, verdict (`watch`, `skim` or `skip`), tags, diamond flag with reason, optional quotes. An invalid reply, a failed or timed-out process, or a note the writer refuses leaves that item "unrated" (logged) and the run carries on. A malformed reply is retried once.
- `[summarizer] max_calls` (default 30) caps `claude -p` calls per run, retries included. Once reached, remaining items are unrated.
- The binary is `[summarizer] claude_cmd` or, if set, `$DAILY_PULSE_CLAUDE` (a command string; tests point it at a fake script). Tests never run the real `claude`.
- Returned ratings pass through the quote guard against the transcript and the item excerpt: no run of more than 40 copied words, and quoted words capped under 50 per item. The runner calls it for YouTube items only so far.

## YouTube source

- Set `[sources.youtube] playlist_url` to a public playlist (the shipped config points at the "brain" playlist). While it is empty the source reports `not configured` and the run carries on.
- yt-dlp is needed at run time (it is not a Python dependency). `scripts/install-ytdlp.sh` installs it into the repo's `.venv` with a plain pip install (no sudo). Point the config at the binary with `ytdlp_cmd = "/path/to/repo/.venv/bin/yt-dlp"` in `[sources.youtube]` (cron has a short PATH, so use the full path), or set `$DAILY_PULSE_YTDLP`, which wins over the config. With `--fixtures` the adapter reads recorded output from `<fixtures>/youtube/` (`playlist.json`, `<id>.info.json`, `<id>.en.vtt` for manual subtitles, `<id>.en.auto.vtt` for auto captions) and never runs yt-dlp.
- The playlist is read with `yt-dlp --flat-playlist -J`. Entries that carry an upload date are ordered by it, newest first. Otherwise `playlist_order` in `[sources.youtube]` says how the list is ordered: `"newest_first"` or `"oldest_first"` (new videos appended at the bottom, as in the shipped playlist). When it is unset the order is detected by comparing the upload dates of the first and last entries (two `-J` calls, remembered in `<data_dir>/state/youtube-order.json`; if the dates cannot tell, it is newest first and a warning is logged). The first-run cap keeps the newest videos that pass the [quality gate](#quality-gate). Private and deleted placeholder entries are skipped. Each new video then gets `-J` metadata (title, channel, duration, upload date, views) and English captions: manual subtitles first, auto captions only if there are none.
- Captions are requested with `--sub-langs en.*,en` and the file is chosen deterministically (`.en.vtt` if present, else the first by name). They are cleaned (markup and entities removed; rolling auto-caption repeats removed for auto captions only, never from manual subtitles; `[mm:ss]` timestamps kept). Captions that are nothing but sound tags such as `[Music]` count as no captions (manual ones then fall back to auto captions) and saved only to `<data_dir>/transcripts/youtube-<id>.txt`. The cleaned text goes to the summarizer, and the note (`<out>/notes/<date>/youtube-<id>.md`) holds an own-words summary, takeaways, a timestamped argument and short quotes, all passing the quote guard.
- No captions: the video is rated from its metadata and description excerpt, and the note is flagged with a `metadata-only` tag and a "Metadata only (no captions)" reason prefix, and it carries no timestamped beats (there is no transcript to ground them).
- A video whose yt-dlp call, caption download or rating fails is logged and skipped. It is not marked processed, so the next run retries it; the other videos are unaffected. A rate-limited or otherwise failed caption download counts as a failure (it is never taken for "no captions"). Failures fall into four kinds:
  - **Source-level** (HTTP 429/5xx, "try again later", timeouts, DNS or connection resets, the bot check, a broken yt-dlp): never a strike against a video. One such failure does not stop the source, so a very long stream whose `-J` call times out cannot block the others; it is counted for that video in `<data_dir>/state/youtube-source-failures.json`. When two different videos fail this way in one run, yt-dlp is clearly down: the run stops for the source (reported `failed`), this run's counts are taken back, and every video is tried again next run. A failure is counted toward giving up on the video only in a run where another video's yt-dlp call worked (otherwise yt-dlp, not the video, is broken, so a multi-day outage never costs a video its place: it is simply retried every run). A video counted in 5 such runs is given up on (marked processed, logged); the count starts again whenever yt-dlp answers for the video or it fails some other way.
  - **Upcoming** (premieres and live events that have not started): a deferral, not a failure. The video stays unprocessed and is looked at again next run, for up to 3 days from the first run that saw it; after that it is an ordinary strike.
  - **Permanent** (private, members-only, age-restricted, region-blocked, removed): skipped at once. "This content isn't available" is not in the source-level list because it is also how YouTube words one removed video.
  - **Anything else** (an extractor error): a strike. The video is given up on after 3 failures (`<data_dir>/state/youtube-attempts.json`): marked processed, logged, and no longer failing the source. Failed ratings are not counted, so a `claude` outage never makes videos get dropped.
- A video with no captions that was uploaded within the last 2 days, or is live or upcoming, is not rated yet: it stays unprocessed (not a failure) and is looked at again next run, for up to 3 days from the first run that saw it (`<data_dir>/state/youtube-pending.json`). After that it gets the metadata-only note. Processed IDs make a rerun find nothing new.
- The per-video state files (`youtube-attempts.json`, `youtube-pending.json`, `youtube-source-failures.json`) do not grow forever: a video's entries are removed when it is finished (noted, given up on or skipped), and entries for videos that have left the playlist are pruned on each run.
- `scripts/install-ytdlp.sh` creates the venv with `python3 -m venv` and falls back to `virtualenv` when `python3-venv` is missing.

## Quality gate

Weak and off-topic items are weeded out cheaply, before a full `claude -p` summary is spent on them. It applies to YouTube and Instagram (GitHub items are already rating-based) and is configured in `[quality]`:

| key | default | meaning |
| --- | --- | --- |
| `min_rating` | 5 | the digest and site rank only items rated at least this (and not `skip`) |
| `min_duration_seconds` / `max_duration_seconds` | 240 / 7200 | YouTube only: drops Shorts and clips, and very long streams. A video whose listing carries no duration is never dropped for length |
| `channel_allowlist` | `[]` | channels (case-insensitive, whole name) that are always kept and bypass every check and triage |
| `channel_blocklist` | `[]` | channels dropped before any claude call |
| `title_blocklist` | `[]` | case-insensitive substrings of a title that drop the item |
| `max_triage_calls` | 10 | cap on triage `claude -p` calls per run |

1. **Rules** (free) run first on the playlist listing: allowlist, channel blocklist, title blocklist, duration.
2. **Triage** is one batched `claude -p` call per 40 remaining items. It sees only title, channel, duration and view count (never transcripts, captions or descriptions) and scores each 0-3 for relevance to the rubric. 0 or 1 drops the item. It uses the same hardening as the summarizer (no tools, scratch cwd, process-group kill on timeout, untrusted text delimited as data) and its own call cap. The prompt is `prompts/triage.md` plus the rubric. A malformed reply, a failed call, a missing entry or the call cap fails open: those items continue to the normal path and the reason is logged. `--dry-run` applies the rules but makes no triage call.
3. A dropped item is logged, marked processed (so it is never re-triaged) and gets no note. The run result lists it under the source's `dropped` (id, title, score, reason).
4. On a source's first run the gate sits before the first-run cap: the cap takes the newest items that pass, and triage stops as soon as it has enough, so a 250-video backlog costs one or two triage calls, not seven.
5. Every decision (rule drop, triage score kept or dropped, allowlisted keep) is appended to `<data_dir>/triage.jsonl` as `{id, source, title, score, decision, reason, date}`. The file is local only (mode 0600, under the gitignored `data/`) and nothing in the digest or site build reads it, so it never reaches `site/` or `gh-pages`. It is there so a later thumbs up/down feature can learn from it.
6. **Rating threshold.** The digest and the site rank only items with rating >= `min_rating` and verdict not `skip`; diamonds come only from the ranked items. Below-threshold notes are still written, and appear as one line, `Also seen: N below threshold` (collapsed in the site, a plain line of links in the digest). The run records its `min_rating` in `run-<date>.json`; `digest` and `site` use that value for the day (the config's, or 5, when there is no record), so an archived day renders the same later.

## GitHub source

`daily_pulse/adapters/github.py` uses the GitHub REST API (stdlib `urllib`), unauthenticated unless the `GITHUB_TOKEN` environment variable is set (read per request, sent only as a bearer header, never logged or stored).

- **Rising**: repos created in the last `rising_days` (default 7), top 10 by stars. Metrics carry `stars`, `age_days` and `star_velocity` (stars / age in days, age floored at one day). Id `rising:<owner/repo>`.
- **Watched accounts** (`watch_accounts`): new repos (`repo:`), releases in the last `lookback_days` (`release:<repo>:<tag>`), the newest tag when a recently pushed repo has no release (`tag:`), a push on the run day when nothing else explains it (`push:<repo>:<date>`), and a star gain of at least `star_delta` (default 50) since the previous run (`stars:<repo>:<date>`). The star baseline lives in `data/state/github.json`; it is not written on `--dry-run`. Forks are ignored.
- **Watched repos** (`watch_repos`, e.g. `can1357/oh-my-pi`): releases only, checked first so a tight rate limit cannot starve them. Top-contributor tracking is not implemented.
- **Rate limits**: `X-RateLimit-Remaining` is read on every response. A 403/429 rate-limit response waits out a short `Retry-After`/reset (at most 30 s) once; otherwise, or when the budget hits zero, the adapter logs a warning, stops calling the API and returns what it already has (`adapter.last_run["degraded"]`). It never raises for a rate limit.
- Tests use recorded responses in `tests/fixtures/github/responses.json` (keyed by API path) via `--fixtures`, or an injected transport. Unauthenticated limits are 60 requests/hour, so a long watch list may degrade without a token.

## Static site

`daily-pulse site --out <dir> [--date YYYY-MM-DD] [--site DIR]` builds `<dir>/site/` (default) in plain
HTML and one shared `style.css` (light/dark via `prefers-color-scheme`, no JavaScript, no external assets):
`index.html` (the day's ranked digest with a diamond callout), `archive/index.html`, `days/<date>.html`
and `notes/<date>/<note>.html`. It copies `notes/*.md` and `digests/<date>.{md,html}` into the tree and
reads nothing else, so `data/` can never reach the site. Pages are built from the site's own `notes/`, so
earlier days stay in the archive; a rebuild is byte-identical.

## Deferred sources

- **X: deferred** (`deferred: no usable read route`). Blockers: no X connector exists on the host, the X API free tier cannot read other accounts' posts, and raw scraping is forbidden. To unblock: provide a connector or a paid API tier that can read the accounts, then implement `list_new`/`fetch_details` in `daily_pulse/adapters/x.py`. The seed handles stay in `[sources.x].handles`.
- **Instagram: deferred until a CLI is configured** (`deferred: no CLI configured`). Blocker: no sanctioned read access to saved/liked reels. To unblock: set `IG_CLI_CMD` to a command that prints the JSON described in `docs/instagram-adapter.md` (saved reels first, then liked). Items carry metadata and a short caption excerpt only; they are rated through the summarizer.
- Both modules appear in the run result with their status and never fail the run.

## Publishing and the daily cron job

`daily-pulse publish [--out DIR] [--site DIR] [--remote URL] [--branch gh-pages] [--no-build]` builds the site
(unless `--no-build`) and force-pushes it as the **only** content of the `gh-pages` branch: one orphan commit
made in a throwaway repository, so removed pages disappear and no other branch is touched. It uses the host's
existing git credentials (SSH key or credential helper; no tokens live in the repo), never prompts, and
without `--remote` publishes to this checkout's `origin`. If the branch already holds exactly this content,
it makes no commit, pushes nothing and exits 0. A `.nojekyll` file is added so GitHub Pages serves the notes'
`.md` files as they are instead of rendering them with Jekyll.

Before anything is pushed the site is checked, and one problem fails the whole publish: a site that lives in or
contains the data directory, a symlink, a dotfile or any file that is not `.html`, `.css`, `.md` or
`runs/*.json`, or text that looks like a secret (GitHub/Anthropic/AWS/Slack tokens, private keys, or the value
of an env var named `*TOKEN`, `*SECRET`, `*PASSWORD`, `*API_KEY` or `IG_CLI_CMD`).

`scripts/run-daily.sh` is the cron entry point. It sets `PATH`, takes its own lock (`data/cron.lock`), then runs
`run`, `digest` and `publish` for today, with stdin closed and git prompts disabled. It exits non-zero if any
step fails, and calls `~/bin/notify -t daily-pulse -- "<message>"` when:

- a step fails (the message names the step and exit code; a failed `run` skips the digest and publish), or
- a module has failed twice in a row (`flagged` in `run-<date>.json`); the site is still published and the
  script exits 0.

A second invocation while one is running logs "already running" and exits 0, without notifying. Environment
overrides: `DAILY_PULSE_BIN`, `_CONFIG`, `_DATA_DIR` (must match the config's `data_dir`), `_SITE`, `_REMOTE`,
`_NOTIFY` (the notifier command), `_DATE`; see the header of the script.

The crontab line (07:00 in the host's local time; adjust the checkout path). The operator installs it with
`crontab -e` after a manual `scripts/run-daily.sh` has been verified; nothing here installs it:

    0 7 * * * /home/deux/github/daily-pulse/scripts/run-daily.sh >> /home/deux/github/daily-pulse/data/logs/cron.log 2>&1

`data/logs/` must exist before the first cron run (`mkdir -p data/logs`); the script itself creates `data/`.

Operating notes:

- **Credentials under cron.** Cron has no SSH agent and git is told never to prompt, so the host needs a push credential that works unattended: an unencrypted deploy key, a git credential helper for an HTTPS remote, or an agent socket. Otherwise publish fails cleanly and you are notified.
- **A refused publish.** If the check above names a file, the site already holds something that must not be public. Delete that file under `site/` (and its source note under `data/out/notes/`, or the next run copies it back), then rerun `scripts/run-daily.sh`.
- **`--site` and the data dir.** Without `--site`, `publish` uses `<out>/site`, which is refused when `--out` is inside the data dir (e.g. `data/out`). Pass `--site` explicitly, as the wrapper does.
- **Overlap with a manual run.** The wrapper's lock only stops two copies of the script. `daily-pulse run` started by hand at the same moment exits 0 without doing anything, and the wrapper then publishes whatever result is on disk. Don't run the pipeline by hand around 07:00.
- **Empty sites and stuck locks.** `publish` refuses a site directory without `index.html`, so a wrong `--site` or an empty build can never blank `gh-pages`. If "already running" persists for 3 hours or more (`DAILY_PULSE_STALE_HOURS`, measured from `data/cron.started`), the wrapper notifies. Children of the wrapper don't inherit the lock.
