"""
rekordbox-toolkit / cli.py

Single entry point for all toolkit operations.
Run with: python3 cli.py <command> [options]

Commands:
    audit       Read-only library health check
    import      Import audio files into the database
    link        Link imported tracks to existing playlists
    relocate    Batch-update paths for moved/renamed files
    duplicates  Find acoustically identical files via Chromaprint
    process     Detect BPM/key and normalise loudness on audio files

All write commands enforce:
  - Rekordbox not running (via write_db())
  - Timestamped backup created before any write (via write_db())
  - sys.exit(1) on unrecoverable error

All module imports are deferred inside command handlers. This means
`python3 cli.py --help` runs instantly without loading pyrekordbox,
mutagen, librosa, aubio, etc.
"""

import argparse
import logging
import sys
from pathlib import Path

# NOTE: config.py is NOT imported at the top level — it requires the user
# config file to exist, and the `setup` command must work before that file
# is created. All config imports are deferred inside command handlers.

log = logging.getLogger(__name__)


# ─── Logging setup ────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


# ─── Command handlers ─────────────────────────────────────────────────────────

def cmd_audit(args: argparse.Namespace) -> None:
    """Run a full read-only audit and print the summary."""
    from audit import full_audit
    from config import DJMT_DB, MUSIC_ROOT
    from db_connection import read_db

    root = Path(args.root) if args.root else MUSIC_ROOT

    log.info("Opening database (read-only): %s", DJMT_DB)
    try:
        with read_db(DJMT_DB) as db:
            report = full_audit(db, root=root)
        print(report.summary())
    except Exception:
        log.exception("Audit failed")
        sys.exit(1)


def cmd_import(args: argparse.Namespace) -> None:
    """Import audio files under PATH into the database."""
    from config import DJMT_DB
    from db_connection import read_db, write_db
    from importer import import_directory

    root = Path(args.path)
    if not root.is_dir():
        log.error("PATH is not a directory: %s", root)
        sys.exit(1)

    resume: bool = getattr(args, "resume", False)

    if args.dry_run:
        log.info("DRY RUN — no writes will occur")
        try:
            with read_db(DJMT_DB) as db:
                report = import_directory(root, db, dry_run=True)
            print(report.summary())
        except Exception:
            log.exception("Dry-run import failed")
            sys.exit(1)
    else:
        if resume:
            log.info("Resume mode enabled — previously committed files will be skipped")
        log.info("Importing from: %s", root)
        try:
            with write_db(DJMT_DB) as db:
                report = import_directory(root, db, dry_run=False, resume=resume)
            print(report.summary())
            if report.failed > 0:
                log.warning("%d tracks failed to import — see log above", report.failed)
        except Exception:
            log.exception("Import failed")
            sys.exit(1)


def cmd_link(args: argparse.Namespace) -> None:
    """Link imported tracks under PATH to existing playlists."""
    from config import DJMT_DB
    from db_connection import read_db, write_db
    from playlist_linker import link_directory

    root = Path(args.path)
    if not root.is_dir():
        log.error("PATH is not a directory: %s", root)
        sys.exit(1)

    dry_run: bool = getattr(args, "dry_run", False)
    audit_log = Path(args.audit_log) if getattr(args, "audit_log", None) else None

    if dry_run:
        log.info("DRY RUN — no playlist rows will be written")

    log.info("Linking tracks under: %s", root)
    try:
        ctx = read_db(DJMT_DB) if dry_run else write_db(DJMT_DB)
        with ctx as db:
            report = link_directory(root, db, dry_run=dry_run)
    except Exception:
        log.exception("Playlist linking failed")
        sys.exit(1)

    print(report.summary())

    # Write fuzzy audit log if requested, or automatically when fuzzy matches exist
    fuzzy = report.all_fuzzy_matches
    if fuzzy:
        if audit_log is None:
            audit_log = Path.home() / "rekordbox-toolkit" / "link_fuzzy_audit.csv"
        n = report.write_fuzzy_audit_log(audit_log)
        print(f"\n  ⚠  {n} fuzzy match(es) written to: {audit_log}")
        print(     "     Review this file before running without --dry-run.")
    elif audit_log is not None:
        print(f"\n  No fuzzy matches — audit log not written.")

    if report.failed > 0:
        log.warning("%d tracks had errors during linking — see log above", report.failed)


