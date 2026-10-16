# Start Talkover: the speech engines (docker compose), then the watchdog, the mic inbox and the floating panel,
# all hidden and detached. Safe to run twice: each program keeps a single copy (named mutex).
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 [-Gpu]
param([switch]$Gpu)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Config = Join-Path $Root 'config.env'
if (-not (Test-Path $Config)) { $Config = Join-Path $Root 'config.example.env' }
$PyW = Join-Path $Root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $PyW)) { Write-Host 'Not installed yet: run install.ps1 first.' -ForegroundColor Red; exit 1 }

$compose = @('compose', '--env-file', $Config, '-f', (Join-Path $Root 'docker-compose.yml'))
if ($Gpu) { $compose += @('-f', (Join-Path $Root 'docker-compose.gpu.yml')) }
$ErrorActionPreference = 'Continue'   # docker writes progress to stderr; Windows PowerShell 5.1 would stop on it
& docker @compose up -d *> $null
if ($LASTEXITCODE) { Write-Host 'docker compose up failed (is Docker Desktop running?). The watchdog keeps retrying.' -ForegroundColor Yellow }

$programs = @('voice_watchdog.py', 'voice_inbox.py', 'voice_panel.py')
if ((Get-Content $Config) -match '^\s*TALKOVER_STOP_WORD\s*=\s*1') { $programs += 'stop_word_listener.py' }
foreach ($p in $programs) {
    Start-Process -FilePath $PyW -ArgumentList ('"' + (Join-Path $Root "src\$p") + '"') -WorkingDirectory (Join-Path $Root 'src') -WindowStyle Hidden
}
Write-Host 'Talkover started. Talk any time; say "stop" or "wait" to cut a reading. Stop it with stop.ps1.' -ForegroundColor Green
