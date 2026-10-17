# Talkover

A hands-free, two-way voice loop for Claude Code on Windows. Fully local.

![Talkover: you ask, Claude answers out loud, you cut in with "wait", and Claude picks up where it was cut off](docs/demo.gif)

The full 60-second demo with sound is on the [v0.1.0 release](https://github.com/VyasSathya/talkover/releases/tag/v0.1.0).
Both voices in it are synthetic (Kokoro); nobody's real voice is used. Claude's replies and the edit are real output of
a Claude Code agent working in a copy of this repo, the panel is drawn by Talkover's own `voice_panel.py`, and the
waits for Claude are shortened. An agent made the video end to end, with no human step.

- **Always-on mic.** No hotkey, no wake word. Talk whenever you want; each thing you say is transcribed and sent to
  Claude Code as a normal message, whether Claude is idle, working, or talking.
- **Spoken replies.** Claude's replies (and what it writes before each tool call) are read aloud as they arrive.
- **Talk over it.** Start talking and the reading pauses. Say "stop" or "wait" and it ends. Say "next", "back" or
  "again" to move between replies. If your words cut a reading, Claude is told where you cut in.
- **Local.** Whisper (speech to text) and Kokoro (text to speech) run in Docker on your machine, bound to 127.0.0.1.
  No audio leaves the computer and nothing costs money.
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
is spoken without Claude having to call a tool. It is Windows-first and adds a spoken stop, barge-in that pauses and
resumes the reading, and a floating control panel. The two can run side by side (see `extras/voicemode-mcp`).

## Requirements

- Windows 10 or 11, 64-bit
- The **Claude desktop app** with the Code tab (the mic types into the window titled "Claude" through UI
  Automation, without stealing focus). Spoken replies work for any Claude Code session, CLI included, because they
  come from Claude Code hooks.
- Python 3.11 or newer from python.org (`winget install Python.Python.3.13`). The Microsoft Store build cannot
  open the microphone, so the installer skips it.
- Docker Desktop (about 8 GB of images on first install; an NVIDIA GPU is optional and makes Whisper much faster)
- A microphone. A headset works best; with speakers, the echo of the reading is filtered out of what the mic hears.

## Install

```powershell
git clone https://github.com/VyasSathya/talkover
cd talkover
powershell -ExecutionPolicy Bypass -File .\install.ps1          # add -Gpu for an NVIDIA GPU, -Autostart to start at logon
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

`install.ps1` creates `.venv`, installs `requirements.txt`, starts the two engines with `docker compose`, downloads
the Whisper model, adds the two speak hooks to `~/.claude/settings.json` (with a backup; `-SkipHooks` leaves it
alone), and runs a health check that has Kokoro say a sentence and Whisper transcribe it.

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

Everything is in `config.env` (created from `config.example.env`): engine ports, the data folder, the Kokoro voice
and speed, how much silence ends an utterance. Environment variables override the file.

Logs: `~/.talkover/logs/`. Health check: `.venv\Scripts\python.exe src\voice_health.py`.

Stop everything: `powershell -ExecutionPolicy Bypass -File .\stop.ps1` (add `-Engines` to stop Docker too).
Remove the hooks: `.venv\Scripts\python.exe src\install_hooks.py --remove`.

## How it works

```
mic -> voice_inbox.py (webrtcvad + pitch, endpointing) -> Whisper :2022 -> typed into the Claude window (uia_deliver.py)
Claude reply -> Stop / PreToolUse hook -> hooks/speak-response.ps1 -> Kokoro :8880 -> speakers
```

The pieces talk through small files in `%TEMP%` (`claude-speak.*`, `claude-voice.*`): the inbox pauses or holds the
reading while you talk, and the hook publishes what it is reading so the inbox can remove the reading's own words
from what the mic heard. `voice_watchdog.py` restarts an engine that stops answering and relaunches the programs after
sleep. `src/stop_word_listener.py` and `extras/voicemode-mcp/` are optional add-ons for the VoiceMode MCP server's
`converse` tool and are not needed for the floater.

## Limits

- Windows only (Win32 windows, UI Automation, named mutexes). What a Mac and Linux port would take:
  [docs/cross-platform.md](docs/cross-platform.md).
- The mic side drives the Claude desktop app's UI. An app update that renames the window or the Send button can break
  delivery until `uia_deliver.py` is updated.
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
