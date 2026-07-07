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
                          codex default: gpt-5.4-mini;
                          claude default: claude-sonnet-4-6)
  --reasoning-effort X    low | medium | high (default: ${REASONING_EFFORT};
                          no-op on claude — it has no effort knob)
  -h, --help              Show this help

Examples:
  ./start.sh
  ./start.sh --backend codex
  ./start.sh --backend codex --model gpt-5.4-mini --reasoning-effort low
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

# Backend defaults: copilot pins gpt-5-mini (benchmarked); codex uses its own
# default (gpt-5.4-mini) unless overridden.
if [[ -z "$MODEL" && "$BACKEND" == "copilot" ]]; then MODEL="gpt-5-mini"; fi

if [[ ! -x venv/bin/python ]]; then
    echo "Creating venv..."
    python3 -m venv venv
    venv/bin/python -m pip install --quiet --upgrade pip
    venv/bin/python -m pip install --quiet -r requirements.txt
fi

case "$BACKEND" in codex) CLI=codex ;; claude) CLI=claude ;; *) CLI=copilot ;; esac
if ! command -v "$CLI" >/dev/null 2>&1; then
    echo "ERROR: '$CLI' not found on PATH (needed for the $BACKEND backend)." >&2
    exit 1
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
