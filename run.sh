#!/bin/bash
# Foreground launcher for the bot.
# When run under launchd, PATH is set in the plist's EnvironmentVariables.
# When run manually, ensure `python3` and `claude` are on PATH.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 bot.py
