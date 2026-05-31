#requires -Version 7.0
# Launch agentry. Run from a PowerShell session where the chosen backend's CLI
# is already authenticated:
#   copilot backend -> `copilot login` (cred-store token reachable to children)
#   codex backend   -> `codex login`   (ChatGPT account)
#
# Run a test instance on a different -Port than a running prod instance to
# avoid an "address already in use" collision (agentry-vs-agentry).

param(
    [int]$Port = 8765,
    [ValidateSet("copilot","codex")]
    [string]$Backend = "copilot",
    [string]$Model = "",
    [ValidateSet("none","minimal","low","medium","high","xhigh","max","")]
    [string]$ReasoningEffort = "low"
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# Backend defaults: copilot pins gpt-5-mini (benchmarked); codex uses its own
# default (gpt-5.4-mini) unless overridden.
if (-not $Model -and $Backend -eq "copilot") { $Model = "gpt-5-mini" }

if (-not (Test-Path .\venv\Scripts\python.exe)) {
    Write-Host "Creating venv..."
    python -m venv venv
    .\venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .\venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
}

$cli = if ($Backend -eq "codex") { "codex" } else { "copilot" }
if (-not (Get-Command $cli -ErrorAction SilentlyContinue)) {
    Write-Warning "$cli not found on PATH. Install and authenticate the $Backend backend first."
    exit 1
}

$pyArgs = @('agentry.py', '--port', $Port, '--backend', $Backend)
if ($Model)            { $pyArgs += @('--model', $Model) }
if ($ReasoningEffort)  { $pyArgs += @('--reasoning-effort', $ReasoningEffort) }
.\venv\Scripts\python.exe @pyArgs
