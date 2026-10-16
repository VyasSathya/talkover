# Stop the Talkover programs started from this folder (and any reading in progress).
#   powershell -ExecutionPolicy Bypass -File .\stop.ps1 [-Engines]     -Engines also stops the Docker engines
param([switch]$Engines)
$Root = $PSScriptRoot
$Src = (Join-Path $Root 'src').ToLower()
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Replace('/', '\').Contains($Src) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "stopped $($_.ProcessId)" }
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'hooks\stop-speaking.ps1')
if ($Engines) {
    $Config = Join-Path $Root 'config.env'
    if (-not (Test-Path $Config)) { $Config = Join-Path $Root 'config.example.env' }
    & docker compose --env-file $Config -f (Join-Path $Root 'docker-compose.yml') stop
}
