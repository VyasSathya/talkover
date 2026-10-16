# Claude Code voice hook. Speaks assistant text through local Kokoro (no fallback voice since 2026-10-12; restarts Kokoro if it hangs).
#   Stop hook:        reads last_assistant_message from stdin JSON and speaks it.
#   PreToolUse hook:  -Mode transcript. Reads transcript_path from stdin JSON, finds assistant text
#                     written since the last user prompt that has not been spoken yet, speaks it.
# Exactly ONE speaker at a time: a named mutex serializes speakers; after taking it, each one
# re-checks the spoken list so duplicates (Stop hook + several PreToolUse hooks) exit silently.
# Control files in %TEMP%:
#   claude-speak.pid     current speaker's pid (kill it to stop that utterance)
#   claude-speak.pause   while present, playback pauses; on removal the sentence replays
#   claude-speak.cancel  touched by stop-speaking.ps1 / voice_inbox.py; waiting speakers newer than it exit
#   claude-speak.spoken  hashes of texts already spoken (dedupe)
#   claude-speak.progress text of the chunk being spoken right now (overwritten per chunk, removed at
#                        the end); voice_inbox.py quotes its tail as "you were cut off after: ..." when
#                        a real message interrupts, and treats its mtime as the start of a new sentence.
#                        It also keeps a timed history of it (with .playing) to remove the reading's own
#                        words from what the mic heard while it played (2026-10-02).
#   claude-speak.playing "<start_ms> <end_ms>" (Unix epoch ms): written just before Play() with the expected
#                        end, rewritten at Stop() with the real stop. voice_inbox.py treats the mic as hearing
#                        the reading only inside that window (+ a short tail), and guards each sentence onset.
#   claude-speak.nav     "<seq> <next|back|again>" (2026-10-10), written by src\speak_nav.py for the user's spoken
#                        "next" / "back" / "again" and the panel's Next / Back buttons. The speaker holding the lock
#                        acts on a seq newer than the one it saw when it took the lock:
#                          next   stop this reply; play the next one queued (nothing if none)
#                          back   play the reply before this one from its start, then this one from its start
#                          again  play this reply again from its start
#   claude-speak.history\ one file per reply read (<ticks>.txt, its cleaned text; the last 50 kept). Back and again
#                        replay from here. With no speaker running, speak_nav.py starts this script with
#                        -Mode replay -Nav back|again, which plays the last reply read.
# Test mode (CLAUDE_SPEAK_TEST=1, with TEMP pointed at a sandbox): its own lock names, its log in TEMP, and a silent
# player that only keeps the time, so a test never plays audio and never waits on the live speakers.
#   claude-voice.pin / claude-voice.shown / claude-speak.held\  which chat's replies are read (2026-10-10/11): see
#                        "Which chat's replies are read" below. -Mode held -Session <first 8 of session_id> plays the
#                        reply held for that chat (started by src\voice_inbox.py when the window shows it).
param([string]$Mode = 'stop', [string]$Nav = '', [string]$Session = '')
$ErrorActionPreference = 'SilentlyContinue'
$TestMode = ($env:CLAUDE_SPEAK_TEST -eq '1')
$MutexSuffix = if ($TestMode) { '-test' } else { '' }

# Settings: environment first, then config.env at the repo root (KEY=VALUE lines, same file src\voice_config.py reads).
$ConfigFile = if ($env:TALKOVER_CONFIG) { $env:TALKOVER_CONFIG } else { Join-Path (Split-Path $PSScriptRoot -Parent) 'config.env' }
$Config = @{}
if (Test-Path $ConfigFile) {
    foreach ($l in Get-Content $ConfigFile) {
        if ($l -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { $Config[$Matches[1]] = $Matches[2].Trim('"', "'") }
    }
}
function Get-Setting([string]$name, [string]$default) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($v) { return $v }
    if ($Config.ContainsKey($name) -and $Config[$name]) { return $Config[$name] }
    return $default
}
$DataDir   = [Environment]::ExpandEnvironmentVariables((Get-Setting 'TALKOVER_HOME' (Join-Path $env:USERPROFILE '.talkover'))) -replace '^~', $env:USERPROFILE
$KokoroUrl = (Get-Setting 'TALKOVER_TTS_BASE' ('http://127.0.0.1:' + (Get-Setting 'TALKOVER_TTS_PORT' '8880'))).TrimEnd('/') + '/v1/audio/speech'
$KokoroContainer = Get-Setting 'TALKOVER_KOKORO_CONTAINER' 'talkover-kokoro'
$Voice     = Get-Setting 'TALKOVER_VOICE' 'af_heart'
$Speed     = [double](Get-Setting 'TALKOVER_SPEED' '1.05')
$MaxChars  = 4000
$MaxPauseS = 10
$MaxWaitS  = 420   # was 240; a reading held for the user's words can now keep the lock up to $MaxHoldS
$LogDir    = Join-Path $DataDir 'logs'
if (-not $TestMode) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
$LogFile   = if ($TestMode) { Join-Path $env:TEMP 'speak.log' } else { Join-Path $LogDir 'speak.log' }
# ms timestamps (to line up with voice_inbox.log); retried because several hook copies can write at once
function Log([string]$m) {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $PID, $m
    for ($k = 0; $k -lt 5; $k++) {
        try { [System.IO.File]::AppendAllText($LogFile, $line + [Environment]::NewLine); return } catch { Start-Sleep -Milliseconds 15 }
    }
}

