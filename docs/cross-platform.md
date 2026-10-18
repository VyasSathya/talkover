# Mac and Linux: what a port would take

Status (2026-10-17): built. macOS and Linux support landed (see the Platforms table in the README); macOS is beta until
the real-device check in MAC-CHECK.md. What follows is the plan it was built from, kept for the reasoning.

## Why it matters

Most Claude Code users are on macOS, and a large share run the CLI on Linux. Windows-only cuts the audience for a
launch to a minority. The speech engines (Docker) and the core audio logic are already portable; the parts that
are not are the edges: how text gets into Claude, how replies are played, the panel, and process plumbing.

## Piece by piece

| Piece | Today (Windows) | Mac / Linux | Size |
|---|---|---|---|
| Mic loop, VAD, endpointing, echo removal (`voice_inbox.py` core, about 2,300 of 2,667 lines) | numpy, sounddevice, webrtcvad | same libraries work as is | small: swap `%TEMP%` for `tempfile.gettempdir()`, named mutexes for file locks |
| Delivery into Claude (`uia_deliver.py`) | UI Automation into the Claude desktop window, no focus change | **CLI in tmux: `tmux send-keys -t <pane> -l "<text>"` then `Enter`** works the same on Mac and Linux and is the simplest robust path. Claude desktop on Mac: Accessibility API (pyobjc `AXUIElement`) or AppleScript keystrokes, needs the Accessibility permission | medium: a delivery interface with tmux, mac-desktop and windows backends |
| Which chat is on screen (`voice_chats.py`, view mode) | reads the desktop app's sidebar through UIA | tmux backend: the target pane is the chat, nothing to detect. Mac desktop: AX tree, path `~/Library/Application Support/Claude` | small for tmux, medium for Mac desktop |
| Spoken replies (`hooks/speak-response.ps1`, 620 lines) | PowerShell, System.Media.SoundPlayer, .NET mutexes | rewrite in Python (the hook command becomes `python hooks/speak_response.py`), play with sounddevice, locks with `fcntl` / `portalocker`. Same %TEMP% file protocol | medium-large: the biggest single job, but the logic is already specified line by line in the PowerShell |
| Floating panel (`voice_panel.py`, 1,563 lines) | Win32 layered window via ctypes, PIL rendering, pystray tray | Mac: a menu bar app (`rumps`) or a small always-on-top Tk/Qt window; Linux: Tk or a tray icon. The panel is optional; the loop works without it | medium; ship v1 without it (voice commands cover stop / next / back / mute) |
| Watchdog, health, start/stop scripts | ctypes mutexes, PowerShell scripts | Python + shell scripts, `launchd` plist / `systemd --user` unit for autostart | small |
| Engines (`docker-compose.yml`) | speaches CPU/CUDA, kokoro-fastapi CPU | work on Linux as is. On Apple Silicon Docker has no GPU, so Whisper is slow-ish on CPU; a native option (whisper.cpp / mlx-whisper server, Kokoro via mlx or onnx) is faster and avoids Docker | small to keep Docker; medium for native Mac engines |

## Recommended order if he says yes

1. **Mac + Linux CLI via tmux** (no panel): portable inbox, tmux delivery backend, Python speak hook, file locks,
   shell start script. This reaches the most Claude Code users with the least new surface. Rough size: 2 to 4 focused
   sessions, most of it the Python port of the speak hook and testing barge-in timing on Mac audio.
2. Mac desktop app delivery through Accessibility, and a menu bar panel.
3. Native Apple Silicon engines as an option.

## Risks

- Barge-in and echo removal were tuned on one Windows machine and headset; Mac built-in mics and speakers will need
  retuning (the echo gate and onset guards).
- Typing into a terminal pane means the voice can only reach CLI sessions running inside tmux; that has to be
  stated up front.
- No Mac hardware here to test on: a port needs a Mac (or a tester) before it is announced.
