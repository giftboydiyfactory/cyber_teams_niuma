#!/bin/bash
# Check for git updates and restart niuma-bot if needed
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/.venv"
LOG="$HOME/.niuma/niuma.log"

cd "$REPO_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') [check] Fetching remote..."
git fetch origin 2>/dev/null

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main 2>/dev/null || git rev-parse origin/master 2>/dev/null)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [check] No updates. LOCAL=$LOCAL"
    exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') [check] Update found!"
echo "  LOCAL:  $LOCAL"
echo "  REMOTE: $REMOTE"

# Pull latest
echo "$(date '+%Y-%m-%d %H:%M:%S') [check] Pulling latest..."
git pull --ff-only origin main 2>/dev/null || git pull --ff-only origin master 2>/dev/null

# Reinstall package
echo "$(date '+%Y-%m-%d %H:%M:%S') [check] Reinstalling package..."
source "$VENV/bin/activate"
pip install -e ".[dev]" --quiet 2>/dev/null

# Reinstall skills
echo "$(date '+%Y-%m-%d %H:%M:%S') [check] Reinstalling skills..."
bash "$REPO_DIR/scripts/install-skills.sh"

# Kill old niuma process
echo "$(date '+%Y-%m-%d %H:%M:%S') [check] Stopping old niuma..."
pkill -f "niuma.main" 2>/dev/null || true
sleep 2

# Start new niuma
echo "$(date '+%Y-%m-%d %H:%M:%S') [check] Starting niuma..."
source "$VENV/bin/activate"
niuma --daemon

NEW_COMMIT=$(git rev-parse --short HEAD)
echo "$(date '+%Y-%m-%d %H:%M:%S') [check] Restarted on commit $NEW_COMMIT"
