+++
base = "main"
bootstrap = ["python3 -m virtualenv -q .venv", ".venv/bin/pip install -q -e '.[dev]'"]
default_type = "backend"

[task_types.backend]
agent = "claude"
model = "sonnet"
effort = "high"
skill = "/tdd"
+++

# AFK

## Pre-PR skill
Run `/pre-pr` before opening the PR. It runs the same checks CI does.

## Dev server
There is no server. The CLI is `.venv/bin/daily-pulse`. Output goes to `data/` and `site/`, both gitignored.

## Verification recipes
Run `.venv/bin/pytest -q`. For anything touching the pipeline, also run `.venv/bin/daily-pulse run --fixtures tests/fixtures --out /tmp/dp-$AFK_SLOT` once that command exists, and open the generated `index.html` to check it. Tests must never call the network, `yt-dlp` or `claude`; mock the adapters.

## Gotchas
- Never write full transcripts or captions into notes, digests or the site. Own-words summaries plus short quotes only, under 50 quoted words per item.
- The run must be idempotent, non-interactive, and must not die because one source failed.
- Never commit anything under `data/`.
- Python venv: this host has no python3-venv; use `python3 -m virtualenv` (user-installed).