$pidFile    = Join-Path $env:TEMP 'claude-speak.pid'
$pauseFile  = Join-Path $env:TEMP 'claude-speak.pause'
$cancelFile = Join-Path $env:TEMP 'claude-speak.cancel'
$spokenFile = Join-Path $env:TEMP 'claude-speak.spoken'
$progressFile = Join-Path $env:TEMP 'claude-speak.progress'
$playingFile  = Join-Path $env:TEMP 'claude-speak.playing'
function Now-Ms { [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }
$userSpeakingFile = Join-Path $env:TEMP 'claude-user.speaking'   # stamped by voice_inbox.py while the user talks
# 2026-10-05 night: the user's words no longer kill the reading; voice_inbox.py holds it with this file (kept fresh every
# 0.1 s) until the user's message is sent, then the paused sentence replays and the rest is read. A stale file (the inbox
# died) holds nothing.
$holdFile   = Join-Path $env:TEMP 'claude-speak.hold'
$MaxHoldS   = 300
# "code" / "chat" / "unknown", written by voice_inbox.py every 1.5 s from the Claude window's Chat / Code switch.
# While it shows a claude.ai chat (which has its own voice mode) nothing is read.
$targetFile = Join-Path $env:TEMP 'claude-voice.target'
function Test-ChatShown {
    if (-not (Test-Path $targetFile)) { return $false }
    $it = Get-Item $targetFile
    if (((Get-Date) - $it.LastWriteTime).TotalSeconds -gt 10) { return $false }
    return ((Get-Content $targetFile -Raw).Trim() -eq 'chat')
}
# 2026-10-06: newest reply wins after the user talks. Every speaker announces itself in claude-speak.newest ("<ticks> <pid>")
# once it has something to say; voice_inbox.py stamps claude-speak.talkedover when the user's words stop a reading. A speaker
# that started before the user talked over the reading, with a newer speaker announced since, is superseded: it drops what
# it has left (waiting, held, or playing its resumed rest) so the user hears only the newest reply. Without the user's talking over
# it, nothing is dropped: replies queue and play in order as before.
$newestFile = Join-Path $env:TEMP 'claude-speak.newest'
$talkedFile = Join-Path $env:TEMP 'claude-speak.talkedover'
$myTicks = [DateTime]::UtcNow.Ticks
function Test-Superseded {
    if (-not (Test-Path $talkedFile) -or -not (Test-Path $newestFile)) { return $false }
    if ((Get-Item $talkedFile).LastWriteTime -le $startedAt) { return $false }
    $n = ((Get-Content $newestFile -Raw) -split '\s+')
    try { return ([int64]$n[0] -gt $myTicks) -and ($n[1] -ne "$PID") } catch { return $false }
}
function Test-WordsHold {
    if (-not (Test-Path $holdFile)) { return $false }
    return ((Get-Date) - (Get-Item $holdFile).LastWriteTime).TotalSeconds -lt 3
}
# The user's silence needed before a sentence starts or resumes. Was 1.5 s: on 2026-10-02 the reading came back in
# every short gap while the user was still talking.
$QuietS = 3.0
$startedAt  = Get-Date

# ---- Next / Back (2026-10-10): skip to the next reply, or hear the previous one again.
$navFile  = Join-Path $env:TEMP 'claude-speak.nav'
$histDir  = Join-Path $env:TEMP 'claude-speak.history'
$HistKeep = 50
$script:navSeen = [int64]0
$script:navMtime = [int64]0
$script:nav = ''
function Read-NavSeq {
    try { $p = ([System.IO.File]::ReadAllText($navFile)).Trim() -split '\s+'; return @([int64]$p[0], "$($p[1])".ToLowerInvariant()) } catch { return @([int64]0, '') }
}
# A command newer than the last one seen: sets $script:nav ('next' | 'back' | 'again') and returns $true.
function Test-Nav {
    if ($script:nav) { return $true }
    $mt = [System.IO.File]::GetLastWriteTimeUtc($navFile).Ticks
    if ($mt -eq $script:navMtime) { return $false }
    $script:navMtime = $mt
    $sc = Read-NavSeq
    if ($sc[0] -le $script:navSeen) { return $false }
    $script:navSeen = $sc[0]
    if ($sc[1] -in @('next', 'back', 'again')) { $script:nav = $sc[1]; Log "nav: $($sc[1])"; return $true }
    return $false
}
function Add-History([string]$clean) {
    try {
        if (-not (Test-Path $histDir)) { [void](New-Item -ItemType Directory -Path $histDir) }
        $name = '{0:D20}_{1}.txt' -f [DateTime]::UtcNow.Ticks, (Get-Sid8 "$($payload.session_id)")
        [System.IO.File]::WriteAllText((Join-Path $histDir $name), $clean, [System.Text.Encoding]::UTF8)
        $all = @(Get-ChildItem $histDir -Filter '*.txt' | Sort-Object Name)
        if ($all.Count -gt $HistKeep) { $all | Select-Object -First ($all.Count - $HistKeep) | Remove-Item -ErrorAction SilentlyContinue }
        return $name
    } catch { return $null }
}
function Get-HistoryNames {
    if (-not (Test-Path $histDir)) { return @() }
    $all = @(Get-ChildItem $histDir -Filter '*.txt' | Sort-Object Name | ForEach-Object { $_.Name })
    $p = Get-Target8     # the pinned chat, or in auto the chat on screen ('-': none; '': not known, all)
    if ($p) { $all = @($all | Where-Object { (Get-HistSid8 $_) -eq $p }) }
    return $all
}
function Get-HistoryBefore([string]$name) {
    $prev = $null
    foreach ($n in (Get-HistoryNames)) { if ($n -ge $name) { break }; $prev = $n }
    return $prev
}
function Read-History([string]$name) {
    try { return [System.IO.File]::ReadAllText((Join-Path $histDir $name), [System.Text.Encoding]::UTF8) } catch { return '' }
}

# ---- Which chat's replies are read.
# Pinned (2026-10-10): you pick which chat the voice follows.
# claude-voice.pin = "<cliSessionId> <local id>", written by src\voice_panel.py when the user picks a chat. While it is
# there only that chat's replies are read: a speaker for another chat exits, and one already reading stops when the pin
# moves away from its chat.
# Auto (2026-10-11), no pin: a reply plays when you go to its chat, never for a chat you are not looking at.
# claude-voice.shown = "<cliSessionId> <title>" of the chat the Claude
# window shows (empty: no Claude Code chat on screen), written by voice_inbox.py every 1.5 s. A reply from any other
# chat is held in claude-speak.held\<first 8>.json (only that chat's newest unheard reply: what it replaces is marked
# spoken, so there is never a backlog), and voice_inbox.py starts this script with -Mode held when the window shows
# that chat. A reading whose chat leaves the screen stops and is held again. With the file missing or stale (the inbox
# is not running) every reply is read, as before.
# History files carry their chat (<ticks>_<first 8 of session_id>.txt) so Back and replay stay inside that chat.
$pinFile = Join-Path $env:TEMP 'claude-voice.pin'
$shownFile = Join-Path $env:TEMP 'claude-voice.shown'
$heldDir = Join-Path $env:TEMP 'claude-speak.held'
function Get-Pin8 {
    try { $p = (([System.IO.File]::ReadAllText($pinFile)).Trim() -split '\s+')[0]; if ($p.Length -ge 8) { return $p.Substring(0, 8) } } catch {}
    return ''
}
# '' = not known (no fresh file: read as before); '-' = no Claude Code chat on screen; else the shown chat's first 8
function Get-Shown8 {
    try {
        $it = Get-Item $shownFile -ErrorAction Stop
        if (((Get-Date) - $it.LastWriteTime).TotalSeconds -gt 10) { return '' }
        $p = (([System.IO.File]::ReadAllText($shownFile)).Trim() -split '\s+')[0]
        if ($p.Length -ge 8) { return $p.Substring(0, 8) } else { return '-' }
    } catch { return '' }
}
function Get-Target8 { $p = Get-Pin8; if ($p) { return $p }; return (Get-Shown8) }
function Get-Sid8([string]$s) { if ($s.Length -ge 8) { return $s.Substring(0, 8) } else { return $s } }
function Get-HistSid8([string]$name) { if ($name -match '_([0-9a-fA-F-]{1,8})\.txt$') { return $matches[1] } else { return '' } }
$script:itemSid = ''
$script:curText = $null
function Test-NotPinned {
    $p = Get-Pin8
    return ($p -and ($script:itemSid -ne $p))
}
function Test-OffTarget { $t = Get-Target8; return ($t -and ($script:itemSid -ne $t)) }
function Get-OffWhy { if (Get-Pin8) { 'voice is pinned to another chat' } else { 'the window shows another chat' } }
function Read-Held([string]$sid8) {
    try { return ([System.IO.File]::ReadAllText((Join-Path $heldDir "$sid8.json"), [System.Text.Encoding]::UTF8) | ConvertFrom-Json) } catch { return $null }
}
# Hold this chat's newest unheard reply for when the window shows it (auto only). What it replaces is marked spoken.
function Save-Held($items) {
    $items = @($items | Where-Object { $_ })
    if ((Get-Pin8) -or $Mode -eq 'replay' -or $items.Count -eq 0) { return }
    try {
        if (-not (Test-Path $heldDir)) { [void](New-Item -ItemType Directory -Path $heldDir) }
        $new = @($items | ForEach-Object { Get-Hash $_ })
        $old = Read-Held $script:itemSid
        $oldH = @()
        if ($old) { $oldH = @(@($old.texts) | ForEach-Object { Get-Hash $_ }) }
        foreach ($h in $oldH) { if (($new -notcontains $h) -and -not (Test-Spoken $h)) { Mark-Spoken $h } }
        if (($oldH -join ',') -eq ($new -join ',')) { return }
        $obj = [pscustomobject]@{ session_id = "$($payload.session_id)"; agent_id = "$($payload.agent_id)"; texts = $items }
        $tmp = Join-Path $heldDir "$($script:itemSid).tmp"
        [System.IO.File]::WriteAllText($tmp, (ConvertTo-Json $obj -Compress -Depth 4), [System.Text.Encoding]::UTF8)
        Move-Item -Force $tmp (Join-Path $heldDir "$($script:itemSid).json")
        Log ("held for when the window shows chat {0}: {1} text(s)" -f $script:itemSid, $items.Count)
    } catch { Log "hold failed: $($_.Exception.Message)" }
}
# A newer reply of this chat is being read: the held one is not wanted any more.
function Clear-Held($keep) {
    $old = Read-Held $script:itemSid
    if (-not $old) { return }
    $k = @(@($keep) | Where-Object { $_ } | ForEach-Object { Get-Hash $_ })
    foreach ($t in @($old.texts)) { $h = Get-Hash $t; if (($k -notcontains $h) -and -not (Test-Spoken $h)) { Mark-Spoken $h } }
    Remove-Item (Join-Path $heldDir "$($script:itemSid).json") -ErrorAction SilentlyContinue
}
# The reading's chat left the screen (auto): hold what it had left, from the start of the reply being read.
function Hold-Rest {
    $r = @()
    if ($script:curText) { $r += $script:curText }
    if ($queue) { foreach ($q in $queue) { if ($q.text) { $r += $q.text } } }
    Save-Held $r
}

if ((Test-Path (Join-Path $env:TEMP 'claude-speak.muted')) -or (Test-Path (Join-Path $env:TEMP 'claude-voice.muted'))) { exit 0 }   # "stop listening": silent until "start listening"
# The Claude window shows a claude.ai chat: voice is for Claude Code only. In auto a reply is held instead (below).
if ((Test-ChatShown) -and ((Get-Pin8) -or $Mode -eq 'replay')) { exit 0 }
if ($Mode -eq 'replay') {
    $payload = [pscustomobject]@{ session_id = 'replay'; agent_id = '' }
} elseif ($Mode -eq 'held') {
    $src = Join-Path $heldDir "$Session.json"
    $claim = Join-Path $heldDir ("{0}.{1}.play" -f $Session, $PID)
    try { [System.IO.File]::Move($src, $claim) } catch { exit 0 }   # nothing held, or another copy took it
    try { $payload = ([System.IO.File]::ReadAllText($claim, [System.Text.Encoding]::UTF8) | ConvertFrom-Json) } catch { $payload = $null }
    Remove-Item $claim -ErrorAction SilentlyContinue
    if (-not $payload) { exit 0 }
    $script:itemSid = Get-Sid8 "$($payload.session_id)"
} else {
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    try { $payload = $raw | ConvertFrom-Json } catch { exit 0 }
    $script:itemSid = Get-Sid8 "$($payload.session_id)"
    if (Test-NotPinned) {   # voice is pinned to another chat: this one is not read
        if ($Mode -eq 'stop') { Log ("not read: voice is pinned to chat {0}, this reply is from {1}" -f (Get-Pin8), $script:itemSid) }
        exit 0
    }
}

function Get-Hash([string]$s) {
    $sha = [System.Security.Cryptography.SHA1]::Create()
    $norm = [regex]::Replace($s, '\s+', ' ').Trim().ToLowerInvariant()
    ([System.BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($norm)))).Replace('-','').Substring(0,16)
}
function Test-Spoken([string]$h) { (Test-Path $spokenFile) -and ((Get-Content $spokenFile) -contains $h) }
function Mark-Spoken([string]$h) {
    Add-Content -Path $spokenFile -Value $h
    $all = @(Get-Content $spokenFile); if ($all.Count -gt 400) { $all | Select-Object -Last 200 | Set-Content $spokenFile }
}

