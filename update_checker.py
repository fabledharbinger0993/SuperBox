"""
update_checker.py — GitHub release update checker for SuperBox.

Checks whether a newer release exists on GitHub at startup (after a short
delay) and caches the result. The Flask API exposes /api/update/status so
the frontend can show a banner without blocking page load.

Public API
----------
start_background_checker()  — call once at app startup
get_status()                — return the last cached status dict (non-blocking)

Status dict shape:
    {
        "update_available": bool,
        "current_version":  str | None,   # local git tag or commit SHA (short)
        "latest_version":   str | None,   # latest GitHub release tag
        "release_url":      str | None,   # HTML URL of the latest release
        "is_git_install":   bool,         # False if running from a ZIP extract
        "checked_at":       str | None,   # ISO timestamp of last check
        "error":            str | None,
    }
"""

import logging
import subprocess
import threading
import time
import urllib.request
import urllib.error
import json
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_GITHUB_API  = "https://api.github.com/repos/fabledharbinger0993/SuperBox/releases/latest"
_STARTUP_DELAY = 20       # seconds after boot before first check (non-blocking)
_REQUEST_TIMEOUT = 8      # seconds for the GitHub API call

_lock: threading.Lock = threading.Lock()
_status: dict = {
    "update_available": False,
    "current_version":  None,
    "latest_version":   None,
    "release_url":      None,
    "is_git_install":   False,
    "checked_at":       None,
    "error":            None,
}


# ── Core check ────────────────────────────────────────────────────────────────

def check_now() -> dict:
    """
    Hit the GitHub releases API, compare against the local install, update cache.
    Returns the new status dict.
    """
    log.info("update_checker: checking for SuperBox updates …")

    current_version, is_git = _local_version()

    try:
        req  = urllib.request.Request(
            _GITHUB_API,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "SuperBox-update-checker/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())

        latest_tag  = data.get("tag_name", "")
        release_url = data.get("html_url", "")

        update_available = _is_newer(latest_tag, current_version, is_git)

        _update_cache(
            update_available=update_available,
            current_version=current_version,
            latest_version=latest_tag,
            release_url=release_url,
            is_git_install=is_git,
            error=None,
        )

        if update_available:
            log.info(
                "update_checker: update available — local=%s latest=%s",
                current_version, latest_tag,
            )
        else:
            log.info(
                "update_checker: SuperBox is up to date (local=%s latest=%s)",
                current_version, latest_tag,
            )

    except urllib.error.URLError as exc:
        msg = f"Could not reach GitHub ({exc.reason})"
        log.info("update_checker: %s", msg)
        _update_cache(error=msg, is_git_install=is_git, current_version=current_version)

    except Exception as exc:
        log.warning("update_checker: check failed — %s", exc)
        _update_cache(error=str(exc), is_git_install=is_git, current_version=current_version)

    return get_status()


def get_status() -> dict:
    """Return the last cached status (never blocks)."""
    with _lock:
        return dict(_status)


# ── Internals ─────────────────────────────────────────────────────────────────

def _local_version() -> tuple[str | None, bool]:
    """
    Return (version_string, is_git_install).

    For git installs: tries the most recent tag first (e.g. "v1.0.0"), falls
    back to the short commit SHA (e.g. "abc1234") if no tags exist.
    For ZIP installs: returns (None, False).
    """
    script_dir = Path(__file__).parent

    # Is this a git repo at all?
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=script_dir,
            capture_output=True,
            check=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None, False   # ZIP install — no git

    # Try latest tag first
    try:
        tag = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=script_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if tag.returncode == 0 and tag.stdout.strip():
            return tag.stdout.strip(), True
    except Exception:
        pass

    # Fall back to short SHA
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=script_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if sha.returncode == 0:
            return sha.stdout.strip(), True
    except Exception:
        pass

    return None, True


def _is_newer(latest_tag: str, current: str | None, is_git: bool) -> bool:
    """
    Return True if the latest GitHub release is newer than the local install.

    - ZIP installs (not git): always show the update banner so they can download.
    - Git installs with a tag: compare semver-style (v1.2.3 > v1.0.0).
    - Git installs with only a SHA: can't compare; stay silent.
    """
    if not latest_tag:
        return False

    if not is_git:
        # Non-git user — always offer the download
        return True

    if not current:
        return False

    # If current looks like a tag (starts with v or digit), compare version tuples
    if current.startswith("v") or (current and current[0].isdigit()):
        try:
            def _parts(tag: str) -> tuple[int, ...]:
                return tuple(int(x) for x in tag.lstrip("v").split(".") if x.isdigit())
            return _parts(latest_tag) > _parts(current)
        except Exception:
            pass

    # Current is a raw SHA — can't determine order; stay silent
    return False


def _update_cache(**kwargs) -> None:
    with _lock:
        for k, v in kwargs.items():
            if k in _status:
                _status[k] = v
        _status["checked_at"] = datetime.now().isoformat(timespec="seconds")


def _background_loop() -> None:
    time.sleep(_STARTUP_DELAY)
    check_now()


def start_background_checker() -> None:
    """Start the one-shot background check. Call once at app startup."""
    t = threading.Thread(target=_background_loop, daemon=True, name="update-checker")
    t.start()
    log.info("update_checker: will check for updates in %ds", _STARTUP_DELAY)
