#!/usr/bin/env python3
"""Record a demo take: the screen with the Claude window, your mic, and what the speakers play (Claude's voice).

    .venv\\Scripts\\python.exe scripts\\demo\\record.py                 # 75 s, then stops by itself
    ... --seconds 60 --out demo-out\\take1
    ... --synthetic scripts\\demo\\synthetic_lines.txt               # fallback: a Kokoro voice plays "you"

Nothing to press: start it, talk to Claude for a minute, it stops on its own (or Ctrl+C). The floater must already be
running. Writes into the take folder:
  screen.mkv     the monitor that shows the Claude window (ffmpeg gdigrab, NVENC when available)
  panel.mkv      the voice panel (wherever it is), shown picture-in-picture in the cut
  mic.wav        your microphone, 48 kHz mono
  system.wav     everything the speakers played (WASAPI loopback): Claude's spoken replies
  track.jsonl    30 times a second: cursor position, Claude window and voice panel rectangles (screen pixels)
  meta.json      wall-clock start of every stream, monitor rectangle, fps, synthetic flag
Everything stays local. render.ps1 turns a take into the finished cut.
"""
import argparse
import ctypes
import ctypes.wintypes as W
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from voice_config import TTS_URL  # noqa: E402  (also loads config.env)
from media import video_encoder  # noqa: E402

u32 = ctypes.windll.user32
try:
    u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2: real pixels, same as ffmpeg sees
except Exception:
    u32.SetProcessDPIAware()

RATE = 48000
FPS = 30
CLAUDE_TITLE = os.getenv("VOICE_INBOX_WINDOW", "Claude")
PANEL_TITLE = "Talkover"


def find_window(title):
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)

    @proc
    def cb(h, _):
        if u32.IsWindowVisible(h):
            n = u32.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(h, b, n + 1)
                if b.value == title:
                    found.append(h)
        return True
    u32.EnumWindows(cb, 0)
    return found[0] if found else None


def rect(h):
    r = W.RECT()
    if not h or not u32.GetWindowRect(h, ctypes.byref(r)):
        return None
    return [r.left, r.top, r.right - r.left, r.bottom - r.top]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


def monitor_of(h):
    mon = u32.MonitorFromWindow(h, 2)   # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    u32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcMonitor
    return [r.left, r.top, r.right - r.left, r.bottom - r.top]


class Screen:
    """ffmpeg gdigrab of one screen region. The wall clock of frame 0 comes from ffmpeg's own progress lines."""

    def __init__(self, mon, path):
        x, y, w, h = mon
        w, h = w - w % 2, h - h % 2
        self.region = [x, y, w, h]
        self.cmd = ["ffmpeg", "-hide_banner", "-y", "-f", "gdigrab", "-framerate", str(FPS), "-draw_mouse", "1",
                    "-offset_x", str(x), "-offset_y", str(y), "-video_size", "%dx%d" % (w, h), "-i", "desktop",
                    *video_encoder(), "-stats_period", "0.5", str(path)]
        self.t0 = None
        self.size = [w, h]

    def start(self):
        self.p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        for line in self.p.stderr:
            m = re.search(r"frame=\s*(\d+)", line)
            if m and self.t0 is None and int(m.group(1)) > 0:
                self.t0 = time.time() - int(m.group(1)) / FPS

    def stop(self):
        try:
            self.p.stdin.write("q")
            self.p.stdin.flush()
        except Exception:
            pass
        try:
            self.p.wait(15)
        except subprocess.TimeoutExpired:
            self.p.kill()


class Track:
    """Audio blocks placed by their wall-clock time, so WASAPI loopback's silent gaps (it can deliver nothing while
    nothing plays) become real silence instead of shifting the rest of the track."""

    def __init__(self, path, rate):
        self.path, self.rate, self.blocks, self.t0 = path, rate, [], None

    def add(self, mono):
        t = time.time() - len(mono) / self.rate
        if self.t0 is None:
            self.t0 = t
        self.blocks.append((t, mono))

    def save(self):
        if not self.blocks:
            return
        rate = self.rate
        end = self.blocks[-1][0] + len(self.blocks[-1][1]) / rate
        out = np.zeros(int((end - self.t0) * rate) + rate, dtype=np.float32)
        pos = 0
        for t, d in self.blocks:
            want = int(round((t - self.t0) * rate))
            if abs(want - pos) < rate // 20:     # within 50 ms: keep the stream contiguous
                want = pos
            n = max(0, min(len(d), len(out) - want))
            out[want:want + n] = d[:n]
            pos = want + len(d)
        out = out[:pos]
        pcm = (np.clip(out, -1, 1) * 32767).astype("<i2")
        with wave.open(str(self.path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())


class Mic:
    def __init__(self, path):
        import sounddevice as sd
        self.track = Track(path, RATE)
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=1024,
                                     callback=lambda d, f, t, st: self.track.add(d[:, 0].copy()))

    def start(self):
        self.stream.start()

    def stop(self):
        self.stream.stop()
        self.stream.close()


