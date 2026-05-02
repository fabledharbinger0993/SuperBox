#!/bin/bash
# shellcheck shell=bash
# FableGear installer
#
# One-command install — paste this in Terminal:
#   curl -fsSL https://raw.githubusercontent.com/fabledharbinger0993/FableGear/main/install.sh | bash
#
# What it does:
#   1. Clones FableGear to ~/FableGear/FableGear  (or pulls if already installed)
#   2. Hands off to launch.sh which handles dependencies, venv, and first launch
#   3. On first launch, offers to add FableGear to your Dock natively

set -euo pipefail

REPO_URL="https://github.com/fabledharbinger0993/FableGear.git"
PARENT_DIR="$HOME/FableGear"
INSTALL_DIR="$PARENT_DIR/FableGear"

# ── Colour output helpers ──────────────────────────────────────────────────
_green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
_blue()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
_red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }

_green "──────────────────────────────────────────"
_green "  FableGear Installer"
_green "──────────────────────────────────────────"

# ── Require git ───────────────────────────────────────────────────────────
if ! command -v git &>/dev/null; then
    _red "Git is required."
    echo "Install Xcode Command Line Tools first, then re-run:"
    echo "  xcode-select --install"
    exit 1
fi

# ── Clone or update ───────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    _blue "FableGear already installed — pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    _blue "Cloning FableGear to $INSTALL_DIR ..."
    mkdir -p "$PARENT_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ── Launch ────────────────────────────────────────────────────────────────
_green "Starting FableGear..."
_green "(A setup window will open if this is your first time — it only runs once.)"
echo ""
bash "$INSTALL_DIR/launch.sh"
