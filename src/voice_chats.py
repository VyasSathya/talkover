#!/usr/bin/env python3
"""
Which Claude Code chat the voice goes to (2026-10-10). With several chats going at once, auto-picking the chat on
screen is the default, but you can also pick one: the list shows the chats active recently, by the titles the Claude
app shows, and stays current when a chat is renamed.

  auto    (no pin file) as before: the user's words go into whatever chat the Claude window shows, every chat's replies are read
  (auto since 2026-10-11: only the chat on screen is read; see "the chat on screen" below)
  pinned  %TEMP%\\claude-voice.pin = "<cliSessionId> <local sessionId>": the user's words go into that chat (voice_inbox.py
          shows it in the app first, through the sidebar) and only its replies are read (speak-response.ps1 compares the
          hook's session_id with the first field)

The chats and their titles come from the Claude desktop app's own session files,
%APPDATA%\\Claude\\claude-code-sessions\\<account>\\<org>\\local_<id>.json (fields title, titleSource, cliSessionId,
lastActivityAt, isArchived). The app rewrites the file when a chat is renamed, so a title is re-read whenever the
file changes. Used by voice_panel.py (the list) and voice_inbox.py (delivery).
"""
import json
import os
import time
from pathlib import Path

SESSIONS_DIR = Path(os.getenv("APPDATA", "")) / "Claude" / "claude-code-sessions"
ACTIVE_HOURS = float(os.getenv("VOICE_CHATS_HOURS", "3"))
DOC_SUFFIX = " - Claude Code"          # the chat's web page is named "<title> - Claude Code"

_cache = {}                            # path -> (mtime, record or None)


def pin_file() -> Path:
    return Path(os.getenv("TEMP", "")) / "claude-voice.pin"


# ---------------------------------------------------------------- the app's session files

def _read(path: Path, mtime: float):
    c = _cache.get(path)
    if c and c[0] == mtime:
        return c[1]
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
        rec = {"cli": str(j.get("cliSessionId") or ""), "local": str(j.get("sessionId") or path.stem),
               "title": " ".join(str(j.get("title") or "").split()),
               "activity": float(j.get("lastActivityAt") or 0) / 1000.0,
               "archived": bool(j.get("isArchived")), "path": str(path)}
    except (OSError, ValueError, TypeError):
        if c:
            return c[1]                # half-written while the app saves it: keep the last good read
        return None
    _cache[path] = (mtime, rec)
    return rec


def _files():
    try:
        accounts = list(os.scandir(SESSIONS_DIR))
    except OSError:
        return
    for a in accounts:
        if not a.is_dir():
            continue
        try:
            orgs = list(os.scandir(a.path))
        except OSError:
            continue
        for o in orgs:
            if not o.is_dir():
                continue
            try:
                for f in os.scandir(o.path):
                    if f.name.startswith("local_") and f.name.endswith(".json"):
                        try:
                            yield Path(f.path), f.stat().st_mtime
                        except OSError:
                            pass
            except OSError:
                continue


def find(local: str):
    """The record for one chat by its local session id, read fresh (its title may have changed)."""
    if not local:
        return None
    for path, mt in _files():
        if path.stem == local:
            return _read(path, mt)
    return None


def active(hours: float = ACTIVE_HOURS, keep: str = ""):
    """Chats with activity in the last `hours`, newest first; `keep` (a local id) is listed whatever its age.
    Archived chats and chats with no title yet are left out (there is no title of the app's to show)."""
    since = time.time() - hours * 3600.0
    out = []
    for path, mt in _files():
        if mt < since and path.stem != keep:
            continue
        r = _read(path, mt)
        if not r or r["archived"] or not r["title"] or not r["cli"]:
            continue
        if r["activity"] >= since or r["local"] == keep:
            out.append(r)
    out.sort(key=lambda r: r["activity"], reverse=True)
    return out


# ---------------------------------------------------------------- the pin

def read_pin():
    """(cliSessionId, local id) of the chat the voice is pinned to, or None for auto."""
    try:
        parts = pin_file().read_text(encoding="utf-8-sig").split()
    except OSError:
        return None
    return (parts[0], parts[1]) if len(parts) >= 2 else None


def set_pin(rec) -> None:
    p = pin_file()
    tmp = p.with_name("claude-voice.pin.tmp")
    tmp.write_text("%s %s" % (rec["cli"], rec["local"]), encoding="ascii")
    os.replace(tmp, p)


def clear_pin() -> None:
    try:
        pin_file().unlink()
    except OSError:
        pass


def pinned():
    """The pinned chat's record (fresh title), or None for auto. A pin whose chat is gone or archived reads as None."""
    pin = read_pin()
    if not pin:
        return None
    r = find(pin[1])
    if not r or r["archived"]:
        return None
    return r


# ---------------------------------------------------------------- the chat on screen (auto, 2026-10-11)
# In auto, a reply plays when you go to its chat, never before. voice_inbox.py writes claude-voice.shown every 1.5 s; the
# speak hook reads only that chat's replies in auto and holds the others in claude-speak.held\<first 8>.json.

def shown_file() -> Path:
    return Path(os.getenv("TEMP", "")) / "claude-voice.shown"


def held_dir() -> Path:
    return Path(os.getenv("TEMP", "")) / "claude-speak.held"


def chat_by_title(title: str):
    """The app's chat with this title (the most recently active one if two share it), or None."""
    for r in active(hours=24 * 14):
        if r["title"] == title:
            return r
    return None


def write_shown(rec) -> None:
    """rec = the chat on screen; None = no Claude Code chat on screen (every reply is held)."""
    p = shown_file()
    tmp = p.with_name("claude-voice.shown.tmp")
    tmp.write_text(("%s %s" % (rec["cli"], rec["title"])) if rec else "", encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- the Claude window (pywinauto UIA wrapper)

def shown_title(win):
    """Title of the chat the Claude window shows, from its page name "<title> - Claude Code"; None if not a code chat."""
    for d in win.descendants(control_type="Document"):
        n = (d.element_info.name or "").strip()
        if n.endswith(DOC_SUFFIX):
            return " ".join(n[:-len(DOC_SUFFIX)].split())
    return None


def sidebar_button(win, title: str):
    """The sidebar entry for a chat. The app names it "<status> <title>" ("Running Docs - Map", "Idle Notes");
    the shortest name that ends with the title wins, so "Map" never picks "Docs - Map"."""
    best = None
    for b in win.descendants(control_type="Button"):
        n = " ".join((b.element_info.name or "").split())
        if n.startswith("More options for ") or n.endswith(", rename session"):
            continue
        if n == title or n.endswith(" " + title):
            if best is None or len(n) < len(best[0]):
                best = (n, b)
    return best[1] if best else None


def show_chat(win, title: str, timeout: float = 2.5):
    """Make the Claude window show this chat (a UIA Invoke on its sidebar entry: no focus change, but the chat on
    screen does change). Returns (ok, what happened)."""
    if shown_title(win) == title:
        return True, "already shown"
    b = sidebar_button(win, title)
    if b is None:
        return False, "not in the app's sidebar (is the sidebar hidden?)"
    b.invoke()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        time.sleep(0.05)
        if shown_title(win) == title:
            time.sleep(0.15)           # its chat box is mounted a moment after the page is renamed
            return True, "switched the app to it"
    return False, "the app did not switch to it"
