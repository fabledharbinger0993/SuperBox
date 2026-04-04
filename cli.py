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
    organize    Consolidate files into Artist / Album / Track hierarchy

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


# ─── Report helpers ───────────────────────────────────────────────────────────

def _emit_report(text: str, subdir: str, filename: str) -> None:
    """
    Print a report so the UI can capture it, then save it to disk.

    Protocol:
      SUPERBOX_REPORT_BEGIN — UI starts capturing
      <plain text lines>   — shown in terminal AND in the inline report card
      SUPERBOX_REPORT_END  — UI stops capturing
      SUPERBOX_REPORT_PATH: /path — UI stores the saved file path
    """
    print("SUPERBOX_REPORT_BEGIN", flush=True)
    print(text, flush=True)
    print("SUPERBOX_REPORT_END", flush=True)
    report_path = _write_report(subdir, filename, text)
    if report_path:
        print(f"SUPERBOX_REPORT_PATH: {report_path}", flush=True)


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

    not_found = by_strategy.get("not_found", 0)
    updated   = total - not_found - failed

    lines = [f"Done updating RekordBox paths.", "", f"{updated} of {total} tracks were updated."]
    if by_strategy.get("exact", 0):
        lines.append(f"  {by_strategy['exact']} matched by exact path.")
    if by_strategy.get("hash", 0):
        lines.append(f"  {by_strategy['hash']} matched by file content.")
    if by_strategy.get("fuzzy", 0):
        lines.append(f"  {by_strategy['fuzzy']} matched by filename.")
    if not_found:
        lines += ["", f"{not_found} tracks couldn't be found at the new location.",
                  "  Run Audit to see which ones and decide what to do."]
    if failed:
        lines += ["", f"{failed} tracks had write errors — check the log above."]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _emit_report("\n".join(lines), "Relocate", f"relocate_{timestamp}.txt")


