#!/usr/bin/env bash
# yt-digest wrapper: finds yt-dlp, then runs yt_digest.py.
# Usage: ./run.sh [run|fetch|summarize|digest|status] [options]
# All arguments are passed through to yt_digest.py.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

# Locate yt-dlp: PATH first, then common install spots.
if ! command -v yt-dlp >/dev/null 2>&1; then
  for d in "$HOME/.local/bin" /opt/homebrew/bin /usr/local/bin /tmp/pdfenv/bin; do
    if [ -x "$d/yt-dlp" ]; then PATH="$d:$PATH"; break; fi
  done
fi

if ! command -v yt-dlp >/dev/null 2>&1; then
  echo "yt-dlp not found. Install it first:" >&2
  echo "  macOS:  brew install yt-dlp" >&2
  echo "  Linux:  pipx install yt-dlp   (or: python3 -m pip install --user yt-dlp)" >&2
  exit 1
fi

exec python3 "$DIR/yt_digest.py" "$@"
