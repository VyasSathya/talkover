"""What differs between Windows, macOS and Linux, in one place.

Windows keeps exactly the behavior it always had (named mutexes, %TEMP%, PowerShell hooks, hidden processes).
macOS and Linux get the POSIX equivalents (lock files, the system temp folder, the Python speak hook, new sessions).
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# The control files the pieces share (claude-speak.*, claude-voice.*). Windows has always used %TEMP%.
TEMP_DIR = Path(os.getenv("TEMP", "")) if IS_WINDOWS else Path(os.getenv("TALKOVER_TEMP") or tempfile.gettempdir())


def temp(name: str) -> Path:
    return TEMP_DIR / name


_HELD = []   # lock handles must live as long as the process


def single_instance(name: str, prefix: str = None):
    """A handle if this is the only running copy of `name`, None if another copy holds it.
    Windows: a named mutex (Local\\<prefix><name>). POSIX: an exclusive flock on a file in the temp folder."""
    if prefix is None:
        prefix = os.getenv("TALKOVER_MUTEX_PREFIX", "Talkover_")
    if IS_WINDOWS:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        h = k32.CreateMutexW(None, False, "Local\\" + prefix + name)
        if not h or ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            return None
        return h
    import fcntl
    path = TEMP_DIR / ("%s%s.lock" % (prefix, name))
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _HELD.append(f)
    return f


def instance_running(name: str, prefix: str = None) -> bool:
    """True if some process holds the single-instance lock for `name` (used by the health check)."""
    if prefix is None:
        prefix = os.getenv("TALKOVER_MUTEX_PREFIX", "Talkover_")
    if IS_WINDOWS:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenMutexW.restype = ctypes.c_void_p
        h = k32.OpenMutexW(0x00100000, False, "Local\\" + prefix + name)   # SYNCHRONIZE
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
            return True
        return False
    import fcntl
    path = TEMP_DIR / ("%s%s.lock" % (prefix, name))
    if not path.exists():
        return False
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def spawn_detached(args, cwd=None):
    """Start a background process with no window that outlives its parent."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if cwd:
        kw["cwd"] = str(cwd)
    if IS_WINDOWS:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    return subprocess.Popen([str(a) for a in args], **kw)


def speak_hook_command(*args):
    """The command that runs the speak hook with these arguments (-Mode held, -Mode replay -Nav back, ...).
    Windows: the PowerShell hook, as always. macOS / Linux: hooks/speak_response.py with the same arguments."""
    from voice_config import HOOKS_DIR
    if IS_WINDOWS:
        return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                "-File", str(HOOKS_DIR / "speak-response.ps1"), *args]
    return [sys.executable, str(HOOKS_DIR / "speak_response.py"), *args]


def run_speak_hook(*args):
    """Start the speak hook in the background (replay, held reply)."""
    if IS_WINDOWS:
        return subprocess.Popen(speak_hook_command(*args), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                close_fds=True)
    return spawn_detached(speak_hook_command(*args))
