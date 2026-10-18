#!/usr/bin/env python3
"""
Spoken "stop" barge-in for VoiceMode.

Listens to the microphone all the time with a tiny offline keyword spotter (Vosk,
grammar limited to the word "stop"). When it hears "stop" it sends `skip_forward`
to VoiceMode's control channel, which cuts the utterance being spoken and jumps
straight to listening, so you can talk over the assistant. Outside playback the
control port is closed and the command is simply dropped.

Optional: only for the VoiceMode MCP server's converse tool (see extras/voicemode-mcp); the floater's own
stop phrases are handled by voice_inbox.py. Requires, in VoiceMode's ~/.voicemode/voicemode.env:
    VOICEMODE_CONTROL_CHANNEL_ENABLED=true
    VOICEMODE_CONTROL_PORT=42711        # loopback TCP (Windows has no AF_UNIX)

Run with the voicemode venv:
    .venv\\Scripts\\pythonw.exe src\\stop_word_listener.py

Log: ~/.talkover/logs/stop_word.log
"""
import json
import logging
import os
import queue
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

from platform_util import single_instance as _single_instance
from voice_config import MUTEX_PREFIX, LOG_DIR, MODELS_DIR

MODEL_DIR = MODELS_DIR / "vosk-model-small-en-us-0.15"
LOG = LOG_DIR / "stop_word.log"
PORT = int(os.getenv("VOICEMODE_CONTROL_PORT", "42711"))
KEYWORDS = ["stop"]          # grammar; add e.g. "shut up" if wanted
COMMAND = "skip_forward"     # cut + advance to the listen turn ("stop" = cut only)
DEBOUNCE_S = 1.5
MIN_CONF = 0.75              # word confidence from Vosk; "[unk] stop" noise scores low
MIN_DUR, MAX_DUR = 0.15, 0.9 # seconds; a spoken "stop" is short
RATE = 16000
BLOCK = 2000                 # 125 ms


def setup_log():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(LOG, maxBytes=512_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log = logging.getLogger("stopword")
    log.setLevel(logging.INFO)
    log.addHandler(h)
    return log


def send(command):
    line = json.dumps({"command": command}) + "\n"
    with socket.create_connection(("127.0.0.1", PORT), timeout=1.0) as s:
        s.sendall(line.encode("utf-8"))


def single_instance(name: str):
    """A handle if this is the only copy, None if one is already running (named mutex / lock file)."""
    return _single_instance(name, MUTEX_PREFIX)


def main():
    log = setup_log()
    global _instance
    _instance = single_instance("stop_word_listener")   # held for the life of the process
    if _instance is None:
        log.info("another stop-word listener is already running; this copy exits")
        return 0
    SetLogLevel(-1)
    if not MODEL_DIR.is_dir():
        log.error("model missing at %s", MODEL_DIR)
        return 1
    model = Model(str(MODEL_DIR))
    grammar = json.dumps(KEYWORDS + ["[unk]"])
    q: "queue.Queue[bytes]" = queue.Queue()

    def cb(indata, frames, t, status):
        q.put(bytes(indata))

    last_fire = 0.0
    log.info("stop-word listener starting (port %s, keywords %s, min_conf %.2f)", PORT, KEYWORDS, MIN_CONF)
    while True:
        try:
            rec = KaldiRecognizer(model, RATE, grammar)
            rec.SetWords(True)
            with sd.RawInputStream(samplerate=RATE, blocksize=BLOCK, dtype="int16",
                                   channels=1, callback=cb):
                log.info("microphone open")
                while True:
                    data = q.get()
                    if not rec.AcceptWaveform(data):
                        continue            # final results only: partials are too noisy
                    res = json.loads(rec.Result())
                    words = res.get("result") or []
                    text = res.get("text", "")
                    # Strict match: the whole utterance is exactly one keyword,
                    # recognised with high confidence and a plausible duration.
                    # "[unk] stop" / "stop stop" / long garbage are rejected.
                    ok = (
                        len(words) == 1
                        and words[0]["word"] in KEYWORDS
                        and words[0].get("conf", 0.0) >= MIN_CONF
                        and MIN_DUR <= (words[0]["end"] - words[0]["start"]) <= MAX_DUR
                    )
                    if ok:
                        now = time.monotonic()
                        if now - last_fire > DEBOUNCE_S:
                            last_fire = now
                            try:
                                send(COMMAND)
                                log.info("heard %r conf=%.2f -> sent %s", text, words[0]["conf"], COMMAND)
                            except OSError:
                                log.info("heard %r but nothing is speaking", text)
                    elif text:
                        log.info("ignored %r %s", text, [(w["word"], round(w.get("conf", 0), 2)) for w in words])
        except Exception as e:  # mic unplugged, device change, etc.
            log.warning("listener error: %s; retrying in 5 s", e)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