def cmd_duplicates(args: argparse.Namespace) -> None:
    """Scan PATH for acoustically identical files and write a CSV report."""
    from duplicate_detector import scan_duplicates, write_csv_report, write_trash_rescue_report

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

    rescue_output = output.with_name(
        output.stem.replace("duplicate_report", "trash_rescue_report")
        if "duplicate_report" in output.stem
        else f"trash_rescue_{output.stem}"
    ).with_suffix(".txt")

    workers = max(1, args.workers)
    log.info("Scanning for duplicates under: %s (workers=%d)", root, workers)
    log.info("This may take a while for large libraries — progress logged every %d files", 100)

    try:
        result = scan_duplicates(root, max_workers=workers)
    except Exception:
        log.exception("Duplicate scan failed")
        sys.exit(1)

    groups   = result.groups
    removable = sum(len(g.recommended_remove) for g in groups)
    trapped_keeps = sum(1 for g in groups if g.keep_in_trash)

    # ── Trash rescue warning ──────────────────────────────────────────────────
    if result.unique_in_trash or trapped_keeps:
        print()
        print("  ╔══════════════════════════════════════════════════════════════╗")
        print("  ║  !!! RESCUE REQUIRED — DO NOT CLEAR TRASH YET !!!           ║")
        print("  ╠══════════════════════════════════════════════════════════════╣")
        if result.unique_in_trash:
            print(f"  ║  {len(result.unique_in_trash):>5} tracks exist ONLY in a trash folder            ║")
            print(f"  ║        → NOT included in the pruning CSV                    ║")
            print(f"  ║        → SuperBox does not offer an automated rescue step   ║")
            print(f"  ║        → move these files manually before clearing trash    ║")
        if trapped_keeps:
            print(f"  ║  {trapped_keeps:>5} duplicate groups have their best copy in trash   ║")
            print(f"  ║        → marked keep_in_trash=YES in the CSV                ║")
            print(f"  ║        → pruner will NOT delete them, but manual trash      ║")
            print(f"  ║          cleanup would — move them first                    ║")
        print("  ╚══════════════════════════════════════════════════════════════╝")

    if not groups and not result.unique_in_trash:
        _emit_report(
            "No duplicates found. Every file in this folder appears to be unique.",
            "Duplicates", f"duplicates_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        )
    else:
        lines = [
            f"Found {len(groups)} groups of identical tracks — {removable} files could be removed.",
            "Each group contains the same recording in different files.",
            "A report has been saved so you can review each group before deleting anything.",
        ]
        try:
            if groups:
                write_csv_report(result, output)
                lines.append(f"\nReport saved to: {output}")
            write_trash_rescue_report(result, rescue_output)
            lines.append(f"Rescue report:   {rescue_output}")
        except Exception:
            log.exception("Failed to write CSV report")
            sys.exit(1)
        _emit_report("\n".join(lines), "Duplicates",
                     f"duplicates_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        if groups:
            print(f"SUPERBOX_REPORT_PATH: {output}", flush=True)


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
            max_workers=max(1, args.workers),
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

    if normalise and not detect_bpm and not detect_key:
        # Normalize-only mode
        report_lines = [
            f"\nDone.\n",
            f"{normalised} tracks were re-encoded to match the loudness target.",
            f"{total - normalised - errored} were already at the right level and skipped.",
        ]
        if errored:
            report_lines.append(f"{errored} had errors — check the log above.")
    elif normalise:
        # Full process mode
        report_lines = [
            f"\nDone.\n",
            f"{total} files were analyzed.",
            f"  BPM written: {bpm_written} files.{f'  {skipped_bpm} already had one.' if skipped_bpm else ''}",
            f"  Key written: {key_written} files.{f'  {skipped_key} already had one.' if skipped_key else ''}",
            f"  Loudness adjusted: {normalised} files.",
        ]
        if errored:
            report_lines.append(f"\n{errored} files had errors — check the log above.")
    else:
        # Tag-only mode
        report_lines = [
            f"\nDone tagging.\n",
            f"{total} files were analyzed.",
            f"  BPM written: {bpm_written} files.{f'  {skipped_bpm} already had one.' if skipped_bpm else ''}",
            f"  Key written: {key_written} files.{f'  {skipped_key} already had one.' if skipped_key else ''}",
        ]
        if errored:
            report_lines.append(f"\n{errored} files had errors — check the log above.")

    report_text = "\n".join(report_lines)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if normalise:
        _emit_report(report_text, "Normalize", f"normalize_{timestamp}.txt")
    else:
        _emit_report(report_text, "Tag Tracks", f"tag_tracks_{timestamp}.txt")

    if errored > 0:
        log.warning("%d files had errors — check log above", errored)


def cmd_convert(args: argparse.Namespace) -> None:
    """Convert audio files to target format (mp3, wav, aif, flac) recursively."""
    import concurrent.futures
    import json
    from pathlib import Path
    from audio_processor import _convert_file
    from scanner import scan_directory

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

    max_workers = max(1, getattr(args, "workers", 1))
    log.info("Converting audio files to %s in %s (workers=%d)", target_format, root, max_workers)

    tracks = list(scan_directory(root))
    total = len(tracks)

    log.info("Found %d audio files", total)

    if not total:
        log.warning("No audio files found")
        return

    done = 0
    success_count = 0
    error_count = 0

    def _emit_progress() -> None:
        print(
            "SUPERBOX_PROGRESS: " + json.dumps({
                "done":      done,
                "total":     total,
                "remaining": total - done,
                "converted": success_count,
                "errors":    error_count,
            }),
            flush=True,
        )

    def _convert_one(track) -> tuple[bool, str, str]:
        ok, msg = _convert_file(track.path, target_format)
        return ok, msg, track.path.name

    _emit_progress()

    if max_workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_convert_one, track): track for track in tracks}
            for future in concurrent.futures.as_completed(futures):
                try:
                    ok, msg, name = future.result()
                except Exception as exc:
                    ok, msg, name = False, str(exc), futures[future].path.name
                done += 1
                if ok:
                    success_count += 1
                    log.info("✓ %s: %s", name, msg)
                else:
                    error_count += 1
                    log.error("✗ %s: %s", name, msg)
                _emit_progress()
    else:
        for i, track in enumerate(tracks):
            log.info("[%d/%d] Converting %s", i + 1, total, track.path.name)
            ok, msg = _convert_file(track.path, target_format)
            done += 1
            if ok:
                success_count += 1
                log.info("✓ %s: %s", track.path.name, msg)
            else:
                error_count += 1
                log.error("✗ %s: %s", track.path.name, msg)
            _emit_progress()

    fmt_upper = target_format.upper()
    lines = [f"Done converting.", "", f"{success_count} of {total} files were converted to {fmt_upper}."]
    if error_count:
        lines.append(f"{error_count} files had errors — check the log above.")
    else:
        lines.append("No errors.")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _emit_report("\n".join(lines), "Convert", f"convert_{timestamp}.txt")

    if error_count > 0:
        log.warning("%d files had errors — check log above", error_count)


