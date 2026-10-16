#!/usr/bin/env python3
"""
Patch voice_mode/control_socket.py so the control channel works on Windows.

Upstream (voice-mode 8.12.0) binds a Unix domain socket, which CPython on Windows
does not expose (socket.AF_UNIX is missing), and calls os.getuid(), which does not
exist on Windows either. This patch keeps the Unix path untouched and adds a
loopback-TCP fallback (127.0.0.1:$VOICEMODE_CONTROL_PORT, default 42711) used only
when AF_UNIX is unavailable. Loopback-only bind + SO_EXCLUSIVEADDRUSE stands in for
the peer-UID check, which returns None (fail-open, as upstream documents).

Idempotent. Re-run after a voice-mode upgrade:
    <voice-mode venv>\\Scripts\\python.exe extras\\voicemode-mcp\\windows_control_tcp.py
"""
import re
import sys
from pathlib import Path

import importlib.util

# Run with the Python that has voice-mode installed; its control_socket.py is patched in place.
_spec = importlib.util.find_spec("voice_mode")
if _spec is None or not _spec.submodule_search_locations:
    sys.exit("voice-mode is not installed in this Python (pip install voice-mode)")
TARGET = Path(list(_spec.submodule_search_locations)[0]) / "control_socket.py"
MARK = "# --- windows-loopback-tcp patch ---"

src = TARGET.read_text(encoding="utf-8")
if MARK in src:
    print("already patched")
    sys.exit(0)

helpers = f'''
{MARK}
# CPython on Windows has no AF_UNIX. Fall back to a loopback TCP port so the
# same JSON-line protocol (and `voicemode control ...`, Stream Deck, keyword
# listeners) keeps working. Unix platforms are unaffected.
_HAS_AF_UNIX = hasattr(socket, "AF_UNIX")


def _tcp_port() -> int:
    return int(os.getenv("VOICEMODE_CONTROL_PORT", "42711"))


def _getuid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _connect_control(path, timeout: float) -> socket.socket:
    """Client-side connect: Unix socket where available, else loopback TCP."""
    if _HAS_AF_UNIX:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(str(path))
        return client
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(("127.0.0.1", _tcp_port()))
    return client
# --- end windows-loopback-tcp patch ---
'''

# 1. helpers after _MAX_HANDLERS line
src, n = re.subn(r"(\n_MAX_HANDLERS = \d+\n)", r"\1" + helpers, src, count=1)
assert n == 1, "anchor _MAX_HANDLERS not found"

# 2. os.getuid() -> _getuid()
src = src.replace("os.getuid()", "_getuid()")

# 3. server bind: TCP branch at the top of _bind
old_bind = '''        path = self._socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(path.parent), 0o700)'''
new_bind = '''        path = self._socket_path
        if not _HAS_AF_UNIX:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                server.bind(("127.0.0.1", _tcp_port()))
                server.listen(8)
                server.settimeout(_POLL_TIMEOUT)
            except OSError:
                server.close()
                raise
            return server
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(path.parent), 0o700)'''
assert old_bind in src, "bind anchor not found"
src = src.replace(old_bind, new_bind, 1)

# 4. _unlink_socket: nothing to unlink on TCP
old_unlink = '''        path = str(self._socket_path)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return'''
new_unlink = '''        if not _HAS_AF_UNIX:
            return
        path = str(self._socket_path)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return'''
assert old_unlink in src, "unlink anchor not found"
src = src.replace(old_unlink, new_unlink, 1)

# 5. client connects (send_control_command + query_status)
old_client = '''    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
'''
new_client = '''    with _connect_control(path, timeout) as client:
'''
count = src.count(old_client)
assert count == 2, f"expected 2 client connect blocks, found {count}"
src = src.replace(old_client, new_client)

TARGET.write_text(src, encoding="utf-8")
print("patched", TARGET)
