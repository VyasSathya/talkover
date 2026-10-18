#!/usr/bin/env python3
"""Talkover's native speech engines: Whisper and Kokoro in one Python process, no Docker.

A drop-in replacement for the two containers in docker-compose.yml. It answers the same requests on the same ports,
so voice_inbox.py, voice_health.py, the speak hooks, install.ps1 and the demo builder need no change:

  speech to text  127.0.0.1:<TALKOVER_STT_PORT, 2022>   (speaches)
    GET  /health                       200 while the process is up; body says whether the model is loaded
    GET  /v1/models                    the active model and every model in the cache
    POST /v1/models/{model_id}         download the model (idempotent, 200); for the active model also waits until
                                       it is loaded, so install scripts return with a warm engine
    POST /v1/audio/transcriptions      OpenAI fields: file, model, language, prompt, temperature, response_format
                                       (json, verbose_json, text, srt, vtt), timestamp_granularities[] (word, segment)
  text to speech  127.0.0.1:<TALKOVER_TTS_PORT, 8880>   (kokoro-fastapi)
    GET  /health
    GET  /v1/models
    GET  /v1/audio/voices              {"voices": [...]}; 503 until Kokoro is loaded (install scripts wait on it)
    POST /v1/audio/speech              JSON: model, input, voice (af_heart; "a+b" mixes voices), speed (0.5 to 2.0),
                                       response_format wav (24 kHz mono 16-bit, 44-byte header) or pcm; 400 on an
                                       unknown voice

Both listen on 127.0.0.1 only: nothing is reachable from another machine. Each model is loaded once, in the
background right at start, and kept in memory. Transcriptions run one at a time (a lock), as do syntheses; the two
engines do not wait on each other. A load that failed (no network for the first download, say) is retried by the
next request. Backends are plain objects (see FasterWhisperBackend, MlxWhisperBackend, KokoroOnnxBackend), so the
tests pass fakes and never import the speech libraries.

Run:  python engines/native/server.py [--stt-port N] [--tts-port N]
Logs: <TALKOVER_HOME>/logs/engines.log. Models: <TALKOVER_HOME>/models (fetch_models.py fetches them ahead of time).
"""
import argparse
import asyncio
import contextlib
import io
import logging
import logging.handlers
import os
import signal
import struct
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_store  # noqa: E402

from fastapi import FastAPI, File, Form, Request, UploadFile  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse, Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

log = logging.getLogger("talkover.native")

BIND_HOST = "127.0.0.1"
WHISPER_RATE = 16000
KOKORO_RATE = 24000
STT_WAIT_S = 900.0          # a transcription waits this long for the model (first start may still be downloading)
TTS_WAIT_S = 300.0
VOICES_WAIT_S = 3.0         # the voices list is the readiness probe: answer 503 quickly while loading
RETRY_AFTER_FAIL_S = 10.0   # a failed load is retried by a request at most this often
LANG_BY_PREFIX = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it", "p": "pt-br"}


# ---------------------------------------------------------------- engines (load once, keep warm, one call at a time)

