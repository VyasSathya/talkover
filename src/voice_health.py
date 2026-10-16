#!/usr/bin/env python3
"""
Health check for the Talkover voice loop. Plain text report, exit code 0 = all OK, 1 = something is off.

Checks:
  * voice_watchdog.py, voice_inbox.py, voice_panel.py (and stop_word_listener.py if enabled) each running exactly once
    (the venv pythonw.exe is a launcher that starts a python.exe child; that pair counts as one copy)
  * the single-instance mutex of each (present = the new single-instance code is running)
  * speak-response.ps1 hook processes (0 or 1 is normal; many = stuck hooks eating the CPU)
  * Docker containers talkover-whisper and talkover-kokoro running
  * a timed test transcription (1.5 s tone generated in memory, POSTed to Whisper)
  * a timed test of Kokoro text to speech
  * a round trip: Kokoro speaks a sentence and Whisper must transcribe it
  * whether the reading is paused right now, and for how long

Run:  .venv\\Scripts\\python.exe src\\voice_health.py   [--engines-only]
Note: the transcription test adds one request to Whisper.
"""
import ctypes
import io
import json
import math
import os
import struct
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

from voice_config import MUTEX_PREFIX, CODE_DIR, KOKORO_CONTAINER, SPEAK_HOOK, STT_URL, TTS_URL, WHISPER_CONTAINER

SCRIPTS = ["voice_watchdog.py", "voice_inbox.py", "voice_panel.py"]
if os.getenv("TALKOVER_STOP_WORD", "0") == "1":
    SCRIPTS.append("stop_word_listener.py")
CONTAINERS = [WHISPER_CONTAINER, KOKORO_CONTAINER]
SLOW_S = 5.0
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

problems = []


def line(status, what, detail=""):
    print("%-5s %-28s %s" % (status, what, detail))
    if status not in ("OK", "INFO"):
        problems.append(what)


def processes():
    cmd = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine "
           "| ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                         capture_output=True, text=True, timeout=60, creationflags=NOWIN).stdout
    data = json.loads(out or "[]")
    return data if isinstance(data, list) else [data]


def mutex_exists(name):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = ctypes.c_void_p
    h = k32.OpenMutexW(0x00100000, False, "Local\\" + MUTEX_PREFIX + name)   # SYNCHRONIZE
    if h:
        k32.CloseHandle(ctypes.c_void_p(h))
        return True
    return False


def check_processes():
    try:
        procs = processes()
    except Exception as e:
        line("FAIL", "process list", str(e))
        return
    by_pid = {p["ProcessId"]: p for p in procs}
    def cmd(p):
        return (p.get("CommandLine") or "").replace("/", "\\").lower()
    for script in SCRIPTS:
        full = str(CODE_DIR / script).lower()          # only copies started from this folder count
        mine = [p for p in procs if full in cmd(p)]
        # a copy = a matching process whose parent is not itself a matching process (launcher -> child)
        roots = [p for p in mine if full not in cmd(by_pid.get(p["ParentProcessId"]) or {})]
        n = len(roots)
        pids = ",".join(str(p["ProcessId"]) for p in roots)
        mtx = "mutex held" if mutex_exists(script[:-3]) else "no mutex (old code)"
        status = "OK" if n == 1 else "FAIL"
        line(status, script, "%d running (pid %s), %s" % (n, pids or "-", mtx))
    hook = str(SPEAK_HOOK).replace("/", "\\").lower()     # only this install's hook, not another copy's
    hooks = [p for p in procs if hook in cmd(p)]
    line("OK" if len(hooks) <= 1 else "WARN", "speak-response.ps1 hooks",
         "%d running (0 or 1 is normal)" % len(hooks))


def check_containers():
    for c in CONTAINERS:
        try:
            r = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}} since {{.State.StartedAt}}", c],
                               capture_output=True, text=True, timeout=20, creationflags=NOWIN)
            out = (r.stdout or r.stderr).strip()
        except Exception as e:
            out = str(e)
        line("OK" if out.startswith("running") else "FAIL", "docker " + c, out[:60])