def cmd_relocate(args: argparse.Namespace) -> None:
    """Batch-update FolderPath for files moved from OLD_ROOT to NEW_ROOT."""
    from config import DJMT_DB
    from db_connection import write_db
    from relocator import relocate_directory

    old_root = Path(args.old_root)
    new_root = Path(args.new_root)

    if not new_root.is_dir():
        log.error("NEW_ROOT is not a directory: %s", new_root)
        sys.exit(1)

    # old_root doesn't need to exist on disk — it's a string prefix matched
    # against FolderPath values in the DB. If it's a typo, relocate_directory
    # will match zero rows and log a warning.
    log.info("Relocating: %s → %s", old_root, new_root)
    try:
        with write_db(DJMT_DB) as db:
            results = relocate_directory(old_root, new_root, db)
    except Exception:
        log.exception("Relocation failed")
        sys.exit(1)

    total = len(results)
    by_strategy: dict[str, int] = {}
    failed = 0
    for r in results:
        by_strategy[r.strategy] = by_strategy.get(r.strategy, 0) + 1
        if not r.success:
            failed += 1

    print("═══ RELOCATION REPORT ═══")
    print(f"  Total processed : {total}")
    print(f"  Exact matches   : {by_strategy.get('exact', 0)}")
    print(f"  Hash matches    : {by_strategy.get('hash', 0)}")
    print(f"  Fuzzy matches   : {by_strategy.get('fuzzy', 0)}")
    print(f"  Not found       : {by_strategy.get('not_found', 0)}")
    print(f"  Write failures  : {failed}")
    print("═════════════════════════")

    if by_strategy.get("not_found", 0) > 0:
        log.warning(
            "%d tracks could not be relocated — run audit to review",
            by_strategy["not_found"],
        )


def cmd_duplicates(args: argparse.Namespace) -> None:
    """Scan PATH for acoustically identical files and write a CSV report."""
    from duplicate_detector import scan_duplicates, write_csv_report

    root = Path(args.path)
    if not root.is_dir():
        log.error("PATH is not a directory: %s", root)
        sys.exit(1)

    output = (
        Path(args.output)
        if args.output
        else Path.home() / "rekordbox-toolkit" / "duplicate_report.csv"
    )

    workers = max(1, args.workers)
    pause = max(0.0, args.pause)

    log.info(
        "Scanning for duplicates under: %s  (workers=%d pause=%.1fs)",
        root, workers, pause,
    )
    log.info("Progress logged every 100 files — this may take 10–30 min for large libraries")

    try:
        groups = scan_duplicates(root, max_workers=workers, pause_seconds=pause)
    except Exception:
        log.exception("Duplicate scan failed")
        sys.exit(1)

    print(f"\n  Duplicate groups found : {len(groups)}")
    print(f"  Files to review        : {sum(len(g.recommended_remove) for g in groups)}")

    if groups:
        try:
            write_csv_report(groups, output)
            print(f"  Report written to      : {output}")
        except Exception:
            log.exception("Failed to write CSV report")
            sys.exit(1)
    else:
        print("  No duplicates found.")


def cmd_process(args: argparse.Namespace) -> None:
    """
    Detect BPM/key and normalise loudness for audio files under PATH.

    Dry-run behavior:
      --dry-run suppresses loudness normalisation (audio file modification).
      BPM and key detection still run and tag values are still written.
      To skip tag writes as well, combine: --no-bpm --no-key --no-normalize.
    """
    from audio_processor import process_directory

    root = Path(args.path)
    if not root.is_dir():
        log.error("PATH is not a directory: %s", root)
        sys.exit(1)

    detect_bpm = not args.no_bpm
    detect_key = not args.no_key
    normalise = not args.no_normalize and not args.dry_run
    workers = max(1, args.workers)
    pause = max(0.0, args.pause)

    log.info(
        "Processing %s — BPM:%s KEY:%s NORMALIZE:%s FORCE:%s DRY_RUN:%s "
        "workers=%d pause=%.1fs",
        root,
        detect_bpm, detect_key, normalise, args.force, args.dry_run,
        workers, pause,
    )

    if args.dry_run:
        log.info(
            "DRY RUN — loudness normalisation suppressed. "
            "BPM/key tag writes will still occur unless --no-bpm / --no-key are set."
        )

    if normalise:
        log.warning(
            "Normalisation will modify audio files in-place. "
            "Originals are backed up as .bak during the operation only. "
            "Ensure your files are backed up independently before proceeding."
        )

    try:
        results = process_directory(
            root,
            detect_bpm=detect_bpm,
            detect_key=detect_key,
            normalise=normalise,
            force=args.force,
            max_workers=workers,
            pause_seconds=pause,
        )
    except Exception:
        log.exception("Processing failed")
        sys.exit(1)

    total = len(results)
    bpm_written = sum(1 for r in results if r.bpm_written)
    key_written = sum(1 for r in results if r.key_written)
    normalised = sum(1 for r in results if r.normalised)
    errored = sum(1 for r in results if not r.ok)
    skipped_bpm = sum(1 for r in results if r.skipped_bpm)
    skipped_key = sum(1 for r in results if r.skipped_key)

    print("═══ PROCESS REPORT ═══")
    print(f"  Files processed : {total}")
    print(f"  BPM written     : {bpm_written}  (skipped existing: {skipped_bpm})")
    print(f"  Key written     : {key_written}  (skipped existing: {skipped_key})")
    print(f"  Normalised      : {normalised}")
    print(f"  Errors          : {errored}")
    print("══════════════════════")

    if errored > 0:
        log.warning("%d files had errors — check log above", errored)


