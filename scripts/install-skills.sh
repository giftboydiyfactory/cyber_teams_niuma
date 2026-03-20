#!/bin/bash
# Install niuma-bot skills into ~/.claude/skills/
# Run this after cloning the repo to set up skill auto-loading.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SKILLS_DIR="$REPO_DIR/skills"
TARGET_DIR="$HOME/.claude/skills"

mkdir -p "$TARGET_DIR"

for skill_dir in "$SKILLS_DIR"/*/; do
    skill_name="$(basename "$skill_dir")"
    target="$TARGET_DIR/$skill_name"

    if [ -L "$target" ]; then
        echo "  Updating symlink: $skill_name"
        rm "$target"
    elif [ -d "$target" ]; then
        echo "  Backing up existing: $skill_name -> ${skill_name}.bak"
        mv "$target" "${target}.bak"
    fi

    ln -s "$skill_dir" "$target"
    echo "  ✅ Installed: $skill_name -> $skill_dir"
done

echo ""
echo "Done! Skills installed to $TARGET_DIR"
echo "These skills will be automatically loaded by Claude Code."
