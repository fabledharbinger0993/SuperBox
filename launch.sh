#!/bin/bash
# SuperBox launcher
# Run directly: bash launch.sh
# Or wrap in Automator > Application > Run Shell Script for a dock icon

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/../venv"

# Create venv and install deps if this is the first run
if [ ! -d "$VENV" ]; then
  echo "First run — setting up virtual environment..."
  python3 -m venv "$VENV"
  source "$VENV/bin/activate"
  pip install -q -r "$SCRIPT_DIR/requirements_ui.txt"
  pip install -q -r "$SCRIPT_DIR/requirements.txt"
fi

source "$VENV/bin/activate"

# Open the browser after a short delay so Flask has time to start
(sleep 1.5 && open http://localhost:5001) &

cd "$SCRIPT_DIR"
python3 app.py
