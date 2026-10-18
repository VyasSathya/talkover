# Talkover

A hands-free, two-way voice loop for Claude Code. Windows, with macOS and Linux in beta. Fully local.

![Talkover: always listening, it types what you say, it talks back, say "wait" and it stops, it picks up where you cut in, fully local](docs/demo.gif)

Watch the 20-second [launch film](https://github.com/VyasSathya/talkover/releases/download/v0.1.0/talkover-launch-16x9.mp4)
([vertical](https://github.com/VyasSathya/talkover/releases/download/v0.1.0/talkover-launch-9x16.mp4)). There is no
voice in it: the floating panel is drawn by Talkover's own `voice_panel.py`, driven by a demo session in which the
"You:" lines are what Whisper heard from a synthetic Kokoro voice and Claude's lines are a real reply from a Claude
Code agent working in a copy of this repo, with the waits shortened. The music is made locally. The original
60-second demo, with both synthetic voices, is on the same [v0.1.0 release](https://github.com/VyasSathya/talkover/releases/tag/v0.1.0).
An agent made both films end to end ([video-studio](https://github.com/VyasSathya/video-studio)).

- **Always-on mic.** No hotkey, no wake word. Talk whenever you want; each thing you say is transcribed and sent to
  Claude Code as a normal message, whether Claude is idle, working, or talking.
- **Spoken replies.** Claude's replies (and what it writes before each tool call) are read aloud as they arrive.
- **Talk over it.** Start talking and the reading pauses. Say "stop" or "wait" and it ends. Say "next", "back" or
  "again" to move between replies. If your words cut a reading, Claude is told where you cut in.
- **Local.** Whisper (speech to text) and Kokoro (text to speech) run on your machine, in Docker or natively (Metal
  on Apple Silicon), bound to 127.0.0.1. No audio leaves the computer and nothing costs money.
- **A small floating panel** that stays on top without taking focus: mic on/off, voice on/off, stop, next, back,
  and a picker for which Claude Code chat the voice follows.

### How it differs from Claude Code's built-in `/voice`

Claude Code's `/voice` is dictation: hold Space (or tap) to talk, the words go into the prompt, and the audio is
transcribed on Anthropic's servers. It does not read replies aloud and is not available in the desktop app. This
project is the other half and the hands-free part: the mic is always open, replies are spoken back, and you can interrupt the
voice the way you would interrupt a person. It is meant for long sessions away from the keyboard (cooking, pacing,
another app or game in front).

### And from VoiceMode

[VoiceMode](https://github.com/mbailey/voicemode) (MIT) is an MCP server that gives Claude a `converse` tool: Claude
decides when to speak and when to listen, turn by turn. This project does not use MCP. The mic is a separate always-on
daemon, so you can talk while Claude is working or talking, and replies are read by Claude Code hooks, so every reply
is spoken without Claude having to call a tool. It started on Windows and adds a spoken stop, barge-in that pauses and
resumes the reading, and a floating control panel. The two can run side by side (see `extras/voicemode-mcp`).

## Platforms

| | Windows 10 / 11 | macOS 13+ (beta) | Linux (beta) |
|---|---|---|---|
| Your words go into | the Claude desktop app's Code tab (UI Automation, no focus change) | a tmux pane running `claude`, or an app (Terminal, iTerm2, Ghostty, Claude) by paste | a tmux pane running `claude` |
| Replies read aloud | any Claude Code session (hooks) | any Claude Code session (hooks) | any Claude Code session (hooks) |
| Engines | Docker | native (no Docker; Whisper on Metal) or Docker | Docker or native |
| Floating panel | yes, with a tray icon | yes | yes (dark backdrop, no transparency) |

macOS and Linux pass CI (GitHub Actions on `macos-latest` and `ubuntu-latest`); macOS is marked beta until it has been
checked on a real Mac with a microphone and speakers ([MAC-CHECK.md](MAC-CHECK.md)).

## Requirements

- **Windows:** the Claude desktop app with the Code tab; Python 3.11+ from python.org (`winget install
  Python.Python.3.13`; the Microsoft Store build cannot open the microphone, so the installer skips it); Docker
  Desktop (about 8 GB of images on first install; an NVIDIA GPU is optional and makes Whisper much faster).
- **macOS:** Python 3.11+ (`brew install python@3.13 python-tk@3.13`) and tmux (`brew install tmux`). On Apple Silicon
  the installer uses the native engines (about 800 MB of models, no Docker).
- **Linux:** Python 3.11+ with venv and tkinter, PortAudio and tmux (`sudo apt install python3-venv python3-tk
  libportaudio2 tmux`), and Docker or the native engines.
- A microphone. A headset works best; with speakers, the echo of the reading is filtered out of what the mic hears.

## Install

**Windows**

```powershell
git clone https://github.com/VyasSathya/talkover
cd talkover
powershell -ExecutionPolicy Bypass -File .\install.ps1          # add -Gpu for an NVIDIA GPU, -Autostart to start at logon
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

**macOS and Linux**

```bash
git clone https://github.com/VyasSathya/talkover
cd talkover
./install.sh          # --native or --docker to choose the engines, --autostart to start at login
tmux new -s work      # run claude inside tmux; Talkover types into that pane
./start.sh            # from another terminal tab
```

The installers create `.venv`, install the requirements, set up the engines (Docker containers, or the native engines
and their models), add the two speak hooks to `~/.claude/settings.json` (with a backup; `-SkipHooks` /
`--skip-hooks` leaves it alone), and run a health check that has Kokoro say a sentence and Whisper transcribe it.

macOS asks for the **Microphone** permission for the terminal app you start Talkover from the first time it listens:
allow it. The **Accessibility** permission is needed only for the paste mode (`TALKOVER_DELIVERY=macos-app`, used
when there is no tmux): System Settings > Privacy & Security > Accessibility, then turn on that terminal app.

Restart Claude Code (or open a new chat) after installing so it picks up the hooks.

## Use

| Say | What happens |
|---|---|
| anything | typed into Claude Code and sent when you pause |
| "stop", "wait", "hold on" | ends the reading |
| "next" / "back" / "again" | next reply, previous reply, this reply from the start |
| "stop listening" / "start listening" | mutes and unmutes the mic |

The panel: the orb shows the state (idle, you talking, thinking, Claude talking). The buttons turn the whole loop,
the mic, or the voice on and off, and stop / skip / go back. The chat list pins the voice to one Claude Code chat;
Auto follows the chat on screen and only reads replies from it.

## Configure

Everything is in `config.env` (created from `config.example.env`): the engines (docker or native) and their ports,
how your words reach Claude on macOS / Linux (`TALKOVER_DELIVERY`, `TALKOVER_TMUX_TARGET`, `TALKOVER_MAC_APP`), the data
folder, the Kokoro voice and speed, how much silence ends an utterance. Environment variables override the file.

Logs: `~/.talkover/logs/`. Health check: `.venv\Scripts\python.exe src\voice_health.py` (Windows) or
`.venv/bin/python src/voice_health.py`.

Stop everything: `stop.ps1` / `./stop.sh` (add `-Engines` / `--engines` to stop the engines too).
Remove the hooks: `src/install_hooks.py --remove` with the venv's Python.

## How it works

```
mic -> voice_inbox.py (webrtcvad + pitch, endpointing) -> Whisper :2022 -> Claude Code
         Windows: typed into the Claude window (uia_deliver.py)   macOS / Linux: tmux pane or app (delivery_posix.py)
Claude reply -> Stop / PreToolUse hook -> speak-response.ps1 (Windows) / speak_response.py -> Kokoro :8880 -> speakers
```

The pieces talk through small files in the temp folder (`claude-speak.*`, `claude-voice.*`): the inbox pauses or holds the
reading while you talk, and the hook publishes what it is reading so the inbox can remove the reading's own words
from what the mic heard. `voice_watchdog.py` restarts an engine that stops answering and relaunches the programs after
sleep. `engines/native/` is the Docker-free engine server. `src/platform_util.py` holds what differs between systems. `src/stop_word_listener.py` and `extras/voicemode-mcp/` are optional add-ons for the VoiceMode MCP server's
`converse` tool and are not needed for the floater.

## Limits

- macOS and Linux are beta: they pass CI, and the macOS real-device check is [MAC-CHECK.md](MAC-CHECK.md). On those
  systems the voice reaches Claude Code through tmux or by pasting into an app; typing into the Claude desktop app
  without taking focus is Windows-only for now, and so is the chat picker.
- On Windows the mic side drives the Claude desktop app's UI. An app update that renames the window or the Send button
  can break delivery until `uia_deliver.py` is updated.
- The barge-in thresholds were tuned on one Windows headset; built-in laptop mics and speakers may need tuning.
- English by default (`language=en` in the Whisper request).

## Make a demo video

`powershell -ExecutionPolicy Bypass -File scripts\demo\demo.ps1` records the screen, your mic and the spoken replies
for 75 seconds while you talk to Claude, then cuts it to 30 to 60 seconds on its own: waits fast-forwarded, the camera
following the input box and the reply, word-by-word captions, the panel picture-in-picture, title and end cards
(Remotion). Output: `demo-out\take-...\demo.mp4` and `demo.gif`. `-Fixture` renders a drawn test take instead,
`-Synthetic` has a Kokoro voice play "you" (labeled synthetic in the video), `-Music file.mp3` adds a ducked music bed.
Needs ffmpeg and Node.js 18+.

## Acknowledgements

The local engines are [speaches](https://github.com/speaches-ai/speaches) (faster-whisper) and
[Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI). The engine setup follows
[VoiceMode](https://github.com/mbailey/voicemode)'s local Whisper and Kokoro defaults.

## License

Not affiliated with or endorsed by Anthropic. Claude and Claude Code are trademarks of Anthropic.

License: [MIT](LICENSE).
