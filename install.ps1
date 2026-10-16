# One-command install for Talkover, hands-free voice for Claude Code (Windows 10/11).
#   powershell -ExecutionPolicy Bypass -File .\install.ps1            CPU engines, hooks into ~/.claude/settings.json
#   ... -Gpu              NVIDIA GPU Whisper (much faster speech to text)
#   ... -SkipHooks        do not touch Claude Code's settings.json
#   ... -Autostart        start the floater at logon (Startup folder shortcut)
#   ... -StopWord         also install the optional Vosk "stop" listener (VoiceMode MCP only)
#   ... -SettingsPath P   write the hooks to another settings.json
#   ... -Python P         use this python.exe for the venv
# Needs: Python 3.11+ from python.org (`winget install Python.Python.3.13`; not the Microsoft Store build) and
# Docker Desktop (running).
param(
    [switch]$Gpu,
    [switch]$SkipHooks,
    [switch]$Autostart,
    [switch]$StopWord,
    [string]$SettingsPath = (Join-Path $env:USERPROFILE '.claude\settings.json'),
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
function Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Fail([string]$m) { Write-Host "error: $m" -ForegroundColor Red; exit 1 }

# ---- settings
$Config = Join-Path $Root 'config.env'
if (-not (Test-Path $Config)) { Copy-Item (Join-Path $Root 'config.example.env') $Config; Step 'created config.env from config.example.env' }
$Cfg = @{}
foreach ($l in Get-Content $Config) { if ($l -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { $Cfg[$Matches[1]] = $Matches[2].Trim('"', "'") } }
function Setting([string]$k, [string]$d) { $v = [Environment]::GetEnvironmentVariable($k); if ($v) { $v } elseif ($Cfg[$k]) { $Cfg[$k] } else { $d } }
$SttPort = Setting 'TALKOVER_STT_PORT' '2022'
$TtsPort = Setting 'TALKOVER_TTS_PORT' '8880'
$SttBase = (Setting 'TALKOVER_STT_BASE' "http://127.0.0.1:$SttPort").TrimEnd('/')
$TtsBase = (Setting 'TALKOVER_TTS_BASE' "http://127.0.0.1:$TtsPort").TrimEnd('/')
$WhisperModel = Setting 'TALKOVER_WHISPER_MODEL' 'Systran/faster-whisper-small'

# ---- Python venv
Step 'Python environment'
# The Microsoft Store Python is not used: its app sandbox blocks the microphone (opening the input stream hangs).
function Find-Python([string[]]$cmd) {
    if (-not (Get-Command $cmd[0] -ErrorAction SilentlyContinue)) { return $null }
    $rest = @($cmd | Select-Object -Skip 1)
    $out = & $cmd[0] @rest -c "import sys; print(sys.version_info >= (3, 11)); print(sys.base_prefix); print(sys.executable)" 2>$null
    if ($LASTEXITCODE -or -not $out -or $out[0] -ne 'True') { return $null }
    if ($out[1] -match 'WindowsApps|PythonSoftwareFoundation') { Write-Host "   skipping Microsoft Store Python ($($out[2])): no mic access" -ForegroundColor Yellow; return $null }
    return $out[2]
}
if ($Python) { $PyExe = Find-Python @($Python) }
else {
    $PyExe = $null
    foreach ($c in @(@('py', '-3.13'), @('py', '-3.12'), @('py', '-3.11'), @('python'), @('python3'))) {
        $PyExe = Find-Python $c
        if ($PyExe) { break }
    }
}
if (-not $PyExe) { Fail 'Python 3.11+ (not the Microsoft Store build) not found. Install it with "winget install Python.Python.3.13", or pass -Python <path to python.exe>.' }
Write-Host "   using $PyExe"
$Venv = Join-Path $Root '.venv'
$VPy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $VPy)) {
    & $PyExe -m venv $Venv
    if ($LASTEXITCODE) { Fail 'could not create .venv' }
}
& $VPy -m pip install --disable-pip-version-check -q --upgrade pip
$req = if ($StopWord) { 'requirements-stopword.txt' } else { 'requirements.txt' }
& $VPy -m pip install --disable-pip-version-check -q -r (Join-Path $Root $req)
if ($LASTEXITCODE) { Fail "pip install -r $req failed" }

# ---- engines
Step 'Speech engines (Docker)'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker not found. Install Docker Desktop, start it, and re-run.' }
$ErrorActionPreference = 'Continue'   # native stderr (docker progress) must not stop Windows PowerShell 5.1
docker info *> $null
if ($LASTEXITCODE) { Fail 'Docker is installed but not running. Start Docker Desktop and re-run.' }
$compose = @('compose', '--env-file', $Config, '-f', (Join-Path $Root 'docker-compose.yml'))
if ($Gpu) { $compose += @('-f', (Join-Path $Root 'docker-compose.gpu.yml')) }
& docker @compose up -d
if ($LASTEXITCODE) { Fail 'docker compose up failed' }

function Wait-Url([string]$url, [string]$what, [int]$seconds = 300) {
    $until = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $until) {
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 $url | Out-Null; return } catch { Start-Sleep 3 }
    }
    Fail "$what did not answer at $url within $seconds s (docker logs for details)"
}
Wait-Url "$SttBase/health" 'Whisper'
Step "Whisper model $WhisperModel (first time: a few hundred MB)"
try { Invoke-WebRequest -UseBasicParsing -Method Post -TimeoutSec 1800 "$SttBase/v1/models/$WhisperModel" | Out-Null }
catch { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 409) { } else { Fail "model download failed: $($_.Exception.Message)" } }
Wait-Url "$TtsBase/v1/audio/voices" 'Kokoro' 600

# ---- optional stop-word model
if ($StopWord) {
    $models = Join-Path ([Environment]::ExpandEnvironmentVariables((Setting 'TALKOVER_HOME' (Join-Path $env:USERPROFILE '.talkover'))) -replace '^~', $env:USERPROFILE) 'models'
    if (-not (Test-Path (Join-Path $models 'vosk-model-small-en-us-0.15'))) {
        Step 'Vosk stop-word model (40 MB)'
        New-Item -ItemType Directory -Force $models | Out-Null
        $zip = Join-Path $env:TEMP 'vosk-model-small-en-us-0.15.zip'
        Invoke-WebRequest -UseBasicParsing 'https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip' -OutFile $zip
        Expand-Archive $zip -DestinationPath $models -Force
        Remove-Item $zip
    }
}

# ---- Claude Code hooks
if (-not $SkipHooks) {
    Step "Claude Code hooks ($SettingsPath)"
    & $VPy (Join-Path $Root 'src\install_hooks.py') --settings $SettingsPath
    if ($LASTEXITCODE -eq 2) { Write-Host '   left as is (see above)' -ForegroundColor Yellow }
    elseif ($LASTEXITCODE) { Fail 'hook install failed' }
}

# ---- autostart
if ($Autostart) {
    $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Talkover.lnk'
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = 'powershell.exe'
    $s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $Root 'start.ps1')`""
    $s.WorkingDirectory = $Root
    $s.WindowStyle = 7
    $s.Save()
    Step "starts at logon ($lnk)"
}

Step 'checking'
& $VPy (Join-Path $Root 'src\voice_health.py') --engines-only
Write-Host ''
Write-Host 'Installed. Start it with:  powershell -ExecutionPolicy Bypass -File .\start.ps1' -ForegroundColor Green
