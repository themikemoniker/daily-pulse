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
CSS/JS); links are absolute, built from `site.base_url` in the config. Rebuilding a date gives identical files.
