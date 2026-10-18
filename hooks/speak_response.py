#!/usr/bin/env python3
"""Claude Code voice hook for macOS and Linux: speaks assistant text through local Kokoro (no fallback voice).

A line-by-line port of hooks/speak-response.ps1, which Windows keeps using. Same command line, same control files,
same formats, so voice_inbox.py, voice_panel.py and speak_nav.py work with either:

    speak_response.py [-Mode stop|transcript|replay|held] [-Nav next|back|again] [-Session <first 8 of session_id>]

  Stop hook:        reads last_assistant_message from the stdin JSON and speaks it.
  PreToolUse hook:  -Mode transcript. Reads transcript_path from the stdin JSON, finds assistant text written since the
                    last user prompt that has not been spoken yet, speaks it.
  -Mode replay      started by speak_nav.py for "back" / "again" with nothing reading: plays the last reply read.
  -Mode held        started by voice_inbox.py when the window shows a chat with a reply held for it.

Exactly ONE speaker at a time: a lock file (fcntl.flock on POSIX, msvcrt.locking on Windows so the tests run there
too) serializes speakers; after taking it, each one re-checks the spoken list so duplicates (Stop hook + several
PreToolUse hooks) exit silently. Control files, all in the temp folder (platform_util.TEMP_DIR):
  claude-speak.pid       current speaker's pid (kill it to stop that utterance; SIGTERM cleans up, SIGKILL just stops)
  claude-speak.pause     while present, playback pauses; on removal the sentence replays
  claude-speak.cancel    touched by the stop script / voice_inbox.py; waiting speakers newer than it exit
  claude-speak.spoken    hashes of texts already spoken (dedupe)
  claude-speak.progress  text of the chunk being spoken right now (overwritten per chunk, removed at the end)
  claude-speak.playing   "<start_ms> <end_ms>" (Unix epoch ms): written just before play with the expected end,
                         rewritten at stop with the real stop
  claude-speak.wav       each chunk's wav, written just before it plays (the panel follows its loudness)
  claude-speak.nav       "<seq> <next|back|again>", written by src/speak_nav.py
  claude-speak.history/  one file per reply read (<ticks>_<first 8 of session_id>.txt, cleaned text; last 50 kept)
  claude-speak.hold      fresh (under 3 s) = the inbox holds the reading for the user's words
  claude-speak.newest / claude-speak.talkedover   newest reply wins after the user talks over a reading
  claude-speak.muted / claude-voice.muted         silent
  claude-user.speaking   stamped by voice_inbox.py while the user talks; 3 s of quiet before a sentence starts
  claude-voice.target / .pin / .shown, claude-speak.held/   which chat's replies are read (see the .ps1)
Test mode (CLAUDE_SPEAK_TEST=1, with the temp folder pointed at a sandbox): its own lock names, its log in the temp
folder, and a silent player that only keeps the time, so a test never plays audio and never imports sounddevice.
"""
import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import voice_config  # noqa: E402  (loads config.env into os.environ)
from platform_util import IS_WINDOWS, TEMP_DIR  # noqa: E402

TEST_MODE = os.getenv("CLAUDE_SPEAK_TEST") == "1"
LOCK_SUFFIX = "-test" if TEST_MODE else ""

KOKORO_URL = voice_config.TTS_URL
KOKORO_CONTAINER = voice_config.KOKORO_CONTAINER
ENGINES = (getattr(voice_config, "ENGINES", None) or os.getenv("TALKOVER_ENGINES") or "docker").strip().lower()
VOICE = os.getenv("TALKOVER_VOICE") or "af_heart"
try:
    SPEED = float(os.getenv("TALKOVER_SPEED") or "1.05")
except ValueError:
    SPEED = 1.05
MAX_CHARS = 4000
MAX_PAUSE_S = 10
MAX_WAIT_S = 420     # a reading held for the user's words can keep the lock up to MAX_HOLD_S
MAX_HOLD_S = 300
QUIET_S = 3.0        # the user's silence needed before a sentence starts or resumes
HIST_KEEP = 50
TICK_S = 0.02        # playback poll: pause, cancel, nav and kill react within about 20 ms

LOG_FILE = TEMP_DIR / "speak.log" if TEST_MODE else voice_config.LOG_DIR / "speak.log"

