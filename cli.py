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
mutagen, librosa, etc.
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

try:
    from SuperBox.config import DJMT_DB, MUSIC_ROOT   # when run as a package
except ImportError:
    from config import DJMT_DB, MUSIC_ROOT             # when run as a script

log = logging.getLogger(__name__)


# ─── Report file helper ───────────────────────────────────────────────────────

def _write_report(subdir: str, filename: str, text: str) -> str | None:
    """
    Write a report text file to REPORTS_DIR/subdir/filename.
    Returns the written path as a string, or None if REPORTS_DIR is unavailable
    (drive not mounted, archive disabled, etc.).
    Failures are logged as warnings — they never abort the command.
    """
    try:
        try:
            from SuperBox.config import REPORTS_DIR  # noqa: PLC0415
        except ImportError:
            from config import REPORTS_DIR           # noqa: PLC0415

        out_dir = REPORTS_DIR / subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / filename
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            from icon_utils import set_file_icon    # noqa: PLC0415
            set_file_icon(out_path)
        except Exception:
            pass
        return str(out_path)
    except Exception as exc:
        log.warning("Could not write report to %s/%s: %s", subdir, filename, exc)
        return None


# ─── Logging setup ────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # pyrekordbox has its own internal handler that also prints warnings —
    # suppress it to ERROR so playlist-not-found noise doesn't appear twice.
    logging.getLogger("pyrekordbox").setLevel(logging.ERROR)


# ─── Command handlers ─────────────────────────────────────────────────────────

def cmd_audit(args: argparse.Namespace) -> None:
    """Run a full read-only audit and print the summary."""
    from audit import full_audit
    from db_connection import read_db

    root = Path(args.root) if args.root else MUSIC_ROOT

    log.info("Opening database (read-only): %s", DJMT_DB)
    try:
        with read_db(DJMT_DB) as db:
            report = full_audit(db, root=root)
        summary_text = report.summary()
        print(summary_text)
        # Write report to REPORTS_DIR/Audit/
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = _write_report("Audit", f"audit_{timestamp}.txt", summary_text)
        if report_path:
            print(f"SUPERBOX_REPORT_PATH: {report_path}", flush=True)
    except Exception:
        log.exception("Audit failed")
        sys.exit(1)


def cmd_import(args: argparse.Namespace) -> None:
    """Import audio files under PATH into the database."""
    from importer import import_directory
    from db_connection import read_db, write_db

    root = Path(args.path)
    if not root.is_dir():
        log.error("PATH is not a directory: %s", root)
        sys.exit(1)

    if args.dry_run:
        log.info("DRY RUN — no writes will occur")
        try:
            with read_db(DJMT_DB) as db:
                report = import_directory(root, db, dry_run=True)
            summary_text = report.summary()
            print(summary_text)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = _write_report("Import", f"preview_import_{timestamp}.txt", summary_text)
            if report_path:
                print(f"SUPERBOX_REPORT_PATH: {report_path}", flush=True)
        except Exception:
            log.exception("Dry-run import failed")
            sys.exit(1)
    else:
        log.info("Importing from: %s", root)
        try:
            with write_db(DJMT_DB) as db:
                report = import_directory(root, db, dry_run=False)
            summary_text = report.summary()
            print(summary_text)
            if report.failed > 0:
                log.warning("%d tracks failed to import — see log above", report.failed)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = _write_report("Import", f"import_{timestamp}.txt", summary_text)
            if report_path:
                print(f"SUPERBOX_REPORT_PATH: {report_path}", flush=True)
        except Exception:
            log.exception("Import failed")
            sys.exit(1)


def cmd_link(args: argparse.Namespace) -> None:
    """Link imported tracks under PATH to existing playlists."""
    from playlist_linker import link_directory
    from db_connection import write_db

    root = Path(args.path)
    if not root.is_dir():
        log.error("PATH is not a directory: %s", root)
        sys.exit(1)

    log.info("Linking tracks under: %s", root)
    try:
        with write_db(DJMT_DB) as db:
            report = link_directory(root, db)
        print(report.summary())
    except Exception:
        log.exception("Playlist linking failed")
        sys.exit(1)


