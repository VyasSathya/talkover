"""Settings and model files for the native speech engines (engines/native/server.py, fetch_models.py).

No Docker: Whisper and Kokoro run inside one Python process. This module only decides which models are used and
where they live, and fetches them; it never imports the speech libraries, so the tests and fetch_models.py stay light.

Where things go (every path follows src/voice_config.py, which also loads config.env):
  <TALKOVER_HOME>/models/kokoro/   kokoro-v1.0.onnx and voices-v1.0.bin (the kokoro-onnx GitHub release)
  <TALKOVER_HOME>/models/whisper/  Hugging Face cache of the Whisper model (models--<org>--<name>/...)
  <TALKOVER_HOME>/logs/engines.log the server's log
TALKOVER_MODELS moves the models folder, as it does for the rest of Talkover.

Speech to text backend (TALKOVER_NATIVE_STT_BACKEND = auto | mlx | faster-whisper):
  auto = mlx-whisper (Metal) on macOS Apple Silicon when it is installed, otherwise faster-whisper (CUDA when a GPU
  works, else CPU int8). The model is TALKOVER_NATIVE_STT_MODEL, else the size of TALKOVER_WHISPER_MODEL (the Docker
  setting) in the backend's own format, else whisper small: mlx-community/whisper-small-mlx or
  Systran/faster-whisper-small. A model id of the other backend's format is translated (Systran/faster-whisper-medium
  becomes mlx-community/whisper-medium-mlx on a Mac), so install scripts can keep posting the Docker model id.
"""
import hashlib
import importlib.util
import os
import platform
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent                 # engines/native/
REPO_DIR = HERE.parent.parent
if str(REPO_DIR / "src") not in sys.path:
    sys.path.insert(0, str(REPO_DIR / "src"))
import voice_config  # noqa: E402  (loads config.env into os.environ; owns TALKOVER_HOME / TALKOVER_MODELS)

KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
# Sizes and SHA-256 of the release files (measured 2026-10-17 from model-files-v1.0, published 2025-01-28).
KOKORO_FILES = {
    "kokoro-v1.0.onnx": (325532387, "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"),
    "kokoro-v1.0.int8.onnx": (92361271, "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb"),
    "voices-v1.0.bin": (28214398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
}
KOKORO_VOICES_FILE = "voices-v1.0.bin"
DEFAULT_KOKORO_MODEL = "kokoro-v1.0.onnx"

DEFAULT_MLX_MODEL = "mlx-community/whisper-small-mlx"
DEFAULT_FW_MODEL = "Systran/faster-whisper-small"
# Whisper sizes both organisations publish under the same name.
_SIZES = {"tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en", "large-v3"}
# The files faster-whisper itself downloads (faster_whisper.utils.download_model); mlx repos are fetched whole.
FW_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")


def _int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    """Everything the native engines read from the environment (config.env included), in one place."""
    stt_port: int
    tts_port: int
    models_dir: Path
    log_file: Path
    stt_backend: str          # "mlx" or "faster-whisper" (already resolved from auto)
    stt_model: str            # Hugging Face repo id of the Whisper model, in that backend's format
    stt_device: str           # auto | cuda | cpu (faster-whisper only)
    kokoro_model: str         # file name in KOKORO_FILES
    default_voice: str

    @property
    def whisper_dir(self) -> Path:
        return self.models_dir / "whisper"

    @property
    def kokoro_dir(self) -> Path:
        return self.models_dir / "kokoro"

    @classmethod
    def from_env(cls) -> "Settings":
        backend = pick_stt_backend(os.getenv("TALKOVER_NATIVE_STT_BACKEND", "auto"))
        kokoro = os.getenv("TALKOVER_NATIVE_KOKORO_MODEL", DEFAULT_KOKORO_MODEL)
        if kokoro not in KOKORO_FILES or kokoro == KOKORO_VOICES_FILE:
            kokoro = DEFAULT_KOKORO_MODEL
        return cls(
            stt_port=_int_env("TALKOVER_STT_PORT", 2022),
            tts_port=_int_env("TALKOVER_TTS_PORT", 8880),
            models_dir=voice_config.MODELS_DIR,
            log_file=voice_config.LOG_DIR / "engines.log",
            stt_backend=backend,
            stt_model=default_stt_model(backend),
            stt_device=os.getenv("TALKOVER_NATIVE_DEVICE", "auto").strip().lower(),
            kokoro_model=kokoro,
            default_voice=os.getenv("TALKOVER_VOICE", "af_heart"),
        )


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def pick_stt_backend(choice: str = "auto") -> str:
    """auto: mlx on Apple Silicon when mlx_whisper is importable, otherwise faster-whisper."""
    choice = (choice or "auto").strip().lower().replace("_", "-")
    if choice in ("mlx", "mlx-whisper"):
        return "mlx"
    if choice in ("faster-whisper", "fw", "ctranslate2"):
        return "faster-whisper"
    if is_apple_silicon() and importlib.util.find_spec("mlx_whisper") is not None:
        return "mlx"
    return "faster-whisper"


def translate_model_id(model_id: str, backend: str) -> str:
    """The same Whisper size in the backend's own format; ids that are not a known size pass through unchanged."""
    if backend == "mlx":
        m = re.fullmatch(r"Systran/faster-whisper-(.+)", model_id)
        if m and m.group(1) in _SIZES:
            return "mlx-community/whisper-%s-mlx" % m.group(1)
    else:
        m = re.fullmatch(r"mlx-community/whisper-(.+)-mlx", model_id)
        if m and m.group(1) in _SIZES:
            return "Systran/faster-whisper-%s" % m.group(1)
    return model_id


def default_stt_model(backend: str) -> str:
    explicit = os.getenv("TALKOVER_NATIVE_STT_MODEL", "").strip()
    if explicit:
        return explicit
    docker = os.getenv("TALKOVER_WHISPER_MODEL", "").strip()
    if docker:
        translated = translate_model_id(docker, backend)
        if backend == "faster-whisper" or translated != docker:
            return translated
    return DEFAULT_MLX_MODEL if backend == "mlx" else DEFAULT_FW_MODEL


# ---------------------------------------------------------------- downloads

def _say(log, msg):
    (log or print)(msg)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_file(url: str, dest: Path, size: int, sha256: str, log=None, timeout=60) -> None:
    """Download url to dest through dest.part, resuming a .part left by an interrupted run (HTTP Range). The file
    only gets its real name after its size and SHA-256 match; a mismatch deletes the .part and raises."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    if have < size:
        req = urllib.request.Request(url, headers={"User-Agent": "talkover-native"})
        if have:
            req.add_header("Range", "bytes=%d-" % have)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if have and r.status != 206:            # the server ignored the range: start over
                have = 0
            mode = "ab" if have else "wb"
            if have:
                _say(log, "   resuming %s at %.0f MB" % (dest.name, have / 1e6))
            next_report = have + size // 10
            with open(part, mode) as f:
                while True:
                    block = r.read(1 << 20)
                    if not block:
                        break
                    f.write(block)
                    have += len(block)
                    if have >= next_report and have < size:
                        _say(log, "   %s: %d%%" % (dest.name, have * 100 // size))
                        next_report = have + size // 10
    got = part.stat().st_size
    if got != size:
        raise IOError("%s: got %d bytes, expected %d (run again to resume)" % (dest.name, got, size))
    digest = sha256_of(part)
    if digest != sha256:
        part.unlink()
        raise IOError("%s: SHA-256 %s does not match the release (%s); deleted, run again" % (dest.name, digest, sha256))
    os.replace(part, dest)


def ensure_kokoro(settings: Settings, verify: bool = False, log=None) -> dict:
    """Make sure the Kokoro model and voices are in settings.kokoro_dir. A file already there is checked by size
    (and by SHA-256 when verify); a missing or wrong one is downloaded. Returns {name: "present"|"downloaded"}."""
    done = {}
    for name in (settings.kokoro_model, KOKORO_VOICES_FILE):
        size, sha = KOKORO_FILES[name]
        path = settings.kokoro_dir / name
        if path.exists() and path.stat().st_size == size and (not verify or sha256_of(path) == sha):
            done[name] = "present"
            continue
        if path.exists():
            _say(log, "   %s is damaged or incomplete: downloading it again" % name)
            path.unlink()
        t0 = time.monotonic()
        _say(log, "   downloading %s (%.0f MB) from the kokoro-onnx release" % (name, size / 1e6))
        download_file(KOKORO_RELEASE + name, path, size, sha, log=log)
        _say(log, "   %s: downloaded in %.0f s" % (name, time.monotonic() - t0))
        done[name] = "downloaded"
    return done


def kokoro_paths(settings: Settings):
    return settings.kokoro_dir / settings.kokoro_model, settings.kokoro_dir / KOKORO_VOICES_FILE


def _required_files(model_id: str):
    """Files a usable snapshot must contain: faster-whisper needs model.bin; mlx needs weights and config."""
    if model_id.startswith("mlx-community/") or "-mlx" in model_id:
        return [["config.json"], ["weights.npz", "weights.safetensors", "model.safetensors"]]
    return [["model.bin"], ["config.json"]]


def _complete(path: Path, model_id: str) -> bool:
    return all(any((path / f).exists() for f in group) for group in _required_files(model_id))


def ensure_whisper(model_id: str, cache_dir: Path, log=None) -> tuple:
    """Make sure the Whisper model is in the Hugging Face cache under cache_dir; offline when it is already complete.
    Returns (local snapshot path, "present"|"downloaded")."""
    from huggingface_hub import snapshot_download   # imported here: only needed when a model is fetched or loaded
    allow = None if (model_id.startswith("mlx-community/") or "-mlx" in model_id) else FW_FILES
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        path = Path(snapshot_download(model_id, cache_dir=str(cache_dir), allow_patterns=allow, local_files_only=True))
        if _complete(path, model_id):
            return path, "present"
    except Exception:
        pass
    _say(log, "   downloading Whisper model %s from Hugging Face" % model_id)
    path = Path(snapshot_download(model_id, cache_dir=str(cache_dir), allow_patterns=allow))
    if not _complete(path, model_id):
        raise IOError("%s downloaded but %s is missing from it" % (model_id, _required_files(model_id)))
    return path, "downloaded"


def downloaded_whisper_models(cache_dir: Path):
    """Model ids with a snapshot in the cache (models--org--name folders)."""
    out = []
    try:
        for d in sorted(cache_dir.iterdir()):
            if d.is_dir() and d.name.startswith("models--") and any((d / "snapshots").glob("*")):
                out.append(d.name[len("models--"):].replace("--", "/"))
    except OSError:
        pass
    return out


def folder_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total / 1e6
