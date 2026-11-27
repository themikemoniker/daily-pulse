# daily-pulse

A daily trends digest. Under construction; see the spec issue.

## Running

    .venv/bin/daily-pulse run --fixtures tests/fixtures --out /tmp/dp-out [--date YYYY-MM-DD] [--dry-run] [--config config/daily-pulse.toml]

- Config lives in `config/daily-pulse.toml` (sources, account lists, first-run cap, `data_dir`).
- Local state, logs and the run lock live under `data_dir` (default `data/`, gitignored): processed IDs, per-module failure counters (`state/`), run logs (`logs/`), `run.lock`.
- Each source adapter runs in isolation. An exception is logged with its traceback and counted; a module with 2 consecutive failures appears in `flagged` in `<out>/run-<date>.json`.
- A source's first run processes only its newest `first_run_cap` items; the older backlog is marked seen and never processed.
- A second run started while one is in progress prints a message and exits 0 without touching state.
- `--dry-run` runs the adapters and prints the result, writing no state, logs, lock or output.
- Adapters register with `daily_pulse.adapters.registry.register(...)` and implement `status`, `list_new`, `fetch_details` (see `daily_pulse/adapters/base.py`).
