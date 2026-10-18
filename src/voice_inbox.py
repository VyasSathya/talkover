#!/usr/bin/env python3
"""
Always-on voice inbox for Claude Code.

Talk at any time, with no keyword and no "speak-then-listen" turn.
This daemon keeps the microphone open permanently, cuts each utterance on its own
silence, transcribes it with the local Whisper server VoiceMode already uses, and
types the text into the Claude Code window followed by Enter -- so whatever the user says
lands as a normal message, whether Claude is idle, working, or talking.

Turn-taking (2026-09-18, from the LiveKit / OpenAI Realtime / Deepgram patterns):
  * Barge-in is pause-then-confirm. >= 500 ms of voiced frames while the speak hook is
    playing PAUSES it (700 ms during the first 1.0 s of a new sentence). When the
    utterance is transcribed: noise / a standalone backchannel ("uh-huh", "okay") resumes
    the reading; a hold phrase ("wait", "hold on", "stop") kills it and delivers nothing
    (HELD until the next real utterance); >= 2 real words kill it, flush the queue and
    deliver. If the pause lasts 2.0 s with no words in the audio so far, the reading resumes.
  * Truncate-to-heard. When a real message cuts the reading, the message carries
    [voice: you were cut off after: "<last 12 words spoken>"] so the assistant knows
    what was and was not heard.
  * Two-stage endpointing. 600 ms of silence ends the utterance; if the transcript
    trails off (filler / open connective / short and unpunctuated) the daemon keeps
    listening up to 2200 ms more and merges what follows before delivering.

Robustness (2026-09-24):
  * Single instance (named mutex Talkover_voice_inbox); a second launch exits at once.
  * Single-flight STT: at most one request to Whisper at a time, never abandoned mid-flight;
    a final transcript that arrives after the loop stopped waiting is still typed (<= 45 s old).
  * Pause watchdog: the reading is never left paused longer than 6 s.

Merge + onset (2026-09-25):
  * One message per burst of talk. A finished utterance is typed into the Claude box WITHOUT Enter;
    Enter is pressed only after MERGE_WINDOW_S of no voice from the user (measured in audio time from the
    end of the user's speech, so Whisper time counts toward it) with no utterance in progress. Talk again
    inside the window and the next transcript is appended to the same unsent message.
  * The "the user is talking" stamp is kept fresh while the user's utterance is at Whisper and while text is
    waiting to be sent, so the reading never starts into that gap.
  * Audible = the speak hook's claude-speak.playing window (start .. stop + tail). While it plays,
    only a run of frames clearly louder than its echo counts as the user (a longer run in the first
    ONSET_GUARD_S of each sentence); when nothing plays, any voice is the user.

Nothing the user says is lost (2026-10-02). On 10-02 10:17-10:19 the user talked for about a minute and a half
over and between readings and almost none of it was typed. The Whisper server log shows no request
at all from 10:17:46 to 10:18:32: no utterance ever started. webrtcvad (mode 3) marks quiet speech
(about 100 rms at the mic) only in scattered frames, never the 300 ms unbroken run the start rule
needs; those scattered frames still stamped "the user is talking", so the reading waited, then resumed
over the user at every 1.5 s gap. What changed:
  * Quiet speech is heard. While the reading is silent, a frame that webrtcvad's less aggressive
    mode 2 calls speech AND that has a voice pitch also counts as the user's voice (for starting and ending
    utterances, the "the user is talking" stamp and the merge window). Besides 10 voiced frames in a row,
    SOFT_START_QUIET such frames within 1.5 s, spread over 0.5 s, start an utterance
    (SOFT_START_PLAYING mode-3 frames while the reading sounds, because its own leak is speech too).
    Every utterance keeps the 1.5 s before its start, and one started this way ends after 1.2 s
    without voice (0.6 s once it shows itself as ordinary speech). The pitch test keeps keyboard and
    click trains (which webrtcvad marks as speech) out. Offline (harness, 10-02): three quiet
    sentences, old code typed one 0.9 s fragment, new code all three in full.
  * Nothing is thrown away as "echo" untranscribed. Audio heard while the reading played that
    never showed itself louder than the reading (the old "echo guard: dropped"), and audio whose
    voice stopped when the reading paused (the old "echo guard: discarded"), is transcribed. The
    words that are the reading itself are removed by lining the transcript up against the text
    that was being read (the speak hook publishes each sentence in claude-speak.progress; a watcher
    thread keeps a timed history of it). Whatever is left is the user's and is delivered as usual, with
    the cut-off note. Audio heard over a still-playing reading is transcribed in the background so
    the loop keeps listening, and is checked early (after 2.5 s) so the reading stops sooner when
    it turns out to be the user.
  * Frames carry the time they were captured, so "was the reading audible" is judged when the
    sound was heard, not when the loop got to it (the loop stalls while it waits for Whisper).
  * The reading yields sooner: while the learned echo is quiet (headset), 60 ms above the echo gate
    (120 ms at a sentence onset) stamps the user as talking; the speak hook polls every 20 ms and waits
    for 3 s of quiet (was 1.5 s) before it starts or resumes a sentence.
  * The learned echo level survives restarts (voice_inbox_state.json); mic overflows, lost audio
    and voice that never became an utterance are logged.

Sound that is not the user's words never holds the reading for long (2026-10-05). In one session the user was eating; a smack stopped the
reading ("the user over the reading: 60 ms louder than the echo gate (849 rms)"), the chewing that followed kept stamping "the user
is talking" (webrtcvad passes crunches as voice, and wet mouth sounds can show a pitch), the speak hook waits as long as
that stamp is fresh, and the reading never came back until the user's next message killed it. The same noise once kept a
reading from starting for 92 s. A reading interrupted by a sound should start again where it left off. What changed:
  * Only voice with a pitch stamps "the user is talking": over the reading, a run of frames louder than the echo gate that are
    voiced AND pitched; with the reading silent, 2 of the last 3 frames voiced and pitched; inside an utterance, only
    once it has a few pitched frames. Smacks, clicks, crunches and most coughs (broadband, no pitch) stamp nothing.
  * Every hold of the reading (paused, or waiting on the stamp) has to be confirmed by the user's words within HOLD_CONFIRM_S.
    The audio so far is checked in the background as it comes in (never blocking the loop). 2+ real words, or a hold
    phrase, stop the reading for good with the cut-off note, as before. A transcript of nothing of the user's, or no words by
    the deadline, releases it at once: the stamp is cleared (not left to go stale for the hook's 3 s), the pause file
    removed, and the hook replays the sentence it stopped in. That sentence then plays through while the sounds go on:
    sound alone cannot stop it again; the user's words found by a check, or 700 ms of loud pitched voice, still can. Past the
    deadline a hold lasts only while the user's voice (pitched, sustained: never chewing, clicks or a cough) is going on and a
    check is still to answer, at most HOLD_MAX_S, so the reading never comes back over the user mid-sentence.
  * The echo level was never saved because it was never learned: every learning path needed Whisper to hear the
    reading's own words in the mic audio, and with the headset it never does (every check: "0 words matched"; the leak
    is too low and muffled to register), so learn_echo, and with it save_state, never ran; voice_inbox_state.json did
    not exist. Audio over the reading in which Whisper heard no words at all now sets the level from its pitched voiced
    frames, kept above their loudest 2-frame run and between ECHO_RMS_QUIET_MIN and the default (never a less sensitive
    gate than before); it is saved (atomically) and loaded at start. Learning from audio that did contain the reading's
    words now uses its pitched frames only: chewing under the reading had raised the gate to 517 in the harness.

The user's words hold the reading, they no longer end it (2026-10-05 night). Every reply cut that evening was cut by their own
words over it, and the kill threw the rest away, plus every reading queued behind it when the user's message was sent. Now
the user's words stop the sound and hold it (claude-speak.hold, kept fresh while the user talks and while the user's words wait to be
sent); once the message is sent the hook replays the sentence the user talked over and reads on, then the queued readings
in order. Claude's note says the rest is still read, so it does not repeat it. Only a hold phrase, the mute phrase or
the panel end a reading for good. See WordsHold.

Claude Code only (2026-10-05 night). target_watch reads the Claude window's Chat / Code switch every 1.5 s into
claude-voice.target. With a claude.ai chat on screen nothing the user says is typed (it is dropped, not kept), and the speak
hook stays silent.

Next / Back (2026-10-10). "next" / "skip", "back" / "previous", "again" / "repeat that", said as the whole utterance
(a leading "uh" / "okay" allowed) while a reply is being read or within NAV_AFTER_S after one ended, move the reading
(speak_nav.py; the speak hook does it) and are never typed. Anywhere else they are the user's speech, as before.

Chat picking (2026-10-10). With no %TEMP%\\claude-voice.pin (auto) the user's words go into the chat the window shows, as
before. When the user picks a chat in the panel's list, every delivery first makes the window show that chat (voice_chats.py:
a UIA Invoke on its sidebar entry; focus stays where it is, the chat on screen changes), and nothing is typed if it
cannot be shown.
Auto reads only the chat on screen (2026-10-11): target_watch writes that chat to claude-voice.shown; the speak hook
holds replies of other chats (the newest one per chat) and target_watch starts it to play a held reply once the window
shows that chat.

Rule for the agent while this runs: never call converse with wait_for_response=true
(the daemon is the only ear); speak with wait_for_response=false.

Run with the voicemode venv:
    .venv\\Scripts\\python.exe src\\voice_inbox.py   (start.ps1 starts everything)
Log: ~/.talkover/logs/voice_inbox.log (TALKOVER_HOME)
"""
import collections
import ctypes
import ctypes.wintypes as W
import io
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.request
import wave
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
import sounddevice as sd
import webrtcvad


def single_instance(name: str):
    """A handle if this is the only copy, None if one is already running (Windows: named mutex; macOS / Linux: a
    lock file). Keep the handle alive for the life of the process (the OS frees it when the process exits)."""
    return _single_instance(name, MUTEX_PREFIX)

from platform_util import IS_WINDOWS, TEMP_DIR, run_speak_hook, single_instance as _single_instance
from voice_config import MUTEX_PREFIX, LEVEL_PORT, CODE_DIR, DATA_DIR, LOG_DIR, SPEAK_HOOK, STT_URL as _STT_URL

LOG = LOG_DIR / "voice_inbox.log"
STATE_FILE = DATA_DIR / "voice_inbox_state.json"   # learned echo level, kept across restarts
STT_URL = os.getenv("VOICE_INBOX_STT_URL", _STT_URL)
CONTROL_PORT = int(os.getenv("VOICEMODE_CONTROL_PORT", "42711"))
TARGET_TITLE = os.getenv("VOICE_INBOX_WINDOW", "Claude")   # exact window title of Claude Code
BARGE_IN = os.getenv("VOICE_INBOX_BARGE_IN", "true").lower() in ("1", "true", "yes")

RATE = 16000
FRAME_MS = 30
FRAME = RATE * FRAME_MS // 1000            # samples per VAD frame
VAD_MODE = 3


