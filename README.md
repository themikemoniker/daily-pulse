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
