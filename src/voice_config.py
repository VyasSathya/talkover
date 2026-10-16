"""Paths and settings shared by every piece of Talkover.

Each value comes from the environment first, then from config.env at the repo root (KEY=VALUE lines, # comments),
then from the default below. Importing this module loads config.env into os.environ (without overriding anything
already set), so the speak hook and every script see the same values.
"""
import os
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent          # src/
REPO_DIR = CODE_DIR.parent


def _load_env_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(Path(os.getenv("TALKOVER_CONFIG", str(REPO_DIR / "config.env"))))


def _path(name: str, default: Path) -> Path:
    v = os.getenv(name)
    return Path(os.path.expandvars(os.path.expanduser(v))) if v else default


# Logs, learned state and the stop-word model live here (never in the repo).
DATA_DIR = _path("TALKOVER_HOME", Path.home() / ".talkover")
LOG_DIR = DATA_DIR / "logs"
MODELS_DIR = _path("TALKOVER_MODELS", DATA_DIR / "models")

# The Claude Code hooks (speak-response.ps1 reads replies aloud; stop-speaking.ps1 cuts it off).
HOOKS_DIR = _path("TALKOVER_HOOKS", REPO_DIR / "hooks")
SPEAK_HOOK = HOOKS_DIR / "speak-response.ps1"
STOP_SCRIPT = HOOKS_DIR / "stop-speaking.ps1"

# Local engines (docker-compose.yml): Whisper (speaches) for speech to text, Kokoro for text to speech.
STT_BASE = os.getenv("TALKOVER_STT_BASE", "http://127.0.0.1:" + os.getenv("TALKOVER_STT_PORT", "2022")).rstrip("/")
TTS_BASE = os.getenv("TALKOVER_TTS_BASE", "http://127.0.0.1:" + os.getenv("TALKOVER_TTS_PORT", "8880")).rstrip("/")
STT_URL = STT_BASE + "/v1/audio/transcriptions"
STT_HEALTH = STT_BASE + "/health"
TTS_URL = TTS_BASE + "/v1/audio/speech"
WHISPER_CONTAINER = os.getenv("TALKOVER_WHISPER_CONTAINER", "talkover-whisper")
KOKORO_CONTAINER = os.getenv("TALKOVER_KOKORO_CONTAINER", "talkover-kokoro")


def pythonw() -> str:
    """The windowless interpreter next to the one running now (the venv's pythonw.exe)."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


# UDP port where voice_inbox.py sends mic levels for the panel's orb.
LEVEL_PORT = int(os.getenv("TALKOVER_LEVEL_PORT", "42712"))

# Named mutexes keep one copy of each program running ("Local\<prefix><program>").
MUTEX_PREFIX = os.getenv("TALKOVER_MUTEX_PREFIX", "Talkover_")
