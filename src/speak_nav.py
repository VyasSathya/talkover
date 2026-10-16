#!/usr/bin/env python3
"""
Next / Back for the Claude Code reading (2026-10-10): once you have heard enough of a reply, skip to the next one,
or go back and hear the previous one again.

Used by voice_inbox.py (the user's spoken "next" / "skip", "back", "again" / "repeat that") and voice_panel.py (the Next and
Back buttons). The reading itself is ~/.claude/hooks/speak-response.ps1, which does the work:
  next   stop the reply being read now and play the next queued one (nothing if none)
  back   play the reply before the one being read, from its start (then the interrupted one, from its start)
  again  play the reply being read again from its start
With a reply being read, the command goes to that speaker through claude-speak.nav ("<seq> <cmd>"). With nothing
being read, back and again start the hook in replay mode, which plays the last reply read (from
claude-speak.history); next does nothing.

All files are in %TEMP%, read when called (a test points TEMP at a sandbox).
"""
import ctypes
import os
import subprocess
import time
from pathlib import Path

from voice_config import SPEAK_HOOK as HOOK
COMMANDS = ("next", "back", "again")
_last_seq = [0]


def _tmp() -> Path:
    return Path(os.getenv("TEMP", ""))


def _alive(pid: int) -> bool:
    """The process exists and has not exited (never signals it: os.kill(pid, 0) terminates on Windows)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong(0)
        ok = k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def reading_pid():
    """pid of the speaker holding the lock, if it is alive."""
    try:
        pid = int((_tmp() / "claude-speak.pid").read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def has_history() -> bool:
    d = _tmp() / "claude-speak.history"
    try:
        return any(p.suffix == ".txt" for p in d.iterdir())
    except OSError:
        return False


def muted() -> bool:
    t = _tmp()
    return (t / "claude-speak.muted").exists() or (t / "claude-voice.muted").exists()


def _write_nav(cmd: str) -> None:
    seq = max(time.time_ns() // 1000, _last_seq[0] + 1)
    _last_seq[0] = seq
    p = _tmp() / "claude-speak.nav"
    tmp = p.with_name("claude-speak.nav.tmp")
    tmp.write_text("%d %s" % (seq, cmd), encoding="ascii")
    os.replace(tmp, p)


def _start_replay(cmd: str) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)     # hidden: never a console window
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle",
                      "Hidden", "-File", str(HOOK), "-Mode", "replay", "-Nav", cmd],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def nav(cmd: str) -> str:
    """Do next / back / again. Returns what happened, in plain words, for the caller's log."""
    cmd = (cmd or "").lower()
    if cmd not in COMMANDS:
        return "unknown command %r" % cmd
    if muted():
        return "the voice is off: nothing to %s" % cmd
    pid = reading_pid()
    if pid is not None:
        _write_nav(cmd)
        return "sent to the reading (pid %d)" % pid
    if cmd == "next":
        return "nothing is being read: nothing to skip"
    if not has_history():
        return "nothing has been read yet"
    _start_replay(cmd)
    return "nothing was being read: replaying the last reply"
