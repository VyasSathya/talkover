#!/usr/bin/env bash
# Real-device check for macOS (and Linux). Run after ./install.sh, from the repo folder:
#   ./scripts/mac_check.sh
# Takes about a minute. It speaks one sentence, listens to the mic for 3 seconds (say something), types into a
# throwaway tmux pane, opens the panel for a few seconds, and writes mac-check-report.txt to paste back.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VPY="$ROOT/.venv/bin/python"
REPORT="$ROOT/mac-check-report.txt"
[ -x "$VPY" ] || { echo "Run ./install.sh first." >&2; exit 1; }
: > "$REPORT"
say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
res() { printf '%-28s %s\n' "$1" "$2" | tee -a "$REPORT"; }

say "Talkover device check  $(date '+%Y-%m-%d %H:%M:%S')"
say "$(uname -srm); $(sw_vers -productVersion 2>/dev/null || true); $("$VPY" -V)"
say "engines: $(grep -E '^TALKOVER_ENGINES=' config.env 2>/dev/null | cut -d= -f2)"
say ""

# 1. engines (start the native ones for the check if they are not running)
started=""
if ! curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:$(grep -E '^TALKOVER_STT_PORT=' config.env | cut -d= -f2 || echo 2022)/health" 2>/dev/null; then
  if grep -qE '^TALKOVER_ENGINES=native' config.env; then
    "$VPY" engines/native/server.py >/dev/null 2>&1 & started=$!
    for _ in $(seq 1 60); do sleep 2; "$VPY" - <<'PY' && break
import sys, urllib.request; sys.path.insert(0, "src"); import voice_config as c
try: urllib.request.urlopen(c.STT_HEALTH, timeout=2); urllib.request.urlopen(c.TTS_BASE + "/v1/audio/voices", timeout=2)
except Exception: sys.exit(1)
PY
    done
  fi
fi
"$VPY" src/voice_health.py --engines-only 2>&1 | tee -a "$REPORT"
say ""

# 2. spoken reply through the real speakers (the Python speak hook, a Stop-hook payload)
say "You should now HEAR: \"Talkover can speak on this Mac.\""
printf '{"session_id":"maccheck","hook_event_name":"Stop","last_assistant_message":"Talkover can speak on this Mac."}' \
  | "$VPY" hooks/speak_response.py
res "speak hook" "exit $? (did you hear the sentence? note it in the report)"

# 3. microphone level (first run triggers the macOS Microphone prompt for this terminal app)
say "Say something for 3 seconds..."
"$VPY" - <<'PY' 2>&1 | tee -a "$REPORT"
import numpy as np, sounddevice as sd
try:
    x = sd.rec(int(3 * 16000), samplerate=16000, channels=1, dtype="int16"); sd.wait()
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    print("%-28s %s" % ("microphone", ("OK, rms %.0f" % rms) if rms > 30 else ("SILENT (rms %.1f): check System Settings > Privacy > Microphone" % rms)))
    print("%-28s %s" % ("input device", sd.query_devices(kind="input")["name"]))
except Exception as e:
    print("%-28s FAIL %s" % ("microphone", e))
PY

# 4. typing into a tmux pane (what the inbox does with TALKOVER_DELIVERY=tmux)
if command -v tmux >/dev/null 2>&1; then
  out="$(mktemp)"; sock="tkocheck$$"
  tmux -L "$sock" new-session -d -s t "cat > $out"
  "$VPY" - "$sock" <<'PY'
import subprocess, sys, time; sys.path.insert(0, "src"); import delivery_posix as d
sock = sys.argv[1]
class T(d.Tmux):
    def _keys(self, *a):
        return subprocess.run(["tmux", "-L", sock, "send-keys", "-t", "t", *a]).returncode == 0
s = d.Sink(T(target="t")); s.stage("typed by Talkover", True); s.send(["typed by Talkover"], ["keys"], ""); time.sleep(0.5)
PY
  got="$(cat "$out")"; tmux -L "$sock" kill-server 2>/dev/null; rm -f "$out"
  [ "$got" = "typed by Talkover" ] && res "tmux delivery" "OK" || res "tmux delivery" "FAIL (got: $got)"
else
  res "tmux delivery" "SKIPPED (brew install tmux)"
fi

# 5. Accessibility (only needed for TALKOVER_DELIVERY=macos-app)
if [ "$(uname -s)" = Darwin ]; then
  if osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true' >/dev/null 2>&1; then
    res "accessibility" "OK (paste mode can work)"
  else
    res "accessibility" "NOT GRANTED (only needed for macos-app paste mode)"
  fi
fi

# 6. the floating panel opens (look at the top of the screen for 6 seconds)
say "The panel should appear at the top center of the screen for 6 seconds."
TALKOVER_MUTEX_PREFIX=MacCheck_ "$VPY" src/voice_panel.py >/dev/null 2>&1 & pp=$!
sleep 6
if kill -0 "$pp" 2>/dev/null; then res "panel" "OK (running; did it look right? rounded, transparent corners)"; kill "$pp"; else res "panel" "FAIL (exited; see ~/.talkover/logs/voice_panel.log)"; fi

[ -n "$started" ] && kill "$started" 2>/dev/null
say ""
say "Report: $REPORT"