class Loopback:
    """What the default speakers play (PyAudioWPatch, WASAPI loopback), mixed down to mono."""

    def __init__(self, path):
        import pyaudiowpatch as pa
        self.pa = pa.PyAudio()
        dev = self.pa.get_default_wasapi_loopback()
        self.ch, rate = dev["maxInputChannels"], int(dev["defaultSampleRate"])
        self.track = Track(path, rate)

        def cb(data, n, ti, st):
            a = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
            self.track.add(a.reshape(-1, self.ch).mean(axis=1))
            return (None, pa.paContinue)
        self.stream = self.pa.open(format=pa.paInt16, channels=self.ch, rate=rate, input=True,
                                   input_device_index=dev["index"], frames_per_buffer=1024, stream_callback=cb)

    def start(self):
        self.stream.start_stream()

    def stop(self):
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()


def tracker(stop, path, claude):
    pt = W.POINT()
    panel = find_window(PANEL_TITLE)
    with open(path, "w", encoding="utf-8") as f:
        while not stop.is_set():
            u32.GetCursorPos(ctypes.byref(pt))
            f.write(json.dumps({"t": round(time.time(), 3), "cursor": [pt.x, pt.y],
                                "claude": rect(claude), "panel": rect(panel)}) + "\n")
            time.sleep(1 / FPS)


def synthetic_player(lines, stop, log):
    """Fallback only: speak each line in a second Kokoro voice through the speakers, waiting for the reply between."""
    import urllib.request
    import io
    import sounddevice as sd
    time.sleep(3)
    for text in lines:
        if stop.is_set():
            return
        body = json.dumps({"model": "kokoro", "voice": "am_michael", "input": text, "response_format": "wav"}).encode()
        req = urllib.request.Request(TTS_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            with wave.open(io.BytesIO(r.read())) as w:
                rate, pcm = w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        t = time.time()
        sd.play(pcm, rate, blocking=True)
        log.append({"t": t, "dur": len(pcm) / rate, "text": text})
        stop.wait(14)                                      # time for Claude to answer and read part of it


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=75)
    ap.add_argument("--out", default=str(ROOT / "demo-out" / time.strftime("take-%Y%m%d-%H%M%S")))
    ap.add_argument("--synthetic", help="text file, one line per thing 'you' say (fallback demo, labeled synthetic)")
    a = ap.parse_args()

    claude = find_window(CLAUDE_TITLE)
    if not claude:
        sys.exit("No window titled %r. Open the Claude desktop app (Code tab) first." % CLAUDE_TITLE)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    mon = monitor_of(claude)

    screen = Screen(mon, out / "screen.mkv")
    panel_rect = rect(find_window(PANEL_TITLE))
    panel = Screen([panel_rect[0] - 12, panel_rect[1] - 12, panel_rect[2] + 24, panel_rect[3] + 24], out / "panel.mkv")         if panel_rect else None
    mic = Mic(out / "mic.wav")
    loop = Loopback(out / "system.wav")
    stop = threading.Event()
    synth_log = []

    print("Recording %.0f s of the monitor with the Claude window. Talk to Claude; it stops by itself." % a.seconds)
    import sounddevice as sd
    # WASAPI loopback delivers nothing while nothing plays; a silent output stream keeps it flowing.
    keepalive = sd.OutputStream(samplerate=RATE, channels=1, dtype="float32",
                                callback=lambda o, f, t, s: o.fill(0))
    keepalive.start()
    mic.start()
    loop.start()
    screen.start()
    if panel:
        panel.start()
    tr = threading.Thread(target=tracker, args=(stop, out / "track.jsonl", claude), daemon=True)
    tr.start()
    if a.synthetic:
        lines = [l.strip() for l in Path(a.synthetic).read_text(encoding="utf-8").splitlines() if l.strip()]
        threading.Thread(target=synthetic_player, args=(lines, stop, synth_log), daemon=True).start()
    try:
        end = time.time() + a.seconds
        while time.time() < end:
            left = int(end - time.time())
            print("\r  %2d s left " % left, end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    print("\nStopping...")
    stop.set()
    mic.stop()
    loop.stop()
    screen.stop()
    if panel:
        panel.stop()
    time.sleep(0.3)
    keepalive.stop()
    mic.track.save()
    loop.track.save()
    meta = {"video_t0": screen.t0, "panel_t0": panel.t0 if panel else None,
            "panel_rect": panel.region if panel else None,
            "mic_t0": mic.track.t0, "system_t0": loop.track.t0, "fps": FPS, "monitor": mon,
            "video_size": screen.size, "synthetic": bool(a.synthetic), "synthetic_lines": synth_log,
            "recorded": time.strftime("%Y-%m-%d %H:%M:%S")}
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("Take saved:", out)


if __name__ == "__main__":
    main()
