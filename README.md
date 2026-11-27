# daily-pulse

A daily trends digest. Under construction; see the spec issue.

## Running

    .venv/bin/daily-pulse run --fixtures tests/fixtures --out /tmp/dp-out [--date YYYY-MM-DD] [--dry-run] [--config config/daily-pulse.toml]

- Config lives in `config/daily-pulse.toml` (sources, account lists, first-run cap, `data_dir`). A relative `data_dir` is resolved against the config file's directory, so cron can run from any cwd. Requires Python 3.11+.
- Local state, logs and the run lock live under `data_dir` (default `data/`, gitignored): processed IDs, per-module failure counters (`state/`), run logs (`logs/`), `run.lock`.
- Each source adapter runs in isolation. An exception is logged with its traceback and counted; a module with 2 consecutive failures appears in `flagged` in `<out>/run-<date>.json`. A same-date rerun keeps the earlier file as `run-<date>.<n>.json`.
- A source's first run (until it has processed at least one item) processes only its newest `first_run_cap` items; the older backlog is marked seen and never processed.
- A second run started while one is in progress prints a message and exits 0 without touching state.
- `--dry-run` runs the adapters and prints the result, writing no state, logs, lock or output.
- Adapters register with `daily_pulse.adapters.registry.register(...)` and implement `status`, `list_new`, `fetch_details` (see `daily_pulse/adapters/base.py`).
- Errors are one-line messages with exit 1: unreadable or invalid config, unreadable or wrong-shape state files, a state file that cannot be written (the run stops, the source is not blamed, the partial result is still written), an unusable lock location, or an unwritable `--out` (checked with a real test write before any item is consumed). If the result file cannot be written after state is committed, the result is saved to `data/logs/run-<date>.unsaved.json`.
- Known trade-off: processed IDs are saved per item as the run goes (so a crash never reprocesses finished items), while the result file is written at the end. A hard crash (SIGKILL, power loss) between the two can leave items marked processed without a result; clean failures are covered by the preflight write test and the `unsaved.json` fallback. Downstream tickets that write per-item notes should write them before calling `mark_processed`.