# ---- collect candidate texts (cheap pre-check; the real check happens under the lock) ----
$texts = @()
if ($Mode -eq 'transcript') {
    $tp = $payload.transcript_path
    if (-not $tp -or -not (Test-Path $tp)) { exit 0 }
    # One scan at a time. A new copy starts on every tool call; if one is already scanning, it
    # will find the same text, so this one leaves. Without this they stacked up 36 deep at full CPU.
    $scan = New-Object System.Threading.Mutex($false, ('Global\ClaudeSpeakScan' + $MutexSuffix))
    $haveScan = $false
    try { $haveScan = $scan.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $haveScan = $true }
    if (-not $haveScan) { exit 0 }
    # Read only the last 4 MB (Get-Content -Tail walked the whole file: 4.9 s on an 84 MB transcript).
    # Then the last 400 lines of that, as before. The first line after the seek is partial: dropped.
    $lines = @()
    try {
        $fs = New-Object System.IO.FileStream($tp, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]'ReadWrite, Delete')
        try {
            $tailBytes = [int64]4MB
            $from = [Math]::Max([int64]0, $fs.Length - $tailBytes)
            [void]$fs.Seek($from, [System.IO.SeekOrigin]::Begin)
            $buf = New-Object byte[] ($fs.Length - $from)
            $got = 0
            while ($got -lt $buf.Length) { $n = $fs.Read($buf, $got, $buf.Length - $got); if ($n -le 0) { break }; $got += $n }
        } finally { $fs.Dispose() }
        $all = [System.Text.Encoding]::UTF8.GetString($buf, 0, $got) -split "`r?`n"
        if ($from -gt 0 -and $all.Count -gt 0) { $all = $all[1..($all.Count - 1)] }
        $lines = @($all | Where-Object { $_ } | Select-Object -Last 400)
    } catch { $lines = Get-Content -Path $tp -Tail 400 }
    $pending = @()
    foreach ($ln in $lines) {
        # tool output lines can be megabytes and never hold anything to speak; parsing them in
        # PowerShell 5.1 takes minutes. Assistant text and user prompts are always small.
        if ($ln.Length -gt 200000) { continue }
        if ($ln -notmatch '"type":\s*"(assistant|user)"') { continue }
        try { $e = $ln | ConvertFrom-Json } catch { continue }
        if ($e.type -eq 'user') {
            $c = $e.message.content
            $isPrompt = $false
            if ($c -is [string]) { $isPrompt = $true }
            elseif ($c -and ($c | Where-Object { $_.type -eq 'text' })) { $isPrompt = $true }
            if ($isPrompt) { $pending = @() }
        } elseif ($e.type -eq 'assistant') {
            foreach ($blk in $e.message.content) {
                if ($blk.type -eq 'text' -and $blk.text -and $blk.text.Trim()) { $pending += $blk.text }
            }
        }
    }
    foreach ($t in $pending) { if (-not (Test-Spoken (Get-Hash $t))) { $texts += $t } }
    try { $scan.ReleaseMutex() } catch {}
} elseif ($Mode -eq 'held') {
    $texts = @(@($payload.texts) | Where-Object { $_ })     # held texts are unheard by definition: no spoken check
} else {
    $t = $payload.last_assistant_message
    if ($t -and -not (Test-Spoken (Get-Hash $t))) { $texts += $t }
}
$replayStart = $null
if ($Mode -eq 'replay') {
    # started by speak_nav.py for "back" / "again" with nothing reading: the last reply read, from its start
    $replayStart = @(Get-HistoryNames) | Select-Object -Last 1
    if (-not $replayStart) { Log "replay ($Nav): nothing has been read yet"; exit 0 }
    $script:itemSid = Get-HistSid8 $replayStart
} else {
    if ($texts.Count -eq 0) { exit 0 }
    if (-not (Get-Pin8)) {
        $s8 = Get-Shown8
        if ($s8 -and $s8 -ne $script:itemSid) { Save-Held $texts; exit 0 }   # auto: not the chat on screen, held
        if ($s8 -and $Mode -ne 'held') { Clear-Held $texts }               # on screen: its newer reply wins
    } elseif ($Mode -eq 'held' -and (Test-OffTarget)) { exit 0 }
    try { [System.IO.File]::WriteAllText($newestFile, ("{0} {1}" -f $myTicks, $PID)) } catch {}
}

