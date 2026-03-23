#!/bin/bash
# niuma-bot FULL RESET: clear all state and start fresh
# Use this only when you want a completely new Manager session and chat.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/.venv"
DB="$HOME/.niuma/niuma.db"

echo "🐴 niuma-bot: FULL RESET..."

# 1. Kill all existing niuma processes
echo "  [1/4] Killing old processes..."
pkill -9 -f "niuma" 2>/dev/null || true
sleep 1

# 2. Ensure venv exists
if [ ! -d "$VENV" ]; then
    echo "  [2/4] Creating venv..."
    python3 -m venv "$VENV"
    source "$VENV/bin/activate"
    pip install -e "$REPO_DIR[dev]" --quiet
else
    echo "  [2/4] Venv exists, activating..."
    source "$VENV/bin/activate"
fi

# 3. Clear ALL DB state (nuclear option)
echo "  [3/4] Clearing all state..."
mkdir -p "$HOME/.niuma"
python3 -c "
import asyncio, aiosqlite, os

async def clean():
    db_path = os.path.expanduser('$DB')
    if not os.path.exists(db_path):
        print('    No DB yet, will be created on first run')
        return
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute('DELETE FROM bot_state')
        await conn.execute('DELETE FROM poll_state')
        await conn.execute('DELETE FROM sessions')
        await conn.execute('DELETE FROM messages')
        await conn.execute('DELETE FROM watched_chats')
        await conn.commit()
        print('    Cleared ALL state (bot_state, poll_state, sessions, messages, watched_chats)')

asyncio.run(clean())
"

# 4. Start daemon
echo "  [4/4] Starting daemon..."
niuma --daemon

echo ""
echo "⚠️  niuma-bot started with FRESH state. New Manager session and chat will be created."
echo "   Logs: tail -f ~/.niuma/niuma.log"
