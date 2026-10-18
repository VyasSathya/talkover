"""Typing what you said into Claude Code on macOS and Linux (Windows uses UI Automation, see uia_deliver.py).

Two ways, picked by TALKOVER_DELIVERY (auto | tmux | macos-app):

  tmux       Claude Code runs in a tmux pane. Text goes in with `tmux send-keys -l` and Enter, into that pane only,
             whatever app is in front. The most reliable way on both macOS and Linux. The pane is TALKOVER_TMUX_TARGET
             (e.g. "work:1.0" or "%3"), or found automatically: the first pane running `claude`.
  macos-app  macOS only. The text is pasted into an app (TALKOVER_MAC_APP: Terminal, iTerm2, Ghostty or Claude for the
             desktop app) through System Events: that app comes to the front for a moment, Cmd+V, Return, then the app
             you were in comes back and your clipboard is restored. Needs the Accessibility permission for the app
             that runs Talkover (System Settings > Privacy & Security > Accessibility).

auto: tmux when a Claude pane is found (or TALKOVER_TMUX_TARGET is set), else macos-app on macOS, else tmux.

The inbox stages text first (typed, not sent) and sends once you have been quiet; both backends follow that:
stage() types without Enter, send() adds anything not staged plus the cut-off note, then presses Enter.
"""
import logging
import os
import shutil
import subprocess
import sys
import time

log = logging.getLogger("voice_inbox")

IS_MAC = sys.platform == "darwin"


def _run(args, input_text=None, timeout=5.0):
    return subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)


class Tmux:
    name = "tmux"

    def __init__(self, target=None):
        self.fixed = target or os.getenv("TALKOVER_TMUX_TARGET") or None
        self._found = None

    @staticmethod
    def available() -> bool:
        return shutil.which("tmux") is not None

    def find_pane(self):
        """The first tmux pane whose running command or title says claude (Claude Code sets its pane title)."""
        try:
            r = _run(["tmux", "list-panes", "-a", "-F",
                      "#{pane_id}\t#{pane_current_command}\t#{pane_start_command}\t#{pane_title}"])
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and any("claude" in p.lower() for p in parts[1:]):
                return parts[0]
        return None

    def target(self):
        if self.fixed:
            return self.fixed
        if not self._found:
            self._found = self.find_pane()
        return self._found

    def _keys(self, *args) -> bool:
        t = self.target()
        if not t:
            log.warning("tmux: no Claude pane found (set TALKOVER_TMUX_TARGET, e.g. work:1.0)")
            return False
        try:
            r = _run(["tmux", "send-keys", "-t", t, *args])
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("tmux send-keys failed: %s", e)
            self._found = None
            return False
        if r.returncode != 0:
            log.warning("tmux send-keys to %s failed: %s", t, (r.stderr or "").strip())
            self._found = None              # the pane may be gone: look again next time
            return False
        return True

    def type(self, text: str) -> bool:
        return self._keys("-l", "--", text) if text else True

    def enter(self) -> bool:
        return self._keys("Enter")


class MacApp:
    name = "macos-app"

    def __init__(self, app=None):
        self.app = app or os.getenv("TALKOVER_MAC_APP") or "Terminal"

    @staticmethod
    def _q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _osa(self, script: str) -> bool:
        try:
            r = _run(["osascript", "-e", script], timeout=8.0)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("osascript failed: %s", e)
            return False
        if r.returncode != 0:
            log.warning("osascript: %s", (r.stderr or "").strip()[:300])
            return False
        return True

    def _paste(self, text: str, enter: bool) -> bool:
        """Bring the app forward, paste, optionally Return, then give focus and the clipboard back."""
        try:
            old = _run(["pbpaste"]).stdout
        except (OSError, subprocess.TimeoutExpired):
            old = None
        if text:
            _run(["pbcopy"], input_text=text)
        keys = []
        if text:
            keys.append('keystroke "v" using command down')
            keys.append("delay 0.08")
        if enter:
            keys.append("key code 36")
        script = "\n".join([
            'tell application "System Events" to set prevApp to name of first application process whose frontmost is true',
            "tell application %s to activate" % self._q(self.app),
            "delay 0.15",
            'tell application "System Events"',
            *("    " + k for k in keys),
            "end tell",
            "delay 0.05",
            'if prevApp is not %s then tell application prevApp to activate' % self._q(self.app),
        ])
        ok = self._osa(script)
        if old is not None and text:
            time.sleep(0.2)
            _run(["pbcopy"], input_text=old)
        return ok

    def type(self, text: str) -> bool:
        return self._paste(text, enter=False) if text else True

    def enter(self) -> bool:
        return self._paste("", enter=True)


def choose():
    """The backend for this machine (None on Windows: the inbox keeps UI Automation)."""
    if sys.platform == "win32":
        return None
    want = (os.getenv("TALKOVER_DELIVERY") or "auto").lower()
    if want == "tmux":
        return Tmux()
    if want in ("macos-app", "mac", "app"):
        return MacApp()
    t = Tmux()
    if t.fixed or (Tmux.available() and t.find_pane()):
        return t
    if IS_MAC:
        return MacApp()
    return t


class Sink:
    """The three calls the inbox makes, on top of a backend."""

    def __init__(self, backend):
        self.b = backend

    def describe(self) -> str:
        if isinstance(self.b, Tmux):
            return "tmux pane %s" % (self.b.target() or "(not found yet)")
        return "macOS app %s" % self.b.app

    def deliver(self, text: str) -> bool:
        return self.b.type(text) and self.b.enter()

    def stage(self, text: str, first: bool) -> str:
        return "keys" if self.b.type(text if first else " " + text) else ""

    def send(self, parts, staged, note: str) -> bool:
        rest = [p for p, how in zip(parts, staged) if not how] + ([note] if note else [])
        if rest and not self.b.type(" " + " ".join(rest)):
            return False
        time.sleep(0.1)
        return self.b.enter()


def sink():
    b = choose()
    return Sink(b) if b is not None else None
