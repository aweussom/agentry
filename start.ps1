#requires -Version 7.0
# Launch agentry. Run from a PowerShell session where the chosen backend's CLI
# is already authenticated:
#   copilot backend -> `copilot login` (cred-store token reachable to children)
#   codex backend   -> `codex login`   (ChatGPT account)
#   claude backend  -> Claude Code CLI already logged in (its own OAuth/API key)
#
# Run a test instance on a different -Port than a running prod instance to
# avoid an "address already in use" collision (agentry-vs-agentry).

param(
    [int]$Port = 8765,
    [ValidateSet("copilot","codex","claude")]
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
    # github-copilot-sdk needs Python 3.11+; prefer the newest via the py launcher.
    $py = $null
    $pyList = if (Get-Command py -ErrorAction SilentlyContinue) { py -0p 2>$null } else { @() }
    foreach ($v in "3.13", "3.12", "3.11") {
        if ($pyList -match [regex]::Escape("-V:$v")) { $py = @("py", "-$v"); break }
    }
    if (-not $py) {
        $sys = python --version 2>$null
        if ($sys -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge 11) { $py = @("python") }
        else {
            Write-Warning "Python 3.11+ not found (required by github-copilot-sdk). Install it, e.g.: winget install Python.Python.3.13"
            exit 1
        }
    }
    & $py[0] @($py[1..($py.Count-1)] + @('-m', 'venv', 'venv'))
    .\venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .\venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
}

# The copilot backend runs on the SDK's own downloaded runtime — no CLI on
# PATH needed — but it reads the ~/.copilot credential store, so a one-time
# `copilot login` (from any installed Copilot CLI) must have happened.
if ($Backend -eq "copilot") {
    if (-not (Test-Path (Join-Path $HOME ".copilot"))) {
        Write-Warning 'No ~/.copilot found. Log in once first: install a Copilot CLI and run "copilot login".'
        exit 1
    }
} else {
    $cli = switch ($Backend) { "codex" { "codex" } default { "claude" } }
    if (-not (Get-Command $cli -ErrorAction SilentlyContinue)) {
        Write-Warning "$cli not found on PATH. Install and authenticate the $Backend backend first."
        exit 1
    }
}

# Optional: the claude backend shows real 5h/weekly quota if the claude-code-quota
# tool's cache exists. Point the user at it if it's missing — agentry works fine
# without it (falls back to a coarse per-turn signal).
if ($Backend -eq "claude" -and -not (Test-Path (Join-Path $HOME ".claude\quota-data.json"))) {
    Write-Host "  (i) claude quota display off: no ~/.claude/quota-data.json found."
    Write-Host "      Optional — install https://github.com/aweussom/claude-code-quota for 5h/weekly %."
    Write-Host "      Caveat: that cache is refreshed by an ACTIVE/interactive claude-code session's"
    Write-Host "      status line, NOT by agentry's headless 'claude -p' — keep a claude session"
    Write-Host "      running elsewhere to keep the numbers fresh."
}

$pyArgs = @('agentry.py', '--port', $Port, '--backend', $Backend)
if ($Model)            { $pyArgs += @('--model', $Model) }
if ($ReasoningEffort)  { $pyArgs += @('--reasoning-effort', $ReasoningEffort) }
.\venv\Scripts\python.exe @pyArgs