PID_FILE = TEMP_DIR / "claude-speak.pid"
PAUSE_FILE = TEMP_DIR / "claude-speak.pause"
CANCEL_FILE = TEMP_DIR / "claude-speak.cancel"
SPOKEN_FILE = TEMP_DIR / "claude-speak.spoken"
PROGRESS_FILE = TEMP_DIR / "claude-speak.progress"
PLAYING_FILE = TEMP_DIR / "claude-speak.playing"
WAV_FILE = TEMP_DIR / "claude-speak.wav"
USER_SPEAKING_FILE = TEMP_DIR / "claude-user.speaking"
HOLD_FILE = TEMP_DIR / "claude-speak.hold"
TARGET_FILE = TEMP_DIR / "claude-voice.target"
NEWEST_FILE = TEMP_DIR / "claude-speak.newest"
TALKED_FILE = TEMP_DIR / "claude-speak.talkedover"
NAV_FILE = TEMP_DIR / "claude-speak.nav"
HIST_DIR = TEMP_DIR / "claude-speak.history"
PIN_FILE = TEMP_DIR / "claude-voice.pin"
SHOWN_FILE = TEMP_DIR / "claude-voice.shown"
HELD_DIR = TEMP_DIR / "claude-speak.held"
RESTART_LOCK = TEMP_DIR / "claude-kokoro.restarting"
SPEAK_LOCK = TEMP_DIR / ("ClaudeSpeakHook%s.lock" % LOCK_SUFFIX)
SCAN_LOCK = TEMP_DIR / ("ClaudeSpeakScan%s.lock" % LOCK_SUFFIX)

MY_PID = os.getpid()
STARTED_AT = time.time()
# .NET ticks (100 ns since 0001-01-01), the format the PowerShell hook writes to claude-speak.newest and history names
_EPOCH_TICKS = 621355968000000000


def net_ticks() -> int:
    return time.time_ns() // 100 + _EPOCH_TICKS


MY_TICKS = net_ticks()


class KokoroDown(Exception):
    """Kokoro did not answer the one-word check (and a restart, if allowed, did not bring it back)."""


# ---------------------------------------------------------------- small helpers