def _frames(ms: int) -> int:
    """Number of 30 ms frames needed to cover `ms` (rounded up)."""
    return -(-ms // FRAME_MS)


START_FRAMES = 10                          # ~300 ms of voice in a row starts an utterance (was 4: too twitchy)
BARGE_VOICED_MS = 500                      # voice needed before the reading is paused (LiveKit min_duration 0.5 s)
BARGE_VOICED_MS_BOUNDARY = 700             # ... during the boundary cooldown after a new sentence starts
BOUNDARY_COOLDOWN_S = 1.0                  # how long after a sentence starts the 700 ms rule applies
PAUSE_AFTER_VOICED_FRAMES = _frames(BARGE_VOICED_MS)                    # 17 frames = 510 ms
PAUSE_AFTER_VOICED_FRAMES_BOUNDARY = _frames(BARGE_VOICED_MS_BOUNDARY)  # 24 frames = 720 ms
# Endpoint floor: send as soon as possible. Its own variable: voicemode.env sets
# VOICEMODE_SILENCE_THRESHOLD_MS=2500 for VoiceMode, which would silently become a 2.5 s floor here.
END_SILENCE_MS = int(os.getenv("VOICE_INBOX_FLOOR_MS", "600"))
EXTEND_SILENCE_MS = 2200                   # extra silence allowed when the transcript trails off (ceiling = 2800 ms)
MAX_UTTERANCE_S = 90
MIN_UTTERANCE_S = 0.6
PRE_ROLL_FRAMES = 50                       # keep 1.5 s before an utterance starts (was 300 ms: a quiet lead-in was cut)

# 2026-10-02: quiet speech. webrtcvad mode 3 marks speech at ~100 rms in scattered frames only (measured on
# Kokoro speech plus room noise: longest run 7 frames, so the 10-in-a-row rule never fired). With nothing
# playing, a frame mode 2 calls speech AND that has a voice pitch also counts as voice (47-63% of quiet
# speech frames; 0 of noise or click frames). Not while the reading sounds: mode 2 marks half its leak.
QUIET_VAD_MODE = 2
SOFT_WINDOW = 50                           # frames (1.5 s) the quiet start rule looks at
SOFT_START_QUIET = 8                       # voice frames with a pitch in the window -> utterance (nothing playing)
SOFT_START_PLAYING = 15                    # ... while the reading is audible (mode 3 only; its leak gives ~8%)
SOFT_MIN_SPAN = 17                         # ... spread over at least 0.5 s: clean speech starts on the old rule first
SOFT_END_MS = 1200                         # an utterance started that way ends after this long without voice
PERIODIC_MIN = 0.3                         # pitch periodicity (normalised autocorrelation, 60-400 Hz) of a voice
                                           # frame; click and keyboard frames that webrtcvad passes score ~0

# 2026-09-24: never leave the reading paused for long, and never pile requests onto Whisper.
MAX_PAUSE_S = 6.0                          # watchdog: a pause file older than this is removed (reading resumes)
STT_FINAL_WAIT_S = 15.0                    # the loop waits this long for a final transcript, then moves on
STT_PARTIAL_WAIT_S = 3.0                   # ... and this long for the mid-utterance check
STT_HTTP_TIMEOUT_S = 120                   # the request itself is never abandoned early (abandoned ones keep running
                                           # server-side and everything queues behind them)
STT_LATE_MAX_S = 45.0                      # a transcript that arrives late is still typed if it is at most this old
# Echo guard: the mic also hears Claude's own voice. Pausing stops that voice, so if the "voice" stops
# when the reading pauses, it may have been the reading. Since 2026-10-02 the transcript decides.
ECHO_TAIL_FRAMES = 5                       # 150 ms after the pause: playback tail, ignored
ECHO_CONFIRM_FRAMES = 17                   # then 510 ms in which the user must still be talking
ECHO_CONFIRM_VOICED = 6                    # 180 ms of voice in that window = it is the user
ECHO_GATE_RATIO = 2.0                      # once the echo level is known, barge-in needs frames 2x louder (+6 dB)
ECHO_RMS_DEFAULT = 150.0                   # first start without a saved level (the gate observed before 10-02 was <= 310)
QUIET_ECHO_RMS = 250.0                     # learned echo at or below this (headset leak): the faster yield applies

# 2026-09-25: one message per burst of talk, and the reading's own onset never counts as the user.
MERGE_WINDOW_S = 2.0                       # Enter only after this long with no voice from the user (audio time from end of speech)
HOLD_MERGE_S = 6.0                         # after "wait" / "hold on" with text still unsent: wait this long instead
MERGE_MAX_WAIT_S = 12.0                    # no new text for this long and no utterance in progress: send regardless
MERGE_NOISE_RUN = 2                        # outside an utterance, a voiced run this long (60 ms) counts as the user talking
MARK_KEEP_S = 0.1                          # keeper refreshes the "the user is talking" stamp this often while it is held
ECHO_TAIL_S = 0.25                         # after the reading's audio stops, the room tail is still the reading
AUDIBLE_MARK_FRAMES = 3                    # while it plays: 90 ms in a row louder than the echo gate = the user ...
AUDIBLE_MARK_FRAMES_QUIET = 2              # ... 60 ms while the learned echo is quiet (2026-10-02: yield sooner)
ONSET_GUARD_S = 0.6                        # first part of each sentence (onset is loudest vs the learned median) ...
ONSET_MARK_FRAMES = 6                      # ... needs 180 ms in a row louder than the echo gate
ONSET_MARK_FRAMES_QUIET = 4                # ... 120 ms while the learned echo is quiet

# 2026-10-02: audio heard over the reading is transcribed and the reading's own words removed.
READ_POLL_S = 0.025                        # the reading-history watcher looks at claude-speak.playing this often
ECHO_CHECK_MAX_S = 12.0                    # an utterance over the reading not yet shown to be the user is checked at this length
ECHO_PARTIAL_S = 2.5                       # ... and checked early, in the background, once it is this long
ECHO_PARTIAL_EVERY_S = 4.0                 # ... then again every this many seconds of audio
EARLY_KILL_WORDS = 4                       # words of the user's (reading removed) that stop the reading from an early check
CHECK_FLUSH_WAIT_S = 8.0                   # a pending Enter waits at most this long for a background check
STRIP_MIN_SCORE = 6                        # alignment score (2 per matching word) that marks a stretch as the reading

# 2026-10-05: nothing but the user's words holds the reading for long (see the module notes).
HOLD_CONFIRM_S = 2.0                       # a hold (paused, or waiting on "the user is talking") with no words of the user's by then is
                                           # released: the reading picks up the sentence it stopped in (was the 2.0 s
                                           # false-barge timeout, which covered the pause only)
HOLD_CHECK_GRACE_S = 0.5                   # ... a check of the user's audio still at Whisper then gets this much longer to answer
HOLD_FIRST_CHECK_S = 0.6                   # the audio so far is checked this far into a hold (in the background). Whisper
                                           # takes 1.2-2.0 s on speech here (0.2 s on noise): a first word or two is in
                                           # by then and the answer is back near the deadline
HOLD_CHECK_LAST_S = 1.2                    # ... the first check leaves no later than this; after that only while the user's
                                           # voice is going on (VOICE_ON_*), one check after another until the user's words
HOLD_CHECK_MAX_S = 8.0                     # ... at most its last this-many seconds (a long utterance stays quick to check)
HOLD_RETRY_S = 0.2                         # Whisper busy: the check is tried again this soon
PROTECT_QUIET_S = 1.5                      # the replayed sentence is protected until it has played through and no sound
                                           # has claimed the user for this long (eating goes on: the reading keeps going)
HOLD_MAX_S = 5.0                           # past HOLD_CONFIRM_S a hold lasts only while the user's voice is going on, or after it
                                           # did while the user's audio is at Whisper (the user's words are still to be found); never
                                           # longer than this
VOICE_HANG_S = 0.8                         # the user's voice counts as going on this long after it was last on (a breath)
VOICE_ON_FRAMES, VOICE_ON_MIN = _frames(1000), 14   # "the user's voice is going on": 14 of the last 33 frames voiced with a
VOICE_ON_RECENT = _frames(300)                       # pitch, one in the last 0.3 s (measured: speech is on for most of
                                                     # its length; mm-hmm for 0.4 s while it lasts; chewing, clicks,
                                                     # smacks and a cough never)
CLAIM_FRAMES, CLAIM_WINDOW = 2, 3          # with the reading silent: 2 of the last 3 frames voiced AND pitched = the user
UTT_PITCHED_MIN = 3                        # an utterance keeps the reading waiting only once it has this many pitched frames
PROTECT_BARGE_FRAMES = PAUSE_AFTER_VOICED_FRAMES_BOUNDARY   # the sentence replayed after a release stops only for
                                           # 700 ms of loud pitched voice (mm-hmm measured 400 ms; talk gets there in ~1 s)
USER_QUIET_S = 3.0                         # the speak hook's $QuietS: a stamp younger than this keeps it waiting
ECHO_RMS_QUIET_MIN = 100.0                 # echo learned from audio with no words in it: never below this (gate 200) ...
ECHO_QUIET_MIN_FRAMES = 10                 # ... and only from at least this many pitched voiced frames of the reading
ECHO_RUN_MARGIN = 1.15                     # ... with the gate this far above the loudest 2-frame run of the reading's leak

# Hold phrases: said alone they stop the reading and deliver nothing (HELD until the next utterance).
STOP_WORDS = {"stop", "stop talking", "shut up", "be quiet", "quiet", "stop reading",
              "wait", "hold on", "one sec", "one second", "hang on"}
# Backchannel tokens (hyphens removed): an utterance made only of these keeps the reading going.
BACKCHANNEL_TOKENS = {"uhhuh", "uh", "huh", "mhmm", "mmhmm", "mhm", "mm", "hmm", "okay", "ok",
                      "yeah", "yep", "right", "sure", "oh", "um"}
# Trailing fillers / open connectives: the speaker is probably not done -> extend the endpoint.
TRAILING_OPEN = ("um", "uh", "like", "and", "so", "but", "or", "because", "then", "that", "the", "a",
                 "to", "of", "with", "i", "i'm", "im", "it's", "its", "you know", "i mean", "hold on")

# Whisper hallucinations on near-silence; dropped when they are the whole transcript.
JUNK = {"", ".", "you", "thank you", "thanks", "bye", "okay", "ok", "hmm", "uh", "um",
        "thank you.", "you.", "so", "the", "oh", "yeah"}

# Caption phrases Whisper invents on near-silence, alone or chained ("Thank you. Bye. Bye. Bye."). A transcript made only
# of these is dropped (2026-10-03: a "bye" nobody said was being typed). Anything else in the transcript keeps it.
PHANTOM_PHRASES = {"thank you", "thanks", "thank you so much", "thank you very much", "thanks so much", "bye", "bye bye",
                   "goodbye", "good bye", "bye now", "thanks for watching", "thank you for watching",
                   "thanks for watching everyone", "thank you for watching everyone", "please subscribe",
                   "like and subscribe", "subscribe", "see you next time", "see you in the next video",
                   "ill see you next time", "see you", "you", "okay", "ok", "peace", "cheers",
                   "subtitles by the amara org community", "transcribed by", "music", "applause",
                   # 2026-10-05, on the GPU engine: "Thank you so much for watching, and I'll see you in the next video!"
                   "thank you so much for watching", "thanks so much for watching", "thank you all for watching",
                   "ill see you in the next video", "see you in the next one", "ill see you in the next one",
                   "see you next time", "ill see you next time everyone", "bye everyone", "goodbye everyone"}


KEEP_SHORT = {"hello", "hey", "hi", "testing", "test", "hello hello", "can you hear me", "are you there"}


# 2026-10-06: the whole family by pattern, not exact phrases. Got through as the user's message: "Thank you very much for
# watching this video and I'll see you in the next video." A clause counts only if ALL of it is caption sign-off
# (thanks, for watching / listening, see you next time / in the next video, subscribe / like / comment, bye); a
# transcript is dropped only when every clause is. "Thank you, that fixed it" keeps "that fixed it" and is kept.
_PH_THANKS = r"(?:thank you|thanks|thank u)(?: (?:so|very|all|everyone|everybody|guys|you all))*(?: (?:much|so much|very much))?"
_PH_WATCH = r"(?:for (?:watching|listening|viewing|tuning in|joining(?: us)?|your (?:time|attention|support))(?: (?:this|the|my|our) (?:video|episode|stream|show|channel))?(?: (?:everyone|everybody|guys|all))?)"
_PH_SEE = (r"(?:(?:i ?(?:ll|will) |we ?(?:ll|will) |hope to )?see (?:you|ya|u)(?: (?:guys|all|everyone))?"
           r"(?: (?:next time|in the next (?:one|video|episode|time)|in the next|soon|later|tomorrow|there|again|around))?(?: (?:everyone|guys|bye))?)")
_PH_SUB = (r"(?:(?:please |and |dont forget to |do not forget to |make sure to |remember to )*"
           r"(?:like|subscribe|comment|share|hit the (?:like|bell)(?: button)?|ring the bell|click the bell)"
           r"(?:(?: and| or|,)? (?:like|subscribe|comment|share|hit the bell|the bell|notification bell))*"
           r"(?: (?:to (?:the|my|our) channel|for more(?: videos)?|below|down below))*)")
_PH_BYE = r"(?:(?:bye|goodbye|good bye|bye bye|peace|cheers|take care)(?: (?:now|everyone|everybody|guys|all|for now))?)"
_PH_UNIT = r"(?:%s(?: %s)?|%s|%s|%s|%s)" % (_PH_THANKS, _PH_WATCH, _PH_WATCH, _PH_SEE, _PH_SUB, _PH_BYE)
_PH_CLAUSE = re.compile(r"^(?:(?:and|so|but|well|okay|ok|alright|all right|um|uh) )*%s(?: (?:and|so|but|then|also|until then) (?:%s))*$"
                        % (_PH_UNIT, _PH_UNIT))


def is_phantom(text: str) -> bool:
    parts = [re.sub(r"[^a-z ]", "", p.replace("'", "")).strip() for p in re.split(r"[.!?,;:\n]+", text.lower())]
    parts = [" ".join(p.split()) for p in parts if p.strip()]
    parts = [re.sub(r"^(and|so|but) ", "", p) for p in parts]   # "and I'll see you in the next video"
    return bool(parts) and all(p in PHANTOM_PHRASES or _PH_CLAUSE.match(p) for p in parts)

log = logging.getLogger("voice_inbox")
_level_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # mic level to the panel orb


def setup_log():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(LOG, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(h)


# ---------------------------------------------------------------- transcript classification

def normalize(text: str) -> str:
    t = re.sub(r"[^\w\s'-]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip(" '-")


_HOLD_RE = re.compile(r"^(?:(?:%s)[\s,]*)+$" % "|".join(
    re.escape(p) for p in sorted(STOP_WORDS, key=len, reverse=True)))


MUTE_PHRASES = {"stop listening", "mute yourself", "pause listening", "go to sleep", "stop transcribing",
                "mute the mic", "stop sending"}
UNMUTE_PHRASES = {"start listening", "wake up", "unmute yourself", "resume listening", "start sending",
                  "unmute the mic", "claude wake up"}


def mute_command(text: str) -> str:
    """'mute' | 'unmute' | '' for an utterance that is only a listening command (backchannel lead allowed)."""
    norm = normalize(text)
    toks = norm.split()
    lead = 0
    while lead < len(toks) and toks[lead].replace("-", "") in BACKCHANNEL_TOKENS:
        lead += 1
    rest = " ".join(toks[lead:])
    if rest in MUTE_PHRASES:
        return "mute"
    if rest in UNMUTE_PHRASES:
        return "unmute"
    return ""


# Next / Back (2026-10-10). Said alone (a backchannel or "no" / "just" lead and a trailing "please" allowed), these move
# the reading and are never typed: "next" stops the reply being read and plays the next one queued; "back" plays the
# reply before it from its start; "again" plays the one being read (or the last one read) from its start. The speak
# hook does the moving (speak_nav.py).
NAV_PHRASES = {
    "next": {"next", "skip", "next one", "skip it", "skip that", "skip this", "skip this one", "skip that one",
             "next message", "next reply", "go next", "skip ahead"},
    "back": {"back", "go back", "previous", "previous one", "previous message", "back one", "go back one"},
    "again": {"again", "repeat", "repeat that", "repeat it", "say that again", "say it again", "one more time",
              "read that again", "read it again"},
}
_NAV_RE = {k: re.compile(r"^(?:(?:%s)[\s,]*)+$" % "|".join(re.escape(p) for p in sorted(v, key=len, reverse=True)))
           for k, v in NAV_PHRASES.items()}


NAV_AFTER_S = 3.0      # a command counts while a reply is being read, or if the user starts it within this long after one ended


def nav_command(text: str) -> str:
    """'next' | 'back' | 'again' | '' for an utterance that is ONLY a Next / Back command (a leading "uh" / "okay"
    allowed). A "next" said in the middle of a sentence must not skip anything. A
    "next" or "back" inside anything else is the user's speech, as before."""
    toks = normalize(text).split()
    while toks and toks[0].replace("-", "") in BACKCHANNEL_TOKENS:
        toks = toks[1:]
    rest = " ".join(toks)
    if not rest:
        return ""
    for k, rx in _NAV_RE.items():
        if rx.match(rest):
            return k
    return ""


def classify(text: str) -> str:
    """Classify a transcript: 'junk' | 'backchannel' | 'hold' | 'short' | 'message'.
    'short' = one real (non-backchannel) word: a message when idle, a false interruption
    while the reading is playing."""
    norm = normalize(text)
    if not norm:
        return "junk"
    toks = [t.replace("-", "") for t in norm.split()]
    if all(t in BACKCHANNEL_TOKENS for t in toks):
        return "backchannel"
    # "wait" / "hold on" alone, or after a backchannel ("okay wait"), is a hold.
    lead = 0
    while lead < len(toks) and toks[lead] in BACKCHANNEL_TOKENS:
        lead += 1
    if _HOLD_RE.match(" ".join(norm.split()[lead:])):
        return "hold"
    # 2026-10-05: "Hello?" is how the user checks the mic, and it was dropped as a one-word hallucination, so the user saw nothing
    # ("im talking and itsn ot showing anythng for u"). A greeting or "testing" alone goes through.
    if norm in KEEP_SHORT:
        return "short"
    if text.lower().strip() in JUNK or (len(toks) < 2 and len(text.strip()) < 8) or is_phantom(text):
        return "junk"
    real = [t for t in toks if t not in BACKCHANNEL_TOKENS]
    return "message" if len(real) >= 2 else "short"


def real_words(text: str) -> int:
    return sum(1 for t in normalize(text).split() if t.replace("-", "") not in BACKCHANNEL_TOKENS)


def looks_incomplete(text: str) -> bool:
    """Semantic stand-in for a turn detector: trailing filler / open connective, or a short
    unpunctuated fragment, means the speaker is probably not done."""
    norm = normalize(text)
    if not norm:
        return False
    toks = norm.split()
    for phrase in TRAILING_OPEN:
        if norm == phrase or norm.endswith(" " + phrase):
            return True
    if len(toks) < 4 and not text.rstrip().endswith((".", "!", "?")):
        return True
    return False


# ---------------------------------------------------------------- removing the reading from a transcript

def _canon(tok: str) -> str:
    t = tok.lower().replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^\w']+", "", t).strip("'")


def _words(text: str):
    """[(original token, canonical form)] for every token that has letters or digits."""
    out = []
    for raw in re.findall(r"\S+", text or ""):
        c = _canon(raw)
        if c:
            out.append((raw, c))
    return out


def _same(a: str, b: str) -> bool:
    """Equal, or (both 4+ letters) one substitution / insertion apart: Whisper's spelling of the
    reading's words drifts ("realise"/"realize", "ok"/"okay" are left to the alignment's gaps)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if la < 4 or lb < 4 or abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    if la > lb:
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def _best_local(t, ref, gone):
    """Smith-Waterman over words: the best-scoring stretch of transcript t that lines up with a
    stretch of the reading ref. Returns (score, first, last) indexes into t, or None.
    Besides word = word it lines up Whisper's merges and splits ("outside" for "out side",
    "every one" for "everyone") and a last word cut off mid-way ("s" for "side")."""
    n, m = len(t), len(ref)
    if not n or not m:
        return None
    MATCH, MISS, GAP = 2, -1, -1
    H = [[0] * (m + 1) for _ in range(n + 1)]
    MV = [[0] * (m + 1) for _ in range(n + 1)]   # 1 diag, 2 skip t, 3 skip ref, 4 t=ref+ref, 5 t+t=ref
    best, bi, bj = 0, 0, 0
    for i in range(1, n + 1):
        ti = None if gone[i - 1] else t[i - 1]
        prev_t = None if (i < 2 or gone[i - 2]) else t[i - 2]
        last = i == n
        row, up = H[i], H[i - 1]
        for j in range(1, m + 1):
            rj = ref[j - 1]
            ok = ti is not None and (_same(ti, rj) or (last and len(ti) < len(rj) and rj.startswith(ti)))
            s, mv = 0, 0
            v = up[j - 1] + (MATCH if ok else MISS)
            if v > s:
                s, mv = v, 1
            v = up[j] + GAP
            if v > s:
                s, mv = v, 2
            v = row[j - 1] + GAP
            if v > s:
                s, mv = v, 3
            if ti is not None and j >= 2 and ti == ref[j - 2] + rj:
                v = up[j - 2] + MATCH
                if v > s:
                    s, mv = v, 4
            if ti is not None and prev_t is not None and rj == prev_t + ti:
                v = H[i - 2][j - 1] + MATCH
                if v > s:
                    s, mv = v, 5
            row[j], MV[i][j] = s, mv
            if s > best:
                best, bi, bj = s, i, j
    if best <= 0:
        return None
    i, j, first = bi, bj, bi - 1
    while i > 0 and j > 0 and H[i][j] > 0 and MV[i][j]:
        mv = MV[i][j]
        if mv == 1:
            first, i, j = i - 1, i - 1, j - 1
        elif mv == 2:
            first, i = i - 1, i - 1
        elif mv == 3:
            j -= 1
        elif mv == 4:
            first, i, j = i - 1, i - 1, j - 2
        else:
            first, i, j = i - 2, i - 2, j - 1
    return best, first, bi - 1


def strip_reading(text: str, readings, words=None, t0=None):
    """Remove from a transcript the words that are the reading itself (Claude's sentences that were
    audible while this audio was recorded). Returns (the user's words, number of words removed).
    A word is removed only when (a) it is part of a stretch that lines up with the reading's text
    over about three words or more (a shared word or two is left alone), and (b) when Whisper gave
    word times (words=[(token, start, end)] relative to the audio, t0 = capture time of its first
    frame), it was said while the reading was audible. So whatever the user says after the reading stops
    is never removed, even when the user quotes it back."""
    if not text or not readings:
        return text or "", 0
    ref = [c for r in readings for _, c in _words(r)]
    if not ref:
        return text, 0
    toks = []                               # (original token, canonical, start, end)
    if words and t0 is not None:
        for raw, s, e in words:
            c = _canon(raw)
            if c:
                toks.append((raw, c, s, e))
    if not toks:
        toks = [(raw, c, None, None) for raw, c in _words(text)]
    if not toks:
        return text, 0
    t = [x[1] for x in toks]
    used = [False] * len(t)                 # lined up with the reading already (not searched again)
    drop = [False] * len(t)
    for _ in range(8):                      # the same sentence can be heard twice (replayed after a pause)
        hit = _best_local(t, ref, used)
        if not hit or hit[0] < STRIP_MIN_SCORE:
            break
        _, a, b = hit
        for i in range(a, b + 1):
            if used[i]:
                continue
            used[i] = True
            s, e = toks[i][2], toks[i][3]
            if s is None or HISTORY.audible_during(t0 + s - 0.15, t0 + e + 0.15):
                drop[i] = True
    removed = sum(drop)
    if not removed:
        return text, 0
    kept = [x[0] for x, d in zip(toks, drop) if not d]
    if toks[0][2] is not None:              # Whisper word tokens carry their own leading spaces
        return "".join(kept).strip(), removed
    return " ".join(kept), removed


# ---------------------------------------------------------------- control channel

SPEAK_PID_FILE = TEMP_DIR / "claude-speak.pid"     # written by ~/.claude/hooks/speak-response.ps1
SPEAK_PAUSE_FILE = TEMP_DIR / "claude-speak.pause"  # while present, that hook pauses playback
SPEAK_CANCEL_FILE = TEMP_DIR / "claude-speak.cancel"  # touched: queued speakers give up
SPEAK_PROGRESS_FILE = TEMP_DIR / "claude-speak.progress"  # text of the chunk being spoken right now
USER_SPEAKING_FILE = TEMP_DIR / "claude-user.speaking"  # touched while the user is talking; the speak hook yields to it
SPEAK_PLAYING_FILE = TEMP_DIR / "claude-speak.playing"  # "<start_ms> <end_ms>" epoch ms of the sentence playing
# 2026-10-05 night: the user's words no longer kill the reading. They hold it (this file, kept fresh by mark_keeper) until the user's
# message is sent; then the hook replays the sentence the user talked over and reads the rest. See WordsHold.
SPEAK_HOLD_FILE = TEMP_DIR / "claude-speak.hold"
# 2026-10-06: stamped when the user's words stop a reading. The speak hook then drops that reading's rest (waiting, held or
# resumed) as soon as a newer reply announces itself in claude-speak.newest: the user hears the newest reply only.
SPEAK_TALKED_FILE = TEMP_DIR / "claude-speak.talkedover"
# Which view the Claude window shows ("code" / "chat" / "unknown"), written by target_watch every TARGET_POLL_S. The
# inbox delivers only into Claude Code, and the speak hook stays silent while the window shows a claude.ai chat.
VOICE_TARGET_FILE = TEMP_DIR / "claude-voice.target"
TARGET_POLL_S = 1.5
WORDS_HOLD_MAX_S = 300.0                   # a words hold never lasts longer than this
WORDS_HOLD_IDLE_S = 8.0                    # ... nor this long after the last stamp backed by the user's words
_last_user_mark = [0.0]


class ReadingHistory:
    """Timed record of what the speak hook played: one item per Play() of a sentence, with its
    audible window (wall-clock seconds) and its text. Filled by a watcher thread that polls
    claude-speak.playing (the hook writes it just before Play() with the expected end and rewrites
    it at Stop() with the real stop; it writes claude-speak.progress, the sentence text, before
    that). Read by the frame loop with each frame's capture time, so a loop that is behind (waiting
    on Whisper) still knows whether the reading was audible when a frame was heard, and what was
    being read."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = []                     # [{"start": s, "end": s, "text": str}]
        self._mtime = None
        self._present = False

    def poll(self):
        try:
            mt = SPEAK_PLAYING_FILE.stat().st_mtime_ns
        except OSError:
            if self._present:               # removed: reading finished or was killed; it is silent from now
                now = time.time()
                with self.lock:
                    if self.items and self.items[-1]["end"] > now:
                        self.items[-1]["end"] = now
            self._present, self._mtime = False, None
            return
        self._present = True
        if mt == self._mtime:
            return
        try:
            a, b = SPEAK_PLAYING_FILE.read_text(encoding="utf-8-sig").split()[:2]
            start, end = int(a) / 1000.0, int(b) / 1000.0
        except (OSError, ValueError):
            return                          # caught mid-write; the next poll reads it
        self._mtime = mt
        text = read_progress()
        with self.lock:
            for it in reversed(self.items[-6:]):
                if abs(it["start"] - start) < 0.002:     # same Play(): the hook rewrote the real stop
                    it["end"] = end
                    if text and not it["text"]:
                        it["text"] = text
                    break
            else:   # full_end: the expected end written before Play(), i.e. the whole sentence's length
                self.items.append({"start": start, "end": end, "full_end": end, "text": text})
                del self.items[:-400]

    def audible_at(self, t: float) -> bool:
        """The reading's sound could be reaching the mic at wall time t (playing, plus the room tail)."""
        with self.lock:
            for it in reversed(self.items[-12:]):
                if it["start"] - 0.05 <= t <= it["end"] + ECHO_TAIL_S:
                    return True
                if it["end"] + ECHO_TAIL_S < t - 60:
                    break
        return False

    def onset_at(self, t: float) -> bool:
        with self.lock:
            for it in reversed(self.items[-12:]):
                if 0 <= t - it["start"] < ONSET_GUARD_S:
                    return True
        return False

    def start_at(self, t: float):
        """Start time of the sentence audible at t (identifies the sentence), or None."""
        with self.lock:
            for it in reversed(self.items[-12:]):
                if it["start"] - 0.05 <= t <= it["end"] + ECHO_TAIL_S:
                    return it["start"]
        return None

    def audible_during(self, a: float, b: float) -> bool:
        """The reading was audible at some moment in [a, b] (wall time)."""
        with self.lock:
            for it in reversed(self.items[-40:]):
                if it["start"] - 0.05 <= b and it["end"] + ECHO_TAIL_S >= a:
                    return True
        return False

    def texts_between(self, t0: float, t1: float):
        """What of the reading was audible at any time in [t0, t1]: for each sentence played then, the
        words it had got through (a sentence cut short, or still playing at t1, only counts up to that
        point plus two words), in order, each once. Words it never played cannot be in the echo, so
        when the user says them back right after cutting in, they stay the user's."""
        out = []
        with self.lock:
            for it in self.items:
                if not (it["start"] - 0.05 <= t1 and it["end"] + ECHO_TAIL_S >= t0 and it["text"]):
                    continue
                words = it["text"].split()
                full = max(0.1, it.get("full_end", it["end"]) - it["start"])
                played = min(it["end"], t1) - it["start"]
                frac = max(0.0, min(1.0, played / full))
                text = it["text"] if frac >= 0.97 else " ".join(words[:int(frac * len(words) + 0.999) + 2])
                if text and text not in out:
                    out.append(text)
        return out

    def text_at(self, t: float) -> str:
        with self.lock:
            for it in reversed(self.items[-12:]):
                if it["start"] - 0.05 <= t <= it["end"] + ECHO_TAIL_S:
                    return it["text"]
        return ""

    def completed_since(self, t: float) -> bool:
        """A sentence that started at or after wall time t has played to its end (not cut short)."""
        now = time.time()
        with self.lock:
            for it in reversed(self.items[-12:]):
                if it["start"] < t - 0.05:
                    break
                if it["end"] <= now and it["end"] >= it.get("full_end", it["end"]) - 0.1:
                    return True
        return False


HISTORY = ReadingHistory()


_reading_seen = [0.0]                  # wall time a speak hook last held the reading (Next / Back timing)


def reading_near(t: float) -> bool:
    """A reply is being read now, or one was within NAV_AFTER_S before wall time t (when the user started talking)."""
    return SPEAK_PID_FILE.exists() or (t - _reading_seen[0]) <= NAV_AFTER_S


def reading_watch() -> None:
    last_err = 0.0
    while True:
        try:
            if SPEAK_PID_FILE.exists():
                _reading_seen[0] = time.time()
            HISTORY.poll()
        except Exception as e:
            if time.monotonic() - last_err > 60:
                last_err = time.monotonic()
                log.warning("reading watcher: %s", e)
        time.sleep(READ_POLL_S)


_last_words_mark = [0.0]               # last stamp backed by the user's words (monotonic)
_pid_cache = {"mt": None, "pid": None}


def _stamp_file() -> None:
    """Write the "the user is talking" stamp (at most every 0.15 s; the speak hook looks at its age)."""
    now = time.monotonic()
    if now - _last_user_mark[0] < 0.15:
        return
    _last_user_mark[0] = now
    try:
        USER_SPEAKING_FILE.write_text(time.strftime("%H:%M:%S"))
    except OSError:
        pass


def _clear_stamp() -> None:
    """Make the stamp old at once, so the hook goes on now instead of USER_QUIET_S after the last stamp."""
    _last_user_mark[0] = 0.0
    try:
        t = time.time() - 60.0
        os.utime(USER_SPEAKING_FILE, (t, t))
    except OSError:
        pass


def speak_pid():
    """pid of the speak hook that holds the speaker lock (it writes claude-speak.pid), or None."""
    try:
        mt = SPEAK_PID_FILE.stat().st_mtime_ns
    except OSError:
        _pid_cache.update(mt=None, pid=None)
        return None
    if mt != _pid_cache["mt"]:
        try:
            _pid_cache.update(mt=mt, pid=int(SPEAK_PID_FILE.read_text().strip()))
        except (OSError, ValueError):
            return _pid_cache["pid"] or -1          # being written: it is there
    return _pid_cache["pid"]


class ReadingHold:
    """2026-10-05. The reading held by us: paused (claude-speak.pause), or kept waiting by the "the user is talking" stamp (the
    speak hook will not start or go on with a sentence while that stamp is younger than USER_QUIET_S). A hold has to be
    confirmed by the user's words within HOLD_CONFIRM_S, or it is released: the stamp is cleared, the pause removed, and the hook
    replays the sentence it stopped in. That sentence then plays through: a stamp from sound alone (not backed by the user's
    words) is refused until it has. Used by the frame loop and by mark_keeper, which keeps the time while the loop waits
    on Whisper."""

    def __init__(self):
        self.lock = threading.RLock()
        self.pid = None
        self.protect_since = 0.0      # wall time of the last release (0: off)
        self.releases = 0             # counts releases, so the loop notices one done by mark_keeper
        self.last_claim = 0.0         # monotonic time of the last claim from sound (refused ones too)
        self.voice_on_at = 0.0        # monotonic time the user's voice was last going on (set_voice, from the frame loop)
        self._clear()

    def _clear(self):
        self.t0 = 0.0                 # monotonic start of the current hold (0: none)
        self.words = False            # the user's words confirmed in it: it lasts as long as the user talks
        self.spoke = False            # the user's voice was going on during it (speech-like, not just a sound)
        self.checks = 0               # checks of the user's audio sent during it
        self.check_at = 0.0           # monotonic time the check now at Whisper was sent (0: none)
        self.retry_at = 0.0

    def set_voice(self, on: bool) -> None:
        """Frame loop: is the user's voice going on right now (VOICE_ON_*)?"""
        if on:
            with self.lock:
                self.voice_on_at = time.monotonic()
                if self.t0:
                    self.spoke = True

    def voice_recent(self, now: float) -> bool:
        return now - self.voice_on_at < VOICE_HANG_S

    def protected(self) -> bool:
        return self.protect_since > 0.0

    def _begin(self, why: str, words: bool) -> None:
        self._clear()
        self.t0 = time.monotonic()
        self.words = words
        log.info("hold: the reading waits (%s)%s", why,
                 "" if words else "; the user's words must confirm it within %.1f s" % HOLD_CONFIRM_S)

    def stamp(self, words: bool = False) -> bool:
        """Stamp "the user is talking". words: backed by the user's words (found by a transcript, or the user's text waiting to be sent).
        Otherwise it is a claim from sound: refused while a released sentence replays; it starts or keeps up a hold
        that the user's words must confirm."""
        with self.lock:
            if words:
                _last_words_mark[0] = time.monotonic()
            else:
                self.last_claim = time.monotonic()
                if self.protect_since:
                    return False
            _stamp_file()
            if speak_pid() is not None:
                if not self.t0:
                    self._begin("the user's words" if words else "the user's voice", words)
                elif words:
                    self.words = True
            return True

    def confirm(self) -> None:
        with self.lock:
            if self.t0:
                self.words = True

    def check_due(self) -> bool:
        with self.lock:
            if not self.t0 or self.words or self.check_at:
                return False
            now = time.monotonic()
            if now < self.retry_at:
                return False
            age = now - self.t0
            if self.checks == 0 and HOLD_FIRST_CHECK_S <= age <= HOLD_CHECK_LAST_S:
                return True
            # the user's voice goes on: keep checking until the user's words are known (each check takes Whisper 1.2-2 s)
            return (self.voice_recent(now) and age >= HOLD_FIRST_CHECK_S and age < HOLD_MAX_S - 1.0)

    def check_sent(self) -> float:
        with self.lock:
            self.checks += 1
            self.check_at = time.monotonic()
            return self.t0

    def check_back(self, hold_t0: float, busy: bool) -> None:
        with self.lock:
            if hold_t0 != self.t0:
                return
            self.check_at = 0.0
            if busy:
                self.checks -= 1
                self.retry_at = time.monotonic() + HOLD_RETRY_S

    def tick(self) -> None:
        """Notice a hold that began without a stamp of ours (the pause, or a hook that started while the stamp was
        fresh), end the protection once the replayed sentence has played through, and keep the deadline."""
        with self.lock:
            pid = speak_pid()
            if pid != self.pid:           # another speak hook (or none): its own sentences, its own holds
                self.pid, self.protect_since = pid, 0.0
                self._clear()
            if pid is None:
                return
            now = time.monotonic()
            paused = SPEAK_PAUSE_FILE.exists()
            fresh = now - _last_user_mark[0] < USER_QUIET_S
            if not self.t0:
                if paused or fresh:
                    self._begin("paused" if paused else "the user's sound just before it started",
                                now - _last_words_mark[0] < USER_QUIET_S)
            elif not (paused or fresh):
                self._clear()             # nothing of ours holds it now: the hook goes on by itself
            if (self.protect_since and now - self.last_claim >= PROTECT_QUIET_S
                    and HISTORY.completed_since(self.protect_since)):
                self.protect_since = 0.0
                log.info("the replayed sentence played through and the sound stopped; sound can hold the reading again")
            if self.t0 and not self.words:
                age = now - self.t0
                # Past the deadline only the user's voice going on, or (once it was on) a check of it still at Whisper, or
                # any check about to answer keeps the reading waiting; nothing keeps it past HOLD_MAX_S. A sound that
                # never sounded like the user's voice (eating, clicks, a cough) is let go at HOLD_CONFIRM_S; so is an
                # mm-hmm whose own transcript is still on its way (that transcript does not hold the reading).
                keep = age < HOLD_MAX_S and (self.voice_recent(now) or (self.spoke and self.check_at) or (
                    self.check_at and age < HOLD_CONFIRM_S + HOLD_CHECK_GRACE_S))
                if age >= HOLD_CONFIRM_S and not keep:
                    self.release("no words of the user's")

    def release(self, why: str, force: bool = False) -> bool:
        """Let the reading go on now, unless the user's words hold it (force: the final transcript says there were none):
        clear the stamp and remove the pause. The hook replays the sentence it stopped in; until that has played
        through, sound alone cannot hold it again."""
        with self.lock:
            if (self.words and not force) or speak_pid() is None:
                return False
            paused = SPEAK_PAUSE_FILE.exists()
            if not (self.t0 or paused or time.monotonic() - _last_user_mark[0] < USER_QUIET_S):
                return False
            age = time.monotonic() - self.t0 if self.t0 else 0.0
            _clear_stamp()
            if paused:
                try:
                    SPEAK_PAUSE_FILE.unlink()
                except OSError:
                    pass
            self._clear()
            self.protect_since = time.time()
            self.releases += 1
            log.info("resumed after %.1f s held (%s): it picks up the sentence it stopped in, which then plays through",
                     age, why)
            return True


HOLD = ReadingHold()


def mark_user_speaking(words: bool = False) -> bool:
    """Stamp the moment we heard the user; the speak hook will not start or continue a sentence while it is fresh. Since
    2026-10-05 a stamp from sound alone is provisional: see ReadingHold."""
    return HOLD.stamp(words)


CUTOFF_WORDS = 12


def speaking() -> bool:
    return SPEAK_PID_FILE.exists()


def in_boundary_cooldown() -> bool:
    """True for the first BOUNDARY_COOLDOWN_S after the speak hook started a new chunk."""
    try:
        return time.time() - SPEAK_PROGRESS_FILE.stat().st_mtime < BOUNDARY_COOLDOWN_S
    except OSError:
        return False


def pause_threshold_frames() -> int:
    return PAUSE_AFTER_VOICED_FRAMES_BOUNDARY if in_boundary_cooldown() else PAUSE_AFTER_VOICED_FRAMES


def read_progress() -> str:
    try:
        return SPEAK_PROGRESS_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def cutoff_note(progress: str) -> str:
    words = progress.split()
    if not words:
        return ""
    return '[voice: you were cut off after: "%s"]' % " ".join(words[-CUTOFF_WORDS:])


def pause_speak_hook(reason: str = "speech") -> bool:
    """Speech detected: pause the Claude Code Stop-hook voice. It resumes (replaying the
    sentence) if the utterance turns out to be nothing, and is killed if a message is delivered."""
    if not speaking():
        return False
    try:
        SPEAK_PAUSE_FILE.touch()
        log.info("paused (%s)", reason)
        return True
    except OSError:
        return False


def resume_speak_hook(reason: str = "no message delivered"):
    if not SPEAK_PAUSE_FILE.exists():
        return
    try:
        SPEAK_PAUSE_FILE.unlink()
        log.info("resumed (%s)", reason)
    except OSError:
        pass


class WordsHold:
    """2026-10-05 night. In one evening every reply that was cut short was cut by their own words ("Okay, I also-",
    "I don't know"): the inbox then killed the speak hook, the rest of the reply
    was never read, readings queued behind it were killed when the user's message was sent, and the reply to the user's message began
    "The part the voice cut" and was cut the same way. Now the user's words HOLD the reading instead of killing it: the pause stops the sound at
    once, this hold keeps it stopped while the user talks and while the user's words wait to be sent, and when the message is sent
    the hold ends; the hook waits out the user's 3 s of quiet, replays the sentence the user talked over and reads the rest, and the
    readings queued behind it follow in order. Only a hold phrase ("stop", "wait"), the mute phrase, the panel's stop
    or mute still end a reading for good."""

    def __init__(self):
        self.lock = threading.Lock()
        self.since = 0.0          # monotonic start (0: no hold)

    def active(self) -> bool:
        return self.since > 0.0

    def hold(self, reason: str) -> str:
        """Stop the sound now and hold the reading for the user's words. Returns the note for Claude ('' if nothing reads)."""
        pid = speak_pid()
        if pid is None:
            return ""
        progress = read_progress()
        with self.lock:
            fresh = not self.since
            if fresh:
                self.since = time.monotonic()
        try:
            SPEAK_TALKED_FILE.write_text(time.strftime("%H:%M:%S"))   # 2026-10-06: a newer reply now replaces this one
            SPEAK_HOLD_FILE.write_text(time.strftime("%H:%M:%S"))
            SPEAK_PAUSE_FILE.touch()              # the sound stops now; the hold keeps it stopped past the watchdog
        except OSError:
            pass
        skip_voicemode_playback()
        if fresh:
            log.info("%s -> reading held (pid %s) until the user's message is sent; then it replays the sentence the user talked "
                     "over and reads on", reason, pid)
        return rest_note(progress)

    def keep(self) -> None:
        """mark_keeper: keep the file fresh (the hook ignores a stale one: a dead inbox never holds it), and end a hold
        nothing of the user's backs any more."""
        if not self.since:
            return
        now = time.monotonic()
        if now - self.since > WORDS_HOLD_MAX_S:
            self.release("held %.0f s, the most it may be" % (now - self.since))
            return
        if now - max(_last_words_mark[0], self.since) > WORDS_HOLD_IDLE_S:
            self.release("no words of the user's for %.0f s" % WORDS_HOLD_IDLE_S)
            return
        try:
            SPEAK_HOLD_FILE.write_text(time.strftime("%H:%M:%S"))
        except OSError:
            pass

    def release(self, why: str) -> None:
        with self.lock:
            if not self.since:
                return
            age = time.monotonic() - self.since
            self.since = 0.0
        try:
            SPEAK_HOLD_FILE.unlink()
        except OSError:
            pass
        log.info("words hold ended after %.1f s (%s): the reading goes on after the user's quiet, from the sentence the user "
                 "talked over", age, why)


WORDS = WordsHold()


_LEAD_INS = {"no", "nope", "hey", "okay", "ok", "oh", "um", "uh", "yo", "dude", "bro", "please", "just", "no no"}


def starts_with_stop(text: str) -> bool:
    """The user's message opens with a hold phrase ("stop", "wait", "hold on", "shut up" ...), after lead-ins like "no" or
    "hey": then the reading ends for good instead of going on after the user's message."""
    toks = re.sub(r"[^a-z' ]", " ", text.lower()).split()
    while toks and toks[0] in _LEAD_INS:
        toks = toks[1:]
    for n in (3, 2, 1):
        if " ".join(toks[:n]) in STOP_WORDS:
            return True
    return False


def rest_note(progress: str) -> str:
    """Note for Claude when the user's words stop a reading that will go on afterwards (so it does not repeat it)."""
    words = progress.split()
    if not words:
        return ""
    return ('[voice: the user talked over you after: "%s"; the rest of that reply is dropped once you answer]' % " ".join(words[-CUTOFF_WORDS:]))


def view_mode() -> str:
    """What the Claude window shows, from target_watch's file (fresh) or a direct read: 'code' / 'chat' / 'unknown'."""
    try:
        if time.time() - VOICE_TARGET_FILE.stat().st_mtime < TARGET_POLL_S * 4:
            return VOICE_TARGET_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        pass
    return _read_view_mode()


def _read_view_mode() -> str:
    if not IS_WINDOWS:
        return "code"           # macOS / Linux: the tmux pane or app the backend types into runs Claude Code
    with _DELIVER_LOCK:
        try:
            if str(CODE_DIR) not in sys.path:
                sys.path.insert(0, str(CODE_DIR))
            import uia_deliver
            uia_deliver.WINDOW_TITLE = TARGET_TITLE
            return uia_deliver.view_mode()
        except Exception as e:
            log.warning("view mode: %s", e)
            return "unknown"


_HELD_STARTED = {}


def _read_shown(mode: str):
    """(known, record) for the chat the window shows. known False = cannot tell (the hook then reads every reply, as
    before); record None = no Claude Code chat on screen, or one whose title the app's session files do not have."""
    if mode == "chat":
        return True, None
    if mode != "code" or not IS_WINDOWS:
        return False, None      # macOS / Linux: no desktop-app sidebar to read; every reply is read
    with _DELIVER_LOCK:
        if str(CODE_DIR) not in sys.path:
            sys.path.insert(0, str(CODE_DIR))
        import uia_deliver
        import voice_chats
        uia_deliver.WINDOW_TITLE = TARGET_TITLE
        win = uia_deliver._claude_window()
        title = voice_chats.shown_title(win) if win is not None else None
    if not title:
        return False, None
    return True, voice_chats.chat_by_title(title)


def _play_held(rec) -> None:
    """Auto: the window shows a chat with a reply held for it: start the speak hook to play it (hidden)."""
    import voice_chats
    if rec is None or not rec["cli"] or voice_chats.read_pin():
        return
    sid8 = rec["cli"][:8]
    try:
        mt = (voice_chats.held_dir() / (sid8 + ".json")).stat().st_mtime
    except OSError:
        return
    if _HELD_STARTED.get(sid8) == mt:
        return
    _HELD_STARTED[sid8] = mt
    run_speak_hook("-Mode", "held", "-Session", sid8)
    log.info("window shows chat %r: playing the reply held for it", rec["title"])


def target_watch() -> None:
    """Keep claude-voice.target current (the speak hook reads it) and log each change of view. Since 2026-10-11 also
    claude-voice.shown (the chat on screen; in auto only its replies are read) and the start of held replies."""
    last, last_err, last_shown = None, 0.0, None
    while True:
        try:
            m = _read_view_mode()
            tmp = VOICE_TARGET_FILE.with_suffix(".tmp")
            tmp.write_text(m, encoding="utf-8")
            os.replace(tmp, VOICE_TARGET_FILE)
            if m != last:
                log.info("Claude window shows %s%s", m, "" if m == "code" else
                         ": the user's words are not delivered and replies are not read until it shows Claude Code")
                last = m
            known, rec = _read_shown(m)
            if known:
                import voice_chats
                voice_chats.write_shown(rec)
                key = rec["cli"] if rec else ""
                if key != last_shown:
                    log.info("chat on screen: %s", repr(rec["title"]) if rec else "none (replies are held)")
                    last_shown = key
                _play_held(rec)
        except Exception as e:
            if time.monotonic() - last_err > 60:
                last_err = time.monotonic()
                log.warning("target watcher: %s", e)
        time.sleep(TARGET_POLL_S)


def kill_speak_hook(reason: str = "message delivered") -> str:
    """Stop the Claude Code Stop-hook voice for good and flush queued speakers.
    Returns the cut-off note (what was being spoken) when a reading was actually cut, else ''.
    Since 2026-10-05 night only for a hold phrase, the mute phrase or a mute: the user's words hold instead (WordsHold)."""
    WORDS.release("ended for good: " + reason)
    try:
        pid = int(SPEAK_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        pid = None
    note = cutoff_note(read_progress()) if pid else ""
    try:
        SPEAK_CANCEL_FILE.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))   # queued speakers exit
    except OSError:
        pass
    if pid:
        try:
            os.kill(pid, 9)
            log.info("%s -> killed speak hook pid %d", reason, pid)
        except OSError:
            pass
        skip_voicemode_playback()
    for f in (SPEAK_PID_FILE, SPEAK_PAUSE_FILE, SPEAK_PROGRESS_FILE, SPEAK_PLAYING_FILE):
        try:
            f.unlink()
        except OSError:
            pass
    return note


def skip_voicemode_playback():
    """Cut VoiceMode's own converse playback (loopback control channel), if any. Runs in a
    daemon thread: nothing listens on the port unless VoiceMode is playing, and on Windows a
    refused loopback connect waits out the whole 0.5 s timeout (measured 514 ms) -- that stalled
    every interrupting message and the audio loop."""
    if BARGE_IN:
        threading.Thread(target=_skip_voicemode_playback, name="skip-forward", daemon=True).start()


def _skip_voicemode_playback():
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=0.5) as s:
            s.sendall((json.dumps({"command": "skip_forward"}) + "\n").encode())
        log.info("speech during playback -> skip_forward")
    except OSError:
        pass  # nothing is speaking


