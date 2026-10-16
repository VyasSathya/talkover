#!/usr/bin/env python3
"""
Talkover: a floating orb for the voice loop. Always on top, draggable, never takes focus.

One animated orb carries the state through motion:
  idle        slow breathing, cool blue
  you talk    teal, follows your mic level
  thinking    soft shimmer after you finished and before Claude answers
  Claude      warm coral, follows Claude's voice
  off         grey and still
Beside it, left to right:
  power       the whole voice loop on or off in one click (listening AND speaking)
  person      your mic (listening) on or off
  robot       Claude's voice (speaking) on or off
  back        play the reply before the one being read (or the last one read) from its start
  stop        stop the current reading (same as stop-speaking.ps1)
  next        stop the reply being read and play the next queued one (nothing if none)
  chats       open or close the chat list (2026-10-10, voice_chats.py): Auto, then the Claude Code chats active in
              the last few hours, newest first, by the titles the Claude app shows (re-read when the app renames
              one). Auto = as before. Clicking a chat pins the voice to it: the user's words go into that chat, only its
              replies are read. The up / down arrows under the list step to the previous / next chat. The chats
              button is tinted while a chat is pinned.
  x          hide to the tray (the tray icon brings it back; Quit is only in the tray)
Click the orb to show or hide the caption line. Drag anywhere to move it; the position is remembered.

Control files in %TEMP% (shared with voice_inbox.py and speak-response.ps1; names and meanings unchanged):
  claude-listen.muted    present = the mic daemon delivers nothing
  claude-voice.muted     present = Claude's replies are not spoken
  claude-speak.muted     present = both of the above (the "stop listening" phrase sets it)
  claude-speak.pid       present while Claude is reading aloud
  claude-speak.pause     present while the reading is paused
  claude-speak.cancel    written by Stop: queued readings exit
  claude-user.speaking   touched by voice_inbox.py while the user is talking (fresh = talking now)
  claude-speak.progress  text of the sentence being read (mtime = the sentence started)
Optional live levels, used when present (otherwise the orb animates from the files above):
  UDP 127.0.0.1:42712    voice_inbox.py sends the rms of each mic frame as ASCII, e.g. b"812.4"
                         ("speak 0.42" = a 0..1 playback level, if anything ever sends one)
  claude-speak.wav       the speak hook writes each sentence's wav just before playing it; the orb
                         follows its loudness envelope in time with the playback

Window: a Win32 layered window (per-pixel alpha, WS_EX_NOACTIVATE) rendered with PIL and numpy.
Run:  .venv\\Scripts\\pythonw.exe src\\voice_panel.py   (start.ps1 starts everything)
"""
import ctypes
import json
import logging
import math
import os
import queue
import re
import socket
import subprocess
import time
from ctypes import wintypes as wt
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from voice_config import MUTEX_PREFIX, DATA_DIR, LEVEL_PORT, LOG_DIR, STOP_SCRIPT
import speak_nav   # Next / Back (2026-10-10), shared with voice_inbox.py
import voice_chats  # chat picking (2026-10-10), shared with voice_inbox.py and read by speak-response.ps1

TEMP = Path(os.getenv("TEMP", ""))
LISTEN_MUTED = TEMP / "claude-listen.muted"
VOICE_MUTED = TEMP / "claude-voice.muted"
BOTH_MUTED = TEMP / "claude-speak.muted"
PID_FILE = TEMP / "claude-speak.pid"
PAUSE_FILE = TEMP / "claude-speak.pause"
CANCEL_FILE = TEMP / "claude-speak.cancel"
USER_SPEAKING = TEMP / "claude-user.speaking"
PROGRESS_FILE = TEMP / "claude-speak.progress"
SPEAK_WAV = TEMP / "claude-speak.wav"
LOG = LOG_DIR / "voice_inbox.log"
PANEL_LOG = LOG_DIR / "voice_panel.log"
STATE_FILE = Path(os.getenv("VOICE_PANEL_STATE", str(DATA_DIR / "voice_panel.json")))
NO_TRAY = os.getenv("VOICE_PANEL_NOTRAY") == "1"

USER_FRESH_S = 0.45        # claude-user.speaking younger than this = the user is talking now
THINK_MAX_S = 90.0         # after a typed message, shimmer until Claude speaks or this long
AUDIO_LAG_S = 0.06         # playback starts a moment after the progress file is written
CHATS_REFRESH_S = 2.0      # the chat list (titles, order, the pin) is re-read this often
MAX_CHATS = 8

log = logging.getLogger("voice_panel")


# ---------------------------------------------------------------- control files (the contract)

def _unlink(*files):
    for f in files:
        try:
            f.unlink()
        except OSError:
            pass


def listening_on() -> bool:
    return not (LISTEN_MUTED.exists() or BOTH_MUTED.exists())


def voice_on() -> bool:
    return not (VOICE_MUTED.exists() or BOTH_MUTED.exists())


def set_listening(on: bool) -> None:
    if on:
        had_both = BOTH_MUTED.exists()
        _unlink(LISTEN_MUTED, BOTH_MUTED)
        if had_both:
            VOICE_MUTED.touch()      # the combined switch also silenced the voice; keep it silent
    else:
        LISTEN_MUTED.touch()


def set_voice(on: bool) -> None:
    if on:
        had_both = BOTH_MUTED.exists()
        _unlink(VOICE_MUTED, BOTH_MUTED)
        if had_both:
            LISTEN_MUTED.touch()     # keep listening off; only the voice was asked for
    else:
        VOICE_MUTED.touch()
        stop_reading()


def loop_on() -> bool:
    """The master switch shows ON while either half of the loop is on."""
    return listening_on() or voice_on()


def set_loop(on: bool) -> None:
    """Everything on or everything off in one step."""
    if on:
        _unlink(BOTH_MUTED, LISTEN_MUTED, VOICE_MUTED)
    else:
        LISTEN_MUTED.touch()
        BOTH_MUTED.touch()
        stop_reading()


def stop_reading() -> None:
    try:
        CANCEL_FILE.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
    except OSError:
        pass
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 9)
    except (OSError, ValueError):
        pass
    _unlink(PID_FILE, PAUSE_FILE)
    if STOP_SCRIPT.exists():
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(STOP_SCRIPT)],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------- settings

def load_settings() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(d: dict) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning("could not save settings: %s", e)


# ---------------------------------------------------------------- live levels

