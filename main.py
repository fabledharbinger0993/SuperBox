"""
RekitBox / main.py

Native-window entry point for both development and PyInstaller builds.

Starts the Flask/Waitress server in a background daemon thread, waits for it
to be ready, then opens a pywebview window.  Because the server thread is a
daemon, it automatically dies when the main thread (pywebview) exits — no
cleanup needed.

If the server is already running on port 5001 (e.g. a second launch while the
app is open), the existing server is reused and a new window is opened.
"""

import os
import sys
import threading
import time
from pathlib import Path

# ── Resource root — works in both dev and PyInstaller bundle ─────────────────
# PyInstaller extracts everything to sys._MEIPASS at runtime.
# In dev, __file__ is just the repo root.
_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).parent.resolve()))

# Make sure toolkit modules are importable
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Tell app.py where to find templates and static when bundled
os.environ.setdefault('REKITBOX_ROOT', str(_ROOT))

# ── Server config ─────────────────────────────────────────────────────────────
# Bind to all interfaces so Tailscale (and LAN) can reach the mobile API.
# The desktop UI still opens via localhost; the mobile API uses the Tailscale IP.
_HOST = '0.0.0.0'
_PORT = 5001
_LOCAL_URL = f'http://127.0.0.1:{_PORT}/'   # used for health-check and browser


def _server_running() -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(_LOCAL_URL, timeout=1)
        return True
    except Exception:
        return False


def _start_server() -> None:
    from waitress import serve
    from app import app as flask_app
    serve(flask_app, host=_HOST, port=_PORT, threads=8)


def _wait_for_server(retries: int = 40, delay: float = 0.15) -> bool:
    for _ in range(retries):
        if _server_running():
            return True
        time.sleep(delay)
    return False


class _Api:
    """Exposed to JS as window.pywebview.api — used for the native folder picker.

    pywebview's create_file_dialog() is far more reliable than osascript in
    the PyInstaller bundle because it goes through WKWebView's native APIs
    rather than requiring Finder Automation permission.
    """
    def __init__(self):
        self._window = None

    def pick_folder(self):
        if not self._window:
            return None
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if result:
            import os
            return os.path.normpath(result[0])
        return None


if __name__ == '__main__':
    if not _server_running():
        threading.Thread(target=_start_server, daemon=True).start()
        if not _wait_for_server():
            print('RekitBox: server failed to start', file=sys.stderr)
            sys.exit(1)

    import webview

    _api = _Api()

    window = webview.create_window(
        title='RekitBox',
        url=_LOCAL_URL,
        width=1400,
        height=900,
        min_size=(900, 600),
        resizable=True,
        background_color='#07070f',
        js_api=_api,
    )

    _api._window = window

    webview.start(debug=False)