class Engine:
    """One backend, loaded in a background thread and kept warm. Calls go through run(), one at a time."""

    def __init__(self, backend, kind: str):
        self.backend = backend
        self.kind = kind
        self._call_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._loading = False
        self._failed_at = 0.0
        self.error = None
        self.load_s = None
        self.calls = 0

    @property
    def state(self) -> str:
        if self._ready.is_set():
            return "ready"
        return "loading" if self._loading else ("error" if self.error else "idle")

    def start(self) -> None:
        """Begin loading in the background (no-op when loaded or loading)."""
        with self._state_lock:
            if self._ready.is_set() or self._loading:
                return
            self._loading = True
        threading.Thread(target=self._load, name="load-" + self.kind, daemon=True).start()

    def _load(self) -> None:
        t0 = time.monotonic()
        log.info("%s: loading %s %s", self.kind, getattr(self.backend, "name", "?"), getattr(self.backend, "model_id", ""))
        try:
            self.backend.load()
            self.load_s = time.monotonic() - t0
            self.error = None
            self._ready.set()
            log.info("%s: ready in %.1f s (%s)", self.kind, self.load_s, getattr(self.backend, "device", None) or "-")
        except Exception as e:  # the next request retries
            self.error = "%s: %s" % (type(e).__name__, e)
            self._failed_at = time.monotonic()
            log.exception("%s: load failed", self.kind)
        finally:
            with self._state_lock:
                self._loading = False

    def wait_ready(self, timeout: float) -> bool:
        """True once loaded. Starts a load if none is running (a failed one is retried after RETRY_AFTER_FAIL_S)."""
        if self._ready.is_set():
            return True
        if not self._loading and (not self.error or time.monotonic() - self._failed_at >= RETRY_AFTER_FAIL_S):
            self.start()
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if self._ready.wait(0.1):
                return True
            if not self._loading and self.error:
                return False
        return self._ready.is_set()

    def run(self, fn, *args, **kwargs):
        with self._call_lock:
            self.calls += 1
            return fn(*args, **kwargs)

    def status(self) -> dict:
        b = self.backend
        return {"status": "ok", "engine": self.kind, "state": self.state, "backend": getattr(b, "name", None),
                "model": getattr(b, "model_id", None), "device": getattr(b, "device", None),
                "load_seconds": None if self.load_s is None else round(self.load_s, 2), "error": self.error}


# ---------------------------------------------------------------- real backends (heavy imports only inside load())

def _add_nvidia_dll_dirs() -> None:
    """Windows: CUDA libraries installed as pip wheels (nvidia-cublas-cu12, nvidia-cudnn-cu12) live in
    site-packages/nvidia/*/bin, which ctranslate2 does not search. Add them when present; harmless otherwise."""
    if sys.platform != "win32":
        return
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if not nv.is_dir():
            continue
        for bin_dir in nv.glob("*/bin"):
            with contextlib.suppress(OSError):
                os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


