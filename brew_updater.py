"""
brew_updater.py — background Homebrew update checker for SuperBox.

Checks whether the Homebrew formulae SuperBox depends on are outdated.
Runs once at startup (after a short delay so it doesn't slow the boot)
and then again every 7 days in a daemon thread.

Public API
----------
start_background_checker()  — call once at app startup
check_now()                 — trigger an immediate check; returns status dict
get_status()                — return the last cached status dict (non-blocking)

Status dict shape:
    {
        "outdated":    [{"name": str, "installed": str, "current": str}, ...],
        "checked_at":  "2026-04-02T18:30:00" | None,
        "error":       str | None,
    }
"""

import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime

log = logging.getLogger(__name__)

# ── Packages SuperBox directly depends on via Homebrew ────────────────────────

# Detect the running Python's formula name (e.g. "python@3.14")
_PY_FORMULA = f"python@{sys.version_info.major}.{sys.version_info.minor}"

BREW_DEPS: frozenset[str] = frozenset({
    _PY_FORMULA,
    "ffmpeg",
    "lame",          # MP3 encoding
    "opus",          # Opus encoding
    "libvorbis",     # OGG encoding
    "flac",          # FLAC encoding
    "libsndfile",    # WAV / AIFF I/O
})

# ── Check interval ────────────────────────────────────────────────────────────

_CHECK_INTERVAL = 7 * 24 * 3600   # 7 days in seconds
_STARTUP_DELAY  = 45               # seconds after boot before first check

# ── In-memory cache ───────────────────────────────────────────────────────────

_lock: threading.Lock = threading.Lock()
_status: dict = {
    "outdated":   [],
    "checked_at": None,
    "error":      None,
}


# ── Core check ────────────────────────────────────────────────────────────────

def check_now() -> dict:
    """
    Run ``brew outdated --json=v2``, filter to SuperBox deps, update cache.
    Returns the new status dict.
    """
    log.info("brew_updater: checking for Homebrew updates …")
    try:
        result = subprocess.run(
            ["brew", "outdated", "--json=v2"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "brew exited with a non-zero status")

        data      = json.loads(result.stdout)
        formulae  = data.get("formulae", [])

        outdated = []
        for pkg in formulae:
            name = pkg.get("name", "")
            # Direct match OR base-name match (e.g. "python@3.14" prefix "python")
            if name in BREW_DEPS or any(
                name == dep or name.startswith(dep.split("@")[0] + "@")
                for dep in BREW_DEPS
            ):
                installed = pkg.get("installed_versions", ["?"])
                current   = pkg.get("current_version", "?")
                outdated.append({
                    "name":      name,
                    "installed": installed[0] if installed else "?",
                    "current":   current,
                })

        _update_cache(outdated=outdated, error=None)
        if outdated:
            names = ", ".join(p["name"] for p in outdated)
            log.info("brew_updater: %d package(s) outdated — %s", len(outdated), names)
        else:
            log.info("brew_updater: all SuperBox Homebrew packages are up to date")

    except FileNotFoundError:
        msg = "brew not found — Homebrew may not be installed"
        log.warning("brew_updater: %s", msg)
        _update_cache(error=msg)

    except Exception as exc:
        log.warning("brew_updater: check failed — %s", exc)
        _update_cache(error=str(exc))

    return get_status()


def get_status() -> dict:
    """Return the last cached status (never blocks)."""
    with _lock:
        return dict(_status)


# ── Internals ─────────────────────────────────────────────────────────────────

def _update_cache(*, outdated: "list | None" = None, error: "str | None" = None) -> None:
    with _lock:
        if outdated is not None:
            _status["outdated"] = outdated
        _status["checked_at"] = datetime.now().isoformat(timespec="seconds")
        _status["error"]      = error


def _background_loop() -> None:
    """Daemon thread: wait for startup delay, then check weekly forever."""
    time.sleep(_STARTUP_DELAY)
    while True:
        check_now()
        time.sleep(_CHECK_INTERVAL)


def start_background_checker() -> None:
    """Start the weekly background checker. Call once at app startup."""
    t = threading.Thread(
        target=_background_loop,
        daemon=True,
        name="brew-updater",
    )
    t.start()
    log.info(
        "brew_updater: background checker started — first check in %ds, then every 7 days",
        _STARTUP_DELAY,
    )
