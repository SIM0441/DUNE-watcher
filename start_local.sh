#!/usr/bin/env bash
# Local watcher for when the PC is on: polls every ~90s, pops a Windows notification and
# opens the booking page in your browser. Telegram is left to the GitHub Actions job.
# Started at Windows login by "dune-watcher.vbs" in the Startup folder.
cd "$(dirname "$0")"
exec 9>/tmp/dune-watcher.lock
flock -n 9 || exit 0   # already running
unset TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_IDS
export DUNE_STATE_FILE="$PWD/seen_local.json"
exec python3 -u watch_dune.py --interval 90 --open-browser >> watch.log 2>&1