def tone_wav(seconds=1.5, rate=16000, hz=440.0):
    frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate)))
                      for i in range(int(seconds * rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(frames)
    return buf.getvalue()


def check_stt(warmup=False):
    """Timed transcription of a tone. warmup: one untimed request first (right after install the model is still
    loading, which says nothing about normal speed)."""
    if warmup:
        try:
            _stt_request(60)
        except Exception:
            pass
    t0 = time.monotonic()
    try:
        text = _stt_request(60)
        dt = time.monotonic() - t0
        line("OK" if dt <= SLOW_S else "SLOW", "whisper test (1.5 s tone)",
             "%.2f s, text %r" % (dt, text[:40]))
    except Exception as e:
        line("FAIL", "whisper test (1.5 s tone)", "%.1f s: %s" % (time.monotonic() - t0, e))


def _stt_request(timeout):
    body = tone_wav()
    boundary = "----voicehealth%d" % int(time.time() * 1000)
    parts = []
    for name, val in (("model", "whisper-1"), ("response_format", "json"), ("language", "en"),
                      ("temperature", "0")):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, name, val)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n" % boundary).encode() + body + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(STT_URL, data=b"".join(parts),
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")).get("text", "")


def check_tts():
    data = json.dumps({"model": "kokoro", "voice": "af_heart", "input": "Voice health check.",
                       "response_format": "wav"}).encode()
    req = urllib.request.Request(TTS_URL, data=data, headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            audio = r.read()
        dt = time.monotonic() - t0
        line("OK" if dt <= SLOW_S and len(audio) > 1000 else "SLOW", "kokoro test (3 words)",
             "%.2f s, %d bytes of audio" % (dt, len(audio)))
    except Exception as e:
        line("FAIL", "kokoro test (3 words)", "%.1f s: %s" % (time.monotonic() - t0, e))


def check_roundtrip():
    """Kokoro says a sentence, Whisper must hear it: proves both engines and the model work end to end."""
    phrase = "The quick brown fox jumps over the lazy dog."
    t0 = time.monotonic()
    try:
        data = json.dumps({"model": "kokoro", "voice": "af_heart", "input": phrase, "response_format": "wav"}).encode()
        req = urllib.request.Request(TTS_URL, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            audio = r.read()
        boundary = "----voiceround%d" % int(time.time() * 1000)
        parts = [("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
                 for k, v in (("model", "whisper-1"), ("response_format", "json"), ("language", "en"))]
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"r.wav\"\r\n"
                      "Content-Type: audio/wav\r\n\r\n" % boundary).encode() + audio + b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode())
        req = urllib.request.Request(STT_URL, data=b"".join(parts),
                                     headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        with urllib.request.urlopen(req, timeout=120) as r:
            text = json.loads(r.read().decode("utf-8")).get("text", "")
        heard = sum(w in text.lower() for w in ("quick", "brown", "fox", "lazy", "dog"))
        line("OK" if heard >= 4 else "FAIL", "round trip (kokoro -> whisper)",
             "%.2f s, heard %r" % (time.monotonic() - t0, text.strip()[:60]))
    except Exception as e:
        line("FAIL", "round trip (kokoro -> whisper)", "%.1f s: %s" % (time.monotonic() - t0, e))


def check_pause():
    p = Path(os.getenv("TEMP", "")) / "claude-speak.pause"
    try:
        age = time.time() - p.stat().st_mtime
        line("OK" if age <= 6 else "WARN", "reading paused", "yes, for %.1f s" % age)
    except OSError:
        line("INFO", "reading paused", "no")


def main():
    engines_only = "--engines-only" in sys.argv
    print("Talkover health check  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    if not engines_only:
        check_processes()
    check_containers()
    check_stt(warmup=engines_only)          # --engines-only is what install.ps1 runs, right after the model download
    check_tts()
    check_roundtrip()
    if not engines_only:
        check_pause()
    print("RESULT: %s" % ("all OK" if not problems else "problems: " + ", ".join(problems)))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
