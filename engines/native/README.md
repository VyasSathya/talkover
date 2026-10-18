# Native speech engines (no Docker)

`server.py` runs Whisper (speech to text) and Kokoro (text to speech) in one Python process. It answers the same
requests as the two Docker containers in `docker-compose.yml` (speaches and kokoro-fastapi) on the same ports, bound to
127.0.0.1 only, so the rest of Talkover does not change.

| Engine | Port | Backend |
|---|---|---|
| Speech to text | `TALKOVER_STT_PORT` (2022) | mlx-whisper on Apple Silicon (Metal); elsewhere faster-whisper, CUDA float16 when a GPU works, else CPU int8 |
| Text to speech | `TALKOVER_TTS_PORT` (8880) | kokoro-onnx (Kokoro-82M on onnxruntime), 24 kHz mono 16-bit WAV |

Endpoints: `GET /health` (both), `GET /v1/models`, `POST /v1/models/{id}`, `POST /v1/audio/transcriptions`,
`GET /v1/audio/voices`, `POST /v1/audio/speech`. The docstring at the top of `server.py` has the details.

## How the installers use it

With `TALKOVER_ENGINES=native` in `config.env`, `install.sh` / `install.ps1` skip Docker and instead:

1. `pip install -r requirements-native.txt` into the Talkover venv (Python 3.11 to 3.13);
2. `python engines/native/fetch_models.py` (downloads the models; safe to run again, interrupted downloads resume);
3. start `python engines/native/server.py` in the background and wait for `GET /health` on the STT port and
   `GET /v1/audio/voices` on the TTS port (that one answers 503 until Kokoro is loaded).

After that `voice_watchdog.py` keeps it running: it starts `server.py` with no arguments from the repo root, so every
setting comes from the environment or `config.env`.

## Running it by hand

```
python engines/native/fetch_models.py
python engines/native/server.py                       # ports from config.env, else 2022 / 8880
python engines/native/server.py --stt-port 12122 --tts-port 18980
python src/voice_health.py --engines-only             # the round trip: Kokoro speaks, Whisper must hear it
```

Settings (environment or `config.env`; empty means default):

| Setting | Default | |
|---|---|---|
| `TALKOVER_NATIVE_STT_BACKEND` | `auto` | `mlx` or `faster-whisper` to force one |
| `TALKOVER_NATIVE_STT_MODEL` | whisper small | any Hugging Face repo in the backend's format; else the size of `TALKOVER_WHISPER_MODEL` |
| `TALKOVER_NATIVE_DEVICE` | `auto` | faster-whisper only: `cuda` or `cpu` |
| `TALKOVER_NATIVE_THREADS` | half the cores, 4 to 8 | faster-whisper CPU threads |
| `TALKOVER_NATIVE_KOKORO_MODEL` | `kokoro-v1.0.onnx` | `kokoro-v1.0.int8.onnx` is smaller and faster on weak CPUs |
| `TALKOVER_VOICE` | `af_heart` | the voice used when a request names none |

## Models: sizes and where they go

Under `<TALKOVER_HOME>/models` (default `~/.talkover/models`; `TALKOVER_MODELS` moves it):

| File | Size | Folder |
|---|---|---|
| `kokoro-v1.0.onnx` (or `kokoro-v1.0.int8.onnx`) | 326 MB (92 MB) | `kokoro/` |
| `voices-v1.0.bin` (54 voices) | 28 MB | `kokoro/` |
| `Systran/faster-whisper-small` | 486 MB | `whisper/` (Hugging Face cache layout) |
| `mlx-community/whisper-small-mlx` (Apple Silicon) | about 480 MB | `whisper/` |

The Kokoro files come from the kokoro-onnx GitHub release `model-files-v1.0` and are checked against its sizes and
SHA-256 (in `model_store.py`). The log is `<TALKOVER_HOME>/logs/engines.log`.
