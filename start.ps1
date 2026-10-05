#requires -Version 7.0
# Launch agentry. Run from a PowerShell session where the chosen backend's CLI
# is already authenticated:
#   copilot backend -> `copilot login` (cred-store token reachable to children)
#   codex backend   -> `codex login`   (ChatGPT account)
#   claude backend  -> Claude Code CLI already logged in (its own OAuth/API key)
#   grok backend    -> `grok login`    (grok.com / SuperGrok account; keep XAI_API_KEY unset)
#
# Run a test instance on a different -Port than a running prod instance to
# avoid an "address already in use" collision (agentry-vs-agentry).
#
# Flags: PowerShell style (-Model X -ReasoningEffort high; prefixes such as
# -Reasoning work) or GNU style exactly as start.sh / agentry.py take them
# (--model X --reasoning-effort high). GNU flags are forwarded to agentry.py
# verbatim and win over the launcher defaults. A misspelt flag is never a
# silent no-op: ValidateSet rejects a bad -Backend/-ReasoningEffort value
# here, and anything else unrecognised is forwarded and exits 2 at argparse.

[CmdletBinding(PositionalBinding = $false)]
param(
    [int]$Port = 8765,
    [ValidateSet("copilot","codex","claude","grok")]
    [string]$Backend = "copilot",
    [string]$Model = "",
    [ValidateSet("none","minimal","low","medium","high","xhigh","max","ultra","")]
    [string]$ReasoningEffort = "",
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$Rest = @()
)

# argparse's choices are case-sensitive; PowerShell's ValidateSet is not.
$ReasoningEffort = $ReasoningEffort.ToLowerInvariant()
$Backend = $Backend.ToLowerInvariant()

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# Backend defaults: copilot pins gpt-5.6-luna — cheapest current AI-credit
# tier ($0.20/M input, ~10x below terra), benchmarked 2026-08; codex follows
# its own configured model (last selected in the codex TUI) unless overridden.
if (-not $Model -and $Backend -eq "copilot") { $Model = "gpt-5.6-luna" }
# Effort default is per backend: low everywhere except grok, whose models are
# weak enough that high (the CLI's own default) is the sensible floor.
if (-not $ReasoningEffort) { $ReasoningEffort = if ($Backend -eq "grok") { "high" } else { "low" } }

# github-copilot-sdk needs Python 3.11+. A venv left over from an older
# interpreter silently skips the SDK, so validate the venv's version on every
# start and rebuild it when it's missing, broken, or too old.
$minMinor = 11

function Find-Python {
    # Prefer the newest 3.x via the py launcher, fall back to `python` on PATH.
    $pyList = if (Get-Command py -ErrorAction SilentlyContinue) { py -0p 2>$null } else { @() }
    foreach ($v in "3.13", "3.12", "3.11") {
        if ($pyList -match [regex]::Escape("-V:$v")) { return @("py", "-$v") }
    }
    $sys = python --version 2>$null
    if ($sys -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge $minMinor) { return @("python") }
    return $null
}

$venvPy = ".\venv\Scripts\python.exe"
$venvOk = $false
if (Test-Path $venvPy) {
    $ver = & $venvPy --version 2>$null
    if ($ver -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge $minMinor) { $venvOk = $true }
    else { Write-Host "Existing venv is $ver (need 3.$minMinor+); recreating..." }
}

if (-not $venvOk) {
    $py = Find-Python
    if (-not $py) {
        Write-Warning "Python 3.$minMinor+ not found (required by github-copilot-sdk). Install it, e.g.: winget install Python.Python.3.13"
        exit 1
    }
    if (Test-Path .\venv) { Remove-Item -Recurse -Force .\venv }
    Write-Host "Creating venv..."
    & $py[0] @($py[1..($py.Count-1)] + @('-m', 'venv', 'venv'))
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        Write-Warning "venv creation failed."
        exit 1
    }
    & $venvPy -m pip install --quiet --upgrade pip
}

# Verify deps are importable (catches a venv created before requirements.txt
# grew, or an interrupted install); install only when something is missing.
& $venvPy -c "import flask, copilot" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing requirements..."
    & $venvPy -m pip install --quiet -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "pip install -r requirements.txt failed."
        exit 1
    }
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
    $cli = switch ($Backend) { "codex" { "codex" } "grok" { "grok" } default { "claude" } }
    # grok's installer drops the exe in ~/.grok/bin and adds it to PATH for new
    # shells; accept that location directly so a fresh install works at once.
    $grokHome = Join-Path $HOME ".grok\bin\grok.exe"
    if (-not (Get-Command $cli -ErrorAction SilentlyContinue) -and
        -not ($Backend -eq "grok" -and (Test-Path $grokHome))) {
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
$pyArgs += $Rest   # GNU-style flags; argparse lets the last occurrence win
.\venv\Scripts\python.exe @pyArgs
