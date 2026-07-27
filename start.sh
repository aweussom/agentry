#!/usr/bin/env bash
# Launch agentry. Run from a shell where the chosen backend's CLI is already
# authenticated (`copilot login`, `codex login`, or the Claude Code CLI's own
# login). Mirrors start.ps1 — same flag names, same defaults.
#
# Run a test instance on a different --port than a running prod instance to
# avoid an "address already in use" collision (agentry-vs-agentry).
set -euo pipefail

PORT=8765
BACKEND="copilot"
MODEL=""
REASONING_EFFORT="low"

usage() {
    cat <<EOF
Usage: ./start.sh [options]
  --port N                HTTP port (default: ${PORT})
  --backend NAME          copilot | codex | claude (default: ${BACKEND})
  --model NAME            Model override (copilot default: gpt-5-mini;
                          codex default: whatever codex itself is
                          configured for (last TUI selection);
                          claude default: claude-sonnet-4-6)
  --reasoning-effort X    low | medium | high (default: ${REASONING_EFFORT};
                          no-op on claude — it has no effort knob)
  -h, --help              Show this help

Examples:
  ./start.sh
  ./start.sh --backend codex
  ./start.sh --backend codex --model gpt-5.6-luna --reasoning-effort low
  ./start.sh --backend claude
  ./start.sh --port 9000
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --backend) BACKEND="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --reasoning-effort) REASONING_EFFORT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

cd "$(dirname "$(readlink -f "$0")")"

# Backend defaults: copilot pins gpt-5-mini (benchmarked); codex follows its
# own configured model (last selected in the codex TUI) unless overridden.
if [[ -z "$MODEL" && "$BACKEND" == "copilot" ]]; then MODEL="gpt-5-mini"; fi

if [[ ! -x venv/bin/python ]]; then
    echo "Creating venv..."
    # github-copilot-sdk needs Python 3.11+; prefer the newest available.
    PY=""
    for v in python3.13 python3.12 python3.11; do
        command -v "$v" >/dev/null 2>&1 && PY="$v" && break
    done
    if [[ -z "$PY" ]]; then
        if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PY=python3
        else
            echo "ERROR: Python 3.11+ not found (required by github-copilot-sdk)." >&2
            exit 1
        fi
    fi
    "$PY" -m venv venv
    venv/bin/python -m pip install --quiet --upgrade pip
    venv/bin/python -m pip install --quiet -r requirements.txt
fi

# The copilot backend runs on the SDK's own downloaded runtime — no CLI on
# PATH needed — but it reads the ~/.copilot credential store, so a one-time
# `copilot login` (from any installed Copilot CLI) must have happened.
if [[ "$BACKEND" == "copilot" ]]; then
    if [[ ! -d "$HOME/.copilot" ]]; then
        echo "ERROR: no ~/.copilot found. Log in once first: npm i -g @github/copilot && copilot login" >&2
        exit 1
    fi
else
    case "$BACKEND" in codex) CLI=codex ;; *) CLI=claude ;; esac
    if ! command -v "$CLI" >/dev/null 2>&1; then
        echo "ERROR: '$CLI' not found on PATH (needed for the $BACKEND backend)." >&2
        exit 1
    fi
fi

# Optional: the claude backend shows real 5h/weekly quota if the claude-code-quota
# tool's cache exists. Point the user at it if it's missing — agentry works fine
# without it (falls back to a coarse per-turn signal).
if [[ "$BACKEND" == "claude" && ! -f "$HOME/.claude/quota-data.json" ]]; then
    echo "  (i) claude quota display off: no ~/.claude/quota-data.json found."
    echo "      Optional — install https://github.com/aweussom/claude-code-quota for 5h/weekly %."
    echo "      Caveat: that cache is refreshed by an ACTIVE/interactive claude-code session's"
    echo "      status line, NOT by agentry's headless 'claude -p' — keep a claude session"
    echo "      running elsewhere to keep the numbers fresh."
fi

ARGS=(agentry.py --port "$PORT" --backend "$BACKEND")
[[ -n "$MODEL" ]]            && ARGS+=(--model "$MODEL")
[[ -n "$REASONING_EFFORT" ]] && ARGS+=(--reasoning-effort "$REASONING_EFFORT")

exec venv/bin/python "${ARGS[@]}"
