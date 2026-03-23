#!/bin/bash
# niuma-bot start: kill old processes, preserve state, start daemon
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/.venv"

echo "🐴 niuma-bot: starting..."

# 1. Kill all existing niuma processes
echo "  [1/3] Killing old processes..."
pkill -9 -f "niuma" 2>/dev/null || true
sleep 1

# 2. Ensure venv exists
if [ ! -d "$VENV" ]; then
    echo "  [2/3] Creating venv..."
    python3 -m venv "$VENV"
    source "$VENV/bin/activate"
    pip install -e "$REPO_DIR[dev]" --quiet
else
    echo "  [2/3] Venv exists, activating..."
    source "$VENV/bin/activate"
fi

# 3. Start daemon (preserves DB state: manager session, chat, poll state)
echo "  [3/3] Starting daemon..."
mkdir -p "$HOME/.niuma"
niuma --daemon

echo ""
echo "✅ niuma-bot started! Logs: tail -f ~/.niuma/niuma.log"
echo "   Manager session and chat preserved from previous run."
