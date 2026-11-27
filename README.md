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

## Summarizer

- `daily_pulse.summarizer.Summarizer` rates one item by running `claude -p` with the prompt on stdin: `prompts/rubric.md` (the rubric, in every call) plus `prompts/summarize.md` (the task and the strict-JSON reply format). Both files are read on every call, so editing them needs no code change. `summarize_items(summarizer, items, notes_dir, date, transcripts=None)` rates each item and passes the result to `write_note`.
- The reply must be one JSON object: summary, takeaways, beats (with timestamps), rating (integer 1-10), reason, verdict (`watch`, `skim` or `skip`), tags, diamond flag with reason, optional quotes. An invalid reply, a failed or timed-out process, or a note the writer refuses leaves that item "unrated" (logged) and the run carries on. A malformed reply is retried once.
- `[summarizer] max_calls` (default 30) caps `claude -p` calls per run, retries included. Once reached, remaining items are unrated.
- The binary is `[summarizer] claude_cmd` or, if set, `$DAILY_PULSE_CLAUDE` (a command string; tests point it at a fake script). Tests never run the real `claude`.
- Returned ratings pass through the quote guard against the transcript and the item excerpt: no run of more than 40 copied words, and quoted words capped under 50 per item. Not yet called by the runner; the digest ticket plugs in.

## GitHub source

`daily_pulse/adapters/github.py` uses the GitHub REST API (stdlib `urllib`), unauthenticated unless the `GITHUB_TOKEN` environment variable is set (read per request, sent only as a bearer header, never logged or stored).

- **Rising**: repos created in the last `rising_days` (default 7), top 10 by stars. Metrics carry `stars`, `age_days` and `star_velocity` (stars / age in days, age floored at one day). Id `rising:<owner/repo>`.
- **Watched accounts** (`watch_accounts`): new repos (`repo:`), releases in the last `lookback_days` (`release:<repo>:<tag>`), tags when a recently pushed repo has no release (`tag:`), a push on the run day when nothing else explains it (`push:<repo>:<date>`), and a star gain of at least `star_delta` (default 50) since the previous run (`stars:<repo>:<date>`). The star baseline lives in `data/state/github.json`; it is not written on `--dry-run`. Forks are ignored.
- **Watched repos** (`watch_repos`, e.g. `can1357/oh-my-pi`): releases only. Top-contributor tracking is not implemented.
- **Rate limits**: `X-RateLimit-Remaining` is read on every response. A 403/429 rate-limit response waits out a short `Retry-After`/reset (at most 30 s) once; otherwise, or when the budget hits zero, the adapter logs a warning, stops calling the API and returns what it already has (`adapter.last_run["degraded"]`). It never raises for a rate limit.
- Tests use recorded responses in `tests/fixtures/github/responses.json` (keyed by API path) via `--fixtures`, or an injected transport. Unauthenticated limits are 60 requests/hour, so a long watch list may degrade without a token.
