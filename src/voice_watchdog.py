"""Keeps Talkover working after idle hours and sleep (2026-10-12).

Voice used to stop working after the machine sat idle for a while. On 2026-10-12 Kokoro (docker container talkover-kokoro)
had hung after hours idle: a three-word request took 69 s and timed out, so every reply fell back to the Windows voice.

Every 90 s, and at once after the machine wakes (a gap of more than 3 min between checks), this:
  - asks Kokoro for one word (12 s limit); after 2 failures in a row it restarts the engine (one restart per 4 min,
    shared with the speak hook through the temp folder's claude-kokoro.restarting);
  - checks Whisper answers on its port (2022 by default), and starts or restarts it the same way;
  - starts any of voice_inbox.py, voice_panel.py (and stop_word_listener.py if enabled) that is not running (their
    single-instance locks tell), hidden, never as a child of a session.
Engines: TALKOVER_ENGINES=docker (default) restarts the Docker containers; TALKOVER_ENGINES=native starts and restarts
engines/native/server.py instead (its pid is kept in the temp folder's talkover-engines.pid), giving it time to load.
The one-word request every 90 s also keeps Kokoro from going cold. Everything it does goes to logs/voice_watchdog.log.
Single instance (Talkover_voice_watchdog). Started by start.ps1 / start.sh (and at logon if set up), hidden."""
import json, logging, os, subprocess, sys, time, urllib.request

from platform_util import IS_WINDOWS, TEMP_DIR, instance_running, single_instance, spawn_detached
from voice_config import (MUTEX_PREFIX, CODE_DIR, ENGINES, KOKORO_CONTAINER, LOG_DIR, REPO_DIR, STT_HEALTH, TTS_URL,
                          WHISPER_CONTAINER, pythonw)

LOG = LOG_DIR / "voice_watchdog.log"
PYW = pythonw()
KOKORO = TTS_URL
WHISPER = STT_HEALTH
LOCK = TEMP_DIR / "claude-kokoro.restarting"
ENGINES_PID = TEMP_DIR / "talkover-engines.pid"
NATIVE_SERVER = REPO_DIR / "engines" / "native" / "server.py"
NATIVE_GRACE_S = 240          # a native engine just started is loading models: no restart before this
EVERY = 90
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
PROGRAMS = {"voice_inbox": "voice_inbox.py", "voice_panel": "voice_panel.py"}
if os.getenv("TALKOVER_STOP_WORD", "0") == "1":   # optional, VoiceMode MCP only
    PROGRAMS["stop_word_listener"] = "stop_word_listener.py"

_native_started = [0.0]


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
    kw = {"creationflags": NOWIN} if IS_WINDOWS else {}
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, **kw)
    except Exception as e:
        logging.warning("docker %s failed: %s", " ".join(args), e)
        return None


def start_native(reason):
    """(Re)start engines/native/server.py: stop the old one (pid file), start a new one hidden."""
    try:
        old = int(ENGINES_PID.read_text().strip())
        os.kill(old, 9)
        logging.info("native engines: stopped pid %d", old)
    except (OSError, ValueError):
        pass
    p = spawn_detached([PYW, str(NATIVE_SERVER)], cwd=REPO_DIR)
    ENGINES_PID.write_text(str(p.pid))
    _native_started[0] = time.time()
    logging.info("native engines: started pid %d (%s)", p.pid, reason)


def restart(container, shared_lock=False):
    if ENGINES == "native":
        if time.time() - _native_started[0] < NATIVE_GRACE_S:
            logging.info("native engines are still starting: not restarting yet")
            return
        if shared_lock:
            LOCK.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
        start_native("%s did not answer" % ("kokoro" if container == KOKORO_CONTAINER else "whisper"))
        return
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
        if not instance_running(name, MUTEX_PREFIX):
            logging.info("%s was not running: starting it", script)
            spawn_detached([PYW, str(CODE_DIR / script)], cwd=CODE_DIR)


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(LOG), level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    me = single_instance("voice_watchdog", MUTEX_PREFIX)
    if me is None:
        sys.exit(0)
    logging.info("watchdog started (engines: %s)", ENGINES)
    if ENGINES == "native" and not (kokoro_ok() and whisper_ok()):
        start_native("not running")
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
