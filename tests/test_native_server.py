"""Tests for engines/native/server.py, the Docker-free Whisper + Kokoro server.

Fake backends stand in for faster-whisper / mlx-whisper / kokoro-onnx, so nothing is downloaded and no speech library
is imported: the tests need only fastapi, uvicorn, python-multipart, numpy, httpx and pytest, and run in a few seconds
on Windows, macOS and Linux. Requests are built byte for byte the way Talkover's own code builds them
(voice_inbox.transcribe, voice_health, scripts/demo/build.py, the speak hooks' Kokoro body).
"""
import asyncio
import io
import json
import math
import socket
import struct
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engines" / "native"))
import model_store  # noqa: E402
import server as native  # noqa: E402

HEAVY = ("faster_whisper", "mlx_whisper", "mlx", "kokoro_onnx", "onnxruntime", "ctranslate2", "huggingface_hub")


# ---------------------------------------------------------------- fakes

class FakeStt:
    """Records each call; answers with fixed words at fixed times."""
    name = "faster-whisper"
    model_id = "Systran/faster-whisper-small"
    device = "cpu int8"

    def __init__(self, fail_load=False, delay=0.0):
        self.fail_load = fail_load
        self.delay = delay
        self.calls = []
        self.ensured = []
        self.loads = 0
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def load(self):
        self.loads += 1
        if self.fail_load:
            raise RuntimeError("no model here")

    def ensure(self, model_id):
        status = "present" if model_id in self.ensured else "downloaded"
        self.ensured.append(model_id)
        return {"id": model_id, "path": "/fake/" + model_id, "status": status}

    def downloaded(self):
        return sorted(set(self.ensured))

    def transcribe(self, audio, language, temperature, word_timestamps, prompt):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            self.calls.append({"n": len(audio), "dtype": str(audio.dtype), "language": language,
                               "temperature": temperature, "words": word_timestamps, "prompt": prompt})
            words = [{"word": " The", "start": 0.0, "end": 0.2, "probability": 0.9},
                     {"word": " quick", "start": 0.2, "end": 0.5, "probability": 0.9},
                     {"word": " fox.", "start": 0.5, "end": 0.9, "probability": 0.8}]
            seg = {"start": 0.0, "end": 0.9, "text": " The quick fox.", "tokens": [1, 2, 3], "temperature": 0.0,
                   "avg_logprob": -0.2, "compression_ratio": 1.1, "no_speech_prob": 0.01,
                   "words": words if word_timestamps else []}
            return {"text": "The quick fox.", "language": language or "en", "duration": len(audio) / 16000,
                    "segments": [seg]}
        finally:
            with self._guard:
                self.active -= 1


class FakeTts:
    """A sine at 24 kHz whose length follows the text and the speed."""
    name = "kokoro-onnx"
    model_id = "kokoro-v1.0.onnx"
    device = "CPUExecutionProvider"
    sample_rate = 24000

    def __init__(self):
        self.calls = []

    def load(self):
        pass

    def voices(self):
        return ["af_bella", "af_heart", "am_adam", "bf_emma"]

    def synthesize(self, text, voices, speed, lang):
        self.calls.append({"text": text, "voices": voices, "speed": speed, "lang": lang})
        if text == "???":
            raise ValueError("no phonemes")
        n = int(24000 * 0.05 * len(text) / speed)
        return (0.3 * np.sin(2 * np.pi * 220 * np.arange(n) / 24000)).astype(np.float32)


@pytest.fixture
def stt():
    fake = FakeStt()
    eng = native.Engine(fake, "stt")
    return fake, eng, TestClient(native.create_stt_app(eng))


@pytest.fixture
def tts():
    fake = FakeTts()
    eng = native.Engine(fake, "tts")
    return fake, eng, TestClient(native.create_tts_app(eng, "af_heart"))


# ---------------------------------------------------------------- requests the way Talkover builds them

