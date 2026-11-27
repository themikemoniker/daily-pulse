#!/usr/bin/env bash
# Install yt-dlp into this repo's virtualenv (.venv). Needs no root: it is a plain pip install
# into the venv, which the current user owns. Safe to re-run; it upgrades yt-dlp.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="$repo/.venv"

if [ ! -x "$venv/bin/pip" ]; then
  # This host has no python3-venv; virtualenv is installed per user.
  python3 -m virtualenv -q "$venv"
fi

"$venv/bin/pip" install --quiet --upgrade yt-dlp

echo "yt-dlp installed: $venv/bin/yt-dlp"
echo "Point the config at it: [sources.youtube] ytdlp_cmd = \"$venv/bin/yt-dlp\""
echo "(or export DAILY_PULSE_YTDLP=$venv/bin/yt-dlp; cron has a short PATH, so use the full path)."
