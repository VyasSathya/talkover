"""Cross-platform pieces that need no microphone, speakers or screen: locks, delivery, hook install, inbox text logic."""
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


def test_single_instance_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("TALKOVER_TEMP", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    import platform_util
    importlib.reload(platform_util)
    name = "test_%s" % uuid.uuid4().hex[:8]
    first = platform_util.single_instance(name, "TkTest_")
    assert first is not None
    assert platform_util.instance_running(name, "TkTest_")
    code = ("import sys; sys.path.insert(0, %r); import platform_util; "
            "print('none' if platform_util.single_instance(%r, 'TkTest_') is None else 'got')") % (str(SRC), name)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                         env=dict(os.environ)).stdout.strip()
    assert out == "none"          # a second process cannot take it


def test_speak_hook_command_per_os():
    import platform_util
    cmd = platform_util.speak_hook_command("-Mode", "replay", "-Nav", "back")
    if sys.platform == "win32":
        assert cmd[0] == "powershell" and cmd[-5].endswith("speak-response.ps1")
    else:
        assert cmd[0] == sys.executable and cmd[1].endswith("speak_response.py")
    assert cmd[-4:] == ["-Mode", "replay", "-Nav", "back"]


def test_install_hooks_per_os(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other.sh"}]}]}}))
    run = lambda *a: subprocess.run([sys.executable, str(SRC / "install_hooks.py"), "--settings", str(settings), *a],
                                    capture_output=True, text=True, timeout=60)
    assert run().returncode == 0
    assert run().returncode == 0                  # idempotent
    d = json.loads(settings.read_text())
    cmds = [h["command"] for ev in ("Stop", "PreToolUse") for g in d["hooks"][ev] for h in g["hooks"]]
    want = "speak-response.ps1" if sys.platform == "win32" else "speak_response.py"
    assert sum(want in c for c in cmds) == 2
    assert "other.sh" in cmds                      # other hooks are kept
    assert any(c.endswith("-Mode transcript") for c in cmds)
    assert run("--remove").returncode == 0
    d = json.loads(settings.read_text())
    assert [h["command"] for g in d["hooks"]["Stop"] for h in g["hooks"]] == ["other.sh"]


def test_install_hooks_refuses_other_copy(tmp_path):
    settings = tmp_path / "settings.json"
    other = "powershell -File C:/elsewhere/speak-response.ps1" if sys.platform == "win32" else "python3 /elsewhere/speak_response.py"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": other}]}]}}))
    r = subprocess.run([sys.executable, str(SRC / "install_hooks.py"), "--settings", str(settings)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert other in settings.read_text()


def test_config_engines_default(monkeypatch):
    monkeypatch.delenv("TALKOVER_ENGINES", raising=False)
    monkeypatch.setenv("TALKOVER_CONFIG", str(ROOT / "config.example.env"))
    import voice_config
    importlib.reload(voice_config)
    assert voice_config.ENGINES in ("docker", "native")
    assert voice_config.STT_URL.endswith("/v1/audio/transcriptions")


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("tmux") is None, reason="needs tmux (macOS / Linux)")
def test_tmux_delivery(tmp_path):
    """Stage two parts, send once: the pane receives one line with both, then Enter."""
    sock = "tkotest%d" % os.getpid()
    out = tmp_path / "typed.txt"
    env = dict(os.environ, TMUX="")
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "t", "cat > %s" % out], check=True, env=env)
    try:
        import delivery_posix

        class SockTmux(delivery_posix.Tmux):
            def _keys(self, *args):
                r = subprocess.run(["tmux", "-L", sock, "send-keys", "-t", "t", *args], capture_output=True, text=True)
                return r.returncode == 0
        sink = delivery_posix.Sink(SockTmux(target="t"))
        assert sink.stage("hello there", True) == "keys"
        assert sink.stage("and more", False) == "keys"
        assert sink.send(["hello there", "and more", "a late part"], ["keys", "keys", ""], "[voice: note]")
        time.sleep(0.5)
        assert out.read_text().strip() == "hello there and more a late part [voice: note]"
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], env=env)


def test_delivery_choice_env(monkeypatch):
    import delivery_posix
    if sys.platform == "win32":
        assert delivery_posix.choose() is None
        return
    monkeypatch.setenv("TALKOVER_DELIVERY", "tmux")
    assert isinstance(delivery_posix.choose(), delivery_posix.Tmux)
    monkeypatch.setenv("TALKOVER_DELIVERY", "macos-app")
    monkeypatch.setenv("TALKOVER_MAC_APP", "iTerm2")
    b = delivery_posix.choose()
    assert isinstance(b, delivery_posix.MacApp) and b.app == "iTerm2"


def _inbox():
    pytest.importorskip("sounddevice", reason="needs PortAudio")
    pytest.importorskip("webrtcvad", reason="needs webrtcvad-wheels")
    import voice_inbox
    return voice_inbox


def test_inbox_commands():
    vi = _inbox()
    assert vi.nav_command("next") == "next"
    assert vi.nav_command("okay, go back") == "back"
    assert vi.nav_command("say that again") == "again"
    assert vi.nav_command("I think the next step is tests") == ""
    assert vi.mute_command("stop listening") == "mute"
    assert vi.mute_command("uh start listening") == "unmute"
    assert vi.starts_with_stop("no, stop, that is the wrong folder")
    assert not vi.starts_with_stop("please keep going")


def test_inbox_phantoms_and_reading_removal():
    vi = _inbox()
    assert vi.is_phantom("Thank you. Bye. Bye.")
    assert not vi.is_phantom("Thank you, now run the tests.")
    reading = ["It installs the Python packages and starts the two speech engines."]
    mine, removed = vi.strip_reading("It installs the Python packages wait stop that", reading)
    assert removed >= 4 and "wait" in mine.lower()
