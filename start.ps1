#requires -Version 7.0
# Launch copilot-proxy. Run from a PowerShell session where `copilot` is
# already authenticated (cred-store token must be reachable to child procs).

param(
    [int]$Port = 8765,
    [string]$Model = "gpt-5-mini",
    [ValidateSet("none","low","medium","high","xhigh","max","")]
    [string]$ReasoningEffort = "low"
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path .\venv\Scripts\python.exe)) {
    Write-Host "Creating venv..."
    python -m venv venv
    .\venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .\venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
}

if (-not (Get-Command copilot -ErrorAction SilentlyContinue)) {
    Write-Warning "copilot.exe not found on PATH. Install GitHub Copilot CLI first."
    exit 1
}

$pyArgs = @('copilot_proxy.py', '--port', $Port)
if ($Model)            { $pyArgs += @('--model', $Model) }
if ($ReasoningEffort)  { $pyArgs += @('--reasoning-effort', $ReasoningEffort) }
.\venv\Scripts\python.exe @pyArgs