# ---- take the one-speaker lock ----
$mutex = New-Object System.Threading.Mutex($false, ('Global\ClaudeSpeakHook' + $MutexSuffix))
$have = $false
$deadline = (Get-Date).AddSeconds($MaxWaitS)
while (-not $have -and (Get-Date) -lt $deadline) {
    if ((Test-Path $cancelFile) -and ((Get-Item $cancelFile).LastWriteTime -gt $startedAt)) { Log 'cancelled while waiting'; exit 0 }
    if (Test-Superseded) { Log 'a newer reply came after the user talked: dropped while waiting'; exit 0 }
    if (Test-OffTarget) { Log "$(Get-OffWhy): not read now (waiting)"; Save-Held $texts; exit 0 }
    try { $have = $mutex.WaitOne(500) } catch [System.Threading.AbandonedMutexException] { $have = $true }
}
if (-not $have) { Log 'gave up waiting for lock'; exit 0 }
# The user may have spoken in the instant between our last check and getting the lock: the inbox kills
# the current speaker and touches the cancel file at the same moment, and the abandoned lock can
# hand straight to us. Check again now, or we read old text over the user.
if ((Test-Path $cancelFile) -and ((Get-Item $cancelFile).LastWriteTime -gt $startedAt)) {
    Log 'cancelled at lock'; try { $mutex.ReleaseMutex() } catch {}; exit 0
}
if (Test-Superseded) { Log 'a newer reply came after the user talked: dropped at lock'; try { $mutex.ReleaseMutex() } catch {}; exit 0 }
if (Test-OffTarget) { Log "$(Get-OffWhy): not read now (at lock)"; Save-Held $texts; try { $mutex.ReleaseMutex() } catch {}; exit 0 }

