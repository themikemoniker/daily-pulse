#!/usr/bin/env bash
# run_all.sh — the daily digest pipeline. Runs every module, then the
# unified digest. Safe to run from cron/launchd: each step logs and
# continues on failure; all modules are idempotent.
#
#   ./run_all.sh
#
# Env it respects:
#   X_BEARER_TOKEN   X API token (x_digest skips itself without it)
#   GITHUB_TOKEN     raises GitHub limit 60 -> 5000 req/hr (recommended)
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$HOME/workspace/yt-digest"
LOG="$OUT/digest-run.log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

# yt-dlp location (run.sh logic, reused here)
if ! command -v yt-dlp >/dev/null 2>&1; then
  for d in "$HOME/.local/bin" /opt/homebrew/bin /usr/local/bin /tmp/pdfenv/bin; do
    if [ -x "$d/yt-dlp" ]; then PATH="$d:$PATH"; break; fi
  done
fi

cd "$DIR"
log "=== daily digest run $(date '+%Y-%m-%d %H:%M') ==="
python3 yt_digest.py run >>"$LOG" 2>&1 || log "WARN: yt_digest failed (see log)"
python3 ig_digest.py >>"$LOG" 2>&1 || log "WARN: ig_digest failed (see log)"
if [ -n "${X_BEARER_TOKEN:-}" ]; then
  python3 x_digest.py >>"$LOG" 2>&1 || log "WARN: x_digest failed (see log)"
else
  log "x_digest skipped: X_BEARER_TOKEN not set (see x-feasibility.md)"
fi
python3 gh_digest.py >>"$LOG" 2>&1 || log "WARN: gh_digest failed (see log)"
python3 digest_all.py >>"$LOG" 2>&1 || log "WARN: digest_all failed (see log)"
log "=== done. Digest: $OUT/digests/daily-$(date '+%Y-%m-%d').md ==="
