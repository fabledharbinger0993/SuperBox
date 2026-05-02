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

# ── First-run setup (run inline here so the user sees progress) ──────────
# launch.sh normally opens setup.sh in a new Terminal window, but that
# requires macOS Automation permission for Terminal — which silently fails
# on a fresh machine, hanging the poll loop forever.  When installing via
# this script we already have a visible Terminal, so run it here directly.
if [ ! -f "$INSTALL_DIR/.fablegear_ready" ]; then
    _blue "Running first-time setup (installs Homebrew, ffmpeg, Python packages)..."
    _blue "You may be prompted for your Mac password."
    echo ""
    bash "$INSTALL_DIR/setup.sh"
fi

# ── Launch ────────────────────────────────────────────────────────────────
_green "Starting FableGear..."
echo ""
bash "$INSTALL_DIR/launch.sh"