class Levels:
    """Mic level over UDP (from voice_inbox.py) and Claude's playback envelope (from claude-speak.wav)."""

    def __init__(self):
        self.sock = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setblocking(False)
            s.bind(("127.0.0.1", LEVEL_PORT))
            self.sock = s
        except OSError as e:
            log.info("level port %d not available (%s); orb uses file state only", LEVEL_PORT, e)
        self.mic = 0.0
        self.mic_t = 0.0
        self.spk = 0.0
        self.spk_t = 0.0
        self.env = None
        self.env_dur = 0.0
        self.env_mtime = 0.0

    def drain(self, now: float) -> None:
        if not self.sock:
            return
        for _ in range(256):
            try:
                data = self.sock.recv(128)
            except OSError:
                break
            try:
                txt = data.decode("ascii", "ignore").strip()
                if txt.startswith("speak"):
                    self.spk = max(0.0, min(1.0, float(txt.split()[1])))
                    self.spk_t = now
                else:
                    rms = float(txt.split()[-1])
                    db = 20.0 * math.log10(max(rms, 1.0))
                    self.mic = max(0.0, min(1.0, (db - 44.0) / 26.0))
                    self.mic_t = now
            except (ValueError, IndexError):
                pass

    def mic_level(self, now: float):
        return self.mic if now - self.mic_t < 0.25 else None

    def load_wav(self) -> None:
        m = _mtime(SPEAK_WAV)
        if not m or m == self.env_mtime:
            return
        self.env_mtime = m
        try:
            b = SPEAK_WAV.read_bytes()
            i = b.find(b"fmt ")
            ch = int.from_bytes(b[i + 10:i + 12], "little") if i >= 0 else 1
            rate = int.from_bytes(b[i + 12:i + 16], "little") if i >= 0 else 24000
            j = b.find(b"data")
            pcm = b[j + 8:] if j >= 0 else b[44:]
            x = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
            if ch > 1:
                x = x[:len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
            win = max(1, rate // 50)
            n = len(x) // win
            if n < 2:
                self.env = None
                return
            rms = np.sqrt((x[:n * win].reshape(n, win) ** 2).mean(axis=1))
            ref = max(float(np.percentile(rms, 95)), 1.0)
            self.env = np.clip(rms / ref, 0.0, 1.0) ** 0.8
            self.env_dur = len(x) / float(rate)
        except Exception as e:     # a half-written file: try again on the next change
            self.env = None
            self.env_mtime = 0.0
            log.debug("wav envelope failed: %s", e)


def synth_level(t: float, seed: float) -> float:
    """Speech-like motion for when no real level is published."""
    v = 0.55 + 0.22 * math.sin(t * 11.0 + seed) + 0.14 * math.sin(t * 17.3 + 2 * seed) + 0.09 * math.sin(t * 5.1)
    syl = abs(math.sin(t * 6.3 + seed)) ** 0.6
    return max(0.0, min(1.0, v * syl * 0.85))


# ---------------------------------------------------------------- drawing helpers

def to_premul(img: Image.Image) -> np.ndarray:
    a = np.asarray(img.convert("RGBA"), dtype=np.float32) / 255.0
    a[..., :3] *= a[..., 3:4]
    return a


def colored(mask: Image.Image, rgb, alpha=1.0) -> np.ndarray:
    m = np.asarray(mask, dtype=np.float32)[..., None] / 255.0 * alpha
    out = np.empty(m.shape[:2] + (4,), np.float32)
    out[..., :3] = np.asarray(rgb, np.float32) * m
    out[..., 3:4] = m
    return out


def over(dst: np.ndarray, src: np.ndarray, x: int, y: int, k: float = 1.0) -> None:
    h, w = src.shape[:2]
    d = dst[y:y + h, x:x + w]
    if k >= 0.999:
        d *= 1.0 - src[..., 3:4]
        d += src
    elif k > 0.001:
        d *= 1.0 - src[..., 3:4] * k
        d += src * k


def to_bgra8(p: np.ndarray) -> np.ndarray:
    v = np.clip(p * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return v[..., [2, 1, 0, 3]]


def circle_mask(size: int, diameter: float, ss: int = 4) -> Image.Image:
    S = size * ss
    m = Image.new("L", (S, S), 0)
    o = (size - diameter) / 2.0 * ss
    ImageDraw.Draw(m).ellipse([o, o, o + diameter * ss - 1, o + diameter * ss - 1], fill=255)
    return m.resize((size, size), Image.BOX)


def glyph_mask(kind: str, px: int, slash: bool = False) -> Image.Image:
    ss = 4
    S = px * ss
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)

    def B(x0, y0, x1, y1):
        return [x0 * S, y0 * S, x1 * S, y1 * S]

    def C(cx, cy, r):
        return [(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S]

    if kind == "human":
        d.ellipse(C(0.5, 0.29, 0.165), fill=255)
        d.pieslice(B(0.17, 0.56, 0.83, 1.22), 180, 360, fill=255)
    elif kind == "robot":
        d.ellipse(C(0.5, 0.10, 0.07), fill=255)
        d.rectangle(B(0.465, 0.12, 0.535, 0.26), fill=255)
        d.rounded_rectangle(B(0.16, 0.25, 0.84, 0.84), radius=0.17 * S, fill=255)
        d.rounded_rectangle(B(0.04, 0.44, 0.13, 0.66), radius=0.03 * S, fill=255)
        d.rounded_rectangle(B(0.87, 0.44, 0.96, 0.66), radius=0.03 * S, fill=255)
        d.ellipse(C(0.36, 0.50, 0.08), fill=0)
        d.ellipse(C(0.64, 0.50, 0.08), fill=0)
        d.rounded_rectangle(B(0.35, 0.66, 0.65, 0.72), radius=0.03 * S, fill=0)
    elif kind == "power":
        w = int(0.11 * S)
        d.arc(C(0.5, 0.55, 0.34), start=-55, end=235, fill=255, width=w)
        d.line([(0.5 * S, 0.10 * S), (0.5 * S, 0.50 * S)], fill=255, width=w)
        d.ellipse(C(0.5, 0.10, 0.055), fill=255)
        d.ellipse(C(0.5, 0.50, 0.055), fill=255)
    elif kind == "stop":
        d.rounded_rectangle(B(0.18, 0.18, 0.82, 0.82), radius=0.14 * S, fill=255)
    elif kind == "next":         # the next reply: triangle and bar
        d.polygon([(0.14 * S, 0.16 * S), (0.70 * S, 0.50 * S), (0.14 * S, 0.84 * S)], fill=255)
        d.rounded_rectangle(B(0.72, 0.16, 0.86, 0.84), radius=0.03 * S, fill=255)
    elif kind == "back":         # the reply before: bar and triangle
        d.polygon([(0.86 * S, 0.16 * S), (0.30 * S, 0.50 * S), (0.86 * S, 0.84 * S)], fill=255)
        d.rounded_rectangle(B(0.14, 0.16, 0.28, 0.84), radius=0.03 * S, fill=255)
    elif kind == "chats":        # the chat list: three rows
        for yy in (0.24, 0.50, 0.76):
            d.ellipse(C(0.15, yy, 0.075), fill=255)
            d.rounded_rectangle(B(0.33, yy - 0.06, 0.90, yy + 0.06), radius=0.06 * S, fill=255)
    elif kind in ("chprev", "chnext"):   # previous chat (up the list) / next chat (down the list)
        w = int(0.13 * S)
        a, b = (0.30, 0.70) if kind == "chprev" else (0.70, 0.30)
        d.line([(0.18 * S, b * S), (0.50 * S, a * S), (0.82 * S, b * S)], fill=255, width=w, joint="curve")
        for x in (0.18, 0.82):
            d.ellipse(C(x, b, 0.065), fill=255)
    elif kind == "hide":
        w = int(0.12 * S)
        d.line([(0.2 * S, 0.2 * S), (0.8 * S, 0.8 * S)], fill=255, width=w)
        d.line([(0.8 * S, 0.2 * S), (0.2 * S, 0.8 * S)], fill=255, width=w)
    if slash:
        a, b = (0.08 * S, 0.08 * S), (0.92 * S, 0.92 * S)
        d.line([a, b], fill=0, width=int(0.26 * S))
        d.line([a, b], fill=255, width=int(0.10 * S))
    return m.resize((px, px), Image.BOX)


def load_font(px: int):
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in ("segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(str(fonts / name), px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(px)
    except TypeError:
        return ImageFont.load_default()


# ---------------------------------------------------------------- palettes (deep, mid, highlight)

PAL = {
    "idle":   ((0.10, 0.24, 0.52), (0.45, 0.66, 0.98), (0.86, 0.94, 1.00)),
    "listen": ((0.02, 0.36, 0.46), (0.22, 0.80, 0.86), (0.82, 1.00, 0.98)),
    "speak":  ((0.66, 0.22, 0.14), (0.98, 0.58, 0.38), (1.00, 0.90, 0.78)),
    "think":  ((0.20, 0.20, 0.50), (0.56, 0.54, 0.94), (0.92, 0.90, 1.00)),
    "muted":  ((0.16, 0.18, 0.24), (0.40, 0.44, 0.54), (0.66, 0.70, 0.78)),
    "off":    ((0.17, 0.17, 0.18), (0.38, 0.38, 0.40), (0.50, 0.50, 0.52)),
}
PAL = {k: np.array(v, np.float32) for k, v in PAL.items()}
ACCENT_HUMAN = (0.30, 0.86, 0.90)
ACCENT_ROBOT = (0.98, 0.56, 0.36)
ACCENT_PIN = (0.48, 0.68, 0.98)      # a chat is picked (the chats button, the picked row's mark)
OFF_TINT = (0.90, 0.30, 0.30)


class OrbPainter:
    def __init__(self, cell: int, radius: float, scale: float):
        self.cell, self.R, self.s = cell, radius, scale
        c = (cell - 1) / 2.0
        yy, xx = np.mgrid[0:cell, 0:cell].astype(np.float32)
        self.px, self.py = xx - c, yy - c
        self.d = np.sqrt(self.px ** 2 + self.py ** 2)
        self.inside_cell = np.clip((c - 0.5 - self.d) / (7.0 * scale), 0.0, 1.0) ** 2
        self.out = np.zeros((cell, cell, 4), np.float32)

    def paint(self, pal, level, swell, rot, blobs, shimmer, shim_pos, glow, grey, alpha):
        R = self.R * swell
        nx, ny, d = self.px / R, self.py / R, self.d
        body = np.clip(R - d + 0.5, 0.0, 1.0)
        c1, c2, c3 = pal[0], pal[1], pal[2]
        g = np.clip(0.5 - 0.5 * (ny * math.cos(rot) - nx * math.sin(rot)), 0.0, 1.0)
        col = c1 + (c2 - c1) * g[..., None]
        for bx, by, sig, w in blobs:
            b = np.exp(-((nx - bx) ** 2 + (ny - by) ** 2) * (0.5 / (sig * sig))) * w
            col += (c3 - col) * b[..., None]
        r2 = np.clip(1.0 - (nx * nx + ny * ny), 0.0, 1.0)
        col *= (0.70 + 0.30 * np.sqrt(r2))[..., None]
        spec = np.exp(-((nx + 0.30) ** 2 + (ny + 0.42) ** 2) * 9.0) * 0.22
        col += (1.0 - col) * spec[..., None]
        if shimmer > 0.01:
            band = np.exp(-((nx * 0.55 - ny * 0.83) - shim_pos) ** 2 * 12.0) * (0.55 * shimmer)
            col += (1.0 - col) * band[..., None]
        if grey > 0.01:
            lum = col[..., 0] * 0.3 + col[..., 1] * 0.59 + col[..., 2] * 0.11
            col += (lum[..., None] - col) * grey
        np.clip(col, 0.0, 1.0, out=col)
        gw = (3.0 + 5.0 * level) * self.s
        halo = np.exp(-np.clip(d - R, 0.0, None) / gw) * glow * (1.0 - body) * self.inside_cell
        ab, ag = body * alpha, halo * alpha
        out = self.out
        out[..., :3] = col * ab[..., None] + c2 * ag[..., None]
        out[..., 3] = ab + ag
        return out


# ---------------------------------------------------------------- layout and rendering

class Layout:
    """All geometry in physical pixels for one DPI scale and caption setting."""
    W_LOG, H_LOG, CAP_LOG = 392, 64, 24
    ROW_LOG, LIST_PAD_LOG, FOOT_LOG = 30, 8, 40     # the chat list (open): row height, padding, the arrows' row

    def __init__(self, s: float, caption: bool, rows: int = 0):
        """rows = 0: the chat list is closed; otherwise the number of rows shown (Auto + the chats)."""
        self.s, self.caption, self.nrows = s, caption, rows
        P = lambda v: int(round(v * s))  # noqa: E731
        self.M = P(16)
        self.cw = P(self.W_LOG)
        top_log = self.H_LOG + (self.CAP_LOG if caption else 0)
        list_log = (self.LIST_PAD_LOG + rows * self.ROW_LOG + self.FOOT_LOG) if rows else 0
        self.ch = P(top_log + list_log)
        self.radius = P(24) if (caption or rows) else self.ch // 2
        self.w, self.h = self.cw + 2 * self.M, self.ch + 2 * self.M
        cy = self.M + P(32)

        def cell(cx_log, cell_log, cy_px=cy):
            n = P(cell_log)
            return (self.M + P(cx_log) - n // 2, cy_px - n // 2, n)

        self.orb = cell(32, 64)
        self.orb_r = 19 * s
        # name: (cx_log, cy_log, cell_log, diameter_log, glyph fraction)
        spec = {"power": (86, 32, 44, 36, 0.46), "human": (142, 32, 38, 32, 0.50), "robot": (180, 32, 38, 32, 0.52),
                "back": (218, 32, 38, 32, 0.40), "stop": (256, 32, 38, 32, 0.34), "next": (294, 32, 38, 32, 0.40),
                "chats": (334, 32, 34, 30, 0.42), "hide": (368, 32, 24, 22, 0.40)}
        self.rows = []
        self.list_top = None
        if rows:
            self.list_top = self.M + P(top_log)
            for i in range(rows):
                self.rows.append((self.M + P(10), self.M + P(top_log + self.LIST_PAD_LOG + i * self.ROW_LOG),
                                  self.cw - 2 * P(10), P(self.ROW_LOG)))
            fy = top_log + self.LIST_PAD_LOG + rows * self.ROW_LOG + self.FOOT_LOG / 2.0
            spec["chprev"] = (self.W_LOG / 2.0 - 22, fy, 34, 30, 0.40)
            spec["chnext"] = (self.W_LOG / 2.0 + 22, fy, 34, 30, 0.40)
        self.buttons = {}
        for name, (cx, cyl, cl, dl, gf) in spec.items():
            cpx = self.M + P(cyl)
            x, y, n = cell(cx, cl, cpx)
            self.buttons[name] = {"rect": (x, y, n), "d": P(dl), "glyph": gf,
                                  "c": (self.M + P(cx), cpx), "r": dl * s / 2.0}
        self.divider_x = self.M + P(114)
        self.cap = (self.M + P(20), self.M + P(62), self.cw - 2 * P(20), P(22)) if caption else None

    def hit(self, x: int, y: int):
        for name, b in self.buttons.items():
            cx, cy = b["c"]
            if (x - cx) ** 2 + (y - cy) ** 2 <= (b["r"] + 3 * self.s) ** 2:
                return name
        for i, (rx, ry, rw, rh) in enumerate(self.rows):
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return "row:%d" % i
        ox, oy, n = self.orb
        if (x - ox - n / 2) ** 2 + (y - oy - n / 2) ** 2 <= (self.orb_r + 5 * self.s) ** 2:
            return "orb"
        return None


class Renderer:
    def __init__(self, lay: Layout):
        self.lay = lay
        s = lay.s
        self.base = self._base()
        ox, oy, n = lay.orb
        self.orb = OrbPainter(n, lay.orb_r, s)
        self._btn_cache = {}
        self._rings = {}
        self.font = load_font(max(9, int(round(12 * s))))
        self.row_font = load_font(max(9, int(round(13 * s))))
        self._rows = {}
        self._strip_key = None
        self._strip = None

    def _base(self) -> np.ndarray:
        lay, s = self.lay, self.lay.s
        W, H, M = lay.w, lay.h, lay.M
        ss = 4
        # shadow
        sh = Image.new("L", (W, H), 0)
        off = int(round(3 * s))
        ImageDraw.Draw(sh).rounded_rectangle([M, M + off, M + lay.cw, M + lay.ch + off], radius=lay.radius,
                                             fill=int(255 * 0.50))
        sh = sh.filter(ImageFilter.GaussianBlur(7 * s))
        out = colored(sh, (0, 0, 0))
        # card: supersampled rounded rect with a soft vertical gradient
        big = Image.new("L", (W * ss, H * ss), 0)
        ImageDraw.Draw(big).rounded_rectangle([M * ss, M * ss, (M + lay.cw) * ss - 1, (M + lay.ch) * ss - 1],
                                              radius=lay.radius * ss, fill=255)
        mask = np.asarray(big.resize((W, H), Image.BOX), np.float32) / 255.0
        ys = np.clip((np.arange(H, dtype=np.float32) - M) / max(1, lay.ch), 0, 1)[:, None]
        top, bot = np.array([0.125, 0.125, 0.140], np.float32), np.array([0.075, 0.075, 0.085], np.float32)
        rgb = top + (bot - top) * ys[..., None]
        a = (mask * 0.965)[..., None]
        card = np.concatenate([rgb * a, a], axis=-1).astype(np.float32)
        over(out, card, 0, 0)
        # hairline border
        inner = Image.new("L", (W * ss, H * ss), 0)
        i = max(1, int(round(1 * s))) * ss
        ImageDraw.Draw(inner).rounded_rectangle([M * ss + i, M * ss + i, (M + lay.cw) * ss - 1 - i,
                                                 (M + lay.ch) * ss - 1 - i], radius=max(1, lay.radius * ss - i),
                                                fill=255)
        ring = np.clip(mask - np.asarray(inner.resize((W, H), Image.BOX), np.float32) / 255.0, 0, 1)
        over(out, colored(Image.fromarray((ring * 255).astype(np.uint8)), (1, 1, 1), 0.10), 0, 0)
        # divider between the master switch and the rest
        dv = Image.new("L", (W, H), 0)
        cy = M + int(round(32 * s))
        hh = int(round(12 * s))
        ImageDraw.Draw(dv).line([(lay.divider_x, cy - hh), (lay.divider_x, cy + hh)], fill=255,
                                width=max(1, int(round(s))))
        over(out, colored(dv, (1, 1, 1), 0.09), 0, 0)
        if lay.list_top is not None:      # hairline above the chat list
            hl = Image.new("L", (W, H), 0)
            ImageDraw.Draw(hl).line([(M + int(round(16 * s)), lay.list_top), (M + lay.cw - int(round(16 * s)),
                                     lay.list_top)], fill=255, width=max(1, int(round(s))))
            over(out, colored(hl, (1, 1, 1), 0.08), 0, 0)
        return out

    def row_bg(self, w: int, h: int) -> Image.Image:
        """Rounded mask for a row's highlight."""
        key = ("bg", w, h)
        m = self._rows.get(key)
        if m is None:
            ss, s = 4, self.lay.s
            big = Image.new("L", (w * ss, h * ss), 0)
            inset = int(round(2 * s)) * ss
            ImageDraw.Draw(big).rounded_rectangle([0, inset, w * ss - 1, h * ss - 1 - inset],
                                                  radius=int(round(9 * s)) * ss, fill=255)
            m = big.resize((w, h), Image.BOX)
            self._rows[key] = m
        return m

    def row_mark(self, h: int) -> Image.Image:
        """The picked row's mark: a short rounded bar at its left edge."""
        key = ("mark", h)
        m = self._rows.get(key)
        if m is None:
            ss, s = 4, self.lay.s
            w = max(2, int(round(3 * s)))
            big = Image.new("L", (w * ss, h * ss), 0)
            ImageDraw.Draw(big).rounded_rectangle([0, int(h * 0.30) * ss, w * ss - 1, int(h * 0.70) * ss],
                                                  radius=w * ss // 2, fill=255)
            m = big.resize((w, h), Image.BOX)
            self._rows[key] = m
        return m

    def row_text(self, text: str, w: int, h: int) -> Image.Image:
        """The row's title in one line of width w, cut with an ellipsis when it does not fit."""
        key = ("text", text, w, h)
        m = self._rows.get(key)
        if m is None:
            f = self.row_font
            t = text
            if f.getlength(t) > w:
                lo, hi = 0, len(t)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if f.getlength(t[:mid].rstrip() + "…") <= w:
                        lo = mid
                    else:
                        hi = mid - 1
                t = t[:lo].rstrip() + "…"
            m = Image.new("L", (w, h), 0)
            asc, desc = f.getmetrics()
            ImageDraw.Draw(m).text((0, (h - asc - desc) // 2), t, font=f, fill=255)
            if len(self._rows) > 300:
                self._rows.clear()
            self._rows[key] = m
        return m

    def button(self, name: str, look: tuple) -> np.ndarray:
        """look = (bg_rgb, bg_a, icon_rgb, icon_a, slash)"""
        key = (name,) + look
        img = self._btn_cache.get(key)
        if img is not None:
            return img
        b = self.lay.buttons[name]
        n, dia = b["rect"][2], b["d"]
        bg_rgb, bg_a, icon_rgb, icon_a, slash = look
        img = np.zeros((n, n, 4), np.float32)
        if bg_a > 0:
            over(img, colored(circle_mask(n, dia), bg_rgb, bg_a), 0, 0)
        g = max(4, int(round(dia * b["glyph"])))
        gm = glyph_mask(name, g, slash)
        o = (n - g) // 2
        over(img, colored(gm, icon_rgb, icon_a), o, o)
        if len(self._btn_cache) > 200:
            self._btn_cache.clear()
        self._btn_cache[key] = img
        return img

    def ring(self, name: str, rgb) -> np.ndarray:
        r = self._rings.get(name)
        if r is not None:
            return r
        b, s = self.lay.buttons[name], self.lay.s
        n, dia = b["rect"][2], b["d"]
        ss = 4
        S = n * ss
        m = Image.new("L", (S, S), 0)
        rad = (dia / 2.0 + 1.0 * s) * ss
        c = S / 2.0
        ImageDraw.Draw(m).ellipse([c - rad, c - rad, c + rad, c + rad], outline=255, width=max(1, int(1.6 * s * ss)))
        m = m.resize((n, n), Image.BOX)
        halo = m.filter(ImageFilter.GaussianBlur(2.2 * s))
        both = Image.fromarray(np.maximum(np.asarray(m), (np.asarray(halo, np.float32) * 0.9).astype(np.uint8)))
        r = colored(both, rgb)
        self._rings[name] = r
        return r

    def strip(self, text: str, rgb):
        key = (text, rgb)
        if key != self._strip_key:
            self._strip_key = key
            h = self.lay.cap[3]
            tw = int(self.font.getlength(text)) + 4 if text else 1
            m = Image.new("L", (max(1, tw), h), 0)
            if text:
                asc, desc = self.font.getmetrics()
                ImageDraw.Draw(m).text((2, (h - asc - desc) // 2), text, font=self.font, fill=255)
            self._strip = m
        return self._strip


# ---------------------------------------------------------------- Win32 layered window

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    winmm = ctypes.WinDLL("winmm")
except OSError:
    winmm = None

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD), ("hwndTrack", wt.HWND), ("dwHoverTime", wt.DWORD)]


P = ctypes.POINTER
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [P(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, P(wt.POINT), P(wt.SIZE), wt.HDC, P(wt.POINT),
                                       wt.COLORREF, P(BLENDFUNCTION), wt.DWORD]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, P(BITMAPINFO), wt.UINT, P(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.PeekMessageW.argtypes = [P(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [P(wt.MSG)]
user32.DispatchMessageW.argtypes = [P(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.MsgWaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.c_void_p, wt.BOOL, wt.DWORD, wt.DWORD]
user32.SetCapture.argtypes = [wt.HWND]
user32.SetCapture.restype = wt.HWND
user32.GetCursorPos.argtypes = [P(wt.POINT)]
user32.GetMessagePos.restype = wt.DWORD
user32.TrackMouseEvent.argtypes = [P(TRACKMOUSEEVENT)]
user32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wt.HANDLE
user32.SetCursor.argtypes = [wt.HANDLE]
user32.SetCursor.restype = wt.HANDLE
user32.MonitorFromRect.argtypes = [P(wt.RECT), wt.DWORD]
user32.MonitorFromRect.restype = wt.HANDLE
user32.GetWindowRect.argtypes = [wt.HWND, P(wt.RECT)]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.IsWindowVisible.argtypes = [wt.HWND]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
try:
    user32.GetDpiForWindow.argtypes = [wt.HWND]
    user32.GetDpiForWindow.restype = wt.UINT
except AttributeError:
    pass

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x80000, 0x80, 0x8, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x1, 0x2, 0x4, 0x10, 0x200
HWND_TOPMOST = wt.HWND(-1)
WM_DESTROY, WM_CLOSE, WM_SETCURSOR, WM_MOUSEACTIVATE = 0x0002, 0x0010, 0x0020, 0x0021
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_CAPTURECHANGED = 0x0200, 0x0201, 0x0202, 0x0215
WM_MOUSELEAVE, WM_DPICHANGED, WM_DISPLAYCHANGE, WM_QUIT = 0x02A3, 0x02E0, 0x007E, 0x0012
MA_NOACTIVATE = 3
HTCLIENT = 1
TME_LEAVE = 0x2
PM_REMOVE = 0x1
QS_ALLINPUT = 0x04FF
ULW_ALPHA = 2
IDC_ARROW, IDC_HAND = 32512, 32649


def set_dpi_awareness() -> None:
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):     # per-monitor v2
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


class LayeredWindow:
    CLASS = "TalkoverOrb"

    def __init__(self, handler):
        self.handler = handler
        self._proc = WNDPROC(self._wndproc)       # keep a reference for the life of the window
        self.hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = self.hinst
        wc.lpszClassName = self.CLASS
        user32.RegisterClassExW(ctypes.byref(wc))
        ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, self.CLASS, "Talkover", WS_POPUP, 0, 0, 10, 10,
                                           None, None, self.hinst, None)
        self.memdc = gdi32.CreateCompatibleDC(None)
        self.hbmp = None
        self.old = None
        self.w = self.h = 0
        self.pix = None
        self.cur_arrow = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        self.cur_hand = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        self.tracking = False

    def dpi(self) -> int:
        try:
            return int(user32.GetDpiForWindow(self.hwnd)) or 96
        except (AttributeError, OSError):
            return 96

    def resize(self, w: int, h: int) -> np.ndarray:
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h                 # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(self.memdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        prev = gdi32.SelectObject(self.memdc, hbmp)
        if self.hbmp:
            gdi32.DeleteObject(self.hbmp)
        else:
            self.old = prev
        self.hbmp, self.w, self.h = hbmp, w, h
        buf = (ctypes.c_uint8 * (w * h * 4)).from_address(bits.value)
        self.pix = np.ctypeslib.as_array(buf).reshape(h, w, 4)
        self.pix[:] = 0
        return self.pix

    def push(self) -> None:
        gdi32.GdiFlush()
        size = wt.SIZE(self.w, self.h)
        src = wt.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, 255, 1)
        user32.UpdateLayeredWindow(self.hwnd, None, None, ctypes.byref(size), self.memdc, ctypes.byref(src),
                                   0, ctypes.byref(blend), ULW_ALPHA)

    def move(self, x: int, y: int) -> None:
        user32.SetWindowPos(self.hwnd, None, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def pos(self):
        r = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return r.left, r.top

    def keep_on_top(self) -> None:
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)

    def show(self, on: bool) -> None:
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if on else SW_HIDE)
        if on:
            self.keep_on_top()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE                 # clicks never take focus from Claude's window
            if msg == WM_SETCURSOR and (lparam & 0xFFFF) == HTCLIENT:
                user32.SetCursor(self.cur_hand if self.handler.hover else self.cur_arrow)
                return 1
            if msg in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP):
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                mp = user32.GetMessagePos()               # screen position when the message was sent
                self.handler.msg_pt = (ctypes.c_short(mp & 0xFFFF).value, ctypes.c_short((mp >> 16) & 0xFFFF).value)
                if msg == WM_MOUSEMOVE:
                    if not self.tracking:
                        tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                        user32.TrackMouseEvent(ctypes.byref(tme))
                        self.tracking = True
                    self.handler.on_move(x, y)
                elif msg == WM_LBUTTONDOWN:
                    user32.SetCapture(hwnd)
                    self.handler.on_down(x, y)
                else:
                    self.handler.on_up(x, y)          # before ReleaseCapture: it sends WM_CAPTURECHANGED
                    user32.ReleaseCapture()
                return 0
            if msg == WM_MOUSELEAVE:
                self.tracking = False
                self.handler.on_leave()
                return 0
            if msg == WM_CAPTURECHANGED:
                self.handler.on_capture_lost()
                return 0
            if msg == WM_DPICHANGED:
                r = ctypes.cast(lparam, P(wt.RECT)).contents
                self.handler.on_dpi(wparam & 0xFFFF, r.left, r.top)
                return 0
            if msg == WM_DISPLAYCHANGE:
                self.handler.on_display_change()
            if msg == WM_CLOSE:
                self.handler.hide()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("window message %#x", msg)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ---------------------------------------------------------------- the panel

class Ease:
    def __init__(self, v=0.0):
        self.v = v

    def to(self, target, dt, up=6.0, down=None):
        k = up if (down is None or target >= self.v) else down
        self.v += (target - self.v) * (1.0 - math.exp(-dt * k))
        return self.v


class Panel:
    def __init__(self, start_hidden: bool = False):
        self.settings = load_settings()
        self.caption = bool(self.settings.get("caption", False))
        self.expanded = bool(self.settings.get("chats", False))
        self.chats = []             # voice_chats.active() records shown in the list, newest first
        self.pin_local = ""         # local session id of the pinned chat, "" = auto
        self._chats_t = 0.0
        self._list_key = None
        self.actions = queue.Queue()
        self.win = LayeredWindow(self)
        self.levels = Levels()
        self.hover = None
        self.msg_pt = (0, 0)
        self.pressed = None
        self.drag = None            # (cursor x, cursor y, window x, window y, moved)
        self.visible = not start_hidden
        self.running = True
        self.tray = None
        # animated state
        self.pal = PAL["idle"].copy()
        self.lvl = Ease()
        self.breath_amp = Ease(1.0)
        self.shimmer = Ease()
        self.glowk = Ease(0.12)
        self.grey = Ease()
        self.alpha = Ease(1.0)
        self.g_human = Ease()
        self.g_robot = Ease()
        self.phase = 0.0
        self.rot = 0.0
        self.t0 = time.perf_counter()
        # polled state
        self.st = {}
        self._poll_t = 0.0
        self._log_t = 0.0
        self._top_t = 0.0
        self.t_typed = 0.0
        self.heard = ""
        self.last_progress = 0.0
        self.progress_text = ""
        self._btn_keys = {}
        self._cap_key = None
        self._err_seen = set()
        self.mode = "idle"
        self.refresh_chats(rebuild=False)
        self.build(self.win.dpi() / 96.0)
        x, y = self.settings.get("x"), self.settings.get("y")
        if x is None or y is None or not self.on_screen(x, y):
            x, y = self.default_pos()
        self.win.move(x, y)
        self.poll(force=True)
        self.render(0.0, force=True)
        self.win.push()
        if self.visible:
            self.win.show(True)
        self._tray()

    # ---- geometry
    def build(self, s: float) -> None:
        self.s = s
        self.lay = Layout(s, self.caption, (1 + len(self.chats)) if self.expanded else 0)
        self.rend = Renderer(self.lay)
        self.pix = self.win.resize(self.lay.w, self.lay.h)
        self.pix[:] = to_bgra8(self.rend.base)
        self._btn_keys = {}
        self._cap_key = None
        self._list_key = None

    def relayout(self):
        """Rebuild at the same top-left corner (caption shown or hidden, chat list opened, closed or resized)."""
        x, y = self.win.pos()
        self.build(self.s)
        self.render(0.0, force=True)
        self.win.push()
        self.win.move(x, y)

    def default_pos(self):
        sw = user32.GetSystemMetrics(0)
        return max(0, sw // 2 - self.lay.w // 2), int(12 * self.s)

    def on_screen(self, x, y) -> bool:
        M = self.lay.M
        r = wt.RECT(int(x) + M, int(y) + M, int(x) + self.lay.w - M, int(y) + self.lay.h - M)
        return bool(user32.MonitorFromRect(ctypes.byref(r), 0))

    # ---- state
    def poll(self, force=False) -> None:
        now = time.time()
        if not force and now - self._poll_t < 0.1:
            return
        self._poll_t = now
        st = self.st
        st["listen"] = listening_on()
        st["voice"] = voice_on()
        st["pid"] = PID_FILE.exists()
        st["pause"] = PAUSE_FILE.exists()
        st["user_t"] = _mtime(USER_SPEAKING)
        pm = _mtime(PROGRESS_FILE)
        if pm and pm != self.last_progress:
            self.last_progress = pm
            try:
                self.progress_text = " ".join(PROGRESS_FILE.read_text(encoding="utf-8-sig", errors="ignore").split())
            except OSError:
                pass
        st["progress_t"] = pm
        self.levels.load_wav()
        if force or now - self._log_t > 0.5:
            self._log_t = now
            self.read_log()
        if force or now - self._chats_t > CHATS_REFRESH_S:
            self.refresh_chats()

    # ---- chats (2026-10-10)
    def refresh_chats(self, rebuild: bool = True) -> None:
        """Re-read the chat list and the pin. Titles follow the app's own session files, so a rename shows here on the
        next read. While the mouse is over the list its order is kept, so a row does not move under the cursor."""
        self._chats_t = time.time()
        try:
            pin = voice_chats.read_pin()
            keep = pin[1] if pin else ""
            chats = voice_chats.active(keep=keep)
        except Exception as e:
            log.warning("chat list: %s", e)
            return
        if pin and not any(r["local"] == keep for r in chats):
            log.info("pinned chat %s is gone or archived: back to auto", keep)
            voice_chats.clear_pin()
            keep = ""
        if len(chats) > MAX_CHATS:
            top = chats[:MAX_CHATS]
            if keep and not any(r["local"] == keep for r in top):
                top[-1] = next(r for r in chats if r["local"] == keep)
            chats = top
        if (self.hover or "").startswith("row:") and self.chats:
            order = {r["local"]: i for i, r in enumerate(self.chats)}
            chats.sort(key=lambda r: order.get(r["local"], len(order)))
        resize = self.expanded and len(chats) != len(self.chats)
        self.chats, self.pin_local = chats, keep
        if resize and rebuild:
            self.relayout()

    def pin_index(self) -> int:
        """Index of the pinned chat in the list, -1 for auto."""
        for i, r in enumerate(self.chats):
            if r["local"] == self.pin_local:
                return i
        return -1

    def pick(self, i: int) -> None:
        """i = -1: auto; otherwise pin the voice to self.chats[i]."""
        if i < 0 or i >= len(self.chats):
            voice_chats.clear_pin()
            self.pin_local = ""
            log.info("voice: auto (the chat the app shows)")
            return
        r = self.chats[i]
        voice_chats.set_pin(r)
        self.pin_local = r["local"]
        log.info("voice pinned to chat %r (%s)", r["title"], r["cli"][:8])

    def step_chat(self, d: int) -> None:
        if not self.chats:
            return
        i = self.pin_index()
        if i < 0:
            i = 0 if d > 0 else len(self.chats) - 1
        else:
            i = (i + d) % len(self.chats)
        self.pick(i)

    def toggle_chats(self) -> None:
        self.expanded = not self.expanded
        self.settings["chats"] = self.expanded
        save_settings(self.settings)
        if self.expanded:
            self.refresh_chats(rebuild=False)
        self.relayout()

    def read_log(self) -> None:
        try:
            with open(LOG, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4000))
                lines = f.read().decode("utf-8", "ignore").splitlines()
        except OSError:
            return
        for line in reversed(lines):
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO typed [\d.]+s: (.*)$", line)
            if m:
                try:
                    self.t_typed = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    pass
                self.heard = m.group(2).strip().strip("'\"")
                return

    def claude_level(self, now_wall: float, t: float):
        """(audible, level) for Claude's voice right now."""
        st, lv = self.st, self.levels
        if not st.get("voice") or not st.get("pid") or st.get("pause"):
            return False, 0.0
        if now_wall - lv.spk_t < 0.25 and lv.spk_t:
            return True, lv.spk
        pt = st.get("progress_t") or 0.0
        if lv.env is not None and lv.env_mtime and lv.env_mtime >= pt - 1.0 and pt:
            start = max(pt, lv.env_mtime)
            el = now_wall - start - AUDIO_LAG_S
            if -0.2 <= el <= lv.env_dur + 0.15:
                i = int(max(0.0, el) * 50)
                return True, float(lv.env[min(i, len(lv.env) - 1)]) if i < len(lv.env) else 0.0
            return False, 0.0
        if not pt:
            return False, 0.0
        return True, synth_level(t, 1.7)

    def decide(self, t: float):
        st = self.st
        now = time.time()
        listen, voice = st.get("listen", True), st.get("voice", True)
        user_talk = listen and (now - (st.get("user_t") or 0.0) < USER_FRESH_S)
        audible, spk_level = self.claude_level(now, t)
        mic = self.levels.mic_level(time.time())
        thinking = (voice and listen and now - self.t_typed < THINK_MAX_S
                    and self.last_progress < self.t_typed and not audible and not user_talk)
        if not listen and not voice:
            mode, level = "off", 0.0
        elif audible and not user_talk:
            mode, level = "speak", spk_level
        elif user_talk:
            mode, level = "listen", (mic if mic is not None else synth_level(t, 0.3) * 0.8)
        elif thinking:
            mode, level = "think", 0.08
        elif not listen:
            mode, level = "muted", 0.0
        else:
            mode, level = "idle", (mic * 0.35 if mic is not None else 0.0)
        return mode, level, user_talk, audible

    # ---- per frame
    def render(self, dt: float, force=False) -> None:
        t = time.perf_counter() - self.t0
        self.levels.drain(time.time())
        self.poll()
        mode, level, user_talk, audible = self.decide(t)
        self.mode = mode
        k_pal = 1.0 - math.exp(-dt * 5.0) if not force else 1.0
        self.pal += (PAL[mode] - self.pal) * k_pal
        L = self.lvl.to(level, dt, up=22.0, down=7.0) if not force else level
        amp = self.breath_amp.to({"idle": 1.0, "muted": 0.6, "think": 0.5, "listen": 0.3, "speak": 0.2,
                                  "off": 0.0}[mode], dt, 3.0)
        shim = self.shimmer.to(1.0 if mode == "think" else 0.0, dt, 4.0)
        glow = self.glowk.to(0.0 if mode == "off" else (0.12 + 0.40 * L), dt, 12.0, 5.0)
        grey = self.grey.to(1.0 if mode == "off" else 0.0, dt, 5.0)
        alpha = self.alpha.to(0.70 if mode == "off" else 1.0, dt, 5.0)
        speed = 0.0 if mode == "off" else (0.45 + 2.4 * L + (0.5 if mode == "think" else 0.0))
        self.phase += dt * speed * (1.0 - grey * 0.95)
        self.rot += dt * (0.25 + 0.8 * L) * (1.0 - grey)
        breath = math.sin(t * 2.0 * math.pi / 4.6)
        swell = 1.0 + 0.045 * breath * amp + 0.16 * L
        ph = self.phase
        blobs = ((0.42 * math.sin(ph * 1.00), 0.42 * math.cos(ph * 0.83 + 1.1), 0.36, 0.50 + 0.35 * L),
                 (0.45 * math.sin(ph * 0.71 + 2.3), 0.40 * math.cos(ph * 1.13 + 0.4), 0.30, 0.40 + 0.40 * L),
                 (0.35 * math.sin(ph * 1.37 + 4.1), 0.45 * math.cos(ph * 0.61 + 2.9), 0.24, 0.30 + 0.50 * L))
        shim_pos = ((t * 0.55) % 1.0) * 3.4 - 1.7
        orb = self.rend.orb.paint(self.pal, L, swell, self.rot, blobs, shim, shim_pos, glow, grey, alpha)
        ox, oy, n = self.lay.orb
        region = self.rend.base[oy:oy + n, ox:ox + n].copy()
        over(region, orb, 0, 0)
        self.pix[oy:oy + n, ox:ox + n] = to_bgra8(region)
        # buttons
        gh = self.g_human.to((0.55 + 0.45 * L) if (user_talk and mode == "listen") else 0.0, dt, 14.0, 5.0)
        gr = self.g_robot.to((0.50 + 0.50 * L) if mode == "speak" else 0.0, dt, 14.0, 5.0)
        self.paint_buttons(gh, gr, audible, force)
        if self.caption:
            self.paint_caption(mode, t)
        if self.lay.rows:
            self.paint_list(force)

    def paint_list(self, force: bool = False) -> None:
        """Auto, then the chats; the picked one has a mark and brighter text; the row under the mouse lights up."""
        lay = self.lay
        titles = ("Auto",) + tuple(r["title"] for r in self.chats)
        sel = self.pin_index() + 1
        hv = self.hover if (self.hover or "").startswith("row:") else None
        pr = self.pressed if (self.pressed or "").startswith("row:") else None
        key = (titles, sel, hv, pr)
        if not force and key == self._list_key:
            return
        self._list_key = key
        pad = int(round(14 * self.s))
        for i, (x, y, w, h) in enumerate(lay.rows):
            region = self.rend.base[y:y + h, x:x + w].copy()
            name = "row:%d" % i
            a = (0.08 if i == sel else 0.0) + (0.05 if hv == name else 0.0) + (0.05 if pr == name else 0.0)
            if a:
                over(region, colored(self.rend.row_bg(w, h), (1.0, 1.0, 1.0), a), 0, 0)
            if i == sel:
                over(region, colored(self.rend.row_mark(h), ACCENT_PIN), int(round(4 * self.s)), 0)
            if i < len(titles):
                rgb = (0.96, 0.96, 0.97) if i == sel else (0.68, 0.68, 0.72)
                over(region, colored(self.rend.row_text(titles[i], w - 2 * pad, h), rgb), pad, 0)
            self.pix[y:y + h, x:x + w] = to_bgra8(region)

    def looks(self, audible: bool):
        st = self.st
        on_loop = st.get("listen") or st.get("voice")
        hv, pr = self.hover, self.pressed

        def bg(name, base_a, tint=(1.0, 1.0, 1.0)):
            a = base_a + (0.07 if hv == name else 0.0) + (0.08 if pr == name else 0.0)
            return tint, round(a, 3)

        L = {}
        if on_loop:
            L["power"] = bg("power", 0.13) + ((0.95, 0.95, 0.96), 1.0, False)
        else:
            L["power"] = bg("power", 0.30, OFF_TINT) + ((1.0, 0.70, 0.70), 1.0, False)
        for name, on in (("human", st.get("listen")), ("robot", st.get("voice"))):
            if on:
                L[name] = bg(name, 0.07) + ((0.92, 0.92, 0.94), 1.0, False)
            else:
                L[name] = bg(name, 0.14, OFF_TINT) + ((0.56, 0.56, 0.60), 1.0, True)
        L["stop"] = bg("stop", 0.14 if audible else 0.05) + ((0.95, 0.95, 0.96), 1.0 if audible else 0.38, False)
        reading = bool(st.get("pid"))
        L["next"] = bg("next", 0.14 if reading else 0.05) + ((0.95, 0.95, 0.96), 1.0 if reading else 0.38, False)
        L["back"] = bg("back", 0.10) + ((0.95, 0.95, 0.96), 0.85, False)
        L["hide"] = bg("hide", 0.0) + ((0.90, 0.90, 0.92), 0.85 if hv == "hide" else 0.38, False)
        if self.pin_local:      # a chat is picked: the chats button carries the accent
            L["chats"] = bg("chats", 0.26, ACCENT_PIN) + ((0.72, 0.84, 1.00), 1.0, False)
        else:
            L["chats"] = bg("chats", 0.14 if self.expanded else 0.07) + ((0.92, 0.92, 0.94), 1.0 if self.expanded else 0.85, False)
        if "chprev" in self.lay.buttons:
            on = bool(self.chats)
            for name in ("chprev", "chnext"):
                L[name] = bg(name, 0.08 if on else 0.04) + ((0.95, 0.95, 0.96), 0.90 if on else 0.30, False)
        return L

    def paint_buttons(self, gh: float, gr: float, audible: bool, force: bool) -> None:
        looks = self.looks(audible)
        glows = {"human": (gh, ACCENT_HUMAN), "robot": (gr, ACCENT_ROBOT)}
        for name, look in looks.items():
            g, rgb = glows.get(name, (0.0, None))
            key = (look, round(g, 2))
            if not force and self._btn_keys.get(name) == key:
                continue
            self._btn_keys[name] = key
            x, y, n = self.lay.buttons[name]["rect"]
            region = self.rend.base[y:y + n, x:x + n].copy()
            if g > 0.01:
                over(region, self.rend.ring(name, rgb), 0, 0, g)
            over(region, self.rend.button(name, look), 0, 0)
            self.pix[y:y + n, x:x + n] = to_bgra8(region)

    def paint_caption(self, mode: str, t: float) -> None:
        x, y, w, h = self.lay.cap
        speaking = mode == "speak" and self.progress_text
        if speaking:
            text, rgb = self.progress_text, (0.93, 0.90, 0.88)
        elif self.heard:
            text, rgb = "You: " + self.heard, (0.50, 0.80, 0.84)
        else:
            text, rgb = "", (1, 1, 1)
        strip = self.rend.strip(text, rgb)
        tw = strip.size[0]
        offset = 0
        if tw > w:
            if speaking:
                dur = self.levels.env_dur if (self.levels.env is not None and self.levels.env_mtime >=
                                              self.last_progress - 1.0) else max(2.0, len(text) / 15.0)
                el = time.time() - self.last_progress
                frac = min(1.0, max(0.0, (el - 0.8) / max(0.5, dur - 1.6)))
            else:
                frac = 0.0
            offset = int((tw - w) * frac)
        key = (text, offset)
        if key == self._cap_key:
            return
        self._cap_key = key
        crop = np.asarray(strip.crop((offset, 0, offset + w, h)), np.float32) / 255.0
        fade = np.ones(w, np.float32)
        f = max(2, int(14 * self.s))
        if offset > 0:
            fade[:f] = np.linspace(0, 1, f)
        if tw - offset > w:
            fade[-f:] = np.linspace(1, 0, f)
        m = crop * fade[None, :]
        layer = np.empty((h, w, 4), np.float32)
        layer[..., :3] = np.asarray(rgb, np.float32) * m[..., None]
        layer[..., 3] = m
        region = self.rend.base[y:y + h, x:x + w].copy()
        over(region, layer, 0, 0)
        self.pix[y:y + h, x:x + w] = to_bgra8(region)

    # ---- mouse
    def on_move(self, x, y):
        if self.drag:
            cx, cy, wx, wy, moved = self.drag
            dx, dy = self.msg_pt[0] - cx, self.msg_pt[1] - cy
            if moved or abs(dx) > 4 or abs(dy) > 4:
                self.drag = (cx, cy, wx, wy, True)
                self.pressed = None
                self.win.move(wx + dx, wy + dy)
            return
        self.hover = self.lay.hit(x, y)

    def on_down(self, x, y):
        wx, wy = self.win.pos()
        self.drag = (self.msg_pt[0], self.msg_pt[1], wx, wy, False)
        self.pressed = self.lay.hit(x, y)

    def on_up(self, x, y):
        drag, pressed = self.drag, self.pressed
        self.drag, self.pressed = None, None
        if drag and drag[4]:
            self.win.move(drag[2] + self.msg_pt[0] - drag[0], drag[3] + self.msg_pt[1] - drag[1])
            self.save_pos()
            return
        target = self.lay.hit(x, y)
        if pressed and target == pressed:
            self.activate(target)

    def on_leave(self):
        if not self.drag:
            self.hover = None

    def on_capture_lost(self):
        if self.drag and self.drag[4]:
            self.save_pos()
        self.drag, self.pressed = None, None

    def on_dpi(self, dpi, x, y):
        self.build(max(96, dpi) / 96.0)
        self.render(0.0, force=True)
        self.win.push()
        self.win.move(x, y)

    def on_display_change(self):
        x, y = self.win.pos()
        if not self.on_screen(x, y):
            self.win.move(*self.default_pos())

    def save_pos(self):
        x, y = self.win.pos()
        self.settings.update({"x": x, "y": y})
        save_settings(self.settings)

    # ---- actions
    def activate(self, name):
        if name == "power":
            set_loop(not loop_on())
        elif name == "human":
            set_listening(not listening_on())
        elif name == "robot":
            set_voice(not voice_on())
        elif name == "stop":
            stop_reading()
        elif name in ("next", "back"):
            log.info("%s button: %s", name, speak_nav.nav(name))
        elif name == "hide":
            self.hide()
        elif name == "orb":
            self.toggle_caption()
        elif name == "chats":
            self.toggle_chats()
        elif name in ("chprev", "chnext"):
            self.step_chat(-1 if name == "chprev" else 1)
        elif name and name.startswith("row:"):
            self.pick(int(name[4:]) - 1)      # row 0 is Auto
        self.poll(force=True)
        self.refresh_tray()

    def toggle_caption(self):
        self.caption = not self.caption
        self.settings["caption"] = self.caption
        save_settings(self.settings)
        self.relayout()

    def show(self):
        x, y = self.win.pos()
        if not self.on_screen(x, y):
            self.win.move(*self.default_pos())
        self.visible = True
        self.render(0.0, force=True)
        self.win.push()
        self.win.show(True)

    def hide(self):
        self.visible = False
        self.win.show(False)

    def reset_position(self):
        self.win.move(*self.default_pos())
        self.save_pos()
        self.show()

    def quit_all(self):
        self.running = False
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        user32.DestroyWindow(self.win.hwnd)

    # ---- tray: the panel is never lost. x hides it; the tray shows it again; Quit is only in the tray.
    def _tray(self):
        if NO_TRAY:
            return
        try:
            import pystray
        except ImportError:
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((2, 2, 62, 62), fill=(22, 22, 26, 255))
        for i in range(14):
            f = i / 13.0
            r = 24 - i * 1.4
            col = (int(40 + 200 * f), int(110 + 120 * f), int(200 + 50 * f), 255)
            d.ellipse((32 - r, 30 - r, 32 + r, 30 + r), fill=col)
        q = self.actions
        menu = pystray.Menu(
            pystray.MenuItem("Show panel", lambda: q.put(self.show), default=True),
            pystray.MenuItem("Hide panel", lambda: q.put(self.hide)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Voice loop (everything)", lambda: q.put(lambda: self.activate("power")),
                             checked=lambda i: loop_on()),
            pystray.MenuItem("Listening (my mic)", lambda: q.put(lambda: self.activate("human")),
                             checked=lambda i: listening_on()),
            pystray.MenuItem("Claude voice", lambda: q.put(lambda: self.activate("robot")),
                             checked=lambda i: voice_on()),
            pystray.MenuItem("Stop reading", lambda: q.put(stop_reading)),
            pystray.MenuItem("Next reply", lambda: q.put(lambda: self.activate("next"))),
            pystray.MenuItem("Back (previous reply)", lambda: q.put(lambda: self.activate("back"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Chat list", lambda: q.put(lambda: self.activate("chats")), checked=lambda i: self.expanded),
            pystray.MenuItem("Auto (the chat the app shows)", lambda: q.put(lambda: self.pick(-1)),
                             checked=lambda i: not self.pin_local),
            pystray.MenuItem("Previous chat", lambda: q.put(lambda: self.activate("chprev"))),
            pystray.MenuItem("Next chat", lambda: q.put(lambda: self.activate("chnext"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show caption", lambda: q.put(self.toggle_caption), checked=lambda i: self.caption),
            pystray.MenuItem("Reset position", lambda: q.put(self.reset_position)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: q.put(self.quit_all)),
        )
        self.tray = pystray.Icon("talkover", img, "Talkover", menu)
        self.tray.run_detached()

    def refresh_tray(self):
        try:
            if self.tray:
                self.tray.update_menu()
        except Exception:
            pass

    # ---- main loop
    def run(self):
        if winmm:
            winmm.timeBeginPeriod(1)
        msg = wt.MSG()
        last = time.perf_counter()
        next_t = last
        try:
            while self.running:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_QUIT:
                        self.running = False
                        break
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                if not self.running:
                    break
                while True:
                    try:
                        fn = self.actions.get_nowait()
                    except queue.Empty:
                        break
                    self.safe(fn)
                now = time.perf_counter()
                if now >= next_t:
                    dt = min(0.1, now - last)
                    last = now
                    if self.visible:
                        self.safe(lambda: self.frame(dt))
                        busy = self.mode in ("speak", "listen", "think") or self.hover or self.drag
                        settled = self.mode == "off" and self.alpha.v < 0.71
                        fps = 60 if busy else (8 if settled else 30)
                    else:
                        self.safe(self.poll)
                        fps = 4
                    next_t = max(next_t + 1.0 / fps, now + 0.002) if now - next_t < 0.1 else now + 1.0 / fps
                wait = max(0.0, next_t - time.perf_counter())
                user32.MsgWaitForMultipleObjects(0, None, False, int(wait * 1000), QS_ALLINPUT)
        finally:
            if winmm:
                winmm.timeEndPeriod(1)

    def frame(self, dt):
        self.render(dt)
        self.win.push()
        if time.time() - self._top_t > 2.0:
            self._top_t = time.time()
            self.win.keep_on_top()

    def safe(self, fn):
        try:
            fn()
        except Exception as e:
            k = repr(e)[:200]
            if k not in self._err_seen:
                self._err_seen.add(k)
                log.exception("panel error")


def setup_logging():
    try:
        PANEL_LOG.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=str(PANEL_LOG), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
    except OSError:
        pass


def single_instance(name: str):
    """Named mutex: a handle if this is the only copy, None if one is already running."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    h = k32.CreateMutexW(None, False, "Local\\" + MUTEX_PREFIX + name)
    if not h or ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        return None
    return h


if __name__ == "__main__":
    _instance = single_instance("voice_panel")   # held for the life of the process
    if _instance is None:
        raise SystemExit(0)                      # a panel is already running
    setup_logging()
    set_dpi_awareness()
    log.info("voice panel starting (level port %d)", LEVEL_PORT)
    Panel().run()
