#!/usr/bin/env python3
"""Turn a take from record.py into an edit: what to keep, at what speed, where the camera looks, and the captions.

    .venv\\Scripts\\python.exe scripts\\demo\\build.py demo-out\\take-...   [--target 45] [--max 60]

Steps (all local):
  1. align mic.wav and system.wav to the screen recording and mix them (audio.wav)
  2. transcribe both with the local Whisper (word timestamps): your turns and Claude's spoken replies
  3. pick exchanges: each of your turns in full, the wait for Claude fast-forwarded, the start of each reply,
     and any moment you talked over a reply (barge-in), until the cut is target..max seconds
  4. find where the screen changes (frame differences, cursor masked out) to aim the camera at the input box while
     you talk and at the reply while Claude talks
  5. cut the video with ffmpeg (cut.mp4, panel.mp4) and write timeline.json for the Remotion render
"""
import argparse
import io
import json
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from voice_config import STT_URL  # noqa: E402
from media import video_encoder  # noqa: E402

RATE = 48000
OUT_FPS = 30
FAST = 6.0                      # speed of the "Claude is working" stretches


# ------------------------------------------------------------------ audio
def read_wav(path):
    with wave.open(str(path)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        return a, w.getframerate()


def write_wav(path, a, rate=RATE):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())


def align(a, rate, t0, video_t0, length):
    """Shift a track so sample 0 is the first video frame; pad or trim to length seconds."""
    if rate != RATE:
        idx = np.arange(0, len(a), rate / RATE)
        a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
    off = int(round((t0 - video_t0) * RATE))
    a = np.concatenate([np.zeros(off, np.float32), a]) if off > 0 else a[-off:]
    n = int(length * RATE)
    return np.pad(a, (0, max(0, n - len(a))))[:n]


def normalize(a, target=0.25):
    loud = np.percentile(np.abs(a), 99.5)
    return a * (target / loud) if loud > 1e-4 else a