def wav16(seconds=1.0, rate=16000, hz=440.0, channels=1):
    frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate))) * channels
                      for i in range(int(seconds * rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def multipart(fields, audio, filename="u.wav"):
    """Exactly voice_inbox.transcribe's body: one part per field, then the file part."""
    boundary = "----voiceinbox%d" % int(time.time() * 1000)
    parts = [("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
             for k, v in fields]
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n" % (boundary, filename)).encode() + audio + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    return b"".join(parts), {"Content-Type": "multipart/form-data; boundary=" + boundary}


INBOX_PLAIN = [("model", "whisper-1"), ("language", "en"), ("temperature", "0"), ("response_format", "json")]
INBOX_WORDS = [("model", "whisper-1"), ("language", "en"), ("temperature", "0"),
               ("response_format", "verbose_json"), ("timestamp_granularities[]", "word")]
HEALTH = [("model", "whisper-1"), ("response_format", "json"), ("language", "en"), ("temperature", "0")]
DEMO = [("model", "whisper-1"), ("language", "en"), ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]


def inbox_words(d):
    """voice_inbox.transcribe's parsing of a verbose_json answer."""
    raw = d.get("words") or [w for s in (d.get("segments") or []) for w in (s.get("words") or [])]
    return [(str(w["word"]), float(w["start"]), float(w["end"])) for w in raw]


# ---------------------------------------------------------------- speech to text

def test_stt_health(stt):
    _, _, c = stt
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["engine"] == "stt"


def test_transcription_json_inbox_fields(stt):
    fake, _, c = stt
    body, headers = multipart(INBOX_PLAIN, wav16(1.0))
    r = c.post("/v1/audio/transcriptions", content=body, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"text": "The quick fox."}
    call = fake.calls[-1]
    assert call["language"] == "en" and call["temperature"] == 0.0 and call["words"] is False
    assert call["n"] == 16000 and call["dtype"] == "float32"


def test_transcription_verbose_json_words(stt):
    fake, _, c = stt
    body, headers = multipart(INBOX_WORDS, wav16(1.0))
    r = c.post("/v1/audio/transcriptions", content=body, headers=headers)
    assert r.status_code == 200
    d = r.json()
    assert fake.calls[-1]["words"] is True
    assert d["text"] == "The quick fox." and d["task"] == "transcribe" and d["language"] == "en"
    assert isinstance(d["duration"], float) and abs(d["duration"] - 1.0) < 0.01
    assert [set(s) >= {"id", "start", "end", "text"} for s in d["segments"]] == [True]
    assert [w["word"] for w in d["words"]] == [" The", " quick", " fox."]
    assert all(isinstance(w["start"], float) and isinstance(w["end"], float) for w in d["words"])
    assert inbox_words(d) == [(" The", 0.0, 0.2), (" quick", 0.2, 0.5), (" fox.", 0.5, 0.9)]


def test_verbose_json_without_word_granularity_has_no_words(stt):
    fake, _, c = stt
    body, headers = multipart([("model", "whisper-1"), ("response_format", "verbose_json")], wav16(0.5))
    d = c.post("/v1/audio/transcriptions", content=body, headers=headers).json()
    assert "words" not in d and d["segments"] and fake.calls[-1]["words"] is False


def test_demo_builder_fields_two_granularities(stt):
    _, _, c = stt
    body, headers = multipart(DEMO, wav16(1.0))
    d = c.post("/v1/audio/transcriptions", content=body, headers=headers).json()
    assert len(d["words"]) == 3 and len(d["segments"]) == 1


def test_health_check_request_and_24k_input_is_resampled(stt):
    fake, _, c = stt
    body, headers = multipart(HEALTH, wav16(1.5, rate=24000), filename="r.wav")   # Kokoro's WAV in the round trip
    r = c.post("/v1/audio/transcriptions", content=body, headers=headers)
    assert r.status_code == 200 and "text" in r.json()
    assert abs(fake.calls[-1]["n"] - 24000) <= 2                                # 1.5 s at 16 kHz


def test_stereo_and_float_wav_decode():
    stereo = native.decode_wav(wav16(0.5, rate=48000, channels=2))
    assert abs(len(stereo) - 8000) <= 2
    pcm = (0.25 * np.ones(800, np.float32)).tobytes()
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt " +
           struct.pack("<IHHIIHH", 16, 3, 1, 16000, 64000, 4, 32) + b"data" + struct.pack("<I", 0xFFFFFFFF))
    x = native.decode_wav(hdr + pcm)
    assert len(x) == 800 and abs(float(x[400]) - 0.25) < 1e-6


def test_text_srt_vtt_and_bad_format(stt):
    _, _, c = stt
    for fmt, check in (("text", lambda t: t == "The quick fox."),
                       ("srt", lambda t: t.startswith("1\n00:00:00,000 --> 00:00:00,900")),
                       ("vtt", lambda t: t.startswith("WEBVTT"))):
        body, headers = multipart([("response_format", fmt)], wav16(0.5))
        r = c.post("/v1/audio/transcriptions", content=body, headers=headers)
        assert r.status_code == 200 and check(r.text), fmt
    body, headers = multipart([("response_format", "mp3")], wav16(0.5))
    r = c.post("/v1/audio/transcriptions", content=body, headers=headers)
    assert r.status_code == 400 and "response_format" in r.json()["error"]["message"]


def test_transcription_missing_file_and_garbage_audio_are_400(stt):
    _, _, c = stt
    r = c.post("/v1/audio/transcriptions", data={"model": "whisper-1"})
    assert r.status_code == 400 and "file" in r.json()["error"]["message"]
    r = c.post("/v1/audio/transcriptions", files={"file": ("x.wav", b"RIFF\x00\x00\x00\x00WAVEjunk", "audio/wav")})
    assert r.status_code == 400


def test_models_list_and_idempotent_download(stt):
    fake, eng, c = stt
    for _ in range(2):
        r = c.post("/v1/models/Systran/faster-whisper-small")
        assert r.status_code == 200
        assert r.json()["active"] is True and r.json()["loaded"] is True
    assert fake.ensured == ["Systran/faster-whisper-small"] * 2 and fake.loads == 1
    r = c.post("/v1/models/Systran/faster-whisper-tiny")                 # another model: fetched, not loaded
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["status"] == "downloaded"
    ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    assert ids[0] == "Systran/faster-whisper-small" and "Systran/faster-whisper-tiny" in ids


def test_failed_load_is_reported_and_503(monkeypatch):
    monkeypatch.setattr(native, "STT_WAIT_S", 2.0)
    fake = FakeStt(fail_load=True)
    eng = native.Engine(fake, "stt")
    c = TestClient(native.create_stt_app(eng))
    body, headers = multipart(INBOX_PLAIN, wav16(0.3))
    r = c.post("/v1/audio/transcriptions", content=body, headers=headers)
    assert r.status_code == 503 and "no model here" in r.json()["error"]["message"]
    h = c.get("/health").json()
    assert h["state"] == "error" and "no model here" in h["error"]


def test_transcriptions_are_serialized():
    fake = FakeStt(delay=0.05)
    eng = native.Engine(fake, "stt")
    c = TestClient(native.create_stt_app(eng))
    body, headers = multipart(INBOX_PLAIN, wav16(0.2))
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(
        c.post("/v1/audio/transcriptions", content=body, headers=headers).status_code)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes == [200] * 5 and len(fake.calls) == 5 and fake.max_active == 1


# ---------------------------------------------------------------- text to speech

def test_tts_health_voices_models(tts):
    _, _, c = tts
    assert c.get("/health").status_code == 200
    assert c.get("/v1/audio/voices").json() == {"voices": ["af_bella", "af_heart", "am_adam", "bf_emma"]}
    assert "kokoro" in [m["id"] for m in c.get("/v1/models").json()["data"]]


def parse_wav(data):
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()


def test_speech_hook_body_gives_valid_wav(tts):
    fake, _, c = tts
    # speak-response.ps1: ConvertTo-Json -Compress of model, voice, input, response_format, speed
    body = json.dumps({"model": "kokoro", "voice": "af_heart", "input": "Hello there.", "response_format": "wav",
                       "speed": 1.05}, separators=(",", ":"))
    r = c.post("/v1/audio/speech", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    data = r.content
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE" and data[36:40] == b"data"
    assert struct.unpack("<I", data[4:8])[0] == len(data) - 8
    assert struct.unpack("<I", data[40:44])[0] == len(data) - 44                 # the hook's length - 44
    ch, width, rate, frames = parse_wav(data)
    assert (ch, width, rate) == (1, 2, 24000) and frames == (len(data) - 44) // 2 > 0
    assert fake.calls[-1] == {"text": "Hello there.", "voices": ["af_heart"], "speed": 1.05, "lang": "en-us"}


def test_speech_speed_voice_and_defaults(tts):
    fake, _, c = tts
    slow = c.post("/v1/audio/speech", json={"input": "Same words.", "speed": 0.5}).content
    fast = c.post("/v1/audio/speech", json={"input": "Same words.", "speed": 2.0}).content
    assert len(slow) > len(fast)
    assert fake.calls[-1]["voices"] == ["af_heart"]                               # default voice
    c.post("/v1/audio/speech", json={"input": "x y", "speed": 9})
    assert fake.calls[-1]["speed"] == 2.0                                         # clamped to Kokoro's range
    r = c.post("/v1/audio/speech", json={"input": "Cheerio.", "voice": "bf_emma"})
    assert r.status_code == 200 and fake.calls[-1]["lang"] == "en-gb"
    r = c.post("/v1/audio/speech", json={"input": "Mixed.", "voice": "af_heart+af_bella"})
    assert r.status_code == 200 and fake.calls[-1]["voices"] == ["af_heart", "af_bella"]
    r = c.post("/v1/audio/speech", json={"input": "Raw.", "response_format": "pcm"})
    assert r.status_code == 200 and r.content[:4] != b"RIFF" and len(r.content) % 2 == 0


def test_speech_one_word_health_request(tts):
    _, _, c = tts
    r = c.post("/v1/audio/speech", json={"model": "kokoro", "voice": "af_heart", "input": "ok",
                                         "response_format": "wav"})
    assert r.status_code == 200 and len(r.content) > 44


def test_speech_errors(tts):
    _, _, c = tts
    r = c.post("/v1/audio/speech", json={"input": "Hi.", "voice": "zz_nobody"})
    assert r.status_code == 400 and "zz_nobody" in r.json()["error"]["message"]
    r = c.post("/v1/audio/speech", json={"input": "Hi.", "voice": "af_heart+zz_nobody"})
    assert r.status_code == 400
    assert c.post("/v1/audio/speech", json={"input": "   "}).status_code == 400
    assert c.post("/v1/audio/speech", json={"input": "Hi.", "response_format": "mp3"}).status_code == 400
    assert c.post("/v1/audio/speech", json={"voice": "af_heart"}).status_code == 400   # no input
    r = c.post("/v1/audio/speech", json={"input": "???"})                              # nothing speakable
    assert r.status_code == 200 and parse_wav(r.content)[3] > 0


def test_voices_503_while_loading(monkeypatch):
    monkeypatch.setattr(native, "VOICES_WAIT_S", 0.2)

    class Slow(FakeTts):
        def load(self):
            time.sleep(1.0)
    eng = native.Engine(Slow(), "tts")
    c = TestClient(native.create_tts_app(eng))
    assert c.get("/v1/audio/voices").status_code == 503
    assert eng.wait_ready(5) and c.get("/v1/audio/voices").status_code == 200


# ---------------------------------------------------------------- binding, ports, settings, imports

def test_default_ports_from_env(monkeypatch):
    monkeypatch.setenv("TALKOVER_STT_PORT", "12345")
    monkeypatch.setenv("TALKOVER_TTS_PORT", "12346")
    a = native.parse_args([])
    assert (a.stt_port, a.tts_port) == (12345, 12346)
    monkeypatch.delenv("TALKOVER_STT_PORT")
    monkeypatch.delenv("TALKOVER_TTS_PORT")
    a = native.parse_args([])
    assert (a.stt_port, a.tts_port) == (2022, 8880)
    assert native.parse_args(["--stt-port", "1", "--tts-port", "2"]).stt_port == 1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_both_servers_bind_127_0_0_1_only():
    stt_port, tts_port = free_port(), free_port()
    stt_app = native.create_stt_app(native.Engine(FakeStt(), "stt"))
    tts_app = native.create_tts_app(native.Engine(FakeTts(), "tts"))
    servers = native.build_servers(stt_app, tts_app, stt_port, tts_port)
    assert [s.config.host for s in servers] == ["127.0.0.1", "127.0.0.1"]
    t = threading.Thread(target=lambda: asyncio.run(native.serve_all(servers)), daemon=True)
    t.start()
    try:
        until = time.time() + 10
        while not all(s.started for s in servers) and time.time() < until:
            time.sleep(0.05)
        assert all(s.started for s in servers)
        bound = [sock.getsockname()[0] for s in servers for srv in s.servers for sock in srv.sockets]
        assert bound and set(bound) == {"127.0.0.1"}
        for port, engine in ((stt_port, "stt"), (tts_port, "tts")):
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=5) as r:
                assert json.loads(r.read())["engine"] == engine
    finally:
        for s in servers:
            s.should_exit = True
        t.join(10)
    assert not t.is_alive()


def test_model_ids_translate_between_backends(monkeypatch):
    assert model_store.translate_model_id("Systran/faster-whisper-small", "mlx") == "mlx-community/whisper-small-mlx"
    assert model_store.translate_model_id("Systran/faster-whisper-medium.en", "mlx") == \
        "mlx-community/whisper-medium.en-mlx"
    assert model_store.translate_model_id("mlx-community/whisper-small-mlx", "faster-whisper") == \
        "Systran/faster-whisper-small"
    assert model_store.translate_model_id("someone/custom", "mlx") == "someone/custom"
    monkeypatch.delenv("TALKOVER_NATIVE_STT_MODEL", raising=False)
    monkeypatch.delenv("TALKOVER_WHISPER_MODEL", raising=False)
    assert model_store.default_stt_model("mlx") == "mlx-community/whisper-small-mlx"
    assert model_store.default_stt_model("faster-whisper") == "Systran/faster-whisper-small"
    monkeypatch.setenv("TALKOVER_WHISPER_MODEL", "Systran/faster-whisper-base")
    assert model_store.default_stt_model("mlx") == "mlx-community/whisper-base-mlx"
    monkeypatch.setenv("TALKOVER_NATIVE_STT_MODEL", "x/y")
    assert model_store.default_stt_model("faster-whisper") == "x/y"
    assert model_store.pick_stt_backend("faster-whisper") == "faster-whisper"
    assert model_store.pick_stt_backend("mlx") == "mlx"


def test_settings_paths_follow_talkover_home(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store.voice_config, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(model_store.voice_config, "LOG_DIR", tmp_path / "logs")
    s = model_store.Settings.from_env()
    assert s.kokoro_dir == tmp_path / "models" / "kokoro" and s.whisper_dir == tmp_path / "models" / "whisper"
    assert s.log_file == tmp_path / "logs" / "engines.log"


def test_download_resumes_and_checks_sha(tmp_path, monkeypatch):
    payload = bytes(range(256)) * 40
    import hashlib
    sha = hashlib.sha256(payload).hexdigest()
    seen = []

    class Resp(io.BytesIO):
        def __init__(self, data, status):
            super().__init__(data)
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        rng = req.get_header("Range")
        seen.append(rng)
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        return Resp(payload[start:], 206 if rng else 200)
    monkeypatch.setattr(model_store.urllib.request, "urlopen", fake_urlopen)
    dest = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(payload[:1000])           # left by an interrupted run
    model_store.download_file("http://x/f.bin", dest, len(payload), sha, log=lambda m: None)
    assert dest.read_bytes() == payload and seen == ["bytes=1000-"] and not (tmp_path / "f.bin.part").exists()
    with pytest.raises(IOError):
        model_store.download_file("http://x/g.bin", tmp_path / "g.bin", len(payload), "0" * 64, log=lambda m: None)
    assert not (tmp_path / "g.bin").exists() and not (tmp_path / "g.bin.part").exists()


def test_no_speech_library_was_imported():
    loaded = [m for m in HEAVY if m in sys.modules]
    assert loaded == [], "the server must import speech libraries only inside backend load(): %s" % loaded
