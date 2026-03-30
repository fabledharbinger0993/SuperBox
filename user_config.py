"""
rekordbox-toolkit / user_config.py

Manages the user's persistent configuration at ~/.rekordbox-toolkit/config.json.

This module has NO dependencies on other toolkit modules — config.py imports
from here, not the other way around. Keep it that way.

Config file schema
------------------
{
  "local_db":      "/Users/name/Library/Pioneer/rekordbox/master.db",
  "device_db":     "/Volumes/MYDRIVE/PIONEER/Master/master.db",
  "music_root":    "/Volumes/MYDRIVE/MY MUSIC",
  "backup_dir":    "/Users/name/.rekordbox-toolkit/backups",
  "target_lufs":   -8.0,
  "lufs_tolerance": 0.5
}

Required keys: local_db, device_db, music_root, backup_dir
Optional keys: target_lufs, lufs_tolerance (filled from DEFAULTS if absent)

Public interface
----------------
  config_exists() -> bool
  load_user_config() -> dict          raises NotConfiguredError if missing/incomplete
  save_user_config(cfg: dict) -> None
  interactive_setup() -> dict         prompts user, validates, saves, returns cfg
"""

import importlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────

CONFIG_DIR  = Path.home() / ".rekordbox-toolkit"
CONFIG_FILE = CONFIG_DIR / "config.json"

# ─── Schema ───────────────────────────────────────────────────────────────────

# Keys that MUST be present and non-empty
REQUIRED_KEYS = ("local_db", "device_db", "music_root", "backup_dir")

# Optional keys with their default values
DEFAULTS: dict = {
    "target_lufs":    -8.0,
    "lufs_tolerance":  0.5,
}

# Smart defaults for the setup wizard (platform-aware where relevant)
_WIZARD_DEFAULTS: dict = {
    "local_db":   str(Path.home() / "Library/Pioneer/rekordbox/master.db")
                  if platform.system() == "Darwin"
                  else str(Path.home() / "AppData/Roaming/Pioneer/rekordbox/master.db"),
    "backup_dir": str(CONFIG_DIR / "backups"),
}

# Human-readable labels for each key, used in setup prompts and error messages
KEY_LABELS: dict[str, str] = {
    "local_db":      "Rekordbox local database",
    "device_db":     "Device (DJ drive) database",
    "music_root":    "Music root on the DJ drive",
    "backup_dir":    "Backup directory",
    "target_lufs":   "Normalisation target (LUFS)",
    "lufs_tolerance":"Normalisation tolerance (±LUFS)",
}


# ─── Exception ────────────────────────────────────────────────────────────────

class NotConfiguredError(RuntimeError):
    """
    Raised when the config file is missing, unreadable, or incomplete.
    The message is human-readable and always ends with a 'Run: ... setup' hint.
    """


# ─── Core I/O ─────────────────────────────────────────────────────────────────

def config_exists() -> bool:
    """True if a config file is present on disk (may still be incomplete)."""
    return CONFIG_FILE.exists()


def load_user_config() -> dict:
    """
    Load and return the config dict from disk.

    Fills in DEFAULTS for any optional keys that are absent.
    Raises NotConfiguredError if the file is missing, unreadable, or if any
    required key is absent or empty.
    """
    if not CONFIG_FILE.exists():
        raise NotConfiguredError(
            f"rekordbox-toolkit has not been configured yet.\n"
            f"  Config expected at: {CONFIG_FILE}\n"
            f"  Run:  python3 cli.py setup"
        )

    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg: dict = json.load(f)
    except json.JSONDecodeError as e:
        raise NotConfiguredError(
            f"Config file is not valid JSON: {CONFIG_FILE}\n"
            f"  Parse error: {e}\n"
            f"  Run:  python3 cli.py setup  to recreate it."
        ) from e
    except OSError as e:
        raise NotConfiguredError(
            f"Could not read config file: {CONFIG_FILE}\n"
            f"  OS error: {e}\n"
            f"  Run:  python3 cli.py setup  to recreate it."
        ) from e

    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        labels = ", ".join(KEY_LABELS.get(k, k) for k in missing)
        raise NotConfiguredError(
            f"Configuration is incomplete — missing: {labels}\n"
            f"  Config file: {CONFIG_FILE}\n"
            f"  Run:  python3 cli.py setup"
        )

    # Apply defaults for optional keys not present in the file
    for key, default in DEFAULTS.items():
        cfg.setdefault(key, default)

    return cfg


