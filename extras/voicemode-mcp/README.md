# Optional: VoiceMode MCP add-ons

Not needed for the floater. These are for people who also use the [VoiceMode](https://github.com/mbailey/voicemode)
MCP server's `converse` tool on Windows.

- `windows_control_tcp.py` patches VoiceMode 8.12's control channel to use loopback TCP on Windows (no AF_UNIX there).
  It contains short excerpts of VoiceMode's `control_socket.py` as patch anchors; VoiceMode is MIT licensed,
  Copyright (c) 2026 Mike Bailey, see `LICENSE-voicemode`.
- `../../src/stop_word_listener.py` listens for a spoken "stop" (Vosk) and sends `skip_forward` to that channel.
  Enable it with `TALKOVER_STOP_WORD=1` and `install.ps1 -StopWord`.