class FasterWhisperBackend:
    """faster-whisper (CTranslate2): CUDA float16 when a GPU works, else CPU int8. The device is proven by a warm-up
    transcription, so a machine with a GPU but no CUDA libraries falls back to the CPU instead of failing later."""
    name = "faster-whisper"

    def __init__(self, settings):
        self.settings = settings
        self.model_id = settings.stt_model
        self.device = None
        self._model = None

    def ensure(self, model_id: str) -> dict:
        path, how = model_store.ensure_whisper(model_id, self.settings.whisper_dir, log=log.info)
        return {"id": model_id, "path": str(path), "status": how}

    def downloaded(self):
        return model_store.downloaded_whisper_models(self.settings.whisper_dir)

    def load(self) -> None:
        path = Path(self.ensure(self.model_id)["path"])
        _add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel
        want = self.settings.stt_device
        options = []
        if want in ("auto", "cuda"):
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    options.append(("cuda", "float16"))
            except Exception as e:
                log.info("stt: no CUDA (%s)", e)
        if want != "cuda" or not options:
            options.append(("cpu", "int8"))
        threads = int(os.getenv("TALKOVER_NATIVE_THREADS", "0") or 0) or max(4, min(8, (os.cpu_count() or 8) // 2))
        last = None
        for device, compute in options:
            try:
                model = WhisperModel(str(path), device=device, compute_type=compute, cpu_threads=threads)
                segments, _ = model.transcribe(np.zeros(WHISPER_RATE, np.float32), language="en", beam_size=1)
                list(segments)                       # the warm-up runs the encoder: proves the device works
                self._model, self.device = model, "%s %s" % (device, compute)
                return
            except Exception as e:
                last = e
                log.warning("stt: %s %s did not work (%s: %s)", device, compute, type(e).__name__, e)
        raise last

    def transcribe(self, audio, language, temperature, word_timestamps, prompt) -> dict:
        segments, info = self._model.transcribe(audio, language=language or None, temperature=temperature,
                                                word_timestamps=word_timestamps, initial_prompt=prompt or None,
                                                vad_filter=False)
        out = []
        for s in segments:
            out.append({"start": s.start, "end": s.end, "text": s.text, "tokens": list(s.tokens),
                        "temperature": s.temperature, "avg_logprob": s.avg_logprob,
                        "compression_ratio": s.compression_ratio, "no_speech_prob": s.no_speech_prob,
                        "words": [{"word": w.word, "start": w.start, "end": w.end, "probability": w.probability}
                                  for w in (s.words or [])]})
        return {"text": "".join(s["text"] for s in out).strip(), "language": info.language,
                "duration": info.duration, "segments": out}


class MlxWhisperBackend:
    """mlx-whisper on Apple Silicon: the model runs on the GPU through Metal. mlx_whisper keeps the loaded model
    (its ModelHolder), so after the warm-up every request reuses it."""
    name = "mlx-whisper"

    def __init__(self, settings):
        self.settings = settings
        self.model_id = settings.stt_model
        self.device = None
        self._mlx = None
        self._path = None

    def ensure(self, model_id: str) -> dict:
        path, how = model_store.ensure_whisper(model_id, self.settings.whisper_dir, log=log.info)
        return {"id": model_id, "path": str(path), "status": how}

    def downloaded(self):
        return model_store.downloaded_whisper_models(self.settings.whisper_dir)

    def load(self) -> None:
        self._path = self.ensure(self.model_id)["path"]
        import mlx.core as mx
        import mlx_whisper
        self._mlx = mlx_whisper
        mlx_whisper.transcribe(np.zeros(WHISPER_RATE, np.float32), path_or_hf_repo=self._path, language="en")
        self.device = "metal" if mx.default_device() == mx.gpu else str(mx.default_device())

    def transcribe(self, audio, language, temperature, word_timestamps, prompt) -> dict:
        r = self._mlx.transcribe(audio, path_or_hf_repo=self._path, language=language or None,
                                 temperature=temperature, word_timestamps=word_timestamps,
                                 initial_prompt=prompt or None, verbose=None)
        out = []
        for s in r.get("segments") or []:
            out.append({"start": s.get("start", 0.0), "end": s.get("end", 0.0), "text": s.get("text", ""),
                        "tokens": list(s.get("tokens") or []), "temperature": s.get("temperature", temperature),
                        "avg_logprob": s.get("avg_logprob", 0.0), "compression_ratio": s.get("compression_ratio", 0.0),
                        "no_speech_prob": s.get("no_speech_prob", 0.0),
                        "words": [{"word": w["word"], "start": w["start"], "end": w["end"],
                                   "probability": w.get("probability", 0.0)} for w in (s.get("words") or [])]})
        return {"text": (r.get("text") or "").strip(), "language": r.get("language") or language,
                "duration": len(audio) / WHISPER_RATE, "segments": out}


class KokoroOnnxBackend:
    """Kokoro-82M through kokoro-onnx (onnxruntime). Output: float32 samples at 24 kHz."""
    name = "kokoro-onnx"
    sample_rate = KOKORO_RATE

    def __init__(self, settings):
        self.settings = settings
        self.model_id = settings.kokoro_model
        self.device = None
        self._k = None

    def load(self) -> None:
        model_store.ensure_kokoro(self.settings, log=log.info)
        model, voices = model_store.kokoro_paths(self.settings)
        from kokoro_onnx import Kokoro
        self._k = Kokoro(str(model), str(voices))
        self.device = ",".join(self._k.sess.get_providers())
        self._k.create("ok", voice=self.settings.default_voice if self.settings.default_voice in self.voices()
                       else self.voices()[0], speed=1.0, lang="en-us")

    def voices(self):
        return sorted(self._k.get_voices())

    def synthesize(self, text, voices, speed, lang):
        voice = voices[0] if len(voices) == 1 else np.mean([self._k.get_voice_style(v) for v in voices], axis=0)
        samples, _rate = self._k.create(text, voice=voice, speed=speed, lang=lang)
        return samples


def make_stt_backend(settings):
    return MlxWhisperBackend(settings) if settings.stt_backend == "mlx" else FasterWhisperBackend(settings)


def make_tts_backend(settings):
    return KokoroOnnxBackend(settings)


# ---------------------------------------------------------------- audio in and out

def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """Windowed-sinc low-pass; cutoff as a fraction of the sample rate. Keeps a downsample from aliasing."""
    n = 63
    t = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff * t) * np.hamming(n)
    h /= h.sum()
    return np.convolve(x, h, mode="same")


def resample(x: np.ndarray, src: int, dst: int = WHISPER_RATE) -> np.ndarray:
    if src == dst or len(x) == 0:
        return x.astype(np.float32, copy=False)
    if src > dst:
        x = _lowpass(x, 0.475 * dst / src)
    n = int(round(len(x) * dst / src))
    return np.interp(np.arange(n) * (src / dst), np.arange(len(x)), x).astype(np.float32)


def decode_wav(data: bytes) -> np.ndarray:
    """A RIFF/WAVE file as float32 mono at 16 kHz. Integer PCM (8/16/24/32-bit) and 32/64-bit float, any rate and
    channel count. Size fields that are wrong (streaming writers put 0 or 0xFFFFFFFF there) are tolerated."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    fmt = None
    pcm = None
    i = 12
    while i + 8 <= len(data):
        cid, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        body = i + 8
        if cid == b"fmt ":
            tag, channels, rate = struct.unpack("<HHI", data[body:body + 8])
            bits = struct.unpack("<H", data[body + 14:body + 16])[0]
            if tag == 0xFFFE and size >= 40:                   # WAVE_FORMAT_EXTENSIBLE: real tag in the subformat
                tag = struct.unpack("<H", data[body + 24:body + 26])[0]
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            end = len(data) if size in (0, 0xFFFFFFFF) or body + size > len(data) else body + size
            pcm = data[body:end]
            break
        i = body + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError("WAV file without fmt or data chunk")
    tag, channels, rate, bits = fmt
    if channels < 1 or rate < 1000:
        raise ValueError("WAV header is not usable (%d channels, %d Hz)" % (channels, rate))
    width = bits // 8
    pcm = pcm[:len(pcm) - len(pcm) % (width * channels)]
    if tag == 1 and bits == 16:
        x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768.0
    elif tag == 1 and bits == 8:
        x = (np.frombuffer(pcm, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif tag == 1 and bits == 24:
        b = np.frombuffer(pcm, np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        x = (np.where(v >= 1 << 23, v - (1 << 24), v)).astype(np.float32) / float(1 << 23)
    elif tag == 1 and bits == 32:
        x = np.frombuffer(pcm, "<i4").astype(np.float32) / float(1 << 31)
    elif tag == 3 and bits == 32:
        x = np.frombuffer(pcm, "<f4").astype(np.float32)
    elif tag == 3 and bits == 64:
        x = np.frombuffer(pcm, "<f8").astype(np.float32)
    else:
        raise ValueError("unsupported WAV encoding (format %d, %d bits)" % (tag, bits))
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return resample(x, rate, WHISPER_RATE)


def decode_audio(data: bytes) -> np.ndarray:
    """WAV is decoded here (what Talkover sends). Anything else goes to PyAV through faster-whisper, when installed."""
    if data[:4] == b"RIFF":
        return decode_wav(data)
    try:
        from faster_whisper.audio import decode_audio as fw_decode
    except ImportError:
        raise ValueError("only WAV audio is supported here (install faster-whisper for other formats)")
    try:
        return fw_decode(io.BytesIO(data), sampling_rate=WHISPER_RATE).astype(np.float32)
    except Exception as e:
        raise ValueError("could not decode the audio: %s" % e)


def wav_bytes(samples: np.ndarray, rate: int = KOKORO_RATE) -> bytes:
    """Mono 16-bit PCM WAV with the canonical 44-byte header (the speak hook reads the data size as length - 44)."""
    pcm = (np.clip(np.asarray(samples, np.float32), -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    header = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" +
              b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) +
              b"data" + struct.pack("<I", len(pcm)))
    return header + pcm


# ---------------------------------------------------------------- response helpers

def _error(status: int, message: str, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)


def _install_errors(app: FastAPI) -> None:
    """OpenAI answers a malformed request with 400 and {"error": {...}}; FastAPI's default would be 422."""
    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError):
        parts = []
        for e in exc.errors():
            where = ".".join(str(p) for p in e.get("loc", ()) if p not in ("body",))
            parts.append("%s: %s" % (where, e.get("msg")) if where else str(e.get("msg")))
        return _error(400, "; ".join(parts) or "invalid request")


