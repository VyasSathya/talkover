#!/usr/bin/env bash
# Stop the Talkover programs started from this folder, and any reading in progress.
#   ./stop.sh            programs and readings
#   ./stop.sh --engines  also the engines (Docker containers, or the native engine process)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="${TALKOVER_TEMP:-${TMPDIR:-/tmp}}"; TMP="${TMP%/}"
pkill -f "$ROOT/src/" && echo "stopped the Talkover programs"
date '+%Y-%m-%dT%H:%M:%S' > "$TMP/claude-speak.cancel" 2>/dev/null || true      # queued readings exit
pkill -f "$ROOT/hooks/speak_response.py" && echo "stopped the reading"
rm -f "$TMP/claude-speak.pid" "$TMP/claude-speak.pause" 2>/dev/null
if [ "${1:-}" = "--engines" ]; then
  if [ -f "$TMP/talkover-engines.pid" ]; then
    kill "$(cat "$TMP/talkover-engines.pid")" 2>/dev/null && echo "stopped the native engines"
    rm -f "$TMP/talkover-engines.pid"
  fi
  pkill -f "$ROOT/engines/native/server.py" 2>/dev/null
  CONFIG="$ROOT/config.env"; [ -f "$CONFIG" ] || CONFIG="$ROOT/config.example.env"
  command -v docker >/dev/null 2>&1 && docker compose --env-file "$CONFIG" -f "$ROOT/docker-compose.yml" stop 2>/dev/null
fi
exit 0