def log(msg: str) -> None:
    """One line in the .ps1 format ("yyyy-MM-dd HH:mm:ss.fff [pid] msg"); retried because several copies write at once."""
    now = datetime.now()
    line = "%s.%03d [%d] %s\n" % (now.strftime("%Y-%m-%d %H:%M:%S"), now.microsecond // 1000, MY_PID, msg)
    for _ in range(5):
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
            return
        except OSError:
            time.sleep(0.015)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def mtime(p: Path):
    """Modification time in seconds, or None when the file is missing."""
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def age(p: Path):
    m = mtime(p)
    return None if m is None else time.time() - m


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""


def write_text(p: Path, s: str) -> None:
    try:
        p.write_text(s, encoding="utf-8")
    except OSError:
        pass


def remove(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def sid8(s) -> str:
    s = "" if s is None else str(s)
    return s[:8]


class FileLock:
    """A cross-process lock on a file, released by the OS when the holder dies (the abandoned-mutex case)."""

    def __init__(self, path: Path):
        self.fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o666)
        self.held = False

    def try_acquire(self) -> bool:
        try:
            if IS_WINDOWS:
                import msvcrt
                os.lseek(self.fd, 0, 0)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.held = True
        return True

    def acquire(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while True:
            if self.try_acquire():
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)

    def release(self) -> None:
        if self.held:
            try:
                if IS_WINDOWS:
                    import msvcrt
                    os.lseek(self.fd, 0, 0)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            self.held = False
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = -1


# ---------------------------------------------------------------- dedupe

def get_hash(s: str) -> str:
    """First 16 hex digits (upper case) of the SHA-1 of the text, whitespace collapsed and lower-cased (as the .ps1)."""
    norm = re.sub(r"\s+", " ", s or "").strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest().upper()[:16]


def spoken_list():
    return [ln.strip() for ln in read_text(SPOKEN_FILE).splitlines() if ln.strip()]


def test_spoken(h: str) -> bool:
    return h in spoken_list()


def mark_spoken(h: str) -> None:
    try:
        with open(SPOKEN_FILE, "a", encoding="utf-8") as f:
            f.write(h + "\n")
        allh = spoken_list()
        if len(allh) > 400:
            write_text(SPOKEN_FILE, "\n".join(allh[-200:]) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- text

def clean_text(text: str) -> str:
    """Markdown to speakable text, exactly the .ps1 Clean-Text."""
    t = text
    t = re.sub(r"(?s)```.*?```", " code block omitted. ", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "link", t)
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*[-*+]\s+", "", t)
    t = re.sub(r"(?m)^\s*\d+\.\s+", "", t)
    t = re.sub(r"(?m)^\|.*\|\s*$", "", t)
    t = re.sub(r"\*\*|__|~~", "", t)
    t = re.sub(r"(?<!\w)[*_](?=\w)|(?<=\w)[*_](?!\w)", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS] + " ... the rest is in the transcript."
    return t


def split_chunks(t: str):
    """Sentences grouped into chunks of up to 140 chars. The first chunk is the first sentence only, so Kokoro returns
    first audio sooner; a long first sentence is cut at the first , ; or : after 30 chars (if the cut is <= 80)."""
    sentences = [s for s in re.split(r"(?<=[.!?:])\s+", t) if s]
    chunks = []
    if sentences:
        first, rest = sentences[0], sentences[1:]
        if len(first) > 80:
            m = re.search(r"[,;:]\s", first[30:])
            if m and 30 + m.start() + 1 <= 80:
                cut = 30 + m.start() + 1
                rest = [first[cut:].strip()] + rest
                first = first[:cut]
        chunks.append(first)
        sentences = rest
    buf = ""
    for s in sentences:
        if buf and len(buf) + len(s) > 140:
            chunks.append(buf)
            buf = ""
        buf = "%s %s" % (buf, s) if buf else s
    if buf:
        chunks.append(buf)
    return chunks


# ---------------------------------------------------------------- WAV

def repair_wav(b: bytes):
    """Fix the RIFF and data sizes (Kokoro streams WAVs with placeholder sizes), like Repair-WavHeader.
    Returns (bytes, data_offset, data_len, sample_rate, channels, bits)."""
    data_off, data_len = 44, max(0, len(b) - 44)
    rate, channels, bits = 24000, 1, 16
    if len(b) < 44:
        return b, data_off, data_len, rate, channels, bits
    b = bytearray(b)
    b[4:8] = (len(b) - 8).to_bytes(4, "little")
    i = 12
    while i < len(b) - 8:
        cid = bytes(b[i:i + 4])
        sz = int.from_bytes(b[i + 4:i + 8], "little")
        if cid == b"fmt " and i + 24 <= len(b):
            channels = int.from_bytes(b[i + 10:i + 12], "little") or 1
            rate = int.from_bytes(b[i + 12:i + 16], "little") or 24000
            bits = int.from_bytes(b[i + 22:i + 24], "little") or 16
        if cid == b"data":
            data_off, data_len = i + 8, len(b) - (i + 8)
            b[i + 4:i + 8] = data_len.to_bytes(4, "little")
            break
        i += 8 + sz + (sz & 1)
    return bytes(b), data_off, data_len, rate, channels, bits


# ---------------------------------------------------------------- players

class SilentPlayer:
    """Test mode stand-in: play_chunk keeps the time itself, so nothing else changes."""

    def play(self, wav):
        pass

    def stop(self):
        pass

    def close(self):
        pass


class SoundPlayer:
    """sounddevice output in blocks of about 20 ms. One stream stays open for the process (no reopen latency per
    sentence); stop() empties the buffer, so silence follows within one block. Killing the process ends the stream."""

    def __init__(self):
        import numpy  # noqa: F401  (lazy: only the real player needs them)
        import sounddevice
        self.np = numpy
        self.sd = sounddevice
        self.stream = None
        self.fmt = None
        self.buf = None
        self.pos = 0
        self.lock = threading.Lock()

    def _callback(self, outdata, frames, _time, _status):
        with self.lock:
            n = 0
            if self.buf is not None:
                part = self.buf[self.pos:self.pos + frames]
                n = len(part)
                outdata[:n] = part
                self.pos += n
                if self.pos >= len(self.buf):
                    self.buf = None
        outdata[n:] = 0

    def play(self, wav):
        b, off, length, rate, ch, bits = wav
        np = self.np
        if bits != 16:
            log("unexpected wav: %d bits; playing as 16" % bits)
        pcm = np.frombuffer(b[off:off + length - (length % (2 * ch))], dtype="<i2").reshape(-1, ch)
        if self.stream is None or self.fmt != (rate, ch):
            self.close()
            self.stream = self.sd.OutputStream(samplerate=rate, channels=ch, dtype="int16", blocksize=max(1, rate // 50),
                                               latency="low", callback=self._callback)
            self.fmt = (rate, ch)
            self.stream.start()
        with self.lock:
            self.buf = pcm
            self.pos = 0

    def stop(self):
        with self.lock:
            self.buf = None

    def close(self):
        if self.stream is not None:
            try:
                self.stream.abort()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


# ---------------------------------------------------------------- Kokoro

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # local engine: never through a proxy


def _post_kokoro(body: dict, timeout: float) -> bytes:
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(KOKORO_URL, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("kokoro http %d" % e.code)


def request_kokoro(pool: ThreadPoolExecutor, chunk: str):
    """Start the request for one chunk (a future), so the next one is fetched while this one plays."""
    body = {"model": "kokoro", "voice": VOICE, "input": chunk, "response_format": "wav", "speed": SPEED}
    return pool.submit(_post_kokoro, body, 240)


def test_kokoro() -> bool:
    """Kokoro answers a one-word request within 12 s."""
    try:
        _post_kokoro({"model": "kokoro", "voice": VOICE, "input": "ok", "response_format": "wav"}, 12)
        return True
    except Exception:
        return False


def ensure_kokoro() -> bool:
    """Kokoro answers, or (docker engines) restart its container, one restart per 4 min shared with every hook copy
    and the watchdog through claude-kokoro.restarting, and wait up to 150 s for it. Native engines: the watchdog
    restarts them; the hook only reports. No fallback voice, by design."""
    if test_kokoro():
        return True
    if ENGINES == "native":
        log("kokoro not answering: the native engine is down (TALKOVER_ENGINES=native); the watchdog restarts it")
        return False
    log("kokoro not answering: restarting it")
    if TEST_MODE:
        log("test mode: no docker restart")
        return False
    a = age(RESTART_LOCK)
    if a is None or a > 240:
        write_text(RESTART_LOCK, datetime.now().astimezone().isoformat())
        try:
            subprocess.run(["docker", "restart", KOKORO_CONTAINER], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120)
        except Exception as e:
            log("docker restart failed: %s" % e)
    for _ in range(75):
        time.sleep(2)
        if test_kokoro():
            log("kokoro back")
            return True
    return False


# ---------------------------------------------------------------- the speaker

class Speaker:
    """One run of the hook: the state the .ps1 keeps in $script: variables."""

    def __init__(self, mode: str, nav: str, session: str):
        self.mode = mode
        self.nav_arg = nav
        self.session = session
        self.payload = {}
        self.item_sid = ""
        self.cur_text = None
        self.queue = []
        self.texts = []
        self.nav = ""
        self.nav_seen = 0
        self.nav_mtime = 0
        self.stop_all = False
        self.pin_stop = False
        self.used_kokoro = False
        self.lock = None
        self.player = None

    # ---- gating files

    def chat_shown(self) -> bool:
        """claude-voice.target says "chat" (a claude.ai chat, which has its own voice) and is fresh."""
        a = age(TARGET_FILE)
        if a is None or a > 10:
            return False
        return read_text(TARGET_FILE).strip() == "chat"

    def superseded(self) -> bool:
        """The user talked over a reading after this speaker started, and a newer speaker has announced itself."""
        t = mtime(TALKED_FILE)
        if t is None or not NEWEST_FILE.exists() or t <= STARTED_AT:
            return False
        n = read_text(NEWEST_FILE).split()
        try:
            return int(n[0]) > MY_TICKS and n[1] != str(MY_PID)
        except (IndexError, ValueError):
            return False

    def words_hold(self) -> bool:
        a = age(HOLD_FILE)
        return a is not None and a < 3

    def user_talking(self, within: float) -> bool:
        a = age(USER_SPEAKING_FILE)
        return a is not None and a < within

    def cancelled(self) -> bool:
        t = mtime(CANCEL_FILE)
        return t is not None and t > STARTED_AT

    # ---- which chat's replies are read

    def pin8(self) -> str:
        p = read_text(PIN_FILE).split()
        return p[0][:8] if p and len(p[0]) >= 8 else ""

    def shown8(self) -> str:
        """'' = not known (no fresh file: read as before); '-' = no Claude Code chat on screen; else its first 8."""
        a = age(SHOWN_FILE)
        if a is None or a > 10:
            return ""
        p = read_text(SHOWN_FILE).split()
        return p[0][:8] if p and len(p[0]) >= 8 else "-"

    def target8(self) -> str:
        return self.pin8() or self.shown8()

    def not_pinned(self) -> bool:
        p = self.pin8()
        return bool(p) and self.item_sid != p

    def off_target(self) -> bool:
        t = self.target8()
        return bool(t) and self.item_sid != t

    def off_why(self) -> str:
        return "voice is pinned to another chat" if self.pin8() else "the window shows another chat"

    # ---- held replies (auto: a reply plays when you go to its chat)

    def read_held(self, s8: str):
        try:
            return json.loads((HELD_DIR / ("%s.json" % s8)).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def held_texts(obj):
        t = (obj or {}).get("texts") or []
        return [t] if isinstance(t, str) else [x for x in t if x]

    def save_held(self, items) -> None:
        """Hold this chat's newest unheard reply for when the window shows it. What it replaces is marked spoken."""
        items = [x for x in (items or []) if x]
        if self.pin8() or self.mode == "replay" or not items:
            return
        try:
            HELD_DIR.mkdir(parents=True, exist_ok=True)
            new = [get_hash(x) for x in items]
            old = self.read_held(self.item_sid)
            old_h = [get_hash(x) for x in self.held_texts(old)] if old else []
            for h in old_h:
                if h not in new and not test_spoken(h):
                    mark_spoken(h)
            if ",".join(old_h) == ",".join(new):
                return
            obj = {"session_id": str(self.payload.get("session_id") or ""),
                   "agent_id": str(self.payload.get("agent_id") or ""), "texts": items}
            tmp = HELD_DIR / ("%s.tmp" % self.item_sid)
            tmp.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, HELD_DIR / ("%s.json" % self.item_sid))
            log("held for when the window shows chat %s: %d text(s)" % (self.item_sid, len(items)))
        except OSError as e:
            log("hold failed: %s" % e)

    def clear_held(self, keep) -> None:
        """A newer reply of this chat is being read: the held one is not wanted any more."""
        old = self.read_held(self.item_sid)
        if not old:
            return
        k = [get_hash(x) for x in (keep or []) if x]
        for t in self.held_texts(old):
            h = get_hash(t)
            if h not in k and not test_spoken(h):
                mark_spoken(h)
        remove(HELD_DIR / ("%s.json" % self.item_sid))

    def hold_rest(self) -> None:
        """The reading's chat left the screen: hold what it had left, from the start of the reply being read."""
        r = []
        if self.cur_text:
            r.append(self.cur_text)
        r.extend(q["text"] for q in self.queue if q.get("text"))
        self.save_held(r)

    # ---- next / back / again

    def read_nav_seq(self):
        try:
            p = NAV_FILE.read_text(encoding="utf-8-sig").split()
            return int(p[0]), (p[1] if len(p) > 1 else "").lower()
        except (OSError, ValueError, IndexError):
            return 0, ""

    @staticmethod
    def nav_mtime_ns() -> int:
        try:
            return NAV_FILE.stat().st_mtime_ns
        except OSError:
            return 0

    def test_nav(self) -> bool:
        """A command newer than the last one seen: sets self.nav ('next' | 'back' | 'again') and returns True."""
        if self.nav:
            return True
        mt = self.nav_mtime_ns()
        if mt == self.nav_mtime:
            return False
        self.nav_mtime = mt
        seq, cmd = self.read_nav_seq()
        if seq <= self.nav_seen:
            return False
        self.nav_seen = seq
        if cmd in ("next", "back", "again"):
            self.nav = cmd
            log("nav: %s" % cmd)
            return True
        return False

    # ---- history

    def add_history(self, clean: str):
        try:
            HIST_DIR.mkdir(parents=True, exist_ok=True)
            name = "%020d_%s.txt" % (net_ticks(), sid8(self.payload.get("session_id")))
            (HIST_DIR / name).write_text(clean, encoding="utf-8")
            names = sorted(p.name for p in HIST_DIR.glob("*.txt"))
            for old in names[:max(0, len(names) - HIST_KEEP)]:
                remove(HIST_DIR / old)
            return name
        except OSError:
            return None

    @staticmethod
    def hist_sid8(name: str) -> str:
        m = re.search(r"_([0-9a-fA-F-]{1,8})\.txt$", name or "")
        return m.group(1) if m else ""

    def history_names(self):
        """History files in order, only the pinned chat's (or in auto the chat on screen) when one is known."""
        try:
            names = sorted(p.name for p in HIST_DIR.glob("*.txt"))
        except OSError:
            return []
        p = self.target8()
        if p:
            names = [n for n in names if self.hist_sid8(n) == p]
        return names

    def history_before(self, name: str):
        prev = None
        for n in self.history_names():
            if n >= name:
                break
            prev = n
        return prev

    @staticmethod
    def read_history(name: str) -> str:
        return read_text(HIST_DIR / name)

    # ---- collecting texts

    def read_payload(self) -> bool:
        """Fill self.payload for the mode. False = nothing to do (exit silently)."""
        if self.mode == "replay":
            self.payload = {"session_id": "replay", "agent_id": ""}
            return True
        if self.mode == "held":
            src = HELD_DIR / ("%s.json" % self.session)
            claim = HELD_DIR / ("%s.%d.play" % (self.session, MY_PID))
            try:
                os.rename(src, claim)    # nothing held, or another copy took it
            except OSError:
                return False
            try:
                self.payload = json.loads(claim.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                self.payload = None
            remove(claim)
            if not isinstance(self.payload, dict):
                return False
            self.item_sid = sid8(self.payload.get("session_id"))
            return True
        try:
            raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        except (OSError, ValueError):
            return False
        if not raw.strip():
            return False
        try:
            self.payload = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(self.payload, dict):
            return False
        self.item_sid = sid8(self.payload.get("session_id"))
        if self.not_pinned():
            if self.mode == "stop":
                log("not read: voice is pinned to chat %s, this reply is from %s" % (self.pin8(), self.item_sid))
            return False
        return True

    def transcript_texts(self):
        """Assistant text written since the last user prompt, not spoken yet. One scan at a time: a new copy starts on
        every tool call; if one is already scanning it will find the same text, so this one leaves."""
        tp = self.payload.get("transcript_path")
        if not tp or not os.path.isfile(tp):
            return None
        scan = FileLock(SCAN_LOCK)
        if not scan.try_acquire():
            scan.release()
            return None
        try:
            lines = tail_lines(tp, 4 * 1024 * 1024, 400)
            pending = []
            for ln in lines:
                # tool output lines can be megabytes and never hold anything to speak
                if len(ln) > 200000 or not re.search(r'"type":\s*"(assistant|user)"', ln):
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                msg = e.get("message") if isinstance(e.get("message"), dict) else {}
                c = msg.get("content")
                if e.get("type") == "user":
                    if isinstance(c, str) or (isinstance(c, list) and any(
                            isinstance(b, dict) and b.get("type") == "text" for b in c)):
                        pending = []
                elif e.get("type") == "assistant" and isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) \
                                and b["text"].strip():
                            pending.append(b["text"])
            return [t for t in pending if not test_spoken(get_hash(t))]
        finally:
            scan.release()

    def collect(self):
        """Candidate texts (cheap pre-check; the real check happens under the lock). None = exit silently."""
        if self.mode == "transcript":
            return self.transcript_texts()
        if self.mode == "held":
            return self.held_texts(self.payload)     # held texts are unheard by definition: no spoken check
        if self.mode == "replay":
            return []
        t = self.payload.get("last_assistant_message")
        if isinstance(t, str) and t and not test_spoken(get_hash(t)):
            return [t]
        return []

    # ---- waiting and playing

    def wait_user_quiet(self) -> bool:
        """Never start a sentence over the user, nor while the inbox holds the pause or the user's words hold the
        reading. Wait for quiet; give up on cancel, nav, supersede or after 120 s (longer while the words hold)."""
        start = time.monotonic()
        logged = False
        while self.user_talking(QUIET_S) or PAUSE_FILE.exists() or self.words_hold():
            el = time.monotonic() - start
            if not logged:
                why = ("held for the user's words" if self.words_hold() else
                       "paused by the inbox" if PAUSE_FILE.exists() else "the user is talking")
                log("waiting: " + why)
                logged = True
            if self.cancelled():
                return False
            if self.test_nav():
                return False
            if self.superseded():
                log("a newer reply came after the user talked: dropping the rest")
                return False
            if el > 120 and not (self.words_hold() and el < MAX_HOLD_S):
                log("gave up waiting after %d s" % int(el))
                return False
            time.sleep(0.1)
        if logged:
            log("quiet after %.1f s" % (time.monotonic() - start))
        if self.off_target():
            log("%s: stopping" % self.off_why())
            self.hold_rest()
            self.pin_stop = True
            return False
        if self.chat_shown():
            log("the window shows a claude.ai chat: stopping")
            return False
        if self.superseded():
            log("a newer reply came after the user talked: dropping the rest")
            return False
        return not self.cancelled()

    def play_chunk(self, wav, chunk_text: str, label: str) -> None:
        """Play one chunk; pause and replay it when the user talks or the pause file appears."""
        b, _off, data_len, rate, ch, bits = wav
        seconds = data_len / float(max(1, rate * ch * max(1, bits // 8)))
        while True:
            if self.test_nav():
                return    # next / back: this reply ends here; the main loop picks what plays next
            if not self.wait_user_quiet():
                if self.nav:
                    return
                if not self.pin_stop:
                    log("yielded: the user spoke, stopping")
                self.stop_all = True
                return
            # audio for the panel orb; written before progress, which the panel times playback from
            try:
                WAV_FILE.write_bytes(b)
            except OSError:
                pass
            write_text(PROGRESS_FILE, chunk_text)
            t0 = now_ms()
            write_text(PLAYING_FILE, "%d %d" % (t0, t0 + int((seconds + 0.15) * 1000)))
            self.player.play(wav)
            log("play %s (%.1f s)" % (label, seconds))
            start = time.monotonic()
            paused = chat = superd = off = False
            why = ""
            tick = 0
            while time.monotonic() - start < seconds + 0.15:
                if PAUSE_FILE.exists():
                    paused, why = True, "pause file"
                    break
                if self.test_nav():
                    break
                # the user started talking mid-sentence: stop now, wait for quiet, replay the sentence
                if self.user_talking(0.3):
                    paused, why = True, "the user is talking"
                    break
                tick += 1
                if tick % 25 == 0 and self.off_target():      # about every 0.5 s
                    off = True
                    break
                if tick % 25 == 0 and self.chat_shown():
                    chat = True
                    break
                if tick % 5 == 0 and self.superseded():       # about every 0.1 s
                    superd = True
                    break
                time.sleep(TICK_S)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self.player.stop()
            write_text(PLAYING_FILE, "%d %d" % (t0, now_ms()))
            if self.nav:
                return
            if chat or superd or off:
                log("%s: stopping" % self.off_why() if off else
                    "the window shows a claude.ai chat: stopping" if chat else
                    "a newer reply came after the user talked: dropping the rest")
                if off:
                    self.hold_rest()
                self.stop_all = True
                return
            if not paused:
                return
            log("paused after %d ms (%s)" % (elapsed_ms, why))
            if not self.wait_paused():
                return
            log("resumed after %.1f s (replaying sentence)" % (time.monotonic() - self._pause_start))

    def wait_paused(self) -> bool:
        """While paused: MAX_PAUSE_S bounds a pause from sound; the user's words (the hold file) keep it up to
        MAX_HOLD_S, and then the user's 3 s of quiet are still waited out. True = replay the sentence."""
        self._pause_start = start = time.monotonic()
        held_logged = False
        while True:
            el = time.monotonic() - start
            a = (PAUSE_FILE.exists() or self.user_talking(QUIET_S)) and el < MAX_PAUSE_S
            b = (self.words_hold() or (held_logged and self.user_talking(QUIET_S))) and el < MAX_HOLD_S
            if not (a or b):
                break
            if not held_logged and self.words_hold():
                log("held for the user's words")
                held_logged = True
            if self.cancelled():
                remove(PAUSE_FILE)
                self.stop_all = True
                return False
            if self.superseded():
                log("a newer reply came after the user talked: dropping the rest")
                self.stop_all = True
                return False
            if self.test_nav():
                return False    # the pause is the inbox's; it lets go of it itself
            time.sleep(0.1)
        remove(PAUSE_FILE)
        if self.superseded():
            log("a newer reply came after the user talked: dropping the rest")
            self.stop_all = True
            return False
        if self.off_target():
            log("%s: stopping" % self.off_why())
            self.hold_rest()
            self.stop_all = True
            return False
        if self.chat_shown():
            log("the window shows a claude.ai chat: stopping")
            self.stop_all = True
            return False
        return True

    # ---- the run

    def take_lock(self) -> bool:
        """Wait for the one-speaker lock, leaving on cancel, supersede or the chat leaving the screen."""
        self.lock = FileLock(SPEAK_LOCK)
        deadline = time.monotonic() + MAX_WAIT_S
        have = False
        while not have and time.monotonic() < deadline:
            if self.cancelled():
                log("cancelled while waiting")
                return False
            if self.superseded():
                log("a newer reply came after the user talked: dropped while waiting")
                return False
            if self.off_target():
                log("%s: not read now (waiting)" % self.off_why())
                self.save_held(self.texts)
                return False
            have = self.lock.acquire(0.5)
        if not have:
            log("gave up waiting for lock")
            return False
        # The user may have spoken in the instant between the last check and getting the lock: check again now,
        # or old text is read over the user.
        if self.cancelled():
            log("cancelled at lock")
            return False
        if self.superseded():
            log("a newer reply came after the user talked: dropped at lock")
            return False
        if self.off_target():
            log("%s: not read now (at lock)" % self.off_why())
            self.save_held(self.texts)
            return False
        return True

    def run(self) -> None:
        if (TEMP_DIR / "claude-speak.muted").exists() or (TEMP_DIR / "claude-voice.muted").exists():
            return    # "stop listening": silent until "start listening"
        # The window shows a claude.ai chat: voice is for Claude Code only. In auto a reply is held instead (below).
        if self.chat_shown() and (self.pin8() or self.mode == "replay"):
            return
        if not self.read_payload():
            return
        texts = self.collect()
        if texts is None:
            return
        self.texts = texts
        replay_start = None
        if self.mode == "replay":
            names = self.history_names()
            if not names:
                log("replay (%s): nothing has been read yet" % self.nav_arg)
                return
            replay_start = names[-1]
            self.item_sid = self.hist_sid8(replay_start)
        else:
            if not self.texts:
                return
            if not self.pin8():
                s8 = self.shown8()
                if s8 and s8 != self.item_sid:
                    self.save_held(self.texts)    # auto: not the chat on screen, held
                    return
                if s8 and self.mode != "held":
                    self.clear_held(self.texts)   # on screen: its newer reply wins
            elif self.mode == "held" and self.off_target():
                return
            write_text(NEWEST_FILE, "%d %d" % (MY_TICKS, MY_PID))

        if not self.take_lock():
            self.lock.release()
            return
        # re-check under the lock: another speaker may have said it while this one waited
        if self.mode != "held":
            self.texts = [t for t in self.texts if not test_spoken(get_hash(t))]
        if not self.texts and not replay_start:
            self.lock.release()
            return
        try:
            # Next / Back commands given before this speaker held the lock were for the one before it.
            self.nav_seen = self.read_nav_seq()[0]
            self.nav_mtime = self.nav_mtime_ns()
            write_text(PID_FILE, str(MY_PID))
            remove(PAUSE_FILE)
            self.speak(replay_start)
        finally:
            self.cleanup()

    def speak(self, replay_start) -> None:
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            if not ensure_kokoro():
                raise KokoroDown("kokoro still down" + ("" if ENGINES == "native" else " after a restart"))
            try:
                self.player = SilentPlayer() if TEST_MODE else SoundPlayer()
            except Exception as e:    # no PortAudio / no output device: nothing can be heard
                log("not read: no audio output (%s)" % e)
                return
            # What plays, in order: new replies (text) and replies already read (hist = their history file).
            if replay_start:
                self.queue.append({"hist": replay_start})
            self.queue.extend({"text": t} for t in self.texts)
            while self.queue:
                item = self.queue.pop(0)
                self.item_sid = self.hist_sid8(item["hist"]) if item.get("hist") else sid8(self.payload.get("session_id"))
                self.cur_text = item.get("text")
                if self.off_target():
                    log("%s: not read now" % self.off_why())
                    self.hold_rest()
                    break
                if item.get("hist"):
                    t = self.read_history(item["hist"])
                    if not t:
                        continue
                    hist = item["hist"]
                else:
                    h = get_hash(item["text"])
                    if self.mode != "held" and test_spoken(h):
                        continue
                    mark_spoken(h)
                    t = clean_text(item["text"])
                    if not t:
                        continue
                    hist = self.add_history(t)
                self.speak_one(pool, t, item, hist)
                if self.stop_all:
                    break
                self.after_nav(hist)
        except KokoroDown as e:
            log("error: %s" % e)
            log("not read: %s down; no fallback voice (by design, 2026-10-12)" %
                ("the native engine" if ENGINES == "native" else "kokoro"))
        except Exception as e:
            log("error: %s" % e)
            if not self.used_kokoro and not TEST_MODE:
                log("not read: kokoro down; no fallback voice (by design, 2026-10-12)")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def speak_one(self, pool, t: str, item, hist) -> None:
        """Speak one reply chunk by chunk, fetching the next chunk while this one plays."""
        chunks = split_chunks(t)
        if not chunks:
            return
        log("speak mode=%s chars=%d chunks=%d session=%s agent=%s%s" % (
            self.mode, len(t), len(chunks), sid8(self.payload.get("session_id")),
            str(self.payload.get("agent_id") or ""), (" replay=%s" % hist) if item.get("hist") else ""))
        self.nav = ""
        pending = request_kokoro(pool, chunks[0])
        for i in range(len(chunks)):
            data = pending.result()
            if i + 1 < len(chunks):
                pending = request_kokoro(pool, chunks[i + 1])
            self.used_kokoro = True
            self.play_chunk(repair_wav(data), chunks[i], "%d/%d" % (i + 1, len(chunks)))
            if self.stop_all or self.nav:
                break

    def after_nav(self, hist) -> None:
        """Next / back / again change what comes after the reply they stopped."""
        cmd, self.nav = self.nav, ""
        if cmd == "next":
            log("next: " + ("on to the next reply" if self.queue else
                            "no reply queued here; the next speaker (if any) goes on"))
        elif cmd == "again" and hist:
            self.queue.insert(0, {"hist": hist})
            log("again: this reply from its start")
        elif cmd == "back" and hist:
            self.queue.insert(0, {"hist": hist})
            prev = self.history_before(hist)
            if prev:
                self.queue.insert(0, {"hist": prev})
                log("back: the reply before (%s), then this one" % prev)
            else:
                log("back: nothing before this reply; this one from its start")

    def cleanup(self) -> None:
        """Remove what this speaker published and let the next one have the lock."""
        if self.player is not None:
            self.player.stop()
            self.player.close()
        if read_text(PID_FILE).strip() == str(MY_PID):
            remove(PID_FILE)
        remove(PAUSE_FILE)
        remove(PROGRESS_FILE)
        remove(PLAYING_FILE)
        if self.used_kokoro:
            log("done" + (" (stopped early)" if self.stop_all else ""))
        if self.lock is not None:
            self.lock.release()


def tail_lines(path: str, tail_bytes: int, keep: int):
    """The last `keep` non-empty lines of the file's last `tail_bytes` (the first line after the seek is partial)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - tail_bytes)
        f.seek(start)
        data = f.read()
    lines = re.split(r"\r?\n", data.decode("utf-8", "replace"))
    if start > 0 and lines:
        lines = lines[1:]
    return [ln for ln in lines if ln][-keep:]


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="speak_response.py", description="Claude Code voice hook (reads replies aloud "
                                 "through local Kokoro). Reads the hook JSON from stdin in stop and transcript mode.")
    ap.add_argument("-Mode", "-mode", "--mode", dest="mode", default="stop",
                    help="stop (default) | transcript | replay | held")
    ap.add_argument("-Nav", "-nav", "--nav", dest="nav", default="", help="next | back | again (with -Mode replay)")
    ap.add_argument("-Session", "-session", "--session", dest="session", default="",
                    help="first 8 of session_id (with -Mode held)")
    a = ap.parse_args(argv)
    return (a.mode or "stop").lower(), (a.nav or "").lower(), a.session or ""


def _on_term(_sig, _frame):
    """Killed by the inbox / stop script: leave through the finally blocks so the sound stops and files are cleaned."""
    raise SystemExit(0)


def main(argv=None) -> int:
    mode, nav, session = parse_args(sys.argv[1:] if argv is None else argv)
    if not TEST_MODE:
        try:
            voice_config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _on_term)
            except (OSError, ValueError):
                pass
    try:
        Speaker(mode, nav, session).run()
    except SystemExit:
        pass
    except Exception as e:    # a hook never fails Claude Code
        log("error: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
