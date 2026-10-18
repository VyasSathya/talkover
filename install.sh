#!/usr/bin/env bash
# One-command install for Talkover on macOS and Linux (Windows: install.ps1).
#   ./install.sh                 native engines on Apple Silicon Macs, Docker elsewhere
#   ./install.sh --native        no Docker: Whisper and Kokoro run in Python (Metal on Apple Silicon)
#   ./install.sh --docker        the two Docker engines (docker-compose.yml)
#   ./install.sh --skip-hooks    do not touch Claude Code's settings.json
#   ./install.sh --settings P    write the hooks to another settings.json
#   ./install.sh --autostart     start Talkover at login (LaunchAgent on macOS, XDG autostart on Linux)
#   ./install.sh --python P      use this python3 for the venv
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
ENGINES=""
SKIP_HOOKS=0
AUTOSTART=0
SETTINGS="$HOME/.claude/settings.json"
PY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --native) ENGINES=native ;;
    --docker) ENGINES=docker ;;
    --skip-hooks) SKIP_HOOKS=1 ;;
    --autostart) AUTOSTART=1 ;;
    --settings) SETTINGS="$2"; shift ;;
    --python) PY="$2"; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[33m    %s\033[0m\n' "$1"; }
fail() { printf '\033[31merror: %s\033[0m\n' "$1" >&2; exit 1; }
OS="$(uname -s)"
ARCH="$(uname -m)"

# ---- settings
[ -f config.env ] || { cp config.example.env config.env; step "created config.env from config.example.env"; }
setting() { local v; v="$(grep -E "^\s*$1\s*=" config.env | tail -1 | cut -d= -f2- | tr -d ' "'"'"'' || true)"; echo "${v:-$2}"; }
set_setting() {   # set KEY=VALUE in config.env (replace or append)
  if grep -qE "^\s*$1\s*=" config.env; then
    sed -i.bak -E "s|^\s*$1\s*=.*|$1=$2|" config.env && rm -f config.env.bak
  else
    printf '%s=%s\n' "$1" "$2" >> config.env
  fi
}
if [ -z "$ENGINES" ]; then
  ENGINES="$(setting TALKOVER_ENGINES "")"
  if [ -z "$ENGINES" ]; then
    if [ "$OS" = Darwin ] && [ "$ARCH" = arm64 ]; then ENGINES=native; else ENGINES=docker; fi
  fi
fi
set_setting TALKOVER_ENGINES "$ENGINES"
STT_PORT="$(setting TALKOVER_STT_PORT 2022)"
TTS_PORT="$(setting TALKOVER_TTS_PORT 8880)"
STT_BASE="$(setting TALKOVER_STT_BASE "http://127.0.0.1:$STT_PORT")"
TTS_BASE="$(setting TALKOVER_TTS_BASE "http://127.0.0.1:$TTS_PORT")"
WHISPER_MODEL="$(setting TALKOVER_WHISPER_MODEL Systran/faster-whisper-small)"
step "engines: $ENGINES"

# ---- Python venv
step "Python environment"
if [ -z "$PY" ]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  done
fi
[ -n "$PY" ] || fail "Python 3.11+ not found. macOS: brew install python@3.13 (or python.org). Linux: your package manager."
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || fail "$PY is older than 3.11"
echo "    using $(command -v "$PY") ($("$PY" -V))"
[ -x .venv/bin/python ] || "$PY" -m venv .venv || fail "could not create .venv (Debian/Ubuntu: apt install python3-venv)"
VPY="$ROOT/.venv/bin/python"
"$VPY" -m pip install --disable-pip-version-check -q --upgrade pip
"$VPY" -m pip install --disable-pip-version-check -q -r requirements.txt || fail "pip install -r requirements.txt failed"
if [ "$ENGINES" = native ]; then
  "$VPY" -m pip install --disable-pip-version-check -q -r requirements-native.txt || fail "pip install -r requirements-native.txt failed"
fi
if ! "$VPY" -c 'import sounddevice' 2>/dev/null; then
  if [ "$OS" = Linux ]; then
    fail "PortAudio is missing. Install it (Debian/Ubuntu: sudo apt install libportaudio2; Fedora: sudo dnf install portaudio) and re-run."
  fi
  fail "sounddevice could not load PortAudio."
fi
"$VPY" -c 'import tkinter' 2>/dev/null || warn "tkinter is missing, so the floating panel will not open (macOS: brew install python-tk@3.13; Debian/Ubuntu: sudo apt install python3-tk). Voice works without it."

# ---- delivery
if command -v tmux >/dev/null 2>&1; then
  echo "    tmux found: run Claude Code inside tmux and Talkover types into that pane"
elif [ "$OS" = Darwin ]; then
  warn "tmux not found (brew install tmux). Without it Talkover pastes into the app named by TALKOVER_MAC_APP (default Terminal)."
else
  warn "tmux not found. On Linux Talkover types into Claude Code through tmux: install it (apt install tmux) and run claude inside it."
fi

# ---- engines
wait_url() {   # url what seconds
  local end=$((SECONDS + $3))
  while [ $SECONDS -lt $end ]; do curl -fsS -o /dev/null --max-time 5 "$1" && return 0; sleep 3; done
  fail "$2 did not answer at $1 within $3 s"
}
NATIVE_PID=""
if [ "$ENGINES" = docker ]; then
  step "Speech engines (Docker)"
  command -v docker >/dev/null 2>&1 || fail "Docker not found. Install Docker (Desktop on macOS), start it, re-run. Or: ./install.sh --native"
  docker info >/dev/null 2>&1 || fail "Docker is installed but not running. Start it and re-run."
  docker compose --env-file config.env -f docker-compose.yml up -d
  wait_url "$STT_BASE/health" Whisper 300
  step "Whisper model $WHISPER_MODEL (first time: a few hundred MB)"
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST --max-time 1800 "$STT_BASE/v1/models/$WHISPER_MODEL" || true)"
  case "$code" in 200|201|409) ;; *) fail "model download failed (HTTP $code)";; esac
  wait_url "$TTS_BASE/v1/audio/voices" Kokoro 600