def barge_in(reason: str = "speech") -> bool:
    """Pause-then-confirm: pause the reading now; the transcript decides resume / kill."""
    if not BARGE_IN:
        return False
    return pause_speak_hook(reason)


STATE = {"muted": False, "echo_rms": 0.0,   # echo_rms: learned level of Claude's own voice at the mic
         "stt_hold": False,                  # the user's utterance is at Whisper: keep "the user is talking" fresh
         "held": False}                      # a hold phrase stopped the reading; nothing until the next real utterance


def load_state() -> None:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        e = float(data.get("echo_rms", 0))
        if e > 0:
            STATE["echo_rms"] = _echo_saved[0] = e
            log.info("echo level %.0f rms from %s (gate %.0f, saved %s)", e, STATE_FILE.name, e * ECHO_GATE_RATIO,
                     data.get("saved", "?"))
            return
    except (OSError, ValueError, TypeError):
        pass
    STATE["echo_rms"] = ECHO_RMS_DEFAULT
    log.info("no saved echo level; starting at %.0f rms (gate %.0f)", ECHO_RMS_DEFAULT,
             ECHO_RMS_DEFAULT * ECHO_GATE_RATIO)


def save_state() -> None:
    """Written whole and then moved into place, so a kill mid-write never leaves a broken file behind."""
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"echo_rms": round(STATE["echo_rms"], 1),
                                   "saved": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning("could not save %s: %s", STATE_FILE.name, e)


