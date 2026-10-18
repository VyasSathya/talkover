# Checking Talkover on a real Mac

macOS support is built and passes CI on `macos-latest`, but CI has no microphone, speakers or screen. This check
covers what CI cannot. It takes about two minutes on an Apple Silicon Mac. A Claude Code session on the Mac can run
it too.

## Run

```bash
git clone https://github.com/VyasSathya/talkover && cd talkover && ./install.sh && ./scripts/mac_check.sh
```

`install.sh` picks the native engines on Apple Silicon (no Docker; Whisper on Metal, Kokoro in Python) and downloads
about 800 MB of models the first time. macOS asks for the **Microphone** permission for your terminal app during the
mic step: allow it.

## What to look for

| Step | Pass |
|---|---|
| health check | `RESULT: all OK`; the round trip line shows the quick brown fox sentence heard back |
| speak hook | you hear "Talkover can speak on this Mac." |
| microphone | `OK, rms` above about 100 while you talk; `SILENT` means the Microphone permission is off |
| tmux delivery | `OK` |
| accessibility | `OK` or `NOT GRANTED`; it only matters for the paste mode (`TALKOVER_DELIVERY=macos-app`) |
| panel | the floating panel shows at the top center for 6 seconds: rounded, with transparent corners, and the buttons readable |

Send back `mac-check-report.txt` (it is written in the repo folder) and say whether you heard the sentence and how
the panel looked.

## Then the real loop

```bash
tmux new -s work      # run claude inside this tmux session
./start.sh            # in another terminal tab
```

Talk; your words should appear in Claude Code and send when you pause, and replies should be read aloud. Say "wait"
over a reply to stop it. `./stop.sh` stops everything.