# re-check under the lock: another speaker may have said it while we waited
if ($Mode -ne 'held') { $texts = @($texts | Where-Object { -not (Test-Spoken (Get-Hash $_)) }) }
if ($texts.Count -eq 0 -and -not $replayStart) { $mutex.ReleaseMutex(); exit 0 }
# Next / Back commands given before this speaker held the lock were for the one before it.
$script:navSeen = (Read-NavSeq)[0]
$script:navMtime = [System.IO.File]::GetLastWriteTimeUtc($navFile).Ticks
Set-Content -Path $pidFile -Value $PID
Remove-Item $pauseFile -ErrorAction SilentlyContinue

function Clean-Text([string]$text) {
    $t = $text
    $t = [regex]::Replace($t, '(?s)```.*?```', ' code block omitted. ')
    $t = [regex]::Replace($t, '`([^`]*)`', '$1')
    $t = [regex]::Replace($t, '\[([^\]]+)\]\([^)]+\)', '$1')
    $t = [regex]::Replace($t, 'https?://\S+', 'link')
    $t = [regex]::Replace($t, '(?m)^#{1,6}\s*', '')
    $t = [regex]::Replace($t, '(?m)^\s*[-*+]\s+', '')
    $t = [regex]::Replace($t, '(?m)^\s*\d+\.\s+', '')
    $t = [regex]::Replace($t, '(?m)^\|.*\|\s*$', '')
    $t = [regex]::Replace($t, '\*\*|__|~~', '')
    $t = [regex]::Replace($t, '(?<!\w)[*_](?=\w)|(?<=\w)[*_](?!\w)', '')
    $t = [regex]::Replace($t, '\s+', ' ').Trim()
    if ($t.Length -gt $MaxChars) { $t = $t.Substring(0, $MaxChars) + ' ... the rest is in the transcript.' }
    return $t
}
function Split-Chunks([string]$t) {
    $sentences = @([regex]::Split($t, '(?<=[.!?:])\s+') | Where-Object { $_ })
    $chunks = New-Object System.Collections.Generic.List[string]; $buf = ''
    # First chunk = first sentence only, so Kokoro returns first audio sooner (311 ms for 55 chars vs
    # 676 ms for 125). A long first sentence is cut at the first , ; or : after 30 chars (if <= 80).
    if ($sentences.Count -gt 0) {
        $first = $sentences[0]; $rest = @($sentences | Select-Object -Skip 1)
        if ($first.Length -gt 80) {
            $m = [regex]::Match($first.Substring(30), '[,;:]\s')
            if ($m.Success -and (30 + $m.Index + 1) -le 80) {
                $cut = 30 + $m.Index + 1
                $rest = @($first.Substring($cut).Trim()) + $rest
                $first = $first.Substring(0, $cut)
            }
        }
        $chunks.Add($first)
        $sentences = $rest
    }
    foreach ($s in $sentences) {
        if ($buf.Length -gt 0 -and ($buf.Length + $s.Length) -gt 140) { $chunks.Add($buf); $buf = '' }
        $buf = if ($buf) { "$buf $s" } else { $s }
    }
    if ($buf) { $chunks.Add($buf) }
    return $chunks
}
function Repair-WavHeader([byte[]]$b) {
    if ($b.Length -lt 44) { return $b }
    [System.BitConverter]::GetBytes([uint32]($b.Length - 8)).CopyTo($b, 4)
    $i = 12
    while ($i -lt $b.Length - 8) {
        $id = [System.Text.Encoding]::ASCII.GetString($b, $i, 4)
        $sz = [System.BitConverter]::ToUInt32($b, $i + 4)
        if ($id -eq 'data') { [System.BitConverter]::GetBytes([uint32]($b.Length - ($i + 8))).CopyTo($b, $i + 4); $script:dataBytes = $b.Length - ($i + 8); break }
        $i += 8 + $sz + ($sz -band 1)
    }
    return $b
}
function Request-Kokoro([string]$chunk) {
    $body = @{ model = 'kokoro'; voice = $Voice; input = $chunk; response_format = 'wav'; speed = $Speed } | ConvertTo-Json -Compress
    $content = New-Object System.Net.Http.StringContent($body, [System.Text.Encoding]::UTF8, 'application/json')
    return $script:http.PostAsync($KokoroUrl, $content)
}
# The user is talking, or stopped less than $QuietS ago.
function Test-UserTalking([double]$within) {
    if (-not (Test-Path $userSpeakingFile)) { return $false }
    return ((Get-Date) - (Get-Item $userSpeakingFile).LastWriteTime).TotalSeconds -lt $within
}
function Test-Cancelled { (Test-Path $cancelFile) -and ((Get-Item $cancelFile).LastWriteTime -gt $startedAt) }
# Never start a sentence over the user. Wait for quiet; if the user sends a message meanwhile, give up.
# Also never start one while the inbox holds the pause (it paused us for the user's voice and is still deciding).
function Wait-UserQuiet {
    $w = [System.Diagnostics.Stopwatch]::StartNew(); $logged = $false
    while ((Test-UserTalking $QuietS) -or (Test-Path $pauseFile) -or (Test-WordsHold)) {
        if (-not $logged) { Log ('waiting: ' + $(if (Test-WordsHold) { 'held for the user''s words' } elseif (Test-Path $pauseFile) { 'paused by the inbox' } else { 'the user is talking' })); $logged = $true }
        if (Test-Cancelled) { return $false }
        if (Test-Nav) { return $false }   # next / back while it waited: the caller moves on
        if (Test-Superseded) { Log 'a newer reply came after the user talked: dropping the rest'; return $false }
        # 120 s as before, longer only while the user's words hold it (a long message of the user's)
        if ($w.Elapsed.TotalSeconds -gt 120 -and -not ((Test-WordsHold) -and $w.Elapsed.TotalSeconds -lt $MaxHoldS)) { Log "gave up waiting after $([int]$w.Elapsed.TotalSeconds) s"; return $false }
        Start-Sleep -Milliseconds 100
    }
    if ($logged) { Log ("quiet after {0:N1} s" -f $w.Elapsed.TotalSeconds) }
    if (Test-OffTarget) { Log "$(Get-OffWhy): stopping"; Hold-Rest; $script:pinStop = $true; return $false }
    if (Test-ChatShown) { Log 'the window shows a claude.ai chat: stopping'; return $false }
    if (Test-Superseded) { Log 'a newer reply came after the user talked: dropping the rest'; return $false }
    return (-not (Test-Cancelled))
}
function Play-Chunk([byte[]]$bytes, [string]$chunkText, [string]$label) {
    $seconds = $script:dataBytes / 48000.0
    while ($true) {
        if (Test-Nav) { return }   # next / back: this reply ends here; the main loop picks what plays next
        if (-not (Wait-UserQuiet)) { if ($script:nav) { return }; if (-not $script:pinStop) { Log 'yielded: the user spoke, stopping' }; $script:stopAll = $true; return }
        # truncate-to-heard: publish what is being spoken now (mtime = this sentence started)
        # audio for the panel orb; written before progress, which the panel times playback from
        try { [System.IO.File]::WriteAllBytes((Join-Path $env:TEMP 'claude-speak.wav'), $bytes) } catch {}
        Set-Content -Path $progressFile -Value $chunkText -Encoding UTF8
        $ms = New-Object System.IO.MemoryStream(,$bytes)
        $script:player.Stream = $ms
        # the moment the reading becomes audible, published before the first sample plays
        $t0 = Now-Ms
        try { [System.IO.File]::WriteAllText($playingFile, ("{0} {1}" -f $t0, ($t0 + [int64](($seconds + 0.15) * 1000)))) } catch {}
        $script:player.Play()
        Log ("play {0} ({1:N1} s)" -f $label, $seconds)
        $sw = [System.Diagnostics.Stopwatch]::StartNew(); $paused = $false; $why = ''; $tick = 0; $chat = $false; $super = $false; $off = $false
        while ($sw.Elapsed.TotalSeconds -lt $seconds + 0.15) {
            if (Test-Path $pauseFile) { $paused = $true; $why = 'pause file'; break }
            if (Test-Nav) { break }
            # the user started talking mid-sentence: stop now, wait for quiet, replay the sentence
            if (Test-UserTalking 0.3) { $paused = $true; $why = 'the user is talking'; break }
            if (((++$tick) % 25 -eq 0) -and (Test-OffTarget)) { $off = $true; break }  # about every 0.5 s
            if (($tick % 25 -eq 0) -and (Test-ChatShown)) { $chat = $true; break }
            if (($tick % 5 -eq 0) -and (Test-Superseded)) { $super = $true; break }      # about every 0.1 s
            Start-Sleep -Milliseconds 20   # was 40: yield to the user sooner (2026-10-02)
        }
        $script:player.Stop(); $ms.Dispose()
        if ($script:nav) { try { [System.IO.File]::WriteAllText($playingFile, ("{0} {1}" -f $t0, (Now-Ms))) } catch {}; return }
        if ($chat -or $super -or $off) {
            try { [System.IO.File]::WriteAllText($playingFile, ("{0} {1}" -f $t0, (Now-Ms))) } catch {}
            Log $(if ($off) { "$(Get-OffWhy): stopping" } elseif ($chat) { 'the window shows a claude.ai chat: stopping' } else { 'a newer reply came after the user talked: dropping the rest' })
            if ($off) { Hold-Rest }
            $script:stopAll = $true; return
        }
        try { [System.IO.File]::WriteAllText($playingFile, ("{0} {1}" -f $t0, (Now-Ms))) } catch {}
        if (-not $paused) { return }
        Log ("paused after {0} ms ({1})" -f [int]$sw.Elapsed.TotalMilliseconds, $why)
        $w = [System.Diagnostics.Stopwatch]::StartNew(); $heldLogged = $false
        # $MaxPauseS bounds a pause from sound; the user's words (the hold file) keep it up to $MaxHoldS, and then the user's 3 s
        # of quiet are still waited out, so it never comes back over the user mid-message.
        while ((((Test-Path $pauseFile) -or (Test-UserTalking $QuietS)) -and $w.Elapsed.TotalSeconds -lt $MaxPauseS) -or
               (((Test-WordsHold) -or ($heldLogged -and (Test-UserTalking $QuietS))) -and $w.Elapsed.TotalSeconds -lt $MaxHoldS)) {
            if ((-not $heldLogged) -and (Test-WordsHold)) { Log 'held for the user''s words'; $heldLogged = $true }
            if (Test-Cancelled) { Remove-Item $pauseFile -ErrorAction SilentlyContinue; $script:stopAll = $true; return }
            if (Test-Superseded) { Log 'a newer reply came after the user talked: dropping the rest'; $script:stopAll = $true; return }
            if (Test-Nav) { return }   # the pause is the inbox's; it lets go of it itself
            Start-Sleep -Milliseconds 100
        }
        Remove-Item $pauseFile -ErrorAction SilentlyContinue
        if (Test-Superseded) { Log 'a newer reply came after the user talked: dropping the rest'; $script:stopAll = $true; return }
        if (Test-OffTarget) { Log "$(Get-OffWhy): stopping"; Hold-Rest; $script:stopAll = $true; return }
        if (Test-ChatShown) { Log 'the window shows a claude.ai chat: stopping'; $script:stopAll = $true; return }
        Log ("resumed after {0:N1} s (replaying sentence)" -f $w.Elapsed.TotalSeconds)
    }
}