def frame_rms(frame: bytes) -> float:
    a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


_LAGS = np.arange(RATE // 400, RATE // 60)             # 40 .. 265 samples = 400 .. 60 Hz


def periodicity(frame: bytes) -> float:
    """Normalised autocorrelation peak in the voice-pitch range. Voiced speech (the user's or Kokoro's)
    scores 0.5-0.9; click/keyboard frames that webrtcvad passes score ~0 (2 of 345 kept at 0.3)."""
    x = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    x = x - x.mean()
    n = x.size
    spec = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(spec * np.conj(spec))[:n]
    if ac[0] <= 0:
        return 0.0
    norm = ac[0] * (n - _LAGS) / n
    return float(np.max(ac[_LAGS] / np.maximum(norm, 1e-9)))


def loud_enough(rms: float) -> bool:
    """True when a frame is clearly louder than the learned echo of the reading (or no echo is known yet)."""
    e = STATE["echo_rms"]
    return e <= 0 or rms >= e * ECHO_GATE_RATIO


def mark_frames(onset: bool) -> int:
    """Run of frames above the echo gate that counts as the user while the reading plays. Shorter while the
    learned echo is quiet (headset leak: Kokoro's own loud syllables never make even a 2-frame run
    above 2x its median there); the old lengths while it is loud (speakers: they do)."""
    quiet = 0 < STATE["echo_rms"] <= QUIET_ECHO_RMS
    if onset:
        return ONSET_MARK_FRAMES_QUIET if quiet else ONSET_MARK_FRAMES
    return AUDIBLE_MARK_FRAMES_QUIET if quiet else AUDIBLE_MARK_FRAMES


def learn_echo(voiced_rms, why: str = "") -> None:
    if not voiced_rms:
        return
    med = float(np.median(voiced_rms))
    e = STATE["echo_rms"]
    STATE["echo_rms"] = med if e <= 0 else 0.5 * e + 0.5 * med
    log.info("echo level now %.0f rms (gate %.0f)%s", STATE["echo_rms"], STATE["echo_rms"] * ECHO_GATE_RATIO,
             (" from " + why) if why else "")
    _echo_saved[0] = STATE["echo_rms"]
    save_state()


def echo_candidate(seq):
    """seq: one value per frame heard while the reading sounded, in order: the rms of a voiced frame with a pitch, 0 for
    any other frame (clicks and crunches have no pitch, so eating is left out). Returns (level, median, loudest 2-frame
    run, frames) or None: the median of the pitched frames, raised if needed so the gate stays ECHO_RUN_MARGIN above
    the loudest 2-frame run of them (the reading's own syllables must never count as the user)."""
    a = np.asarray(seq, dtype=np.float64)
    v = a[a > 0]
    if len(v) < ECHO_QUIET_MIN_FRAMES:
        return None
    run2 = float(np.max(np.minimum(a[:-1], a[1:])))
    med = float(np.median(v))
    return max(med, run2 * ECHO_RUN_MARGIN / ECHO_GATE_RATIO), med, run2, len(v)


def learn_echo_quiet(seq) -> None:
    """2026-10-05: the headset's echo. Audio over the reading in which Whisper heard no words at all is the reading's
    leak (and room noise); see echo_candidate. This evidence keeps the level between ECHO_RMS_QUIET_MIN and the default:
    it can make the gate more sensitive than the default, never less (the user's voice must never raise the gate)."""
    got = echo_candidate(seq)
    if got is None:
        return
    cand, med, run2, nv = got
    e = STATE["echo_rms"]
    new = 0.5 * e + 0.5 * cand
    new = max(ECHO_RMS_QUIET_MIN, min(new, max(e, ECHO_RMS_DEFAULT)))
    STATE["echo_rms"] = new
    if abs(new - _echo_saved[0]) >= 3.0:     # the first time (nothing saved yet), or it moved: save it
        _echo_saved[0] = new
        log.info("echo level now %.0f rms (gate %.0f) from what the mic heard over the reading with no words in it "
                 "(median %.0f, loudest 2-frame run %.0f, %d pitched frames; kept within %.0f..%.0f)", new,
                 new * ECHO_GATE_RATIO, med, run2, nv, ECHO_RMS_QUIET_MIN, max(e, ECHO_RMS_DEFAULT))
        save_state()


_echo_saved = [0.0]


def pause_watchdog() -> None:
    """Whatever else happens (slow STT, a crash in the loop), the reading never stays paused
    longer than MAX_PAUSE_S."""
    while True:
        time.sleep(0.5)
        try:
            age = time.time() - SPEAK_PAUSE_FILE.stat().st_mtime
        except OSError:
            continue
        if age > MAX_PAUSE_S:
            try:
                SPEAK_PAUSE_FILE.unlink()
                log.warning("pause watchdog: paused %.1f s -> resumed", age)
            except OSError:
                pass


def mark_keeper() -> None:
    """The frame loop cannot stamp "the user is talking" while it waits on Whisper or on UI Automation.
    While the user's utterance is being transcribed, or the user's words are typed but not sent, keep the stamp
    fresh from here so the reading never starts into that gap (it would be cut a letter in).
    2026-10-05: the user's text waiting to be sent, or an utterance whose words are known, is backed by the user's words; an
    utterance at Whisper whose words are not known yet is only a claim (see ReadingHold). This thread also keeps the
    hold's deadline while the loop is busy."""
    last_err = 0.0
    while True:
        time.sleep(MARK_KEEP_S)
        try:
            hold = STATE["stt_hold"]
            if PENDING or hold == "words":
                mark_user_speaking(words=True)
            elif hold:
                mark_user_speaking()
            HOLD.tick()
            WORDS.keep()
        except Exception as e:
            if time.monotonic() - last_err > 60:
                last_err = time.monotonic()
                log.warning("mark keeper: %s", e)


# ---------------------------------------------------------------- transcription

def transcribe(pcm: bytes, want_words: bool = False):
    """One HTTP request to Whisper. Only ever called from SingleFlightStt's worker thread.
    Returns (text, words); words = [(token, start s, end s)] when want_words (audio that overlapped
    the reading: the times say which words were said while it was audible), else None."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
    body = buf.getvalue()
    boundary = "----voiceinbox%d" % int(time.time() * 1000)
    fields = [("model", "whisper-1"), ("language", "en"), ("temperature", "0")]
    if want_words:
        fields += [("response_format", "verbose_json"), ("timestamp_granularities[]", "word")]
    else:
        fields += [("response_format", "json")]
    parts = []
    for name, val in fields:
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, name, val)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"u.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n" % boundary).encode() + body + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(STT_URL, data=b"".join(parts),
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=STT_HTTP_TIMEOUT_S) as r:
        d = json.loads(r.read().decode("utf-8"))
    text = (d.get("text") or "").strip()
    if not want_words:
        return text, None
    raw = d.get("words") or [w for s in (d.get("segments") or []) for w in (s.get("words") or [])]
    words = []
    for w in raw:
        try:
            words.append((str(w["word"]), float(w["start"]), float(w["end"])))
        except (KeyError, TypeError, ValueError):
            continue
    return text, (words or None)


class SttJob:
    def __init__(self, pcm: bytes, final: bool, ctx=None, on_done=None, want_words=False):
        self.pcm, self.final = pcm, final
        self.ctx = ctx or {}
        self.on_done = on_done      # background jobs: called (in the stt thread) when finished
        self.want_words = want_words
        self.words = None
        self.submitted = time.monotonic()
        self.text = None
        self.error = None
        self.finished = False
        self.abandoned = False      # the loop stopped waiting; a late final result goes to on_late
        self.done = threading.Event()


class SingleFlightStt:
    """At most ONE request to Whisper at any time. A request is never abandoned mid-flight
    (the server would keep chewing it and new ones would queue behind it). Final transcripts
    wait their turn in order; a mid-utterance check is refused outright when Whisper is busy."""

    def __init__(self, on_late=None):
        self._cv = threading.Condition()
        self._queue = []
        self._busy = None
        self.on_late = on_late
        threading.Thread(target=self._run, name="stt", daemon=True).start()

    def submit(self, pcm: bytes, final: bool, ctx=None, on_done=None, want_words=False) -> SttJob:
        job = SttJob(pcm, final, ctx, on_done, want_words)
        dropped = []
        with self._cv:
            if not final and (self._busy is not None or self._queue):
                job.error, job.finished = "stt busy", True
                job.done.set()
                dropped.append(job)
            else:
                if final:   # a pending mid-utterance check is superseded by the final
                    for j in [j for j in self._queue if not j.final]:
                        self._queue.remove(j)
                        j.error, j.finished = "superseded", True
                        j.done.set()
                        dropped.append(j)
                self._queue.append(job)
                self._cv.notify()
        for j in dropped:           # background jobs always hear back, even when refused
            if j.on_done:
                try:
                    j.on_done(j)
                except Exception as e:
                    log.warning("stt hand-off error: %s", e)
        return job

    def final_in_flight(self) -> bool:
        """A final transcript is queued or running. A background check of audio heard over the
        reading only holds back a pending Enter for CHECK_FLUSH_WAIT_S."""
        now = time.monotonic()

        def holds(j):
            return j.final and (j.ctx.get("kind") != "check" or now - j.submitted < CHECK_FLUSH_WAIT_S)
        with self._cv:
            return (self._busy is not None and holds(self._busy)) or any(holds(j) for j in self._queue)

    def wait(self, job: SttJob, budget: float) -> bool:
        """True if the job finished within budget (job.text / job.error are set)."""
        job.done.wait(budget)
        with self._cv:
            if job.finished:
                return True
            job.abandoned = True
            return False

    def _run(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                job = self._queue.pop(0)
                self._busy = job
            text = err = words = None
            if job.abandoned and time.monotonic() - job.submitted > STT_LATE_MAX_S:
                err = "stale, not sent"
            else:
                t0 = time.monotonic()
                try:
                    text, words = transcribe(job.pcm, job.want_words)
                except Exception as e:
                    err = str(e) or e.__class__.__name__
                took = time.monotonic() - t0
                if took > 5:
                    log.warning("stt slow: %.1f s for %.1f s of audio", took, len(job.pcm) / 2 / RATE)
            with self._cv:
                job.text, job.words, job.error, job.finished = text, words, err, True
                self._busy = None
                late = job.abandoned
            job.done.set()
            try:
                if job.on_done:
                    job.on_done(job)
                elif late and job.final and self.on_late:
                    self.on_late(job)
            except Exception as e:
                log.warning("stt result hand-off error: %s", e)


# Finished background transcripts (audio heard over the reading, early checks, late finals),
# handed from the stt thread to the frame loop, which acts on them in order.
RESULTS = collections.deque()


# ---------------------------------------------------------------- delivery (type into Claude Code)

user32 = ctypes.windll.user32 if IS_WINDOWS else None
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", W.DWORD), ("u", _U)]


def _send(inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def type_chars(text: str):
    ins = []
    for ch in text:
        for flag in (0, KEYEVENTF_KEYUP):
            i = INPUT(); i.type = INPUT_KEYBOARD
            i.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | flag, 0, None)
            ins.append(i)
    if ins:
        _send(ins)


def press_enter():
    down = INPUT(); down.type = INPUT_KEYBOARD; down.ki = KEYBDINPUT(VK_RETURN, 0, 0, 0, None)
    up = INPUT(); up.type = INPUT_KEYBOARD; up.ki = KEYBDINPUT(VK_RETURN, 0, KEYEVENTF_KEYUP, 0, None)
    _send([down, up])


def type_text(text: str):
    type_chars(text)
    time.sleep(0.15)
    press_enter()


def find_window(title: str):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, b, n + 1)
                if b.value == title:
                    found.append(h)
        return True
    user32.EnumWindows(cb, 0)
    return found[0] if found else None


_DELIVER_LOCK = threading.Lock()   # the loop and a late STT result may both deliver

# macOS / Linux: tmux pane or macOS app (delivery_posix.py). None on Windows, which keeps UI Automation.
POSIX_SINK = None
if not IS_WINDOWS:
    import delivery_posix
    POSIX_SINK = delivery_posix.sink()


def deliver(text: str) -> bool:
    with _DELIVER_LOCK:
        return _deliver(text)


def _deliver(text: str) -> bool:
    """Send text into Claude WITHOUT stealing focus (UI Automation, see uia_deliver.py).
    Falls back to keystrokes only when the Claude window is already in front; it never
    calls SetForegroundWindow, so a game or other app in front is left alone."""
    if POSIX_SINK is not None:
        return POSIX_SINK.deliver(text)
    try:
        sys.path.insert(0, str(CODE_DIR))
        from uia_deliver import deliver as uia_send
        import uia_deliver
        w = uia_deliver._claude_window()
        if w is not None and not _show_pinned(w):
            log.warning("pinned chat could not be shown; dropped: %r", text)
            return False
        if uia_send(text):
            return True
        log.warning("uia delivery failed; trying keystroke fallback")
    except Exception as e:
        log.warning("uia delivery error: %s", e)
    h = find_window(TARGET_TITLE)
    if not h:
        log.warning("no window titled %r; dropped: %r", TARGET_TITLE, text)
        return False
    if user32.GetForegroundWindow() != h:
        log.warning("Claude not in front and uia failed; dropped (not stealing focus): %r", text)
        return False
    if _pin_set():
        log.warning("voice is pinned to a chat that could not be shown; dropped: %r", text)
        return False
    type_text(text)
    return True


# ---- staged delivery (2026-09-25): type now, press Enter when the user has been quiet for the merge window

def _ws(s: str) -> str:
    return " ".join((s or "").split())


def _uia_target():
    """(window wrapper, chat box) for the Claude window, found by handle (about 15 ms; the
    Desktop().windows() scan in uia_deliver takes about 1.7 s). Never touches focus."""
    if str(CODE_DIR) not in sys.path:
        sys.path.insert(0, str(CODE_DIR))
    import uia_deliver
    uia_deliver.WINDOW_TITLE = TARGET_TITLE
    win = uia_deliver._claude_window()          # EnumWindows + cached wrapper
    if win is None:
        return None, None
    if not _show_pinned(win):
        return win, None
    return win, uia_deliver._chat_input(win)


def _pin_set() -> bool:
    """Voice is pinned to one chat (the panel's chat list; voice_chats.py). Then keystrokes are never a fallback:
    they would land in whatever chat the window shows."""
    try:
        import voice_chats
        return voice_chats.read_pin() is not None
    except Exception:
        return False


def _show_pinned(win) -> bool:
    """2026-10-10, chat picking. With no pin: True (auto, the chat the window shows, as before). Pinned: make the
    window show that chat first (a UIA Invoke on its sidebar entry; the chat on screen changes, focus does not).
    False = it could not be shown, so nothing is typed anywhere."""
    try:
        import voice_chats
        rec = voice_chats.pinned()
        if rec is None:
            return True
        ok, how = voice_chats.show_chat(win, rec["title"])
    except Exception as e:
        log.warning("pinned chat: %s", e)
        return False
    if how != "already shown":
        (log.info if ok else log.warning)("pinned chat %r: %s", rec["title"], how)
    return ok


def _uia_forget():
    try:
        import uia_deliver
        uia_deliver._forget()
    except Exception:
        pass


def stage_text(text: str, first: bool) -> str:
    """Put `text` at the end of the Claude box WITHOUT sending. Returns how: 'uia', 'keys' or ''."""
    with _DELIVER_LOCK:
        if POSIX_SINK is not None:
            return POSIX_SINK.stage(text, first)
        try:
            win, box = _uia_target()
            if box is not None:
                cur = (box.get_value() or "").strip()
                box.set_edit_text((cur + " " + text) if cur else text)
                return "uia"
            log.warning("stage: no chat input exposed")
        except Exception as e:
            _uia_forget()
            log.warning("stage: uia error: %s", e)
        h = find_window(TARGET_TITLE)
        if h and user32.GetForegroundWindow() == h and not _pin_set():
            type_chars(text if first else " " + text)
            return "keys"
        return ""


def send_staged(parts, staged, note: str) -> bool:
    """Make sure every part (and the cut-off note) is in the box, then press Send / Enter once.
    Parts already staged are not typed again; anything missing (box cleared, staging failed) is added."""
    with _DELIVER_LOCK:
        if POSIX_SINK is not None:
            return POSIX_SINK.send(parts, staged, note)
        try:
            win, box = _uia_target()
            if box is not None:
                import uia_deliver
                cur = (box.get_value() or "").strip()
                have = _ws(cur)
                missing = [p for p in parts if _ws(p) not in have]
                if missing:
                    log.warning("send: %d part(s) not in the box, adding them", len(missing))
                add = " ".join(missing + ([note] if note else []))
                if add:
                    box.set_edit_text((cur + " " + add) if cur else add)
                    btn = uia_deliver.wait_send_button(win)   # appears once the editor has the text
                else:
                    btn = uia_deliver._send_button(win)       # already typed: there now, or Claude is busy
                if btn:
                    btn.invoke()
                else:
                    uia_deliver._post_enter(win)   # busy: no Send button, Enter queues it (no focus change)
                return True
            log.warning("send: no chat input exposed")
        except Exception as e:
            _uia_forget()
            log.warning("send: uia error: %s", e)
        h = find_window(TARGET_TITLE)
        if not h:
            log.warning("no window titled %r", TARGET_TITLE)
            return False
        if user32.GetForegroundWindow() != h:
            log.warning("Claude not in front and uia failed; not stealing focus")
            return False
        if _pin_set():
            log.warning("voice is pinned to a chat that could not be shown; not typing keys into another chat")
            return False
        rest = [p for p, how in zip(parts, staged) if not how] + ([note] if note else [])
        type_chars((" " + " ".join(rest)) if rest else "")
        time.sleep(0.15)
        press_enter()
        return True


class Pending:
    """Text typed into the box but not sent yet. Touched only by the frame loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.parts = []            # transcripts, in order
        self.staged = []           # how each part went in: 'uia' | 'keys' | ''
        self.notes = []            # cut-off notes, appended once at the end
        self.dur = 0.0             # seconds of speech merged
        self.last_add = 0.0        # monotonic time of the last part
        self.hold_until_a = 0.0    # audio clock: a hold phrase keeps it unsent until then

    def __bool__(self):
        return bool(self.parts)


PENDING = Pending()

# ---------------------------------------------------------------- main loop

class Utterance:
    """Per-utterance state for the frame loop. pre = [(frame, capture time, voiced, rms, voice, pitched)] heard
    before it started (up to PRE_ROLL_FRAMES); voiced = webrtcvad mode 3, voice = voiced or quiet voice,
    pitched = it has a voice pitch (periodicity)."""

    def __init__(self, pre, t_start, aud, soft, uid):
        self.uid = uid
        self.frames = [p[0] for p in pre]
        self.t0 = pre[0][1] if pre else t_start   # capture time of the first frame (pre-roll included)
        self.t1 = t_start                          # capture time of the latest frame
        self.soft = soft                           # started by the quiet-speech rule
        self.end_ms = SOFT_END_MS if soft else END_SILENCE_MS
        self.started = time.monotonic()
        self.silence_ms = 0
        self.voiced_run = 0
        recent = pre[-START_FRAMES:]
        self.voice_total = sum(1 for p in pre if p[4])   # frames with any voice (nothing in it -> dropped)
        self.voiced_total = sum(1 for p in pre if p[2])
        self.voiced_rms = [p[3] for p in recent if p[2]]     # loudness of each voiced frame (echo guard)
        self.loud_voiced = sum(1 for r in self.voiced_rms if loud_enough(r))
        # 2026-10-05: voice with a pitch. Only an utterance with some holds the reading; only loud pitched frames pause it.
        self.pitched_total = sum(1 for p in pre if p[4] and p[5])
        self.loud_pitched = sum(1 for p in recent if p[2] and p[5] and loud_enough(p[3]))
        self.barge_base = 0                  # loud_pitched when the reading was last released (the replay needs more)
        self.words = False                   # a check found the user's words in it
        self.audible_rms = [p[3] for p in pre if p[2] and HISTORY.audible_at(p[1])]   # voiced over the reading
        # per frame heard while the reading sounded: rms if voiced with a pitch, else 0 (learn_echo_quiet)
        self.audible_seq = [p[3] if (p[2] and p[5]) else 0.0 for p in pre if HISTORY.audible_at(p[1])]
        self.paused = False                  # reading paused by this utterance
        self.paused_at = 0.0
        self.killed = False                  # reading killed mid-utterance (real words confirmed early)
        self.note = ""                       # cut-off note captured when the reading was killed
        self.floor_done = False              # endpoint floor handled for the current silence run
        self.extending = False               # past the floor, waiting up to the ceiling for more
        self.pending_text = ""
        self.during_playback = aud           # began while the reading was actually sounding
        self.heard_playback = aud            # the reading sounded at some point during it
        self.quiet_voiced = 0                # voiced frames while nothing was sounding (it is the user)
        self.pause_idx = 0                   # len(frames) when the reading was paused
        self.pause_nvoiced = 0
        self.confirm_voiced = 0              # voiced frames after the pause (the reading is silent then)
        self.confirmed = False               # voice continued after the pause -> it is the user, not the echo
        self.suspect = False                 # the voice stopped when the reading paused: the transcript decides
        self.echo_before = []
        self.next_check_s = ECHO_PARTIAL_S   # audio length at which the next background check runs
        self.check_busy = False              # a background check of this utterance is at Whisper

    def seconds(self) -> float:
        return len(self.frames) * FRAME_MS / 1000.0

    def unproven(self) -> bool:
        """Heard over the reading and not (yet) shown to be the user by loudness, by going on after the
        reading stopped, or by its words."""
        return self.heard_playback and not (self.paused or self.killed or self.confirmed)


def file_muted() -> bool:
    _tmp = TEMP_DIR
    return (_tmp / "claude-speak.muted").exists() or (_tmp / "claude-listen.muted").exists()


def on_late(job: SttJob) -> None:
    """A final transcript that came back after the loop stopped waiting: hand it to the loop, which
    removes the reading's words and types it if it is a real message (so the user's words are not lost
    when Whisper is slow)."""
    RESULTS.append(("late", job))


def main():
    setup_log()
    if single_instance("voice_inbox") is None:
        log.info("another voice_inbox is already running; this copy exits")
        return 0
    load_state()
    vad = webrtcvad.Vad(VAD_MODE)
    vad2 = webrtcvad.Vad(QUIET_VAD_MODE)    # quiet voice, only while the reading is silent
    q: "queue.Queue" = queue.Queue()
    stt = SingleFlightStt(on_late=on_late)
    resume_speak_hook("inbox starting: stale pause")
    threading.Thread(target=pause_watchdog, name="pause-watchdog", daemon=True).start()
    threading.Thread(target=mark_keeper, name="mark-keeper", daemon=True).start()
    threading.Thread(target=reading_watch, name="reading-watch", daemon=True).start()
    try:
        SPEAK_HOLD_FILE.unlink()                # a hold left by an earlier copy
    except OSError:
        pass
    threading.Thread(target=target_watch, name="target-watch", daemon=True).start()
    try:
        _uia_target()          # load pywinauto / COM now, not on the user's first message (read only)
    except Exception as e:
        log.warning("uia warm-up: %s", e)
    clk = {"a": 0.0, "voice": -1e9}   # audio clock (s of mic audio processed) and when the user last made voice
    mic = {"overflow": 0}             # PortAudio input overflows (audio the driver dropped)
    cur = {"u": None}                 # the utterance in progress, for background results

    def cb(indata, frames, t, status):
        if status and status.input_overflow:
            mic["overflow"] += 1
        q.put((bytes(indata), time.time()))

    def let_go(reason: str) -> None:
        """A transcript of nothing of the user's: the reading goes on now (2026-10-05). While another utterance of the user's is
        under way, the hold's own deadline decides instead; while the user's words wait to be sent, they hold it."""
        if cur["u"] is None and not PENDING:
            WORDS.release(reason)
            if not HOLD.release(reason, force=True):
                resume_speak_hook(reason)

    def add_part(text: str, dur: float, note: str) -> None:
        """Type one transcript into the box (no Enter); it joins the unsent message."""
        with PENDING.lock:
            first = not PENDING.parts
        how = stage_text(text, first)
        with PENDING.lock:
            PENDING.parts.append(text)
            PENDING.staged.append(how)
            if note and note not in PENDING.notes:
                PENDING.notes.append(note)
            PENDING.dur += dur
            PENDING.last_add = time.monotonic()
            PENDING.hold_until_a = 0.0
            n = len(PENDING.parts)
        log.info("staged part %d (%s) %.1fs: %r; Enter after %.1f s with no voice",
                 n, how or "not in the box yet", dur, text, MERGE_WINDOW_S)

    def flush(reason: str) -> None:
        """Press Enter once for everything typed so far."""
        with PENDING.lock:
            parts, staged, notes, dur = PENDING.parts, PENDING.staged, PENDING.notes, PENDING.dur
            PENDING.parts, PENDING.staged, PENDING.notes, PENDING.dur = [], [], [], 0.0
            PENDING.hold_until_a = 0.0
        if not parts:
            WORDS.release(reason)
            return
        # Was kill_speak_hook("message sent"): every reading that waited while the user talked was thrown away unheard
        # (19:51:40, 19:51:46, 19:52:18, 21:44:06, 21:44:47, 22:03:17 on 10-05). Now they are read after the user's message,
        # in order, starting with the one the user talked over.
        note = " ".join(notes)
        mode = view_mode()
        if mode == "chat":
            log.info("send: the Claude window shows %s, not Claude Code; not sent (%s): %r", mode, reason,
                     " ".join(parts))
            WORDS.release("not sent: the window shows %s" % mode)
            return
        ok = send_staged(parts, staged, note)
        WORDS.release("the user's message was sent" if ok else "the user's message could not be sent")
        log.info("send (%s): %d part(s)", reason, len(parts))
        log.info("%s %.1fs: %r", "typed" if ok else "DROPPED", dur, " ".join(parts))
        if note and ok:
            log.info("cut-off note delivered: %s", note)

    def act(text: str, dur: float, paused=False, killed=False, note="", over_reading=False, t0=None) -> None:
        """Act on one finished transcript (the reading's own words already removed).
        paused/killed: this utterance paused / killed the reading. over_reading: heard over a reading
        that was still playing and never shown to be the user by loudness, so a lone word is not enough."""
        kind = classify(text)
        cmd = mute_command(text)
        if cmd == "mute":
            flush("before the mute phrase: sending what the user already said")
            STATE["muted"] = True
            kill_speak_hook("mute command %r" % text)
            try:
                (TEMP_DIR / "claude-speak.muted").touch()   # speak hook stays silent too
            except OSError:
                pass
            log.info("MUTED by %r; nothing delivered and nothing spoken until an unmute phrase", text)
            return
        if cmd == "unmute":
            STATE["muted"] = False
            for _f in ("claude-speak.muted", "claude-listen.muted", "claude-voice.muted"):
                try:
                    (TEMP_DIR / _f).unlink()
                except OSError:
                    pass
            log.info("UNMUTED by %r", text)
            return
        fmuted = file_muted()
        if fmuted and not STATE["muted"]:
            log.info("MUTED via panel/file; toggle the panel or say an unmute phrase to resume")
        if STATE["muted"] and not fmuted and cmd != "mute":
            STATE["muted"] = False   # panel switched listening back on
            log.info("UNMUTED via panel/file")
        if STATE["muted"] or fmuted:
            log.info("muted: ignored %r (%.1fs)", text, dur)
            let_go("muted; ignoring %r" % text)
            return
        mode = view_mode()
        nav = nav_command(text)
        # when the user's voice started: t0 is the first frame kept, which includes up to PRE_ROLL_FRAMES before it
        began = (t0 + PRE_ROLL_FRAMES * FRAME_MS / 1000.0) if t0 else (time.time() - dur)
        if nav and not (over_reading or paused or killed or reading_near(began)):
            log.info("%r is a %s command, but no reply was being read (or ended within %.0f s): the user's speech, as before",
                     text, nav, NAV_AFTER_S)
            nav = ""
        if nav:
            # 2026-10-10: "next" / "back" / "again" move the reading and are never typed (like the hold phrases).
            if mode == "chat":
                log.info("%s %r (%.1fs): the Claude window shows a chat, nothing is read there; nothing delivered",
                         nav, text, dur)
            else:
                try:
                    if str(CODE_DIR) not in sys.path:
                        sys.path.insert(0, str(CODE_DIR))
                    import speak_nav
                    did = speak_nav.nav(nav)
                except Exception as e:
                    did = "failed: %s" % e
                log.info("%s: %r (%.1fs) -> %s; nothing delivered", nav, text, dur, did)
            let_go("%s command %r" % (nav, text))     # the reading goes on now, from where the command sends it
            return
        if mode == "chat" and kind not in ("junk", "backchannel"):
            # 2026-10-05: voice is for Claude Code only. With a claude.ai chat on screen (it has its own voice mode)
            # nothing the user says is typed anywhere; it is not kept for later either.
            log.info("not delivered: the Claude window shows %s, not Claude Code: %r (%.1fs)", mode, text, dur)
            let_go("the window shows %s" % mode)
            return
        if kind == "junk":
            log.info("dropped junk %r (%.1fs)", text, dur)
            let_go("junk %r" % text)
        elif kind == "backchannel":
            log.info("dropped backchannel %r (%.1fs)", text, dur)
            let_go("backchannel %r" % text)
        elif kind == "hold":
            kill_speak_hook("hold phrase %r" % text)
            STATE["held"] = True
            if PENDING.parts:
                PENDING.hold_until_a = clk["voice"] + HOLD_MERGE_S
                log.info("held: %r (%.1fs); unsent text waits %.1f s for more", text, dur, HOLD_MERGE_S)
            else:
                log.info("held: %r (%.1fs); nothing delivered until the next utterance", text, dur)
        elif kind == "short" and (paused or killed or over_reading):
            log.info("dropped single word during playback %r (%.1fs)", text, dur)
            let_go("single word %r while speaking" % text)
        else:
            # The reading must never talk over the user. A delivered message means the user is engaging, so the
            # reading stops while the user talks (since 2026-10-05 night it is held, not killed, and goes on after the user's
            # message is sent; see WordsHold). Sound that turned out to be nothing resumes it at once.
            if not PENDING.parts and starts_with_stop(text):
                # "No, stop, that is the wrong folder": the user wants it to stop, so it ends for good as before. Only the
                # first part of the user's message (the one that cut in); a "wait" later in the user's talk is not said to the reading.
                n = kill_speak_hook("the user's message starts with a stop phrase %r" % text)
            else:
                n = note if killed else WORDS.hold("the user's message")
            add_part(text, dur, n)     # typed now; Enter after MERGE_WINDOW_S of quiet
            if STATE["held"]:
                log.info("held cleared by a real message")
                STATE["held"] = False

    def drain_results() -> None:
        """Act on background transcripts, oldest first (before any newer utterance is typed)."""
        while RESULTS:
            try:
                kind, job = RESULTS.popleft()
            except IndexError:
                return
            try:
                handle_result(kind, job)
            except Exception as e:
                log.warning("background result error: %s", e)

    def handle_result(kind: str, job: SttJob) -> None:
        ctx = job.ctx
        if kind == "hold":                   # 2026-10-05: is there anything of the user's in what holds the reading?
            if job.error in ("stt busy", "superseded"):     # (the verdict itself was taken in hold_done)
                return
            if job.error or job.text is None:
                log.warning("hold check failed: %s", job.error)
                return
            his = ctx.get("his", "")
            if ctx.get("nav"):
                log.info("hold check (%.1f s): a %s command %r; the reading waits for the final transcript",
                         ctx["dur"], ctx["nav"], his)
                return
            if ctx.get("words"):
                u = cur["u"]
                if u is not None and u.uid == ctx.get("uid"):
                    u.words = True
                    if not u.killed:
                        u.note = WORDS.hold("the user's words while the reading was held %r" % his)
                        u.killed, u.paused = True, False
                log.info("hold check (%.1f s): the user's words %r; the reading waits until the user's message is sent",
                         ctx["dur"], his)
            else:
                log.info("hold check (%.1f s): no words of the user's yet: %r", ctx["dur"], his)
            return
        if kind == "partial":                # early check of audio heard over the reading
            u = cur["u"]
            if u is None or u.uid != ctx.get("uid"):
                return
            u.check_busy = False
            if job.error == "stt busy" or job.error == "superseded":
                u.next_check_s = u.seconds() + 1.0
                return
            if job.error or job.text is None or not u.unproven():
                return
            his, removed = strip_reading(job.text, HISTORY.texts_between(ctx["t0"], ctx["t1"]),
                                         job.words, ctx["t0"])
            if real_words(his) >= EARLY_KILL_WORDS and classify(his) == "message":
                u.note = WORDS.hold("the user's words under the reading %r" % his)
                u.killed = u.confirmed = True
                log.info("early check: the user's words under the reading (%d words of the reading removed): %r",
                         removed, his)
            return
        dur = ctx.get("dur", len(job.pcm) / 2 / RATE)
        if job.error or job.text is None:
            log.warning("stt failed for %.1f s heard %s: %s", dur,
                        "over the reading" if kind == "check" else "(late)", job.error)
            return
        readings = HISTORY.texts_between(ctx["t0"], ctx["t1"])
        his, removed = strip_reading(job.text, readings, job.words, ctx["t0"])
        if removed:
            log.info("removed %d words of the reading from %r", removed, job.text)
        if kind == "late":
            age = time.monotonic() - job.submitted
            if classify(his) != "message" or mute_command(his) or nav_command(his):
                log.info("late transcript not a message, dropped (%.1f s late): %r", age, his)
                return
            if age > STT_LATE_MAX_S:
                log.info("late transcript too old (%.1f s), dropped: %r", age, his)
                return
            log.info("late transcript (%.1f s after the utterance) for the message %.1fs: %r", age, dur, his)
            act(his, dur)
            return
        # kind == "check": audio heard over a playing reading, never shown to be the user by loudness
        if nav_command(his):
            log.info("over the reading: a %s command in %.1f s: %r", nav_command(his), dur, his)
            act(his, dur, over_reading=True)
            return
        k = classify(his)
        if k in ("junk", "backchannel"):
            if removed >= 3 and k == "junk":
                # 2026-10-05: its pitched frames only (was every voiced frame: chewing under the reading was
                # learned as the reading's echo and raised the gate to 517 in the harness)
                got = echo_candidate(ctx.get("audible_seq") or [])
                if got is not None:
                    learn_echo([got[0]], "audio that was only the reading (median %.0f, loudest 2-frame run %.0f, "
                                         "%d pitched frames)" % got[1:])
            elif not removed and classify(job.text) == "junk":
                learn_echo_quiet(ctx.get("audible_seq") or [])    # Whisper heard no words at all: the leak
            log.info("over the reading: %.1f s was the reading itself or noise (%d words matched), "
                     "nothing of the user's: %r", dur, removed, job.text)
            let_go("nothing of the user's over the reading: %r" % job.text)
            return
        log.info("over the reading: the user's words in %.1f s (%d words of the reading removed): %r", dur, removed, his)
        act(his, dur, over_reading=True)

    def submit_check(u, reason: str) -> None:
        """Transcribe audio heard over the reading in the background; the loop keeps listening."""
        pcm = b"".join(u.frames)
        dur = len(pcm) / 2 / RATE
        ctx = {"kind": "check", "t0": u.t0, "t1": u.t1, "dur": dur, "audible_rms": list(u.audible_rms),
               "audible_seq": list(u.audible_seq)}
        stt.submit(pcm, final=True, ctx=ctx, on_done=lambda j: RESULTS.append(("check", j)), want_words=True)
        log.info("%.1f s heard over the reading (%s): sent to stt to separate the user's words from the reading",
                 dur, reason)

    def hold_done(job: SttJob) -> None:
        """A hold check is back (stt thread). The verdict is taken here, at once, so the deadline never lets the
        reading go while the user's words are already known; the loop then stops the reading and logs (handle_result)."""
        ctx = job.ctx
        busy = job.error in ("stt busy", "superseded")
        if not job.error and job.text is not None:
            his, removed = strip_reading(job.text, HISTORY.texts_between(ctx["t0"], ctx["t1"]), job.words, ctx["t0"])
            ctx["his"] = his
            if nav_command(his):
                ctx["nav"] = nav_command(his)    # keep it held until the final transcript moves it (no replay blip)
                HOLD.confirm()
            elif classify(his) in ("message", "hold"):
                ctx["words"] = True
                HOLD.confirm()
        HOLD.check_back(ctx.get("hold", 0.0), busy)
        RESULTS.append(("hold", job))

    def submit_hold_check(u) -> None:
        """2026-10-05: transcribe what holds the reading (the utterance so far, at most its last HOLD_CHECK_MAX_S) in
        the background. The user's words stop the reading for good; nothing of the user's lets the deadline release it."""
        fr = u.frames[-int(HOLD_CHECK_MAX_S * 1000 / FRAME_MS):]
        t0 = u.t0 + (len(u.frames) - len(fr)) * FRAME_MS / 1000.0
        reads = HISTORY.texts_between(t0, u.t1)
        ctx = {"kind": "hold", "uid": u.uid, "t0": t0, "t1": u.t1, "dur": len(fr) * FRAME_MS / 1000.0,
               "hold": HOLD.check_sent()}
        stt.submit(b"".join(fr), final=False, ctx=ctx, on_done=hold_done, want_words=bool(reads))

    def drain_hold_results() -> None:
        """While the loop waits on Whisper for a final transcript: act on hold checks now (the user's words stop the
        reading at once); every other background result waits its turn, in order."""
        keep = []
        while RESULTS:
            try:
                item = RESULTS.popleft()
            except IndexError:
                break
            if item[0] == "hold":
                try:
                    handle_result(*item)
                except Exception as e:
                    log.warning("background result error: %s", e)
            else:
                keep.append(item)
        for item in reversed(keep):
            RESULTS.appendleft(item)

    def service_pending(in_speech: bool) -> None:
        if not PENDING.parts:
            return
        if STATE["muted"] or file_muted():
            flush("listening muted: sending what the user already said")
            return
        if in_speech or stt.final_in_flight():
            return
        quiet = clk["a"] - clk["voice"]
        if quiet >= MERGE_WINDOW_S and clk["a"] >= PENDING.hold_until_a:
            flush("%.1f s with no voice" % quiet)
        elif time.monotonic() - PENDING.last_add > MERGE_MAX_WAIT_S:
            flush("nothing new for %.0f s" % MERGE_MAX_WAIT_S)

    def stt_now(pcm: bytes, final: bool, hold=False, ctx=None, want_words: bool = False):
        """(transcript text or None, error or None, word times or None). None text when Whisper
        failed / was busy / was too slow. hold: it is the user's utterance, so keep "the user is talking" fresh
        while this thread waits ("words": its words are known; True: a claim, see ReadingHold).
        want_words: the audio overlapped the reading (see strip_reading)."""
        job = stt.submit(pcm, final, ctx=ctx, want_words=want_words)
        STATE["stt_hold"] = hold
        try:
            budget = time.monotonic() + (STT_FINAL_WAIT_S if final else STT_PARTIAL_WAIT_S)
            while not job.done.wait(0.05) and time.monotonic() < budget:
                drain_hold_results()
            got = stt.wait(job, 0)
        finally:
            STATE["stt_hold"] = False
        if not got:
            log.warning("stt %s: no answer within %.0f s%s", "final" if final else "partial",
                        STT_FINAL_WAIT_S if final else STT_PARTIAL_WAIT_S,
                        "; it will be typed when it arrives" if final else "")
            return None, "timeout", None
        if job.error:
            if job.error != "stt busy":
                log.warning("stt failed (%s): %s", "final" if final else "partial", job.error)
            return None, job.error, None
        return job.text, None, job.words

    if POSIX_SINK is not None:
        log.info("delivery: %s", POSIX_SINK.describe())
    log.info("voice inbox starting (stt %s, window %r, barge_in %s, floor %d ms, ceiling +%d ms, "
             "barge %d/%d ms, false-barge timeout %.1f s, max pause %.1f s, single-flight stt)",
             STT_URL, TARGET_TITLE, BARGE_IN, END_SILENCE_MS, EXTEND_SILENCE_MS,
             BARGE_VOICED_MS, BARGE_VOICED_MS_BOUNDARY, HOLD_CONFIRM_S, MAX_PAUSE_S)
    log.info("holds: only pitched voice stamps the user; a hold needs the user's words within %.1f s (first check at %.1f s) or "
             "the reading resumes, unless the user's voice is going on (then up to %.1f s); the replayed sentence then stops "
             "only for the user's words or %d ms of loud pitched voice", HOLD_CONFIRM_S, HOLD_FIRST_CHECK_S, HOLD_MAX_S,
             PROTECT_BARGE_FRAMES * FRAME_MS)
    log.info("merge window %.1f s (hold %.1f s, max %.0f s); onset guard %.1f s / %d frames (%d quiet echo), "
             "audible run %d frames (%d quiet echo), echo tail %.2f s",
             MERGE_WINDOW_S, HOLD_MERGE_S, MERGE_MAX_WAIT_S, ONSET_GUARD_S, ONSET_MARK_FRAMES,
             ONSET_MARK_FRAMES_QUIET, AUDIBLE_MARK_FRAMES, AUDIBLE_MARK_FRAMES_QUIET, ECHO_TAIL_S)
    log.info("keep-all: pre-roll %.1f s; quiet start %d (%d over the reading) voiced+pitch frames in %.1f s, "
             "quiet end %d ms; audio over the reading transcribed and the reading's words removed "
             "(early check %.1f s, max %.0f s)", PRE_ROLL_FRAMES * FRAME_MS / 1000.0, SOFT_START_QUIET,
             SOFT_START_PLAYING, SOFT_WINDOW * FRAME_MS / 1000.0, SOFT_END_MS, ECHO_PARTIAL_S, ECHO_CHECK_MAX_S)
    seq = 0
    while True:
        try:
            with sd.RawInputStream(samplerate=RATE, blocksize=FRAME, dtype="int16", channels=1, callback=cb):
                log.info("microphone open")
                pre = collections.deque(maxlen=PRE_ROLL_FRAMES)   # frames heard while no utterance is open
                votes = collections.deque(maxlen=SOFT_WINDOW)     # 1 = voiced frame with a voice pitch
                voiced_run, voice_run, in_speech = 0, 0, False
                loud_run, marked_play = 0, None
                u = cur["u"] = None
                acct = {"t": 0.0, "n": 0, "ovf": mic["overflow"]}  # lost-audio accounting per 30 s
                last = {"t": time.time()}                           # when the last frame came (watchdog)
                orphan = {"t": 0.0, "n": 0}                         # voice outside utterances per 5 s
                recent = collections.deque(maxlen=8)                # rms of the latest frames
                fy = {"pend": None, "streak": 0, "text": None, "off": 0.0, "t": 0.0}   # reading stopping itself
                claims = collections.deque(maxlen=CLAIM_WINDOW)     # 1 = voice with a pitch (reading silent)
                seen_rel = HOLD.releases                            # releases already applied to the utterance
                vo_hist = collections.deque(maxlen=VOICE_ON_FRAMES)  # 1 = voice with a pitch, reading silent
                vo_since = VOICE_ON_RECENT
                while True:
                    try:
                        frame, ft = q.get(timeout=0.5)
                    except queue.Empty:
                        drain_results()
                        HOLD.tick()
                        service_pending(in_speech)
                        # 2026-10-05: after the PC slept the stream stopped and was never reopened, so nothing
                        # the user said reached Claude for hours. No frame for 5 s means the stream is dead: the
                        # error path below closes it and opens the microphone again.
                        if time.time() - last["t"] > 5.0:
                            raise RuntimeError("no mic audio for %.0f s (sleep or a device change); reopening"
                                               % (time.time() - last["t"]))
                        continue
                    last["t"] = time.time()
                    voiced = vad.is_speech(frame, RATE)
                    rms = frame_rms(frame)
                    try:
                        _level_sock.sendto(b"%.1f" % rms, ("127.0.0.1", LEVEL_PORT))
                    except OSError:
                        pass
                    clk["a"] += FRAME_MS / 1000.0
                    # lost audio: frames received vs wall time, and PortAudio overflows
                    if acct["t"] == 0.0:
                        acct["t"] = ft
                    acct["n"] += 1
                    if ft - acct["t"] >= 30.0:
                        lost = (ft - acct["t"]) - acct["n"] * FRAME_MS / 1000.0
                        ovf = mic["overflow"] - acct["ovf"]
                        if lost > 0.3 or ovf:
                            log.warning("mic audio lost: %.1f s missing and %d overflow(s) in the last %.0f s",
                                        lost, ovf, ft - acct["t"])
                            if lost > 5.0:
                                raise RuntimeError("mic audio lost for %.0f s; reopening" % lost)
                        acct.update(t=ft, n=0, ovf=mic["overflow"])
                    aud = HISTORY.audible_at(ft)
                    # voice = webrtcvad mode 3 (as before) or, while the reading is silent, mode 2 with a
                    # voice pitch: quiet speech that mode 3 marks only now and then (measured: 47-63% of
                    # quiet speech frames vs 3-18%, and none of noise or click frames). While the reading
                    # sounds, only mode 3 counts: mode 2 marks half of the reading's own leak.
                    voice, pitched = voiced, None
                    if not aud:
                        v2 = vad2.is_speech(frame, RATE)    # every quiet-time frame: it adapts to the room
                        if v2 and not voiced:
                            pitched = periodicity(frame) >= PERIODIC_MIN
                            voice = pitched
                    # 2026-10-05: every voice frame gets the pitch test. webrtcvad passes smacks, clicks and
                    # crunches as voice; they have no voice pitch, so they no longer stamp "the user is talking".
                    if voice and pitched is None:
                        pitched = periodicity(frame) >= PERIODIC_MIN
                    pitched = bool(pitched)
                    claims.append(1 if (voice and pitched) else 0)
                    # the user's voice going on (the reading silent): keeps a hold past HOLD_CONFIRM_S until a check
                    # finds the user's words; chewing, clicks and coughs never reach it
                    von = 1 if (voice and pitched and not aud) else 0
                    vo_hist.append(von)
                    vo_since = 0 if von else vo_since + 1
                    HOLD.set_voice(sum(vo_hist) >= VOICE_ON_MIN and vo_since < VOICE_ON_RECENT)
                    voice_run = voice_run + 1 if voice else 0
                    loud_run = loud_run + 1 if (voiced and pitched and loud_enough(rms)) else 0
                    recent.append(rms)
                    his = False
                    if voice:
                        # While the reading is sounding, only a run of frames clearly louder than its
                        # known echo, with a voice pitch, is the user (a longer run at the start of a sentence,
                        # where the reading is loudest against its median). With nothing sounding, voice
                        # with a pitch in 2 of the last 3 frames is the user (a lone click or crunch is not).
                        if not aud:
                            his = sum(claims) >= CLAIM_FRAMES
                        elif STATE["echo_rms"] > 0:
                            onset = HISTORY.onset_at(ft)
                            need = mark_frames(onset)
                            his = loud_run >= need
                            play = HISTORY.start_at(ft)
                            if his and marked_play != play:
                                marked_play = play
                                log.info("the user over the reading: %d ms louder than the echo gate, pitched (%.0f rms)%s%s",
                                         loud_run * FRAME_MS, rms, ", sentence onset" if onset else "",
                                         "; the replayed sentence plays through" if HOLD.protected() else "")
                            if (his and HOLD.protected() and play is not None and fy["text"] and fy.get("clean")
                                    and fy["text"] == HISTORY.text_at(ft) and abs((ft - play) - fy["off"]) < 0.25
                                    and ft - fy["t"] < 60):
                                # 2026-10-05: the replay cannot be stopped again, so "stopped itself twice at the
                                # same point" is this: the same point of the same sentence claimed the user again after
                                # the first stop was followed by silence (no sound of the user's at all). That is the reading.
                                learn_echo(list(recent)[-need:], "the reading claimed the user again at the same point "
                                                                 "of the replayed sentence")
                                fy["text"], fy["streak"] = None, 0
                            if his and fy["pend"] is None and play is not None:
                                fy["pend"] = {"t": ft, "off": ft - play, "text": HISTORY.text_at(ft),
                                              "rms": list(recent)[-need:], "after": 0, "voice": 0, "any": 0}
                        if his:
                            mark_user_speaking()
                            if in_speech or voice_run >= MERGE_NOISE_RUN:
                                clk["voice"] = clk["a"]
                    # Did the reading stop for its own sound? If nothing of the user's follows the stop, and the
                    # replay of the same sentence stops at the same point again, that sound was the
                    # reading (speakers, or the volume turned up): learn its level so the gate rises
                    # above it, instead of stopping and replaying the sentence forever.
                    p = fy["pend"]
                    if p is not None:
                        if not aud:
                            p["after"] += 1
                            p["voice"] += 1 if (voice and pitched) else 0
                            p["any"] += 1 if voice else 0
                            if p["voice"] >= ECHO_CONFIRM_VOICED:
                                fy["pend"], fy["streak"] = None, 0          # the user went on: it was the user
                            elif p["after"] >= ECHO_TAIL_FRAMES + ECHO_CONFIRM_FRAMES:
                                same = (p["text"] and p["text"] == fy["text"] and abs(p["off"] - fy["off"]) < 0.2
                                        and p["t"] - fy["t"] < 60)
                                fy["streak"] = fy["streak"] + 1 if same else 1
                                fy.update(text=p["text"], off=p["off"], t=p["t"], pend=None, clean=p["any"] <= 2)
                                if fy["streak"] >= 2:
                                    learn_echo(p["rms"], "the reading stopped itself twice at the same point")
                                    fy["streak"] = 0
                        elif ft - p["t"] > 3.0:
                            fy["pend"] = None
                    drain_results()
                    HOLD.tick()
                    if HOLD.releases != seen_rel:       # the reading was let go (here or by mark_keeper)
                        seen_rel = HOLD.releases
                        if u is not None:
                            u.paused = False
                            u.barge_base = u.loud_pitched
                    service_pending(in_speech)
                    if not in_speech:
                        pre.append((frame, ft, voiced, rms, voice, pitched))
                        vote = bool(voice and pitched)
                        votes.append(1 if vote else 0)
                        voiced_run = voiced_run + 1 if voiced else 0
                        start, soft = voiced_run >= START_FRAMES, False
                        if not start and vote:
                            need = SOFT_START_PLAYING if aud else SOFT_START_QUIET
                            if sum(votes) >= need:
                                first = next(i for i, v in enumerate(votes) if v)
                                if len(votes) - first >= SOFT_MIN_SPAN:
                                    start = soft = True
                        if start:
                            seq += 1
                            if soft:
                                log.info("quiet speech: utterance started on %d voice frames in %.1f s%s",
                                         sum(votes), SOFT_WINDOW * FRAME_MS / 1000.0,
                                         " over the reading" if aud else "")
                            in_speech, voiced_run = True, 0
                            u = cur["u"] = Utterance(list(pre), ft, aud, soft, seq)
                            pre.clear()
                            votes.clear()
                            orphan.update(t=0.0, n=0)
                        elif voice and not aud:
                            # voice that never became an utterance (the 10-02 loss): log it if there is a lot
                            if orphan["t"] == 0.0 or ft - orphan["t"] > 5.0:
                                orphan.update(t=ft, n=0)
                            orphan["n"] += 1
                            if orphan["n"] == 30:
                                log.warning("voice heard but no utterance started: 30 voice frames within 5 s")
                        continue
                    now = time.monotonic()
                    u.frames.append(frame)
                    u.t1 = ft
                    # The user is mid-message until this utterance ends, including the user's thinking pauses.
                    # Keep the marker fresh the whole time so the reader never slips in during a pause.
                    # Audio that began over the reading only counts once it is shown to be the user.
                    if voice and pitched:
                        u.pitched_total += 1
                    if aud:
                        u.heard_playback = True
                        if voiced:
                            u.audible_rms.append(rms)
                        u.audible_seq.append(rms if (voiced and pitched) else 0.0)
                    elif voice and pitched and u.during_playback and not u.confirmed:
                        # the reading is silent (stopped, yielded or paused) and the voice goes on: the user
                        u.quiet_voiced += 1
                        if u.quiet_voiced >= ECHO_CONFIRM_VOICED:
                            u.confirmed = True
                            log.info("voice went on with the reading silent: it is the user")
                    if u.killed or u.words:
                        mark_user_speaking(words=True)       # the user's words are known: hold while the user talks
                        if voice:
                            clk["voice"] = clk["a"]
                    elif (not u.during_playback) or u.confirmed or u.paused:
                        # a claim from sound until the user's words are known (ReadingHold); an utterance of clicks
                        # and crunches (no pitch) holds nothing
                        if u.pitched_total >= UTT_PITCHED_MIN:
                            mark_user_speaking()
                        if voice:
                            clk["voice"] = clk["a"]
                    if voice:
                        u.silence_ms = 0
                        u.floor_done = False
                        u.voice_total += 1
                    else:
                        u.silence_ms += FRAME_MS
                    u.voiced_run = u.voiced_run + 1 if voiced else 0
                    if u.soft and u.voiced_run >= START_FRAMES and u.end_ms != END_SILENCE_MS:
                        u.end_ms = END_SILENCE_MS      # it is ordinary speech after all: the usual quick end
                    if voiced:
                        u.voiced_total += 1
                        u.voiced_rms.append(rms)
                        if loud_enough(rms):
                            u.loud_voiced += 1
                            if pitched:
                                u.loud_pitched += 1
                        # Pause-then-confirm: only real voice (>= 500 ms; 700 ms right after a new
                        # sentence starts) pauses the reading: frames clearly louder than the reading's
                        # echo, with a voice pitch (2026-10-05). The transcript decides the rest. The
                        # sentence replayed after a release stops only for a full second of it.
                        if HOLD.protected():
                            need, reason = PROTECT_BARGE_FRAMES, ", over the replayed sentence"
                        else:
                            need = pause_threshold_frames()
                            reason = ", boundary cooldown" if need == PAUSE_AFTER_VOICED_FRAMES_BOUNDARY else ""
                        barge_frames = u.loud_pitched - u.barge_base
                        # Only a reading that has actually sounded during this utterance is paused; a hook
                        # that is merely waiting is held off by the "the user is talking" stamp instead.
                        if (not u.paused and not u.killed and speaking()
                                and u.heard_playback and barge_frames >= need):
                            if barge_in("%d ms of voice during playback%s" % (barge_frames * FRAME_MS, reason)):
                                u.paused, u.paused_at = True, now
                                u.pause_idx, u.confirm_voiced = len(u.frames), 0
                                u.pause_nvoiced = len(u.voiced_rms)

                    # Echo guard: the reading is silent now, so voice from here on is the user. If the voice
                    # stopped when the reading paused, it may have been the reading itself -- or the user,
                    # stopping right there. Since 10-02 it is not thrown away: the transcript decides.
                    if u.paused and not u.confirmed and not u.suspect:
                        after = len(u.frames) - u.pause_idx
                        if voice and pitched and after > ECHO_TAIL_FRAMES:
                            u.confirm_voiced += 1
                        if u.confirm_voiced >= ECHO_CONFIRM_VOICED:
                            u.confirmed = True
                        elif after >= ECHO_TAIL_FRAMES + ECHO_CONFIRM_FRAMES or u.silence_ms >= u.end_ms:
                            before = u.voiced_rms[:u.pause_nvoiced]
                            if STATE["echo_rms"] > 0 and before and loud_enough(float(np.median(before))):
                                # clearly louder than the reading's echo: the user spoke and stopped right at the pause
                                u.confirmed = True
                            else:
                                u.suspect, u.echo_before = True, before
                                log.info("the voice stopped when the reading paused: transcribing it to tell "
                                         "the reading from the user")

                    # 2026-10-05: whatever holds the reading (the pause, or the stamp) must be the user's words. The
                    # audio so far is checked in the background HOLD_FIRST_CHECK_S into the hold: the user's words stop
                    # the reading for good (handle_result "hold"); HOLD.tick releases it at the deadline.
                    # (Was: a blocking check 2.0 s after a pause only; the stamp's wait had no end.)
                    if HOLD.check_due():
                        submit_hold_check(u)

                    # Audio heard over a still-playing reading and not yet shown to be the user: check it in the
                    # background once it is long enough, so a quiet voice under the reading stops it.
                    if (u.unproven() and not u.suspect and not u.check_busy and speaking()
                            and u.seconds() >= u.next_check_s):
                        u.check_busy = True
                        u.next_check_s = u.seconds() + ECHO_PARTIAL_EVERY_S
                        stt.submit(b"".join(u.frames), final=False,
                                   ctx={"kind": "partial", "uid": u.uid, "t0": u.t0, "t1": u.t1},
                                   on_done=lambda j: RESULTS.append(("partial", j)), want_words=True)

                    # A long stretch over the reading not yet shown to be the user: check what is there so far
                    # in the background and keep listening without a gap (a continuation picks up here).
                    if u.unproven() and not u.suspect and u.seconds() >= ECHO_CHECK_MAX_S:
                        submit_check(u, "%.0f s cap" % ECHO_CHECK_MAX_S)
                        seq += 1
                        nu = Utterance([], ft, aud, u.soft, seq)
                        nu.during_playback, nu.heard_playback = u.during_playback, True
                        u = cur["u"] = nu
                        continue

                    too_long = now - u.started > MAX_UTTERANCE_S
                    at_floor = u.silence_ms >= u.end_ms and not u.floor_done
                    at_ceiling = u.extending and u.silence_ms >= u.end_ms + EXTEND_SILENCE_MS
                    if not (at_floor or at_ceiling or too_long):
                        continue

                    # ---- endpoint: floor reached (or ceiling / max length) -> transcribe
                    if at_ceiling and not too_long:
                        text = u.pending_text
                        dur = len(u.frames) * FRAME_MS / 1000.0
                    else:
                        u.floor_done = True
                        pcm = b"".join(u.frames)
                        dur = len(pcm) / 2 / RATE
                        if dur < MIN_UTTERANCE_S or u.voice_total == 0:   # nothing was said in it
                            in_speech = False
                            u = cur["u"] = None
                            pre.clear()
                            let_go("utterance too short")
                            continue
                        # Heard over the reading and never shown to be the user (it never paused the reading,
                        # was never louder than its echo, did not go on when it stopped): this used to be
                        # dropped untranscribed as "echo". Now it is transcribed in the background and the
                        # reading's words are removed; whatever is left is the user's. "Louder than its echo" counts
                        # frames with a voice pitch only (2026-10-05): loud chewing under the reading had passed
                        # as the user, and a misheard word of the reading ("archivist...") was sent as the user's message.
                        if u.unproven() and not u.suspect and u.loud_pitched < ECHO_CONFIRM_VOICED:
                            submit_check(u, "never louder than the reading")
                            in_speech = False
                            u = cur["u"] = None
                            pre.clear()
                            continue
                        reads = HISTORY.texts_between(u.t0, u.t1)
                        if u.killed or u.words:
                            hold = "words"
                        else:
                            hold = (((not u.during_playback) or u.confirmed or u.paused) and not u.suspect
                                    and u.pitched_total >= UTT_PITCHED_MIN)
                        raw, _err, rw = stt_now(pcm, final=True, hold=hold,
                                                ctx={"kind": "final", "t0": u.t0, "t1": u.t1, "dur": dur},
                                                want_words=bool(reads))
                        if raw is None:
                            in_speech = False
                            u = cur["u"] = None
                            pre.clear()
                            let_go("stt failed or slow")
                            continue
                        text, removed = strip_reading(raw, reads, rw, u.t0)
                        if removed:
                            log.info("removed %d words of the reading from %r", removed, raw)
                        if u.suspect:
                            if classify(text) in ("junk", "backchannel") and not nav_command(text):
                                # It was the reading (or a cough). Learn the echo level only when the
                                # transcript matched the reading, so their own voice never raises the gate.
                                if removed >= 3:
                                    learn_echo(u.echo_before, "audio that stopped when the reading paused")
                                log.info("echo guard: %.1f s was the reading itself (%d words matched), "
                                         "nothing of the user's: %r", dur, removed, raw)
                                in_speech = False
                                u = cur["u"] = None
                                pre.clear()
                                let_go("echo: the transcript was the reading")
                                continue
                            u.confirmed = True
                            log.info("the voice that stopped at the pause was the user: %r", text)
                    drain_results()          # anything heard before this utterance is typed first
                    kind = classify(text)

                    # ---- two-stage endpointing: trailing filler / fragment -> keep listening
                    if not too_long and not at_ceiling and kind in ("backchannel", "short", "message") \
                            and looks_incomplete(text) and not nav_command(text):
                        if not u.extending:
                            log.info("endpoint extended (+%d ms): %r", EXTEND_SILENCE_MS, text)
                        else:
                            log.info("endpoint extended again: %r", text)
                        u.extending, u.pending_text = True, text
                        # (a lone "uh" / "so" here may lead into the user's words: the hold's deadline decides)
                        continue

                    # ---- utterance final: act on the transcript
                    in_speech = False
                    done, u = u, None
                    cur["u"] = None
                    pre.clear()
                    act(text, dur, paused=done.paused, killed=done.killed, note=done.note, t0=done.t0)
        except Exception as e:
            log.warning("mic error: %s; retrying in 5 s", e)
            try:
                flush("mic error: sending what the user already said")
            except Exception as e2:
                log.warning("flush on mic error failed: %s", e2)
            resume_speak_hook("mic error")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
