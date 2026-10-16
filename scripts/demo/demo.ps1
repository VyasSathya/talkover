# One command for the demo video: record a take, cut it, render it.
#   powershell -ExecutionPolicy Bypass -File scripts\demo\demo.ps1                 record 75 s, then everything else
#   ... -Seconds 60            shorter take
#   ... -Take demo-out\take-X  skip recording, re-cut and re-render an existing take
#   ... -Synthetic             fallback: a Kokoro voice plays "you" (labeled synthetic in the video)
#   ... -Fixture               no recording at all: a drawn test take (checks the pipeline)
#   ... -Music path.mp3        music bed, ducked under speech
#   ... -Title "Name" -Url "github.com/you/repo"
# Needs the floater running (for a real take), ffmpeg and Node.js 18+. Output: <take>\demo.mp4 and demo.gif.
param(
    [double]$Seconds = 75,
    [string]$Take = '',
    [switch]$Synthetic,
    [switch]$Fixture,
    [string]$Music = '',
    [string]$Title = 'Talkover',
    [string]$Url = ''
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Demo = $PSScriptRoot
function Run([string]$what, [scriptblock]$step) { Write-Host "==> $what" -ForegroundColor Cyan; & $step; if ($LASTEXITCODE) { Write-Host "failed: $what" -ForegroundColor Red; exit 1 } }

foreach ($tool in 'ffmpeg', 'node', 'npm') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Write-Host "$tool not found (winget install Gyan.FFmpeg / OpenJS.NodeJS.LTS)" -ForegroundColor Red; exit 1 }
}
Run 'demo Python packages' { & $Py -m pip install -q --disable-pip-version-check -r (Join-Path $Demo 'requirements.txt') }

if (-not $Take) {
    $Take = Join-Path $Root ('demo-out\take-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    if ($Fixture) { Run 'drawing a test take' { & $Py (Join-Path $Demo 'fixture.py') $Take } }
    else {
        $args2 = @((Join-Path $Demo 'record.py'), '--seconds', $Seconds, '--out', $Take)
        if ($Synthetic) { $args2 += @('--synthetic', (Join-Path $Demo 'synthetic_lines.txt')) }
        Write-Host 'Recording starts in 3 s. Talk to Claude as you normally would; it stops by itself.' -ForegroundColor Green
        Start-Sleep 3
        Run 'recording' { & $Py @args2 }
    }
}
$Take = (Resolve-Path $Take).Path

$buildArgs = @((Join-Path $Demo 'build.py'), $Take, '--title', $Title, '--url', $Url)
if ($Music) { $buildArgs += @('--music', $Music) }
Run 'cutting' { & $Py @buildArgs }

$rem = Join-Path $Demo 'remotion'
if (-not (Test-Path (Join-Path $rem 'node_modules'))) { Push-Location $rem; Run 'installing Remotion (first time only)' { npm install --no-audit --no-fund }; Pop-Location }
Push-Location $rem
$props = Join-Path $Take 'props.json'   # written by build.py
Run 'rendering' { npx remotion render src/index.ts Demo (Join-Path $Take 'demo.mp4') "--props=$props" "--public-dir=$Take" --codec=h264 --crf=18 --log=error }
Pop-Location

Run 'README GIF' {
    ffmpeg -v error -y -i (Join-Path $Take 'demo.mp4') -t 14 -vf "fps=15,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a" (Join-Path $Take 'demo.gif')
}
Write-Host "Done: $Take\demo.mp4 and demo.gif" -ForegroundColor Green