def cmd_relocate(args: argparse.Namespace) -> None:
    """Batch-update FolderPath for files moved from OLD_ROOT to NEW_ROOT."""
    from relocator import relocate_directory
    from db_connection import write_db

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

    if args.output:
        output = Path(args.output)
    else:
        # Default: write into REPORTS_DIR/Duplicates/ if archive is configured,
        # otherwise fall back to ~/rekordbox-toolkit/
        try:
            try:
                from SuperBox.config import REPORTS_DIR  # noqa: PLC0415
            except ImportError:
                from config import REPORTS_DIR           # noqa: PLC0415
            out_dir = REPORTS_DIR / "Duplicates"
            out_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output = out_dir / f"duplicate_report_{timestamp}.csv"
        except Exception:
            output = Path.home() / "rekordbox-toolkit" / "duplicate_report.csv"

    workers = max(1, args.workers)
    log.info("Scanning for duplicates under: %s (workers=%d)", root, workers)
    log.info("This may take a while for large libraries — progress logged every %d files", 100)

    try:
        groups = scan_duplicates(root, max_workers=workers)
    except Exception:
        log.exception("Duplicate scan failed")
        sys.exit(1)

    print(f"\n  Duplicate groups found : {len(groups)}")
    print(f"  Files to review        : {sum(len(g.recommended_remove) for g in groups)}")

    if groups:
        try:
            write_csv_report(groups, output)
            print(f"  Report written to      : {output}")
            print(f"SUPERBOX_REPORT_PATH: {output}", flush=True)
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

    log.info(
        "Processing %s — BPM:%s KEY:%s NORMALIZE:%s FORCE:%s DRY_RUN:%s",
        root,
        detect_bpm, detect_key, normalise, args.force, args.dry_run,
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

    report_lines = [
        "═══ PROCESS REPORT ═══",
        f"  Files processed : {total}",
        f"  BPM written     : {bpm_written}  (skipped existing: {skipped_bpm})",
        f"  Key written     : {key_written}  (skipped existing: {skipped_key})",
        f"  Normalised      : {normalised}",
        f"  Errors          : {errored}",
        "══════════════════════",
    ]
    report_text = "\n".join(report_lines)
    print(report_text)
    # Write report to REPORTS_DIR/Tag Tracks/
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = _write_report("Tag Tracks", f"tag_tracks_{timestamp}.txt", report_text)
    if report_path:
        print(f"SUPERBOX_REPORT_PATH: {report_path}", flush=True)

    if errored > 0:
        log.warning("%d files had errors — check log above", errored)


def cmd_convert(args: argparse.Namespace) -> None:
    """Convert audio files to target format (mp3, wav, aif, flac) recursively."""
    from pathlib import Path
    from audio_processor import _convert_file

    root = Path(args.path)
    if not root.is_dir():
        log.error("PATH is not a directory: %s", root)
        sys.exit(1)

    target_format = args.format.lower().lstrip(".")
    if target_format not in ("mp3", "wav", "aif", "aiff", "flac"):
        log.error("Unsupported format: %s", args.format)
        sys.exit(1)

    # Normalize aif → aiff
    if target_format == "aif":
        target_format = "aiff"

    log.info("Converting audio files to %s in %s", target_format, root)

    # Find all audio files
    extensions = {".mp3", ".wav", ".aiff", ".aif", ".flac", ".m4a", ".ogg", ".opus"}
    files = [f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in extensions]

    log.info("Found %d audio files", len(files))

    if not files:
        log.warning("No audio files found")
        return

    success_count = 0
    error_count = 0

    for i, fpath in enumerate(files, 1):
        log.info("[%d/%d] Converting %s", i, len(files), fpath.name)
        success, msg = _convert_file(fpath, target_format)
        if success:
            success_count += 1
            log.info("✓ %s: %s", fpath.name, msg)
        else:
            error_count += 1
            log.error("✗ %s: %s", fpath.name, msg)

    print("═══ CONVERT REPORT ═══")
    print(f"  Files processed : {len(files)}")
    print(f"  Converted       : {success_count}")
    print(f"  Errors          : {error_count}")
    print("══════════════════════")

    if error_count > 0:
        log.warning("%d files had errors — check log above", error_count)


# ─── Argument parser ──────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rbtk",
        description="Rekordbox Toolkit — library management for serious DJ libraries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 cli.py audit
  python3 cli.py import "/Volumes/DJMT/DJMT PRIMARY" --dry-run
  python3 cli.py import "/Volumes/DJMT/DJMT PRIMARY"
  python3 cli.py link "/Volumes/DJMT/DJMT PRIMARY"
  python3 cli.py relocate /old/path /new/path
  python3 cli.py duplicates "/Volumes/DJMT/DJMT PRIMARY" --output ~/Desktop/dupes.csv
  python3 cli.py process "/Volumes/DJMT/DJMT PRIMARY" --no-normalize
  python3 cli.py process "/Volumes/DJMT/DJMT PRIMARY" --dry-run --no-bpm --no-key
  python3 cli.py convert "/Volumes/DJMT/DJMT PRIMARY" mp3
  python3 cli.py convert "/Volumes/DJMT/DJMT PRIMARY" flac
        """,
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # ── audit ──
    p_audit = sub.add_parser("audit", help="Read-only library health check")
    p_audit.add_argument(
        "--root",
        metavar="PATH",
        help=f"Music root for orphan scan (default: {MUSIC_ROOT})",
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
    p_import.set_defaults(func=cmd_import)

    # ── link ──
    p_link = sub.add_parser("link", help="Link imported tracks to existing playlists")
    p_link.add_argument("path", metavar="PATH", help="Directory whose tracks to link")
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
        "--workers", "-w",
        metavar="N",
        type=int,
        default=1,
        help="Number of parallel fpcalc workers (default: 1)",
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
    p_process.set_defaults(func=cmd_process)

    # ── convert ──
    p_convert = sub.add_parser(
        "convert",
        help="Convert audio files to target format (mp3, wav, aif, flac)",
    )
    p_convert.add_argument("path", metavar="PATH", help="Directory to convert")
    p_convert.add_argument(
        "format",
        metavar="FORMAT",
        help="Target format: mp3, wav, aif, or flac",
    )
    p_convert.set_defaults(func=cmd_convert)

    return parser


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    _setup_logging(args.verbose)

    log.debug("Command: %s", args.command)
    log.debug("Args: %s", vars(args))

    args.func(args)


if __name__ == "__main__":
    main()
