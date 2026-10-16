#!/usr/bin/env python3
"""
Deliver text into the Claude desktop app's chat input WITHOUT taking focus.

Uses UI Automation (pywinauto, uia backend): finds the top-level window titled
"Claude", sets the chat input's value, then invokes the Send button. A game
(or anything else) can stay in front. Falls back to posting an Enter key to the
Chromium render window if no Send button is exposed.

    from uia_deliver import deliver
    ok = deliver("what I said")

Test from a shell (opens Notepad in front first so focus is elsewhere):
    python uia_deliver.py --test "hello from the background"
"""
import ctypes, ctypes.wintypes as W, logging, os, subprocess, sys, time

log = logging.getLogger("uia_deliver")
WINDOW_TITLE = os.getenv("VOICE_INBOX_WINDOW", "Claude")   # exact title of the Claude desktop window
SEND_NAMES = ("send", "send message", "submit")

u = ctypes.windll.user32
WM_KEYDOWN, WM_KEYUP, VK_RETURN = 0x0100, 0x0101, 0x0D


_CACHE = {"hwnd": None, "win": None}   # window wrapper reused between calls (keyed by HWND); dropped on any error


def _find_hwnd(title: str):
    """Top-level visible window with this exact title, via EnumWindows (under 1 ms). The old
    Desktop(backend="uia").windows() scan walked every window through UIA (0.9 to 1.7 s)."""
    found = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
    @EnumProc
    def cb(h, _):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                if b.value == title:
                    found.append(h)
        return True
    u.EnumWindows(cb, 0)
    return found[0] if found else None


def _claude_window():
    h = _find_hwnd(WINDOW_TITLE)
    if not h:
        _CACHE.update(hwnd=None, win=None)
        return None
    if _CACHE["hwnd"] != h or _CACHE["win"] is None:
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_element_info import UIAElementInfo
        _CACHE.update(hwnd=h, win=UIAWrapper(UIAElementInfo(h)))
    return _CACHE["win"]


def _forget():
    _CACHE.update(hwnd=None, win=None)


def view_mode(win=None) -> str:
    """'code' when the Claude window shows Claude Code, 'chat' when it shows a claude.ai chat (Chat and Cowork),
    'unknown' when the switch cannot be read. Voice is for Claude Code only: claude.ai chats have their own
    voice mode.
    The top bar's Mode switch is two radio buttons, "Chat and Cowork" and "Code" ("Code, working" while busy);
    an artifact pane has its own "Preview" / "Code" pair, so only the Chat radio is read: selected = chat,
    present and not selected = code. About 100 ms."""
    try:
        win = win or _claude_window()
        if win is None:
            return "unknown"
        for r in win.descendants(control_type="RadioButton"):
            name = (r.element_info.name or "").strip().lower()
            if name.startswith("chat") or name == "claude":  # 2026-10-10: the app renamed "Chat and Cowork" to "Claude"
                return "chat" if r.is_selected() else "code"
    except Exception as e:
        _forget()
        log.warning("view mode: %s", e)
    return "unknown"


def _chat_input(win):
    # Not cached: the walk is ~13 ms, and a stale editor element (app rebuilt its input without an
    # error) could swallow a message silently.
    edits = win.descendants(control_type="Edit")
    return edits[0] if edits else None


def _send_button(win):
    for b in win.descendants(control_type="Button"):
        if b.window_text().strip().lower() in SEND_NAMES:
            return b
    return None


def wait_send_button(win, timeout: float = 0.25, step: float = 0.025):
    """Poll for the Send button (it appears once the editor has taken the text) instead of a fixed
    250 ms sleep. None after `timeout` (Claude busy: there is only Stop; the caller posts Enter)."""
    end = time.monotonic() + timeout
    while True:
        btn = _send_button(win)
        if btn or time.monotonic() >= end:
            return btn
        time.sleep(step)


def _post_enter(win):
    hwnd = win.handle
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
    kids = []
    @EnumProc
    def cb(h, l):
        b = ctypes.create_unicode_buffer(256); u.GetClassNameW(h, b, 256)
        if b.value == "Chrome_RenderWidgetHostHWND": kids.append(h)
        return True
    u.EnumChildWindows(hwnd, cb, 0)
    target = kids[0] if kids else hwnd
    u.PostMessageW(target, WM_KEYDOWN, VK_RETURN, 0x001C0001)
    u.PostMessageW(target, WM_KEYUP, VK_RETURN, 0xC01C0001)


def deliver(text: str, retries: int = 3) -> bool:
    """Put `text` in the Claude chat box and send it. Returns True if a send was triggered."""
    for attempt in range(retries):
        try:
            win = _claude_window()
            if not win:
                log.warning("no window titled %r", WINDOW_TITLE); return False
            mode = view_mode(win)
            if mode != "code":
                log.warning("Claude window shows %s, not Claude Code: not delivered", mode); return False
            box = _chat_input(win)
            if not box:
                _forget()
                log.warning("no chat input exposed (accessibility tree not ready?)"); time.sleep(1); continue
            draft = (box.get_value() or "").strip()
            box.set_edit_text((draft + " " + text).strip() if draft else text)
            btn = wait_send_button(win)
            if btn:
                btn.invoke(); log.info("sent via Send button%s", " (draft appended)" if draft else "")
            else:
                _post_enter(win); log.info("no Send button exposed; posted Enter%s", " (draft appended)" if draft else "")
            return True
        except Exception as e:
            _forget()   # the window or its elements may have been rebuilt; resolve again
            log.warning("deliver attempt %d failed: %s", attempt + 1, e); time.sleep(0.5)
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--test" in sys.argv:
        msg = sys.argv[sys.argv.index("--test") + 1]
        np = subprocess.Popen(["notepad.exe"]); time.sleep(1.5)
        fg = u.GetForegroundWindow(); b = ctypes.create_unicode_buffer(256); u.GetWindowTextW(fg, b, 256)
        print("foreground during send:", repr(b.value))
        win = _claude_window(); print("send button names seen:", [x.window_text() for x in win.descendants(control_type='Button') if 'send' in x.window_text().lower()])
        print("deliver ->", deliver(msg))
        time.sleep(1.0); np.terminate()
