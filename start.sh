#!/usr/bin/env bash
# Launch agentry. Run from a shell where `copilot` is already authenticated
# (`copilot login`). Mirrors start.ps1 — same flag names, same defaults.
set -euo pipefail

PORT=8765
MODEL="gpt-5-mini"
REASONING_EFFORT="low"

usage() {
    cat <<EOF
Usage: ./start.sh [options]
  --port N                HTTP port (default: ${PORT})
  --model NAME            Pass to copilot --model (default: ${MODEL})
  --reasoning-effort X    low | medium | high (default: ${REASONING_EFFORT})
  -h, --help              Show this help

Examples:
  ./start.sh
  ./start.sh --model claude-haiku-4.5 --reasoning-effort medium
  ./start.sh --port 9000
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --reasoning-effort) REASONING_EFFORT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

cd "$(dirname "$(readlink -f "$0")")"

if [[ ! -x venv/bin/python ]]; then
    echo "Creating venv..."
    python3 -m venv venv
    venv/bin/python -m pip install --quiet --upgrade pip
    venv/bin/python -m pip install --quiet -r requirements.txt
fi

if ! command -v copilot >/dev/null 2>&1; then
    echo "ERROR: 'copilot' not found on PATH." >&2
    echo "       Install with:  npm install -g @github/copilot" >&2
    echo "       Then log in:   copilot login" >&2
    exit 1
fi

ARGS=(agentry.py --port "$PORT")
[[ -n "$MODEL" ]]            && ARGS+=(--model "$MODEL")
[[ -n "$REASONING_EFFORT" ]] && ARGS+=(--reasoning-effort "$REASONING_EFFORT")

exec venv/bin/python "${ARGS[@]}"