def _stamp(t: float, sep: str) -> str:
    ms = int(round(max(t, 0.0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d%s%03d" % (h, m, s, sep, ms)


def _subtitles(segments, kind: str) -> str:
    lines = ["WEBVTT", ""] if kind == "vtt" else []
    for n, s in enumerate(segments, 1):
        sep = "." if kind == "vtt" else ","
        if kind == "srt":
            lines.append(str(n))
        lines.append("%s --> %s" % (_stamp(s["start"], sep), _stamp(s["end"], sep)))
        lines.append(s["text"].strip())
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- the two apps

def create_stt_app(engine: Engine) -> FastAPI:
    """Speech to text, speaches-compatible. engine wraps an STT backend (real or fake)."""
    app = FastAPI(title="Talkover native speech to text", docs_url=None, redoc_url=None, openapi_url=None)
    _install_errors(app)

    @app.get("/health")
    def health():
        return engine.status()

    @app.get("/v1/models")
    def list_models():
        b = engine.backend
        ids = [b.model_id] + [m for m in b.downloaded() if m != b.model_id]
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": m.split("/")[0],
                                             "active": m == b.model_id} for m in ids]}

    @app.post("/v1/models/{model_id:path}")
    def download_model(model_id: str):
        b = engine.backend
        resolved = model_store.translate_model_id(model_id, "mlx" if b.name == "mlx-whisper" else "faster-whisper")
        active = model_id in ("whisper-1", b.model_id) or resolved == b.model_id
        if active:
            resolved = b.model_id
        t0 = time.monotonic()
        try:
            r = b.ensure(resolved)
        except Exception as e:
            log.exception("stt: model %s could not be fetched", resolved)
            return _error(502, "model %s could not be downloaded: %s" % (resolved, e), "server_error")
        if active and not engine.wait_ready(1800):
            return _error(503, "model %s is downloaded but did not load: %s" % (resolved, engine.error), "server_error")
        log.info("stt: model %s (asked for %s): %s in %.1f s", resolved, model_id, r.get("status"),
                 time.monotonic() - t0)
        return {"id": model_id, "model": resolved, "status": r.get("status"), "active": active,
                "loaded": active and engine.state == "ready"}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(
        file: UploadFile = File(...),
        model: str = Form("whisper-1"),
        language: Optional[str] = Form(None),
        prompt: Optional[str] = Form(None),
        response_format: str = Form("json"),
        temperature: float = Form(0.0),
        timestamp_granularities: Optional[List[str]] = Form(None, alias="timestamp_granularities[]"),
    ):
        fmt = (response_format or "json").strip().lower()
        if fmt not in ("json", "verbose_json", "text", "srt", "vtt"):
            return _error(400, "response_format must be json, verbose_json, text, srt or vtt")
        grans = {str(g).strip().lower() for g in (timestamp_granularities or [])}
        want_words = fmt == "verbose_json" and "word" in grans
        data = await file.read()
        try:
            audio = decode_audio(data)
        except ValueError as e:
            return _error(400, str(e))
        lang = (language or "").strip() or None

        def work():
            if not engine.wait_ready(STT_WAIT_S):
                return None
            t0 = time.monotonic()
            r = engine.run(engine.backend.transcribe, audio, lang, float(temperature), want_words, prompt)
            log.info("stt: %.2f s of audio in %.2f s (%s%s), %d chars", len(audio) / WHISPER_RATE,
                     time.monotonic() - t0, fmt, " +words" if want_words else "", len(r.get("text") or ""))
            return r

        try:
            r = await run_in_threadpool(work)
        except Exception as e:
            log.exception("stt: transcription failed")
            return _error(500, "transcription failed: %s" % e, "server_error")
        if r is None:
            return _error(503, "speech to text is not ready (%s): %s" % (engine.state, engine.error or "loading"),
                          "server_error")
        text = r.get("text") or ""
        segments = r.get("segments") or []
        if fmt == "json":
            return {"text": text}
        if fmt == "text":
            return PlainTextResponse(text)
        if fmt in ("srt", "vtt"):
            return PlainTextResponse(_subtitles(segments, fmt))
        out = {"task": "transcribe", "language": r.get("language") or lang or "en",
               "duration": float(r.get("duration") or len(audio) / WHISPER_RATE), "text": text,
               "segments": [{"id": n, "seek": 0, "start": float(s["start"]), "end": float(s["end"]),
                             "text": s["text"], "tokens": s.get("tokens") or [],
                             "temperature": float(s.get("temperature") or 0.0),
                             "avg_logprob": float(s.get("avg_logprob") or 0.0),
                             "compression_ratio": float(s.get("compression_ratio") or 0.0),
                             "no_speech_prob": float(s.get("no_speech_prob") or 0.0)}
                            for n, s in enumerate(segments)]}
        if want_words:
            out["words"] = [{"word": w["word"], "start": float(w["start"]), "end": float(w["end"]),
                             "probability": float(w.get("probability") or 0.0)}
                            for s in segments for w in (s.get("words") or [])]
        return out

    return app


