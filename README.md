# daily-pulse

A daily trends digest. Under construction; see the spec issue.

## Running

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

- `daily_pulse.notes.write_note(item, rating, notes_dir, date, transcript="")` writes `<notes_dir>/<date>/<source>-<id>.md` (frontmatter plus body; videos get takeaways, a timestamped argument, rating, verdict; github/instagram/x get summary, rating, reason, verdict, tags). Rewriting the same item gives the identical file. An invalid rating (not 1-10, unknown verdict, no reason, bad beat timestamp) raises `NoteError` and writes nothing. Not yet called by the runner; the summarizer and digest tickets plug into it.
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
- Returned ratings pass through the quote guard against the transcript and the item excerpt: no run of more than 40 copied words, and quoted words capped under 50 per item. Not yet called by the runner; the digest ticket plugs in.

## YouTube source

- Set `[sources.youtube] playlist_url` to a public playlist. While it is empty the source reports `not configured` and the run carries on.
- yt-dlp is needed at run time (it is not a Python dependency). `scripts/install-ytdlp.sh` installs it into the repo's `.venv` with a plain pip install (no sudo). Point the config at the binary with `ytdlp_cmd = "/path/to/repo/.venv/bin/yt-dlp"` in `[sources.youtube]` (cron has a short PATH, so use the full path), or set `$DAILY_PULSE_YTDLP`, which wins over the config. With `--fixtures` the adapter reads recorded output from `<fixtures>/youtube/` (`playlist.json`, `<id>.info.json`, `<id>.en.vtt` for manual subtitles, `<id>.en.auto.vtt` for auto captions) and never runs yt-dlp.
- The playlist is read with `yt-dlp --flat-playlist -J` in the order YouTube lists it, taken as newest first, so the first-run cap keeps the top 10. If your playlist appends new videos at the bottom, set `playlist_order = "oldest_first"` in `[sources.youtube]`. Private and deleted placeholder entries are skipped. Each new video then gets `-J` metadata (title, channel, duration, upload date, views) and English captions: manual subtitles first, auto captions only if there are none.
- Captions are cleaned (markup, entities and rolling auto-caption repeats removed; `[mm:ss]` timestamps kept) and saved only to `<data_dir>/transcripts/youtube-<id>.txt`. The cleaned text goes to the summarizer, and the note (`<out>/notes/<date>/youtube-<id>.md`) holds an own-words summary, takeaways, a timestamped argument and short quotes, all passing the quote guard.
- No captions: the video is rated from its metadata and description excerpt, and the note is flagged with a `metadata-only` tag and a "Metadata only (no captions)" reason prefix, and it carries no timestamped beats (there is no transcript to ground them).
- A video whose yt-dlp call, caption download or rating fails is logged and skipped. It is not marked processed, so the next run retries it; the other videos are unaffected. A rate-limited or otherwise failed caption download counts as a failure (it is never taken for "no captions"). Rate limits, timeouts, network errors, the bot check and a broken yt-dlp are never held against a video: the run stops for the source (reported `failed`) and every video is tried again next run. Videos that can never work (private, members-only, age-restricted, region-blocked, unavailable) are skipped at once, and a video that is not out yet (premiere, scheduled live) is given up on after 3 failures (counted in `<data_dir>/state/youtube-attempts.json`): it is marked processed, logged, and no longer fails the source. Failed ratings are not counted, so a `claude` outage never makes videos get dropped.
- A video with no captions that was uploaded within the last 2 days, or is live or upcoming, is not rated yet: it stays unprocessed (not a failure) and is looked at again next run, for up to 3 days from the first run that saw it (`<data_dir>/state/youtube-pending.json`). After that it gets the metadata-only note. Processed IDs make a rerun find nothing new.

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
