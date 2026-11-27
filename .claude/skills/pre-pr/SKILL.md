---
name: pre-pr
description: Run the checks CI runs before opening a PR in daily-pulse.
---

Run `.venv/bin/pytest -q` from the repo root. Fix every failure. Report the result. Do not skip or delete failing tests.