# ─── Setup / settings ────────────────────────────────────────────────────────

def cmd_setup(args: argparse.Namespace) -> None:
    """
    First-run configuration wizard (or update existing settings with --update).

    This command intentionally does NOT import config.py so that it can run
    before the config file exists.
    """
    from user_config import interactive_setup
    interactive_setup(update=getattr(args, "update", False))


def cmd_check(args: argparse.Namespace) -> None:
    """Run the dependency pre-flight check and print a status report."""
    from user_config import check_dependencies, print_dependency_report
    results = check_dependencies()
    all_ok = print_dependency_report(results)
    if not all_ok:
        sys.exit(1)


def cmd_show_config(args: argparse.Namespace) -> None:
    """Print the current configuration values."""
    from user_config import CONFIG_FILE, NotConfiguredError, load_user_config, KEY_LABELS

    try:
        cfg = load_user_config()
    except NotConfiguredError as e:
        print(str(e))
        sys.exit(1)

    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  rekordbox-toolkit configuration  ({CONFIG_FILE})")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    for key, value in cfg.items():
        label = KEY_LABELS.get(key, key)
        print(f"  {label:<32} {value}")
    print()


# ─── Argument parser ──────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rbtk",
        description="Rekordbox Toolkit — library management for serious DJ libraries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 cli.py setup                              # first-run configuration wizard
  python3 cli.py setup --update                     # change an existing setting
  python3 cli.py config                             # show current configuration
  python3 cli.py check                              # verify all dependencies are installed
  python3 cli.py audit
  python3 cli.py import "/Volumes/DJMT/DJMT PRIMARY" --dry-run
  python3 cli.py import "/Volumes/DJMT/DJMT PRIMARY"
  python3 cli.py import "/Volumes/DJMT/DJMT PRIMARY" --resume
  python3 cli.py link "/Volumes/DJMT/DJMT PRIMARY"
  python3 cli.py relocate /old/path /new/path
  python3 cli.py duplicates "/Volumes/DJMT/DJMT PRIMARY" --output ~/Desktop/dupes.csv
  python3 cli.py duplicates "/Volumes/DJMT/DJMT PRIMARY" --workers 4
  python3 cli.py process "/Volumes/DJMT/DJMT PRIMARY" --no-normalize
  python3 cli.py process "/Volumes/DJMT/DJMT PRIMARY" --dry-run --no-bpm --no-key
  python3 cli.py process "/Volumes/DJMT/DJMT PRIMARY" --workers 4 --pause 0.1
        """,
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # ── setup ──
    p_setup = sub.add_parser(
        "setup",
        help="First-run configuration wizard — run this before anything else",
    )
    p_setup.add_argument(
        "--update",
        action="store_true",
        help="Update existing settings (pre-fills prompts with current values)",
    )
    p_setup.set_defaults(func=cmd_setup)

    # ── config ──
    p_config = sub.add_parser("config", help="Show current configuration")
    p_config.set_defaults(func=cmd_show_config)

    # ── check ──
    p_check = sub.add_parser(
        "check",
        help="Verify all system dependencies are installed (ffmpeg, fpcalc, aubio, etc.)",
    )
    p_check.set_defaults(func=cmd_check)

    # ── audit ──
    p_audit = sub.add_parser("audit", help="Read-only library health check")
    p_audit.add_argument(
        "--root",
        metavar="PATH",
        help="Music root for orphan scan (default: configured music_root)",
    )
    p_audit.set_defaults(func=cmd_audit)

    # ── import ──
    p_import = sub.add_parser("import", help="Import audio files into the database")
    p_import.add_argument("path", metavar="PATH", help="Directory to import")
    p_import.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report without writing to the database",
    )
    p_import.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume an interrupted import. Skips files that were successfully "
            "committed in a previous run by reading "
            "~/.rekordbox-toolkit/import_progress.json. "
            "Failed files from the previous run are always retried. "
            "Progress is cleared automatically on clean completion."
        ),
    )
    p_import.set_defaults(func=cmd_import)

    # ── link ──
    p_link = sub.add_parser("link", help="Link imported tracks to existing playlists")
    p_link.add_argument("path", metavar="PATH", help="Directory whose tracks to link")
    p_link.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show all proposed links and fuzzy matches without writing anything. "
            "Run this first to review fuzzy matches before a live run."
        ),
    )
    p_link.add_argument(
        "--audit-log",
        metavar="FILE",
        help=(
            "Write fuzzy match details to this CSV file "
            "(default: ~/rekordbox-toolkit/link_fuzzy_audit.csv when fuzzy matches exist)"
        ),
    )
    p_link.set_defaults(func=cmd_link)

    # ── relocate ──
    p_relocate = sub.add_parser(
        "relocate",
        help="Batch-update paths for moved/renamed files",
    )
    p_relocate.add_argument(
        "old_root",
        metavar="OLD_ROOT",
        help="Previous path prefix stored in the DB (does not need to exist on disk)",
    )
    p_relocate.add_argument("new_root", metavar="NEW_ROOT", help="New path where files now live")
    p_relocate.set_defaults(func=cmd_relocate)

    # ── duplicates ──
    p_dupes = sub.add_parser(
        "duplicates",
        help="Find acoustically identical files via Chromaprint",
    )
    p_dupes.add_argument("path", metavar="PATH", help="Directory to scan")
    p_dupes.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="CSV output path (default: ~/rekordbox-toolkit/duplicate_report.csv)",
    )
    p_dupes.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Number of parallel fpcalc processes (default: 1). "
            "Values > 1 speed up fingerprinting but increase CPU load — "
            "avoid on machines with limited cores or while DJing."
        ),
    )
    p_dupes.add_argument(
        "--pause",
        type=float,
        default=0.0,
        metavar="SECS",
        help=(
            "Seconds to sleep between files in sequential mode (default: 0.0). "
            "Use 0.1–0.5 on older drives to reduce I/O pressure."
        ),
    )
    p_dupes.set_defaults(func=cmd_duplicates)

    # ── process ──
    p_process = sub.add_parser(
        "process",
        help="Detect BPM/key and normalise loudness",
    )
    p_process.add_argument("path", metavar="PATH", help="Directory to process")
    p_process.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing BPM/key tags",
    )
    p_process.add_argument(
        "--no-bpm",
        action="store_true",
        help="Skip BPM detection and tag writes",
    )
    p_process.add_argument(
        "--no-key",
        action="store_true",
        help="Skip key detection and tag writes",
    )
    p_process.add_argument(
        "--no-normalize",
        action="store_true",
        help="Skip loudness normalisation (default: normalise is ON)",
    )
    p_process.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Suppress loudness normalisation. "
            "BPM/key tag writes still occur unless --no-bpm/--no-key are also set."
        ),
    )
    p_process.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Number of parallel processing threads (default: 1). "
            "Values > 1 speed up BPM/key detection but increase CPU load — "
            "avoid while DJing or on machines with limited cores."
        ),
    )
    p_process.add_argument(
        "--pause",
        type=float,
        default=0.0,
        metavar="SECS",
        help=(
            "Seconds to sleep between files in sequential mode (default: 0.0). "
            "Use 0.1–0.5 on older drives to reduce I/O pressure."
        ),
    )
    p_process.set_defaults(func=cmd_process)

    return parser


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    _setup_logging(args.verbose)

    log.debug("Command: %s", args.command)
    log.debug("Args: %s", vars(args))

    try:
        args.func(args)
    except RuntimeError as exc:
        # config.py re-raises NotConfiguredError as RuntimeError so that
        # the error message is always shown cleanly regardless of import depth.
        msg = str(exc)
        if "not been configured" in msg or "python3 cli.py setup" in msg:
            print(f"\n  ✗  {msg}\n", file=sys.stderr)
        else:
            log.exception("Unexpected error")
        sys.exit(1)


if __name__ == "__main__":
    main()
