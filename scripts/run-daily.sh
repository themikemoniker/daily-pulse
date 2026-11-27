#!/usr/bin/env bash
# Daily cron entry point: run the pipeline, build the digest, publish the site, and tell the operator
# (via ~/bin/notify) when a run fails or a module has failed twice in a row.
#
# Non-interactive by design: stdin is /dev/null and git may never prompt. Exits non-zero when the run,
# the digest or the publish fails. A second invocation while one is running exits 0 without doing anything.
#
# Overridable for tests and unusual hosts (all optional):
#   DAILY_PULSE_BIN      the daily-pulse executable   (default: <repo>/.venv/bin/daily-pulse)
#   DAILY_PULSE_CONFIG   the config file              (default: <repo>/config/daily-pulse.toml)
#   DAILY_PULSE_DATA_DIR where state, logs, the run lock and the pipeline output live; must match the
#                        config's data_dir             (default: <repo>/data)
#   DAILY_PULSE_SITE     the built site directory     (default: <repo>/site)
#   DAILY_PULSE_REMOTE   git remote to publish to     (default: the repo's origin)
#   DAILY_PULSE_NOTIFY   the notifier command         (default: ~/bin/notify)
#   DAILY_PULSE_STALE_HOURS  notify when "already running" persists this long (default: 3)
#   DAILY_PULSE_DATE     the run date, YYYY-MM-DD     (default: today)
#   DAILY_PULSE_GH       the gh executable whose login supplies GITHUB_TOKEN when none is set
#                        (default: gh on PATH, else ~/.local/opt/gh-*/bin/gh)
set -u

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PATH="$HOME/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
export GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true SSH_ASKPASS=true
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes}"
export LC_ALL="${LC_ALL:-C.UTF-8}"
exec </dev/null
cd "$ROOT" || exit 1

# The GitHub API allows 60 unauthenticated requests an hour, which a watchlist of a dozen accounts uses up.
# With no GITHUB_TOKEN set, borrow the operator's gh login: the token lives in this process's environment
# only (never in a file, a log or the command line). No gh, no login or a slow gh: carry on unauthenticated.
if [ -z "${GITHUB_TOKEN:-}" ]; then
    GH=${DAILY_PULSE_GH:-$(command -v gh 2>/dev/null || true)}
    if [ -z "$GH" ]; then
        for candidate in "$HOME"/.local/opt/gh-*/bin/gh; do [ -x "$candidate" ] && GH=$candidate; done
    fi
    if [ -n "$GH" ] && [ -x "$GH" ]; then
        token=$(timeout 20 "$GH" auth token 2>/dev/null </dev/null || true)
        [ -n "$token" ] && export GITHUB_TOKEN=$token
        unset token
    fi
fi

BIN=${DAILY_PULSE_BIN:-$ROOT/.venv/bin/daily-pulse}
CONFIG=${DAILY_PULSE_CONFIG:-$ROOT/config/daily-pulse.toml}
DATA_DIR=${DAILY_PULSE_DATA_DIR:-$ROOT/data}
SITE=${DAILY_PULSE_SITE:-$ROOT/site}
NOTIFY=${DAILY_PULSE_NOTIFY:-$HOME/bin/notify}
DATE=${DAILY_PULSE_DATE:-$(date +%F)}
OUT=$DATA_DIR/out

log() { printf '%s run-daily: %s\n' "$(date '+%F %T')" "$*"; }

notify() {  # one short line; never fails the script, never carries command output
    if [ -x "$NOTIFY" ]; then
        "$NOTIFY" -t daily-pulse -- "$1" >/dev/null 2>&1 9>&- || log "notify command failed"
    else
        log "notify command not found: $NOTIFY"
    fi
}

fail() {  # fail <step> <exit code>
    log "$1 failed (exit $2)"
    notify "daily-pulse $DATE: $1 failed (exit $2)"
    exit 1
}

mkdir -p "$DATA_DIR" || { log "cannot create $DATA_DIR"; exit 1; }
# A lock of our own: `daily-pulse run` takes data/run.lock itself and would see ours as another run.
exec 9>"$DATA_DIR/cron.lock" || { log "cannot open the lock file in $DATA_DIR"; exit 1; }
if ! flock -n 9; then
    log "already running; exiting without changes"
    # A run that has held the lock for hours is stuck (or something leaked the lock): say so.
    started=$(cat "$DATA_DIR/cron.started" 2>/dev/null || true)
    case $started in
        ''|*[!0-9]*) ;;
        *) hours=$(( ($(date +%s) - started) / 3600 ))
           if [ "$hours" -ge "${DAILY_PULSE_STALE_HOURS:-3}" ]; then
               notify "daily-pulse $DATE: still 'already running' after ${hours}h; the site is not updating"
           fi ;;
    esac
    exit 0
fi
date +%s > "$DATA_DIR/cron.started"

log "starting $DATE"
"$BIN" run --config "$CONFIG" --out "$OUT" --date "$DATE" 9>&- || fail "run" "$?"

RESULT=$OUT/run-$DATE.json
if [ ! -f "$RESULT" ]; then
    log "no run result at $RESULT"
    notify "daily-pulse $DATE: run failed (no result written)"
    exit 1
fi
# `flagged` lists the modules that have now failed twice in a row.
FLAGGED=$(python3 - "$RESULT" <<'PY'
import json, sys
try:
    flagged = json.load(open(sys.argv[1])).get("flagged", [])
    print(",".join(str(n) for n in flagged))
except (OSError, ValueError, AttributeError):
    sys.exit(1)
PY
) || { log "cannot read $RESULT"; notify "daily-pulse $DATE: run failed (unreadable result)"; exit 1; }

if [ -n "$FLAGGED" ]; then
    log "modules failing repeatedly: $FLAGGED"
    notify "daily-pulse $DATE: failing twice in a row: ${FLAGGED//,/, }"
fi

"$BIN" digest --config "$CONFIG" --out "$OUT" --date "$DATE" 9>&- || fail "digest" "$?"
"$BIN" publish --config "$CONFIG" --out "$OUT" --site "$SITE" --date "$DATE" \
    ${DAILY_PULSE_REMOTE:+--remote "$DAILY_PULSE_REMOTE"} 9>&- || fail "publish" "$?"

log "done $DATE"
