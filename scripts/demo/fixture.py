#!/usr/bin/env python3
"""Make a fake take (same files as record.py) to test build.py and the render without recording anyone.

    .venv\\Scripts\\python.exe scripts\\demo\\fixture.py demo-out\\fixture

A drawn stand-in for the Claude window, "you" spoken by one Kokoro voice into mic.wav, Claude by another into
system.wav, with one moment where you talk over the reply. Needs the Kokoro engine running.
"""
import io
import json
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from voice_config import TTS_URL  # noqa: E402
from media import video_encoder  # noqa: E402

W, H, FPS, RATE, LEN = 2560, 1440, 30, 48000, 48.0
SCRIPT = [  # (start s, who, text)
    (2.0, "you", "Hey Claude, what does the build script do?"),
    (7.5, "claude", "It installs the Python packages, starts the two speech engines in Docker, and downloads the Whisper model."),
    (19.0, "you", "Okay. Add a check that Docker is running first."),
    (25.0, "claude", "Sure. I will add a docker info call at the top of the install script, and if it fails, the script"),
    (30.2, "you", "Wait, make it print a clear message too."),
    (36.0, "claude", "Done. It now stops with a clear message when Docker Desktop is not running."),
]


def tts(text, voice):
    body = json.dumps({"model": "kokoro", "voice": voice, "input": text, "response_format": "wav"}).encode()
    req = urllib.request.Request(TTS_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r, wave.open(io.BytesIO(r.read())) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        rate = w.getframerate()
    return np.interp(np.arange(0, len(a), rate / RATE), np.arange(len(a)), a).astype(np.float32)


def save(path, a):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())


def font(size):
    for name in ("segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "demo-out" / "fixture")
    out.mkdir(parents=True, exist_ok=True)
    mic = np.zeros(int(LEN * RATE), np.float32)
    syst = np.zeros(int(LEN * RATE), np.float32)
    spans = []
    for i, (t, who, text) in enumerate(SCRIPT):
        a = tts(text, "am_michael" if who == "you" else "af_heart")
        end = t + len(a) / RATE
        nxt = next((s for s, w, _ in SCRIPT[i + 1:] if w == "you"), None)
        if who == "claude" and nxt is not None and nxt < end:      # cut off when you talk over it (+0.4 s pause)
            a = a[: int((nxt + 0.4 - t) * RATE)]
            end = t + len(a) / RATE
        dst = mic if who == "you" else syst
        s = int(t * RATE)
        dst[s:s + len(a)] += a[: len(dst) - s]
        spans.append((t, end, who, text))
    save(out / "mic.wav", mic)
    save(out / "system.wav", syst)

    f_big, f_mid, f_small = font(34), font(30), font(24)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
                             "-r", str(FPS), "-i", "-", *video_encoder(20), str(out / "screen.mkv")], stdin=subprocess.PIPE)
    track = []
    t0 = 1_800_000_000.0
    for n in range(int(LEN * FPS)):
        t = n / FPS
        im = Image.new("RGB", (W, H), (24, 25, 28))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 380, H], fill=(18, 19, 22))
        for k, name in enumerate(["Fix install script", "Talkover README", "Demo pipeline"]):
            d.rounded_rectangle([20, 90 + k * 64, 360, 140 + k * 64], 10, fill=(34, 36, 41) if k == 0 else None)
            d.text((40, 102 + k * 64), name, font=f_small, fill=(200, 202, 206))
        y = 160
        for s, e, who, text in spans:
            if t < s:
                break
            if who == "you":
                if t < e + 0.6:                        # still being said: shown in the input box below
                    continue
                d.rounded_rectangle([1500, y, 2300, y + 70], 16, fill=(44, 46, 52))
                d.text((1530, y + 18), text[:60], font=f_mid, fill=(235, 236, 238))
                y += 110
            else:
                shown = text[: int(len(text) * min(1.0, (t - s) / max(0.5, e - s)))]
                lines = [shown[i:i + 70] for i in range(0, len(shown), 70)]
                for ln in lines:
                    d.text((620, y), ln, font=f_big, fill=(225, 226, 229))
                    y += 52
                y += 40
        d.rounded_rectangle([600, H - 190, 2320, H - 70], 22, outline=(70, 72, 78), width=2, fill=(30, 31, 35))
        typing = next(((s, e, text) for s, e, who, text in spans if who == "you" and s <= t < e + 0.6), None)
        if typing:
            s, e, text = typing
            d.text((640, H - 150), text[: int(len(text) * min(1.0, (t - s) / max(0.4, e - s)))], font=f_mid, fill=(240, 240, 242))
        cx, cy = 1400 + 200 * np.sin(t / 5), 900 + 80 * np.cos(t / 4)
        proc.stdin.write(im.tobytes())
        track.append({"t": round(t0 + t, 3), "cursor": [int(cx), int(cy)], "claude": [0, 0, W, H], "panel": None})
    proc.stdin.close()
    proc.wait()

    pw, ph = 448, 144
    pp = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (pw, ph),
                           "-r", str(FPS), "-i", "-", *video_encoder(20), str(out / "panel.mkv")], stdin=subprocess.PIPE)
    for n in range(int(LEN * FPS)):
        t = n / FPS
        who = next((w for s, e, w, _ in spans if s <= t < e), None)
        col = (94, 234, 212) if who == "you" else (251, 154, 108) if who == "claude" else (90, 130, 230)
        im = Image.new("RGB", (pw, ph), (20, 20, 22))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([12, 12, pw - 12, ph - 12], 30, fill=(32, 33, 37))
        r = 30 + (6 * np.sin(t * 8) if who else 2 * np.sin(t * 2))
        d.ellipse([70 - r, 72 - r, 70 + r, 72 + r], fill=col)
        for k in range(5):
            d.ellipse([150 + k * 54, 52, 190 + k * 54, 92], fill=(52, 54, 60))
        pp.stdin.write(im.tobytes())
    pp.stdin.close()
    pp.wait()

    (out / "track.jsonl").write_text("\n".join(json.dumps(r) for r in track) + "\n", encoding="utf-8")
    meta = {"video_t0": t0, "panel_t0": t0, "panel_rect": [0, 0, pw, ph], "mic_t0": t0, "system_t0": t0, "fps": FPS,
            "monitor": [0, 0, W, H], "video_size": [W, H], "synthetic": True, "fixture": True,
            "synthetic_lines": [{"t": t0 + s, "dur": e - s, "text": x} for s, e, w, x in spans if w == "you"],
            "recorded": time.strftime("%Y-%m-%d %H:%M:%S")}
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("fixture take:", out)


if __name__ == "__main__":
    main()