else
  step "Native engines: downloading models (first time: about 800 MB)"
  "$VPY" engines/native/fetch_models.py
  step "Native engines: starting once to check them"
  "$VPY" engines/native/server.py >/dev/null 2>&1 &
  NATIVE_PID=$!
  trap '[ -n "$NATIVE_PID" ] && kill "$NATIVE_PID" 2>/dev/null || true' EXIT
  wait_url "$STT_BASE/health" Whisper 300
  wait_url "$TTS_BASE/v1/audio/voices" Kokoro 300
fi

# ---- Claude Code hooks
if [ "$SKIP_HOOKS" = 0 ]; then
  step "Claude Code hooks ($SETTINGS)"
  set +e
  "$VPY" src/install_hooks.py --settings "$SETTINGS"
  rc=$?
  set -e
  if [ $rc = 2 ]; then warn "left as is (see above)"; elif [ $rc != 0 ]; then fail "hook install failed"; fi
fi

# ---- autostart
if [ "$AUTOSTART" = 1 ]; then
  if [ "$OS" = Darwin ]; then
    plist="$HOME/Library/LaunchAgents/com.github.vyassathya.talkover.plist"
    mkdir -p "$(dirname "$plist")"
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.github.vyassathya.talkover</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$ROOT/start.sh</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST
    step "starts at login ($plist)"
  else
    desk="$HOME/.config/autostart/talkover.desktop"
    mkdir -p "$(dirname "$desk")"
    printf '[Desktop Entry]\nType=Application\nName=Talkover\nExec=/bin/bash %s/start.sh\nX-GNOME-Autostart-enabled=true\n' "$ROOT" > "$desk"
    step "starts at login ($desk)"
  fi
fi

step "checking"
"$VPY" src/voice_health.py --engines-only || true
[ -n "$NATIVE_PID" ] && { kill "$NATIVE_PID" 2>/dev/null || true; NATIVE_PID=""; }

echo
printf '\033[32mInstalled. Start it with:  ./start.sh\033[0m\n'
if [ "$OS" = Darwin ]; then
  cat <<'NOTE'
macOS will ask for two permissions the first time:
  Microphone     for the app you start Talkover from (Terminal, iTerm2, ...): allow it.
  Accessibility  only if Talkover pastes into an app instead of tmux: System Settings > Privacy & Security >
                 Accessibility, and turn on that terminal app (or Python).
NOTE
fi
