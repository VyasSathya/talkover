"""Tests for hooks/speak_response.py, the macOS / Linux speak hook (a port of hooks/speak-response.ps1).

They run on Windows, macOS and Linux with no sound device: CLAUDE_SPEAK_TEST=1 gives the hook its own lock names, its
log in the sandbox temp folder and a silent player that only keeps the time (sounddevice is never imported). A fake
Kokoro (stdlib http.server in a thread) answers with short 24 kHz mono 16-bit WAVs and records every input it was
asked to say, so a test can check exactly what was spoken and in which order.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "speak_response.py"
SID = "abcdef12-3456-7890-abcd-ef1234567890"


# ---------------------------------------------------------------- fake Kokoro

def make_wav(seconds: float, rate: int = 24000) -> bytes:
    """Silence as a WAV with the placeholder sizes Kokoro's streaming output has (the hook must repair them)."""
    pcm = b"\x00\x00" * int(rate * seconds)
    fmt = (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + rate.to_bytes(4, "little") + \
        (rate * 2).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    return (b"RIFF" + b"\xff\xff\xff\xff" + b"WAVE" + b"fmt " + (16).to_bytes(4, "little") + fmt +
            b"data" + b"\xff\xff\xff\xff" + pcm)


class FakeKokoro:
    def __init__(self):
        self.inputs = []
        self.lock = threading.Lock()
        self.seconds = 0.2      # length of every chunk's audio
        self.fail = False       # answer 503 to everything (engine down)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                with owner.lock:
                    owner.inputs.append(body.get("input"))
                if owner.fail or self.path != "/v1/audio/speech":
                    self.send_response(503)
                    self.end_headers()
                    return
                wav = make_wav(0.05 if body.get("input") == "ok" else owner.seconds)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav)))
                self.end_headers()
                self.wfile.write(wav)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def spoken(self):
        """What the hook asked Kokoro to say, without the one-word health checks."""
        with self.lock:
            return [i for i in self.inputs if i != "ok"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def kokoro():
    k = FakeKokoro()
    yield k
    k.close()


@pytest.fixture
def sandbox(tmp_path, kokoro):
    """A temp folder of its own and the environment that points the hook at it and at the fake Kokoro."""
    tmp = tmp_path / "temp"
    tmp.mkdir()
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("TALKOVER_"):
            del env[k]
    env.update({
        "TEMP": str(tmp), "TMP": str(tmp), "TALKOVER_TEMP": str(tmp),
        "CLAUDE_SPEAK_TEST": "1",
        "TALKOVER_TTS_BASE": kokoro.base,
        "TALKOVER_CONFIG": str(tmp_path / "no-config.env"),
        "TALKOVER_HOME": str(tmp_path / "home"),
        "TALKOVER_ENGINES": "docker",
        "PYTHONIOENCODING": "utf-8",
    })
    return Sandbox(tmp, env)


class Sandbox:
    def __init__(self, tmp: Path, env: dict):
        self.tmp = tmp
        self.env = env

    def start(self, payload=None, *args):
        p = subprocess.Popen([sys.executable, str(HOOK), *args], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, env=self.env, cwd=str(REPO))
        p.stdin.write(json.dumps(payload).encode("utf-8") if payload is not None else b"")
        p.stdin.close()
        return p

    def run(self, payload=None, *args, timeout=20):
        p = self.start(payload, *args)
        assert p.wait(timeout) == 0, p.stderr.read().decode("utf-8", "replace")
        return p

    def log(self):
        try:
            return (self.tmp / "speak.log").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def wait_log(self, needle: str, count: int = 1, timeout: float = 15):
        end = time.time() + timeout
        while time.time() < end:
            if sum(needle in ln for ln in self.log()) >= count:
                return
            time.sleep(0.02)
        raise AssertionError("log never showed %r x%d:\n%s" % (needle, count, "\n".join(self.log())))

    def events(self, *needles):
        """The log lines containing any of the needles, reduced to the needle, in order."""
        out = []
        for ln in self.log():
            for n in needles:
                if n in ln:
                    out.append(n)
                    break
        return out


def stop_payload(text, sid=SID):
    return {"session_id": sid, "hook_event_name": "Stop", "last_assistant_message": text}


S1 = "Hello there."
S2 = "The second sentence is here and it is fairly long to fill the buffer up."
S3 = "The third sentence is also long enough that it cannot join the second one."


# ---------------------------------------------------------------- pure functions

def _load_module():
    spec = importlib.util.spec_from_file_location("speak_response_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_clean_split_and_wav_repair():
    m = _load_module()
    assert m.clean_text("## Title\n- **bold** item with `code` and [a link](http://x.y)\n```py\nx=1\n```\nsee "
                        "https://example.com/a now") == \
        "Title bold item with code and a link code block omitted. see link now"
    assert m.split_chunks(" ".join([S1, S2, S3])) == [S1, S2, S3]
    long_first = "This first sentence runs on for quite a while, so it is cut at the comma and the rest follows."
    assert m.split_chunks(long_first) == ["This first sentence runs on for quite a while,",
                                          "so it is cut at the comma and the rest follows."]
    b, off, n, rate, ch, bits = m.repair_wav(make_wav(0.1))
    assert (off, n, rate, ch, bits) == (44, 4800, 24000, 1, 16)
    assert int.from_bytes(b[4:8], "little") == len(b) - 8 and int.from_bytes(b[40:44], "little") == 4800
    assert m.get_hash("  Hello\n  World ") == m.get_hash("hello world") and len(m.get_hash("x")) == 16


# ---------------------------------------------------------------- the hook

def test_stop_speaks_all_chunks_in_order(sandbox, kokoro):
    sandbox.run(stop_payload(" ".join([S1, S2, S3])))
    assert kokoro.spoken() == [S1, S2, S3]
    assert sandbox.events("speak mode=stop", "play 1/3", "play 2/3", "play 3/3", "done") == \
        ["speak mode=stop", "play 1/3", "play 2/3", "play 3/3", "done"]
    assert "session=abcdef12" in "\n".join(sandbox.log())
    for f in ("claude-speak.pid", "claude-speak.progress", "claude-speak.playing", "claude-speak.pause"):
        assert not (sandbox.tmp / f).exists(), f
    assert (sandbox.tmp / "claude-speak.wav").read_bytes()[:4] == b"RIFF"
    hist = list((sandbox.tmp / "claude-speak.history").glob("*_abcdef12.txt"))
    assert len(hist) == 1 and hist[0].read_text(encoding="utf-8") == " ".join([S1, S2, S3])


def test_same_text_twice_is_spoken_once(sandbox, kokoro):
    sandbox.run(stop_payload("Only once please."))
    sandbox.run(stop_payload("Only  once please. "))     # same text after whitespace normalization
    assert kokoro.spoken() == ["Only once please."]
    assert sum("speak mode=" in ln for ln in sandbox.log()) == 1


def test_muted_is_silent(sandbox, kokoro):
    (sandbox.tmp / "claude-voice.muted").write_text("")
    sandbox.run(stop_payload("You should not hear this."))
    assert kokoro.inputs == [] and sandbox.log() == []


def test_cancel_makes_waiting_speaker_exit(sandbox, kokoro):
    kokoro.seconds = 2.5     # the reading is still in its first sentence when the cancel comes
    a = sandbox.start(stop_payload("Charlie one. Charlie two."))
    sandbox.wait_log("play 1/2")
    first = (sandbox.tmp / "claude-speak.newest").read_text()
    b = sandbox.start(stop_payload("Delta never heard."))
    end = time.time() + 10       # b announces itself in claude-speak.newest just before it waits for the lock
    while (sandbox.tmp / "claude-speak.newest").read_text() == first and time.time() < end:
        time.sleep(0.02)
    time.sleep(0.1)
    (sandbox.tmp / "claude-speak.cancel").write_text("now")
    assert b.wait(10) == 0 and a.wait(10) == 0
    assert any("cancelled while waiting" in ln for ln in sandbox.log())
    assert "Delta never heard." not in kokoro.spoken()
    assert sum("speak mode=" in ln for ln in sandbox.log()) == 1
    assert not any("play 2/2" in ln for ln in sandbox.log())     # the reading itself yields at its next sentence


def _line(kind, content):
    if kind == "user":
        return json.dumps({"type": "user", "sessionId": SID, "message": {"role": "user", "content": content}})
    return json.dumps({"type": "assistant", "sessionId": SID,
                       "message": {"role": "assistant", "model": "claude", "content": content}})


def write_transcript(path: Path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_transcript_speaks_only_new_assistant_text(sandbox, kokoro, tmp_path):
    tr = tmp_path / "session.jsonl"
    write_transcript(tr, [
        json.dumps({"type": "summary", "summary": "earlier"}),
        _line("user", "first question"),
        _line("assistant", [{"type": "text", "text": "Old reply already gone."}]),
        _line("user", [{"type": "text", "text": "second question"}]),
        _line("assistant", [{"type": "thinking", "thinking": "hmm"}]),
        _line("assistant", [{"type": "text", "text": "**First** new reply."},
                            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
        _line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}]),   # not a prompt
        _line("assistant", [{"type": "text", "text": "   "}]),
        _line("assistant", [{"type": "text", "text": "Second new reply."}]),
    ])
    payload = {"session_id": SID, "transcript_path": str(tr), "hook_event_name": "PreToolUse"}
    sandbox.run(payload, "-Mode", "transcript")
    assert kokoro.spoken() == ["First new reply.", "Second new reply."]
    sandbox.run(payload, "-Mode", "transcript")              # the next tool call: nothing new
    assert kokoro.spoken() == ["First new reply.", "Second new reply."]
    assert sum("speak mode=transcript" in ln for ln in sandbox.log()) == 2


def test_pause_file_pauses_and_replays_sentence(sandbox, kokoro):
    kokoro.seconds = 0.6
    p = sandbox.start(stop_payload("Echo one. Echo two."))
    sandbox.wait_log("play 1/2")
    (sandbox.tmp / "claude-speak.pause").write_text("")
    sandbox.wait_log("paused after")
    time.sleep(0.4)
    assert not any("play 2/2" in ln for ln in sandbox.log())
    (sandbox.tmp / "claude-speak.pause").unlink()
    assert p.wait(15) == 0
    assert sandbox.events("play 1/2", "paused after", "resumed after", "play 2/2", "done") == \
        ["play 1/2", "paused after", "resumed after", "play 1/2", "play 2/2", "done"]
    assert any("(pause file)" in ln for ln in sandbox.log())


def test_nav_next_skips_to_next_queued_reply(sandbox, kokoro, tmp_path):
    kokoro.seconds = 0.8
    tr = tmp_path / "session.jsonl"
    write_transcript(tr, [
        _line("user", "go"),
        _line("assistant", [{"type": "text", "text": "Alpha one. Alpha two."}]),
        _line("assistant", [{"type": "text", "text": "Bravo."}]),
    ])
    p = sandbox.start({"session_id": SID, "transcript_path": str(tr)}, "-Mode", "transcript")
    sandbox.wait_log("play 1/2")
    nav = sandbox.tmp / "claude-speak.nav"
    nav.write_text("%d next" % (time.time_ns() // 1000), encoding="ascii")
    assert p.wait(15) == 0
    assert sandbox.events("play 1/2", "nav: next", "next: on to the next reply", "play 1/1", "play 2/2") == \
        ["play 1/2", "nav: next", "next: on to the next reply", "play 1/1"]


def test_replay_again_plays_last_reply(sandbox, kokoro):
    sandbox.run(stop_payload("Foxtrot reply."))
    sandbox.run(None, "-Mode", "replay", "-Nav", "again")
    assert kokoro.spoken() == ["Foxtrot reply.", "Foxtrot reply."]
    assert any("speak mode=replay" in ln and "replay=" in ln for ln in sandbox.log())


def test_native_engine_down_exits_without_speaking(sandbox, kokoro):
    kokoro.fail = True
    sandbox.env["TALKOVER_ENGINES"] = "native"
    t0 = time.time()
    sandbox.run(stop_payload("Golf is never heard."))
    assert time.time() - t0 < 10
    assert kokoro.spoken() == []
    log = "\n".join(sandbox.log())
    assert "native engine is down" in log and "not read:" in log
    assert "speak mode=" not in log and "play " not in log