def cmd_organize(args: argparse.Namespace) -> None:
    """Consolidate audio files into Artist / Album / Track hierarchy."""
    import json as _json
    from pathlib import Path
    from library_organizer import organize_library, MIX_FOLDER, ORPHAN_FOLDER

    primary = Path(args.source)
    extra   = [Path(p) for p in (getattr(args, "also_scan", None) or [])]
    sources = [primary] + extra
    target  = Path(args.target)
    mode    = getattr(args, "mode", "assimilate")

    for s in sources:
        if not s.is_dir():
            log.error("SOURCE is not a directory: %s", s)
            sys.exit(1)
    if not target.is_dir():
        try:
            target.mkdir(parents=True, exist_ok=True)
            log.info("Created target directory: %s", target)
        except OSError as e:
            log.error("Cannot create target directory %s: %s", target, e)
            sys.exit(1)

    dry_run     = not args.no_dry_run
    max_workers = max(1, getattr(args, "workers", 1))
    threshold   = float(getattr(args, "mix_threshold", 15)) * 60.0

    if dry_run:
        log.info("DRY RUN — no files will be touched. Pass --no-dry-run to execute.")

    verb = "copy" if mode == "integrate" else "move"
    log.info(
        "Organizing  sources=%s  target=%s  mode=%s  dry_run=%s  workers=%d  mix_threshold=%.0f min",
        [str(s) for s in sources], target, mode, dry_run, max_workers, threshold / 60,
    )

    results = organize_library(
        sources, target,
        mode=mode,
        dry_run=dry_run,
        max_workers=max_workers,
        mix_threshold_sec=threshold,
    )

    moved     = sum(1 for r in results if r.action in ("moved", "dry_run", "conflict_renamed"))
    skipped   = sum(1 for r in results if r.action == "skipped")
    conflicts = sum(1 for r in results if r.action == "conflict_renamed")
    errors    = sum(1 for r in results if r.action == "error")

    src_desc = str(sources[0]) if len(sources) == 1 else f"{len(sources)} source folders"
    action_verb = "copied" if mode == "integrate" else "moved"

    if dry_run:
        dry_verb = "copy" if mode == "integrate" else "move"
        mode_note = (
            "Integration mode — files will be copied to the target; the source drive stays untouched."
            if mode == "integrate" else
            "Assimilation mode — files will be moved and the source will be cleaned up."
        )
        lines = [
            "Here's what would change.",
            "",
            f"{len(results)} files scanned across {src_desc}.",
            f"Mode: {mode_note}",
        ]
        if moved:
            lines.append(f"  {moved} would be {dry_verb}ed into Artist / Album / Track folders.")
        if skipped:
            lines.append(f"  {skipped} are exact copies already at the destination — they'd be skipped.")
        if conflicts:
            lines.append(f"  {conflicts} have a name clash — they'd be renamed (e.g. track_1.mp3).")
        if errors:
            lines.append(f"  {errors} had errors — check the log above.")
        lines += ["", f"Nothing has been {dry_verb}ed. Uncheck \"Dry Run\" and run again to execute."]
    else:
        lines = ["Done organizing.", ""]
        if moved:
            lines.append(f"{moved} files were {action_verb} into Artist / Album / Track folders.")
        if skipped:
            lines.append(f"{skipped} were already at the destination — left alone.")
        if conflicts:
            lines.append(f"{conflicts} name clashes were handled by renaming (e.g. track_1.mp3).")
        if errors:
            lines.append(f"{errors} files had errors — check the log above.")
        else:
            lines.append("No errors.")
        if mode == "integrate":
            lines.append("Source folders were not modified.")
        else:
            lines.append("Empty source folders were cleaned up.")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _emit_report("\n".join(lines), "Organize", f"organize_{timestamp}.txt")

    if dry_run:
        # Emit planned moves so the UI/log shows what would happen
        for r in results:
            if r.action == "dry_run":
                log.info("PLAN  %s  →  %s", r.src.name, r.reason)

    if errors > 0:
        log.warning("%d files had errors — check log above", errors)