def save_user_config(cfg: dict) -> None:
    """
    Write cfg to the config file as formatted JSON.
    Creates ~/.rekordbox-toolkit/ if it doesn't exist.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")  # POSIX convention: newline at end of file


# ─── Dependency validation ────────────────────────────────────────────────────
#
# Checked at startup by check_dependencies() and surfaced by `python3 cli.py check`.
# Commands that need a missing dep are expected to fail fast with a clear message
# rather than deep-stack traceback.

# Each entry: (display_name, check_fn, install_hint)
# check_fn returns True if the dependency is available.
def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None

def _has_python_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None

_SYS = platform.system()  # "Darwin", "Windows", "Linux"


def _ffmpeg_ok() -> bool:
    """ffmpeg must exist AND be able to decode audio (not just be present)."""
    if not _has_binary("ffmpeg"):
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False

def _fpcalc_ok() -> bool:
    if not _has_binary("fpcalc"):
        return False
    try:
        result = subprocess.run(
            ["fpcalc", "-version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _install_hint(mac: str = "", win: str = "", linux: str = "") -> str:
    """Return the platform-appropriate install hint string (empty = N/A)."""
    if _SYS == "Darwin":
        return mac
    if _SYS == "Windows":
        return win
    return linux


# (display_name, check_fn, system_install_hint, pip_hint, used_by)
# system_install_hint is platform-specific (brew / winget / apt).
# pip_hint is the same on all platforms.
DEPENDENCIES: list[tuple[str, object, str, str, str]] = [
    (
        "ffmpeg",
        _ffmpeg_ok,
        _install_hint(
            mac="brew install ffmpeg",
            win="winget install ffmpeg  (or download from https://ffmpeg.org/download.html)",
            linux="sudo apt install ffmpeg  (or equivalent for your distro)",
        ),
        "",
        "process (loudness normalisation)",
    ),
    (
        "fpcalc  (Chromaprint)",
        _fpcalc_ok,
        _install_hint(
            mac="brew install chromaprint",
            win="download fpcalc from https://acoustid.org/chromaprint",
            linux="sudo apt install libchromaprint-tools",
        ),
        "",
        "duplicates",
    ),
    (
        "pyrekordbox",
        lambda: _has_python_module("pyrekordbox"),
        "",
        "pip install pyrekordbox==0.4.4",
        "all commands",
    ),
    (
        "mutagen",
        lambda: _has_python_module("mutagen"),
        "",
        "pip install mutagen",
        "import, process",
    ),
    (
        "aubio",
        lambda: _has_python_module("aubio"),
        _install_hint(
            mac="brew install aubio",
            win="",   # no winget package; pip wheel available
            linux="sudo apt install python3-aubio  (or pip install aubio)",
        ),
        "pip install aubio",
        "process (BPM detection)",
    ),
    (
        "librosa",
        lambda: _has_python_module("librosa"),
        "",
        "pip install librosa",
        "process (key detection)",
    ),
    (
        "pyloudnorm",
        lambda: _has_python_module("pyloudnorm"),
        "",
        "pip install pyloudnorm",
        "process (loudness measurement)",
    ),
    (
        "soundfile",
        lambda: _has_python_module("soundfile"),
        "",
        "pip install soundfile",
        "process (audio I/O)",
    ),
    (
        "pyacoustid",
        lambda: _has_python_module("acoustid"),
        "",
        "pip install pyacoustid",
        "duplicates",
    ),
]


def check_dependencies() -> list[dict]:
    """
    Check all required dependencies and return a list of result dicts.

    Each dict has keys:
      name      : str   — display name
      ok        : bool  — True if available
      brew      : str   — brew install hint (may be empty)
      pip       : str   — pip install hint (may be empty)
      used_by   : str   — which commands need this dep
    """
    results = []
    for name, check_fn, brew, pip_, used_by in DEPENDENCIES:
        try:
            ok = bool(check_fn())
        except Exception:
            ok = False
        results.append({
            "name":    name,
            "ok":      ok,
            "brew":    brew,
            "pip":     pip_,
            "used_by": used_by,
        })
    return results


def print_dependency_report(results: list[dict] | None = None) -> bool:
    """
    Print a formatted dependency report.
    Returns True if all dependencies are satisfied, False otherwise.

    Parameters
    ----------
    results : list[dict], optional
        Output of check_dependencies(). Computed fresh if not provided.
    """
    if results is None:
        results = check_dependencies()

    all_ok = all(r["ok"] for r in results)
    width = max(len(r["name"]) for r in results) + 2

    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  rekordbox-toolkit — dependency check")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    for r in results:
        status = "✓" if r["ok"] else "✗  NOT FOUND"
        print(f"  {r['name']:{width}} {status}")
        if not r["ok"]:
            if r["brew"]:   # platform-specific system package hint
                print(f"    {'':>{width}} install:  {r['brew']}")
            if r["pip"]:
                print(f"    {'':>{width}} install:  {r['pip']}")
            print(f"    {'':>{width}} used by:  {r['used_by']}")
    print()
    if all_ok:
        print("  All dependencies satisfied.")
    else:
        missing = sum(1 for r in results if not r["ok"])
        print(f"  {missing} missing. Install the above, then re-run: python3 cli.py check")
    print()
    return all_ok


# ─── Setup wizard ─────────────────────────────────────────────────────────────

def _prompt(label: str, default: str | None = None, must_exist: bool = False) -> str:
    """
    Prompt the user for a path string. Repeats until non-empty input is given.
    If must_exist=True, verifies the path exists on disk before accepting.
    """
    hint = f"  [{default}]" if default else ""
    while True:
        try:
            raw = input(f"\n  {label}{hint}\n  → ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled.")
            sys.exit(0)

        value = raw or default or ""

        if not value:
            print("  ✗  Cannot be empty — please enter a path.")
            continue

        if must_exist and not Path(value).exists():
            print(f"  ✗  Path not found: {value}")
            print(     "     Check the path and try again, or press Ctrl-C to cancel.")
            continue

        return value


def interactive_setup(*, update: bool = False) -> dict:
    """
    Run the interactive first-run (or re-run) setup wizard.

    Prompts for each required path, validates existence, then writes
    config.json. Returns the saved config dict.

    Parameters
    ----------
    update : bool
        If True, pre-fill prompts with the existing config values (if any)
        instead of the platform defaults. Useful for the 'settings' command.
    """
    existing: dict = {}
    if update and config_exists():
        try:
            existing = load_user_config()
        except NotConfiguredError:
            existing = {}

    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  rekordbox-toolkit — first-run setup" if not update else
          "  rekordbox-toolkit — update settings")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    print("  Press Enter to accept the value shown in [brackets].")
    print("  Paths must exist on disk before you can continue.")

    cfg: dict = {}

    # ── Required paths ──
    cfg["local_db"] = _prompt(
        "Rekordbox local database path\n  "
        "(usually ~/Library/Pioneer/rekordbox/master.db on Mac)",
        default=existing.get("local_db") or _WIZARD_DEFAULTS.get("local_db"),
        must_exist=True,
    )

    cfg["device_db"] = _prompt(
        "DJ drive database path\n  "
        "(e.g. /Volumes/DRIVENAME/PIONEER/Master/master.db)",
        default=existing.get("device_db"),
        must_exist=True,
    )

    cfg["music_root"] = _prompt(
        "Music root on the DJ drive\n  "
        "(the folder that contains your artist/label folders)",
        default=existing.get("music_root"),
        must_exist=True,
    )

    cfg["backup_dir"] = _prompt(
        "Backup directory\n  "
        "(created automatically — backups are written here before every write)",
        default=existing.get("backup_dir") or _WIZARD_DEFAULTS.get("backup_dir"),
        must_exist=False,  # Will be created on first write — doesn't need to exist yet
    )

    # ── Optional: loudness target ──
    current_lufs = existing.get("target_lufs", DEFAULTS["target_lufs"])
    print(
        f"\n  Normalisation target LUFS  [{current_lufs}]\n"
        "  (−8.0 is the DJ standard for CDJ output; −14.0 is streaming standard)\n"
        "  Press Enter to keep the current value, or type a new one."
    )
    try:
        raw_lufs = input("  → ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        sys.exit(0)

    if raw_lufs:
        try:
            cfg["target_lufs"] = float(raw_lufs)
        except ValueError:
            print(f"  ✗  Invalid number — keeping {current_lufs}")
            cfg["target_lufs"] = current_lufs
    else:
        cfg["target_lufs"] = current_lufs

    cfg["lufs_tolerance"] = existing.get("lufs_tolerance", DEFAULTS["lufs_tolerance"])

    # ── Save ──
    save_user_config(cfg)

    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  Configuration saved to: {CONFIG_FILE}")
    print()
    print(f"  Local DB   : {cfg['local_db']}")
    print(f"  Device DB  : {cfg['device_db']}")
    print(f"  Music root : {cfg['music_root']}")
    print(f"  Backup dir : {cfg['backup_dir']}")
    print(f"  Target LUFS: {cfg['target_lufs']}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    print("  Setup complete.")
    print("  To update these settings later: python3 cli.py setup --update")
    print()

    # Run the dependency check automatically so the user knows immediately
    # if any system tools or Python packages need to be installed.
    print("  Running dependency check...")
    print()
    print_dependency_report()

    return cfg