# 2026-10-12: Kokoro hung after hours idle and every reply fell back to the Windows voice, which was not
# acceptable. Now a reply first checks Kokoro with a one-word request; if it does not answer, the container is restarted
# (one restart per 4 min across all hook copies and src\voice_watchdog.py) and the reply waits for it. The
# Windows voice is never used.
function Test-Kokoro {
    try {
        $c = New-Object System.Net.Http.HttpClient; $c.Timeout = [TimeSpan]::FromSeconds(12)
        $b = @{ model = 'kokoro'; voice = $Voice; input = 'ok'; response_format = 'wav' } | ConvertTo-Json -Compress
        $r = $c.PostAsync($KokoroUrl, (New-Object System.Net.Http.StringContent($b, [Text.Encoding]::UTF8, 'application/json'))).GetAwaiter().GetResult()
        $ok = $r.IsSuccessStatusCode; $c.Dispose(); return $ok
    } catch { return $false }
}
function Ensure-Kokoro {
    if (Test-Kokoro) { return $true }
    Log 'kokoro not answering: restarting it'
    $lock = Join-Path $env:TEMP 'claude-kokoro.restarting'
    if (-not (Test-Path $lock) -or ((Get-Date) - (Get-Item $lock).LastWriteTime).TotalSeconds -gt 240) {
        Set-Content -Path $lock -Value (Get-Date -Format o)
        Start-Process docker -ArgumentList 'restart', $KokoroContainer -WindowStyle Hidden -Wait
    }
    for ($k = 0; $k -lt 75; $k++) { Start-Sleep -Seconds 2; if (Test-Kokoro) { Log 'kokoro back'; return $true } }
    return $false
}