def cmd_novelty(args: argparse.Namespace) -> None:
    """Find tracks that only exist on the source and copy them to the destination."""
    from pathlib import Path
    from novelty_scanner import scan_novel

    primary = Path(args.source)
    extra   = [Path(p) for p in (getattr(args, "also_scan", None) or [])]
    sources = [primary] + extra
    dest    = Path(args.dest)
    dry_run = not args.no_dry_run

    for s in sources:
        if not s.is_dir():
            log.error("SOURCE is not a directory: %s", s)
            sys.exit(1)
    if not dest.is_dir():
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error("Cannot create destination %s: %s", dest, e)
            sys.exit(1)

    max_workers = max(1, getattr(args, "workers", 1))

    if dry_run:
        log.info("DRY RUN — no files will be copied. Pass --no-dry-run to execute.")

    log.info(
        "Novel scan  sources=%s  dest=%s  dry_run=%s  workers=%d",
        [str(s) for s in sources], dest, dry_run, max_workers,
    )

    result = scan_novel(
        sources, dest,
        dry_run=dry_run,
        max_workers=max_workers,
    )

    novel   = len(result.novel)
    present = len(result.present)
    errors  = len(result.errors)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    verb = "would be copied" if dry_run else "copied"

    lines = [
        "Novel Track Scan complete.",
        "",
        f"{result.total_src} tracks scanned on source.",
        f"Destination index: {result.dest_index_size} tracks.",
        "",
    ]
    if novel:
        lines.append(f"  {novel} novel tracks {verb} to destination.")
    if present:
        lines.append(f"  {present} tracks confirmed already present — skipped.")
    if errors:
        lines.append(f"  {errors} errors — check log above.")
    if dry_run:
        lines += ["", "Nothing has been copied. Uncheck \"Dry Run\" and run again to execute."]

    _emit_report("\n".join(lines), "Novelty Scan", f"novelty_{timestamp}.txt")

    if errors > 0:
        log.warning("%d files had errors — check log above", errors)


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
    p_process.add_argument(
        "--workers", "-w",
        metavar="N",
        type=int,
        default=1,
        help="Parallel ffmpeg workers for loudness measurement/normalisation (default: 1)",
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
    p_convert.add_argument(
        "--workers", "-w",
        metavar="N",
        type=int,
        default=1,
        help="Parallel ffmpeg workers for conversion (default: 1)",
    )
    p_convert.set_defaults(func=cmd_convert)

    # ── organize ──
    p_organize = sub.add_parser(
        "organize",
        help="Consolidate files into Artist / Album / Track hierarchy",
    )
    p_organize.add_argument(
        "source",
        metavar="SOURCE",
        help="Directory to scan for audio files",
    )
    p_organize.add_argument(
        "target",
        metavar="TARGET",
        help="Root of the organised library (e.g. /Volumes/DJMT/DJMT PRIMARY)",
    )
    p_organize.add_argument(
        "--no-dry-run",
        action="store_true",
        default=False,
        help="Actually move files. Default behaviour is dry-run (preview only).",
    )
    p_organize.add_argument(
        "--workers", "-w",
        metavar="N",
        type=int,
        default=1,
        help="Parallel I/O workers for the move phase (default: 1)",
    )
    p_organize.add_argument(
        "--mix-threshold",
        metavar="MINUTES",
        type=float,
        default=15.0,
        help="Tracks at or above this duration (minutes) go to Live Sets & Mixes (default: 15)",
    )
    p_organize.add_argument(
        "--also-scan",
        metavar="PATH",
        action="append",
        default=[],
        dest="also_scan",
        help="Additional source directory to scan (can be repeated for multiple sources)",
    )
    p_organize.add_argument(
        "--mode",
        choices=["assimilate", "integrate"],
        default="assimilate",
        help=(
            "assimilate: move files, remove source duplicates, prune empty dirs (default). "
            "integrate: copy files to target only — source drive is never modified."
        ),
    )
    p_organize.set_defaults(func=cmd_organize)

    # ── novelty ───────────────────────────────────────────────────────────────
    p_novelty = sub.add_parser(
        "novelty",
        help="Find and copy tracks that exist only on the source (not in destination)",
    )
    p_novelty.add_argument(
        "source",
        metavar="SOURCE",
        help="Drive or directory to scan for novel tracks",
    )
    p_novelty.add_argument(
        "dest",
        metavar="DEST",
        help="Home library root to copy novel tracks into",
    )
    p_novelty.add_argument(
        "--no-dry-run",
        action="store_true",
        default=False,
        help="Actually copy files. Default is dry-run (preview only).",
    )
    p_novelty.add_argument(
        "--workers", "-w",
        metavar="N",
        type=int,
        default=1,
        help="Parallel workers (default: 1)",
    )
    p_novelty.add_argument(
        "--also-scan",
        metavar="PATH",
        action="append",
        default=[],
        dest="also_scan",
        help="Additional source directory (can be repeated)",
    )
    p_novelty.set_defaults(func=cmd_novelty)

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
