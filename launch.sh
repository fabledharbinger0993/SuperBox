#!/bin/bash
# SuperBox launcher
# Run directly: bash launch.sh
# Or wrap in Automator > Application > Run Shell Script for a dock icon

# Silence all output so Automator never sees anything to misread as an error.
# Flask and server logs still go to superbox.log via the explicit redirect below.
exec > /dev/null 2>&1

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

# If the server is already running, just open the browser and exit cleanly
if curl -s --max-time 1 http://localhost:5001 > /dev/null 2>&1; then
  open http://localhost:5001
  exit 0
fi

# Open the browser after a short delay so Flask has time to start
(sleep 2 && open http://localhost:5001) &

cd "$SCRIPT_DIR"
LOG="$SCRIPT_DIR/../superbox.log"
nohup "$VENV/bin/waitress-serve" --host=127.0.0.1 --port=5001 --threads=8 app:app >> "$LOG" 2>&1 &
