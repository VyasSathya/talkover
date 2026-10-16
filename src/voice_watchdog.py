"""Keeps Talkover working after idle hours and sleep (2026-10-12).

Voice used to stop working after the machine sat idle for a while. On 2026-10-12 Kokoro (docker container talkover-kokoro)
had hung after hours idle: a three-word request took 69 s and timed out, so every reply fell back to the Windows voice.

Every 90 s, and at once after the machine wakes (a gap of more than 3 min between checks), this:
  - asks Kokoro for one word (12 s limit); after 2 failures in a row it restarts the container (one restart per 4 min,
    shared with the speak hook through %TEMP%\\claude-kokoro.restarting);
  - checks Whisper (docker container talkover-whisper) answers on port 2022, and starts or restarts it the same way;
  - starts any of voice_inbox.py, voice_panel.py (and stop_word_listener.py if enabled) that is not running (their named mutexes
    tell), hidden, never as a child of a session.
The one-word request every 90 s also keeps Kokoro from going cold. Everything it does goes to logs/voice_watchdog.log.
Single instance (named mutex Talkover_voice_watchdog). Started by start.ps1 (and at logon by the optional Startup shortcut), hidden."""
import ctypes, json, logging, os, subprocess, sys, time, urllib.request
from pathlib import Path

from voice_config import MUTEX_PREFIX, CODE_DIR, KOKORO_CONTAINER, LOG_DIR, STT_HEALTH, TTS_URL, WHISPER_CONTAINER, pythonw

LOG = LOG_DIR / "voice_watchdog.log"
PYW = pythonw()
KOKORO = TTS_URL
WHISPER = STT_HEALTH
LOCK = Path(os.getenv("TEMP", "")) / "claude-kokoro.restarting"
EVERY = 90
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
PROGRAMS = {"voice_inbox": "voice_inbox.py", "voice_panel": "voice_panel.py"}
if os.getenv("TALKOVER_STOP_WORD", "0") == "1":   # optional, VoiceMode MCP only
    PROGRAMS["stop_word_listener"] = "stop_word_listener.py"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateMutexW.restype = ctypes.c_void_p


def mutex(name):
    h = k32.CreateMutexW(None, False, "Local\\" + MUTEX_PREFIX + name)
    taken = ctypes.get_last_error() == 183
    return h, taken


def kokoro_ok():
    body = json.dumps({"model": "kokoro", "voice": "af_heart", "input": "ok", "response_format": "wav"}).encode()
    req = urllib.request.Request(KOKORO, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status == 200 and len(r.read()) > 1000
    except Exception:
        return False


def whisper_ok():
    try:
        with urllib.request.urlopen(WHISPER, timeout=8) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500   # answering at all is enough
    except Exception:
        return False


def docker(*args, timeout=120):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, creationflags=NOWIN)
    except Exception as e:
        logging.warning("docker %s failed: %s", " ".join(args), e)
        return None


def restart(container, shared_lock=False):
    if shared_lock:
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 240:
            logging.info("%s: a restart is already under way", container)
            return
        LOCK.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
    r = docker("ps", "-a", "--filter", "name=^" + container + "$", "--format", "{{.Status}}")
    status = (r.stdout.strip() if r else "") or "unknown"
    logging.info("%s (%s): restarting", container, status)
    docker("start" if status.startswith("Exited") or status.startswith("Created") else "restart", container)


def ensure_programs():
    for name, script in PROGRAMS.items():
        h, taken = mutex(name)
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
        if not taken:
            logging.info("%s was not running: starting it", script)
            subprocess.Popen([PYW, str(CODE_DIR / script)], cwd=str(CODE_DIR), creationflags=NOWIN | DETACHED, close_fds=True)


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(LOG), level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    me, taken = mutex("voice_watchdog")
    if taken:
        sys.exit(0)
    logging.info("watchdog started")
    fails, last = 0, time.time()
    while True:
        now = time.time()
        if now - last > 180:
            logging.info("woke after %.0f s away: checking everything now", now - last)
            time.sleep(5)   # let the network and docker come back first
        last = now
        if kokoro_ok():
            if fails:
                logging.info("kokoro answering again")
            fails = 0
        else:
            fails += 1
            logging.warning("kokoro did not answer (%d in a row)", fails)
            if fails >= 2:
                restart(KOKORO_CONTAINER, shared_lock=True)
                fails = 0
        if not whisper_ok():
            time.sleep(5)
            if not whisper_ok():
                restart(WHISPER_CONTAINER)
        ensure_programs()
        # sleep in short steps so a wake from sleep is noticed within seconds
        until = time.time() + EVERY
        while time.time() < until:
            t0 = time.time()
            time.sleep(5)
            if time.time() - t0 > 60:
                break


if __name__ == "__main__":
    main()