$usedKokoro = $false
$script:stopAll = $false
try {
    Add-Type -AssemblyName System.Net.Http
    $script:http = New-Object System.Net.Http.HttpClient; $script:http.Timeout = [TimeSpan]::FromSeconds(240)
    if (-not $TestMode -and -not (Ensure-Kokoro)) { throw 'kokoro still down after a restart' }
    if ($TestMode) {
        # silent stand-in for SoundPlayer: Play-Chunk keeps the time itself, so nothing else changes
        $script:player = New-Object PSObject -Property @{ Stream = $null }
        $script:player | Add-Member -MemberType ScriptMethod -Name Play -Value { }
        $script:player | Add-Member -MemberType ScriptMethod -Name Stop -Value { }
    } else {
        $script:player = New-Object System.Media.SoundPlayer
    }
    # What plays, in order: new replies (text) and replies already read (hist = their history file). Next / back /
    # again change what comes after the reply they stop.
    $queue = New-Object System.Collections.ArrayList
    if ($replayStart) { [void]$queue.Add(@{ hist = $replayStart }) }
    foreach ($text in $texts) { [void]$queue.Add(@{ text = $text }) }
    while ($queue.Count -gt 0) {
        $item = $queue[0]; $queue.RemoveAt(0)
        $script:itemSid = if ($item.hist) { Get-HistSid8 $item.hist } else { Get-Sid8 "$($payload.session_id)" }
        $script:curText = $item.text
        if (Test-OffTarget) { Log "$(Get-OffWhy): not read now"; Hold-Rest; break }
        if ($item.hist) {
            $t = Read-History $item.hist
            if (-not $t) { continue }
            $hist = $item.hist
        } else {
            $h = Get-Hash $item.text
            if ($Mode -ne 'held' -and (Test-Spoken $h)) { continue }
            Mark-Spoken $h
            $t = Clean-Text $item.text
            if (-not $t) { continue }
            $hist = Add-History $t
        }
        # @() is required: a function that returns a one-element list is unrolled by PowerShell into a
        # plain string, and $chunks[0] was then its FIRST CHARACTER. Every one-chunk reply was sent to
        # Kokoro as a single letter ("T", "B", "Y"): the "I hear the first letter" bug (2026-09-25).
        $chunks = @(Split-Chunks $t)
        $sid = "$($payload.session_id)"; if ($sid.Length -gt 8) { $sid = $sid.Substring(0, 8) }
        Log ("speak mode={0} chars={1} chunks={2} session={3} agent={4}{5}" -f $Mode, $t.Length, $chunks.Count, $sid, "$($payload.agent_id)", $(if ($item.hist) { " replay=$hist" } else { '' }))
        $script:nav = ''
        $pending = Request-Kokoro $chunks[0]
        for ($i = 0; $i -lt $chunks.Count; $i++) {
            $resp = $pending.GetAwaiter().GetResult()
            if (-not $resp.IsSuccessStatusCode) { throw "kokoro http $($resp.StatusCode)" }
            $bytes = $resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            if ($i + 1 -lt $chunks.Count) { $pending = Request-Kokoro $chunks[$i + 1] }
            $usedKokoro = $true
            $script:dataBytes = $bytes.Length - 44
            $bytes = Repair-WavHeader $bytes
            Play-Chunk $bytes $chunks[$i] ("{0}/{1}" -f ($i + 1), $chunks.Count)
            if ($script:stopAll -or $script:nav) { break }
        }
        if ($script:stopAll) { break }
        $cmd = $script:nav; $script:nav = ''
        if ($cmd -eq 'next') {
            Log ('next: ' + $(if ($queue.Count) { 'on to the next reply' } else { 'no reply queued here; the next speaker (if any) goes on' }))
        } elseif ($cmd -eq 'again' -and $hist) {
            [void]$queue.Insert(0, @{ hist = $hist }); Log 'again: this reply from its start'
        } elseif ($cmd -eq 'back' -and $hist) {
            [void]$queue.Insert(0, @{ hist = $hist })
            $prev = Get-HistoryBefore $hist
            if ($prev) { [void]$queue.Insert(0, @{ hist = $prev }); Log "back: the reply before ($prev), then this one" }
            else { Log 'back: nothing before this reply; this one from its start' }
        }
    }
} catch {
    Log "error: $($_.Exception.Message)"
    if (-not $usedKokoro -and -not $TestMode) { Log 'not read: kokoro down; no fallback voice (by design, 2026-10-12)' }
}
if ((Test-Path $pidFile) -and ((Get-Content $pidFile) -eq $PID)) { Remove-Item $pidFile -ErrorAction SilentlyContinue }
Remove-Item $pauseFile -ErrorAction SilentlyContinue
Remove-Item $progressFile -ErrorAction SilentlyContinue
Remove-Item $playingFile -ErrorAction SilentlyContinue
if ($usedKokoro) { Log ('done' + $(if ($script:stopAll) { ' (stopped early)' } else { '' })) }
try { $mutex.ReleaseMutex() } catch {}
exit 0