class SpeechRequest(BaseModel):
    """The OpenAI / kokoro-fastapi body. Unknown fields (stream, return_download_link, ...) are ignored."""
    input: str
    model: str = "kokoro"
    voice: Optional[str] = None
    response_format: str = "wav"
    speed: float = 1.0
    lang_code: Optional[str] = None


def create_tts_app(engine: Engine, default_voice: str = "af_heart") -> FastAPI:
    """Text to speech, kokoro-fastapi-compatible. engine wraps a TTS backend (real or fake)."""
    app = FastAPI(title="Talkover native text to speech", docs_url=None, redoc_url=None, openapi_url=None)
    _install_errors(app)

    @app.get("/health")
    def health():
        return engine.status()

    @app.get("/v1/models")
    def list_models():
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "kokoro"}
                                            for m in ("kokoro", "tts-1", "tts-1-hd")]}

    @app.get("/v1/audio/voices")
    def voices():
        if not engine.wait_ready(VOICES_WAIT_S):
            return _error(503, "text to speech is %s: %s" % (engine.state, engine.error or "loading"), "server_error")
        return {"voices": engine.backend.voices()}

    @app.post("/v1/audio/speech")
    def speech(req: SpeechRequest):
        fmt = (req.response_format or "wav").strip().lower()
        if fmt not in ("wav", "pcm"):
            return _error(400, "response_format must be wav or pcm here")
        text = (req.input or "").strip()
        if not text:
            return _error(400, "input is empty")
        if not engine.wait_ready(TTS_WAIT_S):
            return _error(503, "text to speech is %s: %s" % (engine.state, engine.error or "loading"), "server_error")
        names = [v.strip() for v in (req.voice or default_voice).split("+") if v.strip()]
        known = set(engine.backend.voices())
        unknown = [v for v in names if v not in known]
        if not names or unknown:
            return _error(400, "voice %r not found (GET /v1/audio/voices lists them)" % (req.voice or ""))
        speed = min(2.0, max(0.5, float(req.speed or 1.0)))
        lang = req.lang_code or LANG_BY_PREFIX.get(names[0][0], "en-us")
        t0 = time.monotonic()
        try:
            samples = engine.run(engine.backend.synthesize, text, names, speed, lang)
        except ValueError as e:                      # nothing speakable in the text (only symbols, say)
            log.info("tts: nothing to say in %d chars (%s): short silence", len(text), e)
            samples = np.zeros(KOKORO_RATE // 10, np.float32)
        except Exception as e:
            log.exception("tts: synthesis failed")
            return _error(500, "synthesis failed: %s" % e, "server_error")
        rate = getattr(engine.backend, "sample_rate", KOKORO_RATE)
        log.info("tts: %d chars -> %.2f s of audio in %.2f s (%s, speed %.2f)", len(text), len(samples) / rate,
                 time.monotonic() - t0, "+".join(names), speed)
        if fmt == "pcm":
            pcm = (np.clip(np.asarray(samples, np.float32), -1, 1) * 32767.0).astype("<i2").tobytes()
            return Response(pcm, media_type="audio/pcm")
        return Response(wav_bytes(samples, rate), media_type="audio/wav")

    return app


# ---------------------------------------------------------------- serving both on 127.0.0.1

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Talkover native speech engines (Whisper + Kokoro, no Docker).")
    p.add_argument("--stt-port", type=int, default=model_store._int_env("TALKOVER_STT_PORT", 2022),
                   help="speech to text port (TALKOVER_STT_PORT, default 2022)")
    p.add_argument("--tts-port", type=int, default=model_store._int_env("TALKOVER_TTS_PORT", 8880),
                   help="text to speech port (TALKOVER_TTS_PORT, default 8880)")
    return p.parse_args(argv)


def build_servers(stt_app, tts_app, stt_port: int, tts_port: int):
    """Two uvicorn servers bound to 127.0.0.1. They run in one event loop; signals are handled by serve_all."""
    import uvicorn

    class _Server(uvicorn.Server):
        def install_signal_handlers(self):       # uvicorn before 0.29
            pass

        @contextlib.contextmanager
        def capture_signals(self):               # uvicorn 0.29 and later
            yield

    servers = []
    for app, port in ((stt_app, stt_port), (tts_app, tts_port)):
        cfg = uvicorn.Config(app, host=BIND_HOST, port=port, log_config=None, access_log=False,
                             lifespan="off", timeout_keep_alive=30)
        servers.append(_Server(cfg))
    return servers


async def serve_all(servers) -> None:
    """Run every server until all have stopped; one failing to start (port taken) stops the others."""
    tasks = [asyncio.ensure_future(s.serve()) for s in servers]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for s in servers:
            s.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)
    for t in tasks:
        if t.exception() is not None and not isinstance(t.exception(), SystemExit):
            raise t.exception()


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)   # one "Processing audio" line per request
    if sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def main(argv=None) -> int:
    # pythonw (how the watchdog starts it) has no console: progress bars and prints must not crash on None streams
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    args = parse_args(argv)
    settings = model_store.Settings.from_env()
    setup_logging(settings.log_file)
    log.info("starting: stt %s:%d (%s, %s), tts %s:%d (kokoro-onnx, %s), models in %s", BIND_HOST, args.stt_port,
             settings.stt_backend, settings.stt_model, BIND_HOST, args.tts_port, settings.kokoro_model,
             settings.models_dir)
    stt = Engine(make_stt_backend(settings), "stt")
    tts = Engine(make_tts_backend(settings), "tts")
    stt.start()
    tts.start()
    servers = build_servers(create_stt_app(stt), create_tts_app(tts, settings.default_voice),
                            args.stt_port, args.tts_port)

    def stop(*_):
        log.info("stopping")
        for s in servers:
            s.should_exit = True
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(getattr(signal, sig), stop)
    try:
        asyncio.run(serve_all(servers))
    except SystemExit as e:
        log.error("a server could not start (port taken?)")
        return int(e.code or 1)
    started = all(getattr(s, "started", False) for s in servers)
    log.info("stopped")
    return 0 if started else 1


if __name__ == "__main__":
    sys.exit(main())
