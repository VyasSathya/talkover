# Stop the Claude voice immediately: cancel queued speakers, kill the current one.
$ErrorActionPreference = 'SilentlyContinue'
Set-Content -Path (Join-Path $env:TEMP 'claude-speak.cancel') -Value (Get-Date -Format o)
$pidFile = Join-Path $env:TEMP 'claude-speak.pid'
if (Test-Path $pidFile) { $p = Get-Content $pidFile; if ($p) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }; Remove-Item $pidFile -ErrorAction SilentlyContinue }
Remove-Item (Join-Path $env:TEMP 'claude-speak.pause') -ErrorAction SilentlyContinue
$hook = (Join-Path $PSScriptRoot 'speak-response.ps1').ToLower().Replace('/', '\')
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Replace('/', '\').Contains($hook) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
exit 0
