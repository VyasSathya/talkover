#!/usr/bin/env bash
# Start Talkover on macOS / Linux: the engines (Docker, or native through the watchdog), then the watchdog, the mic inbox
# and the floating panel, in the background. Safe to run twice: each program keeps a single copy (lock file).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
VPY="$ROOT/.venv/bin/python"
[ -x "$VPY" ] || { echo "Not installed yet: run ./install.sh first." >&2; exit 1; }
CONFIG="$ROOT/config.env"; [ -f "$CONFIG" ] || CONFIG="$ROOT/config.example.env"
setting() { local v; v="$(grep -E "^\s*$1\s*=" "$CONFIG" | tail -1 | cut -d= -f2- | tr -d ' "'"'"'' || true)"; echo "${v:-$2}"; }

if [ "$(setting TALKOVER_ENGINES docker)" = docker ]; then
  docker compose --env-file "$CONFIG" -f docker-compose.yml up -d >/dev/null 2>&1 \
    || echo "docker compose up failed (is Docker running?). The watchdog keeps retrying." >&2
fi                                 # native engines: the watchdog starts them

programs=(voice_watchdog.py voice_inbox.py voice_panel.py)
[ "$(setting TALKOVER_STOP_WORD 0)" = 1 ] && programs+=(stop_word_listener.py)
for p in "${programs[@]}"; do
  nohup "$VPY" "$ROOT/src/$p" >/dev/null 2>&1 &
done
echo 'Talkover started. Talk any time; say "stop" or "wait" to cut a reading. Stop it with ./stop.sh.'