def transcribe(a):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        a16 = np.interp(np.arange(0, len(a), RATE / 16000), np.arange(len(a)), a)
        w.writeframes((np.clip(a16, -1, 1) * 32767).astype("<i2").tobytes())
    boundary = "----demo%d" % int(time.time() * 1000)
    parts = [("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
             for k, v in (("model", "whisper-1"), ("language", "en"), ("response_format", "verbose_json"),
                          ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment"))]
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n" % boundary).encode() + buf.getvalue() + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(STT_URL, data=b"".join(parts),
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read().decode("utf-8"))
    words = d.get("words") or [w for s in d.get("segments", []) for w in s.get("words", [])]
    return [{"w": w["word"].strip(), "s": float(w["start"]), "e": float(w["end"])} for w in words if w["word"].strip()]


def phrases(words, gap=0.9):
    out = []
    for w in words:
        if out and w["s"] - out[-1]["e"] <= gap:
            out[-1]["e"] = w["e"]
            out[-1]["words"].append(w)
        else:
            out.append({"s": w["s"], "e": w["e"], "words": [w]})
    for p in out:
        p["text"] = " ".join(w["w"] for w in p["words"])
    return out


def drop_echo(user, claude):
    """With speakers instead of a headset the mic also hears Claude: drop mic phrases that are Claude's words."""
    keep = []
    for p in user:
        mine = {w["w"].lower().strip(".,!?") for w in p["words"]}
        theirs = {w["w"].lower().strip(".,!?") for c in claude if c["e"] > p["s"] - 1 and c["s"] < p["e"] + 1
                  for w in c["words"]}
        if not mine or len(mine & theirs) / len(mine) < 0.6:
            keep.append(p)
    return keep


# ------------------------------------------------------------------ edit
def plan(user, claude, length, target, maximum):
    """Segments of source time: (start, end, speed, kind). kind: you | claude | wait | barge."""
    segs, total = [], 0.0
    events = sorted([dict(p, who="you") for p in user] + [dict(p, who="claude") for p in claude], key=lambda p: p["s"])
    cursor = None
    for i, p in enumerate(events):
        if total >= target:
            break
        if p["who"] == "you":
            s, e = max(0.0, p["s"] - 0.35), min(length, p["e"] + 0.45)
            if cursor is not None and s - cursor > 0.5:
                gap = (cursor, s)
                if gap[1] - gap[0] > 2.0:
                    segs.append((gap[0], gap[1], FAST, "wait"))
                    total += (gap[1] - gap[0]) / FAST
                else:
                    segs.append((gap[0], gap[1], 1.0, "wait"))
                    total += gap[1] - gap[0]
            elif cursor is not None and s < cursor:
                s = cursor
            kind = "barge" if any(c["s"] < p["s"] < c["e"] for c in claude) else "you"
            segs.append((s, e, 1.0, kind))
            total += e - s
            cursor = e
        else:
            if cursor is None:
                continue                        # Claude talking before you said anything: not part of the story
            s = max(p["s"] - 0.2, cursor)
            if s - cursor > 2.0:
                segs.append((cursor, s, FAST, "wait"))
                total += (s - cursor) / FAST
            elif s > cursor:
                segs.append((cursor, s, 1.0, "wait"))
                total += s - cursor
            nxt = next((u["s"] for u in user if u["s"] > p["s"]), None)
            room = max(3.0, min(7.5, maximum - total - 4))
            e = min(p["e"] + 0.3, s + room)
            if nxt is not None and p["s"] < nxt < p["e"]:
                e = max(e, nxt)                 # keep the reading going until you cut in
            segs.append((s, e, 1.0, "claude"))
            total += e - s
            cursor = e
        if total >= maximum:
            break
    return segs


def to_out(t, segs):
    """Source second -> output second, or None if t was cut."""
    out = 0.0
    for s, e, sp, _ in segs:
        if s <= t <= e:
            return out + (t - s) / sp
        out += (e - s) / sp
    return None


# ------------------------------------------------------------------ camera
def activity(video, track, meta, step=0.2):
    """Bounding boxes of where the screen changed, per step seconds (source pixels), cursor masked out."""
    w, h = meta["video_size"]
    sw = 430
    sh = int(round(h * sw / w)) // 2 * 2
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", "fps=%g,scale=%d:%d,format=gray" % (1 / step, sw, sh),
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, sh, sw).astype(np.int16)
    k = sw / w
    mx, my = meta["monitor"][0], meta["monitor"][1]
    cur_t = np.array([r["t"] - meta["video_t0"] for r in track])
    cur_xy = np.array([r["cursor"] for r in track], float) if track else np.zeros((0, 2))
    boxes = []
    for i in range(1, len(frames)):
        d = np.abs(frames[i] - frames[i - 1]) > 18
        if len(cur_t):
            j = int(np.clip(np.searchsorted(cur_t, i * step), 0, len(cur_t) - 1))
            cx, cy = (cur_xy[j][0] - mx) * k, (cur_xy[j][1] - my) * k
            d[max(0, int(cy) - 6):int(cy) + 8, max(0, int(cx) - 6):int(cx) + 8] = False
        ys, xs = np.nonzero(d)
        if len(xs) < 6:
            boxes.append(None)
            continue
        x0, x1 = np.percentile(xs, [3, 97]) / k
        y0, y1 = np.percentile(ys, [3, 97]) / k
        boxes.append((i * step, x0, y0, x1, y1))
    return boxes


def fit(win, w, h):
    """Camera that shows every change in win with a margin: the union of the change boxes, not their median."""
    x0 = np.percentile([b[1] for b in win], 10); x1 = np.percentile([b[3] for b in win], 90)
    y0 = np.percentile([b[2] for b in win], 10); y1 = np.percentile([b[4] for b in win], 90)
    base_vh = min(h, w * 9 / 16)
    base_vw = base_vh * 16 / 9
    bw, bh = max(x1 - x0, 500) * 1.25, max(y1 - y0, 280) * 1.5
    zoom = float(np.clip(min(base_vw / bw, base_vh / bh), 1.0, 1.55))
    return float((x0 + x1) / 2), float((y0 + y1) / 2), zoom


def cameras(segs, boxes, meta, hold=2.5):
    """One shot per segment; long segments get a new shot every `hold` seconds so the camera follows a reply down."""
    w, h = meta["video_size"]
    cams = []
    out = 0.0
    for s, e, sp, kind in segs:
        if kind == "wait" and (e - s) / sp < 1.2 and cams:
            out += (e - s) / sp                 # a short wait keeps the shot: no zoom pumping
            continue
        if kind == "wait":
            cams.append({"t": out, "cx": w / 2, "cy": h / 2, "zoom": 1.0, "kind": kind})
            out += (e - s) / sp
            continue
        t = s
        while t < e - 0.5:
            win = [b for b in boxes if b and s <= b[0] <= min(e, t + hold)]   # everything so far stays in view
            if win:
                cx, cy, zoom = fit(win, w, h)
            else:
                cx, cy, zoom = w / 2, h / 2, 1.0
            last = cams[-1] if cams else None
            moved = not last or abs(last["cx"] - cx) > 120 or abs(last["cy"] - cy) > 80 or abs(last["zoom"] - zoom) > 0.12
            if moved:
                cams.append({"t": out + (t - s) / sp, "cx": cx, "cy": cy, "zoom": zoom, "kind": kind})
            t += hold
        out += (e - s) / sp
    return cams


# ------------------------------------------------------------------ cut
def cut(src, segs, dst, audio=None):
    parts, fc = [], []
    for i, (s, e, sp, kind) in enumerate(segs):
        fc.append("[0:v]trim=start=%.3f:end=%.3f,setpts=(PTS-STARTPTS)/%g,fps=%d[v%d]" % (s, e, sp, OUT_FPS, i))
        if audio is not None:
            if sp == 1.0:
                fc.append("[1:a]atrim=start=%.3f:end=%.3f,asetpts=PTS-STARTPTS[a%d]" % (s, e, i))
            else:
                fc.append("aevalsrc=0:d=%.3f:s=%d[a%d]" % ((e - s) / sp, RATE, i))
            parts.append("[v%d][a%d]" % (i, i))
        else:
            parts.append("[v%d]" % i)
    fc.append("%sconcat=n=%d:v=1:a=%d[v][a]" % ("".join(parts), len(segs), 1 if audio is not None else 0)
              if audio is not None else "%sconcat=n=%d:v=1:a=0[v]" % ("".join(parts), len(segs)))
    venc = video_encoder(18)
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src)] + (["-i", str(audio)] if audio is not None else []) + \
          ["-filter_complex", ";".join(fc), "-map", "[v]"] + (["-map", "[a]", "-c:a", "aac", "-b:a", "192k"] if audio is not None else []) + \
          venc + [str(dst)]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("take")
    ap.add_argument("--target", type=float, default=45)
    ap.add_argument("--max", type=float, default=60)
    ap.add_argument("--title", default="")
    ap.add_argument("--subtitle", default="Hands-free voice for Claude Code. Always listening, talks back, fully local.")
    ap.add_argument("--url", default="")
    ap.add_argument("--music", default="")
    a = ap.parse_args()
    take = Path(a.take)
    meta = json.loads((take / "meta.json").read_text(encoding="utf-8"))
    if not meta.get("video_t0"):
        sys.exit("meta.json has no video start time: the screen recording did not start")
    length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                   str(take / "screen.mkv")], capture_output=True, text=True).stdout.strip())
    print("take: %.1f s" % length)

    mic, mr = read_wav(take / "mic.wav")
    sysa, sr = read_wav(take / "system.wav")
    mic = align(mic, mr, meta["mic_t0"], meta["video_t0"], length)
    sysa = align(sysa, sr, meta["system_t0"], meta["video_t0"], length)
    write_wav(take / "audio.wav", normalize(mic, 0.3) + normalize(sysa, 0.22))

    print("transcribing (local Whisper)...")
    claude = phrases(transcribe(sysa))
    if meta.get("synthetic"):
        user = [{"s": l["t"] - meta["video_t0"], "e": l["t"] - meta["video_t0"] + l["dur"], "text": l["text"],
                 "words": [{"w": x, "s": l["t"] - meta["video_t0"], "e": l["t"] - meta["video_t0"] + l["dur"]}
                           for x in l["text"].split()]} for l in meta["synthetic_lines"]]
        claude = [c for c in claude if not any(u["s"] - 0.3 < c["s"] < u["e"] + 0.3 for u in user)]
    else:
        user = drop_echo(phrases(transcribe(mic)), claude)
    print("you: %d phrases, Claude: %d phrases" % (len(user), len(claude)))
    if not user:
        sys.exit("No speech of yours found in mic.wav. Check the microphone and record again.")

    segs = plan(user, claude, length, a.target, a.max)
    dur = sum((e - s) / sp for s, e, sp, _ in segs)
    print("cut: %d segments, %.1f s" % (len(segs), dur))

    track = [json.loads(l) for l in (take / "track.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    cams = cameras(segs, activity(take / "screen.mkv", track, meta), meta)

    print("cutting video...")
    cut(take / "screen.mkv", segs, take / "cut.mp4", take / "audio.wav")
    panel = None
    if (take / "panel.mkv").exists() and meta.get("panel_t0"):
        off = meta["panel_t0"] - meta["video_t0"]
        psegs = [(max(0.0, s - off), max(0.0, e - off), sp, k) for s, e, sp, k in segs]
        cut(take / "panel.mkv", psegs, take / "panel.mp4")
        panel = {"w": meta["panel_rect"][2], "h": meta["panel_rect"][3]}

    caps = []
    for who, ps in (("you", user), ("claude", claude)):
        for p in ps:
            ws = []
            for w in p["words"]:
                t = to_out(w["s"], segs)
                if t is not None:
                    ws.append({"text": w["w"], "t": round(t, 3)})
            if ws:
                end = to_out(p["e"], segs)
                caps.append({"who": who, "start": ws[0]["t"], "end": round(end if end else ws[-1]["t"] + 0.6, 3), "words": ws})
    caps.sort(key=lambda c: c["start"])

    mx, my = meta["monitor"][:2]
    cursor = []
    for r in track[::3]:
        t = to_out(r["t"] - meta["video_t0"], segs)
        if t is not None:
            cursor.append({"t": round(t, 3), "x": r["cursor"][0] - mx, "y": r["cursor"][1] - my})

    timeline = {
        "fps": OUT_FPS, "duration": round(dur, 3), "source": {"w": meta["video_size"][0], "h": meta["video_size"][1]},
        "segments": [{"t": round(to_out(s, segs) or 0, 3), "dur": round((e - s) / sp, 3), "speed": sp, "kind": k} for s, e, sp, k in segs],
        "cameras": cams, "captions": caps, "cursor": cursor, "panel": panel,
        "title": a.title, "subtitle": a.subtitle, "url": a.url, "synthetic": bool(meta.get("synthetic")),
        "music": bool(a.music),
    }
    (take / "timeline.json").write_text(json.dumps(timeline, indent=1), encoding="utf-8")
    (take / "props.json").write_text(json.dumps({"timeline": timeline}), encoding="utf-8")   # Remotion --props (no BOM)
    if a.music:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", a.music, "-t", str(dur + 8), str(take / "music.mp3")], check=True)
    print("timeline:", take / "timeline.json")


if __name__ == "__main__":
    main()
