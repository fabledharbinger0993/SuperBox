# FableGear

**A free, open-source rekordbox library toolkit for DJs.**

[![Download](https://img.shields.io/github/v/release/fabledharbinger0993/FableGear?label=Download%20FableGear&style=for-the-badge&color=6d28d9&logo=apple&logoColor=white)](https://github.com/fabledharbinger0993/FableGear/releases/latest/download/FableGear.zip)

macOS · Free · No account required · Local-first — no cloud, no subscription

---

## What is FableGear?

FableGear is a local library management suite that runs alongside Rekordbox. It reads and writes the Rekordbox SQLite database directly via [pyrekordbox](https://github.com/dylanljones/pyrekordbox) and runs entirely on your Mac — nothing leaves your machine.

It was built for DJs with large, mature libraries where Rekordbox's own tools run out of road: broken paths after drive remounts, duplicates that slipped through filename-based checks, untagged tracks, libraries spread across multiple drives, and thousands of files that need BPM and key analysis done properly.

FableGear opens as a native window (no browser required) and keeps Rekordbox as the source of truth. Every write operation requires explicit confirmation and Rekordbox must be closed first.

---

## Install

### Option 1 — One command (recommended)

Open Terminal (`⌘ Space` → type `Terminal`) and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/fabledharbinger0993/FableGear/main/install.sh | bash
```

Clones the repo, installs all dependencies, and starts FableGear. The first run opens a setup window — it only runs once. After that, FableGear starts silently and offers to add itself to your Dock as a native app.

### Option 2 — Manual download

1. Click **Download FableGear** above → unzip → double-click **FableGear.app**
2. macOS may block it on first open: right-click → **Open** → **Open Anyway**
3. A setup window runs once, then FableGear launches as a native window

> **Dock icon:** On first launch FableGear will ask if you want a native Dock icon — it compiles one locally on your Mac, which avoids Gatekeeper friction entirely. To remove it later, open **FableGear Uninstall** from `~/Applications/`.

### Requirements

- macOS Monterey 12.0 or later (Apple Silicon or Intel)
- Internet connection on first launch only (to clone the repo and install dependencies)
- Rekordbox must be **closed** for any write operation

---

## Tools

FableGear is organised into two sections: **Rekordbox DB** tools that read or write the database, and **Physical Library** tools that operate on your files directly.

### Rekordbox DB

| Tool | What it does |
|---|---|
| **Library Audit** | Two-pass audit: cross-checks the Rekordbox database against your drives (broken paths, orphaned entries, missing files, untagged tracks) and walks the filesystem for a full physical inventory. Output feeds all downstream tools. |
| **Import** | Adds new audio files to the Rekordbox database. Dry-run mode shows what would happen before writing. Full database backup is created automatically before any write. |
| **Fix Broken Paths** | When a drive remounts under a different name or files move, this bulk-updates every affected path in the database in one operation. |
| **Link Playlists** | Maps your folder structure to Rekordbox playlist names automatically — useful after a bulk import or reorganise. |

### Physical Library

| Tool | What it does |
|---|---|
| **Tag Tracks** | Analyses the actual audio waveform (not the filename) and writes BPM and musical key into the file tags. Tags survive any database rebuild and work in any software. Uses librosa beat tracking and Krumhansl-Schmuckler chroma analysis. |
| **Find & Prune Duplicates** | Acoustic fingerprinting via Chromaprint — finds the same recording regardless of filename, bitrate, or format. Multi-step pruning UI with confirm buttons at different screen corners to prevent muscle-memory clicking through a destructive operation. Files go to Trash, not permanent deletion. |
| **Rename Files** | Pattern-based batch renamer with a learn-from-examples mode — show it a few before/after pairs and it infers the rule. Preflight preview before any rename runs. |
| **Organize Library** | De-fragments your library into `Artist / Album / Track` folder structure using embedded tags. Uses `TPE2` (album artist) over `TPE1` to prevent `Artist feat. Guest` folder explosion. Camelot key prefixes are stripped from artist tags before folder creation. |
| **Normalize Loudness** | Measures integrated loudness (EBU R128) and re-encodes tracks outside your target. Preview mode lets you hear the result first. Originals preserved as `.bak` until verified. |
| **Convert Format** | Re-encodes a folder of audio files to a target format before importing. |
| **Novelty Scanner** | Scans a second drive for tracks not acoustically present in your main library — rescues files from old drives, backups, or USB sticks. |

### Pipeline Builder

Chain any combination of tools into one automated run. Choose **auto mode** (runs straight through, each step feeding the next) or **confirm between steps** — pauses at each gate with four choices:

- **↻ Re-do** — replay the same step
- **✓ Finish** — stop here
- **⏭ Skip** — skip this result and continue
- **⏹ Stop** — abort immediately

---

## Library Health Monitor

FableGear runs a proactive hazard scanner at startup and on demand. It checks for:

- Rekordbox running while a write is attempted
- iCloud / Dropbox sync active on your library folder (can corrupt the database mid-write)
- Database size regression (unexplained shrink — indicator of a bad write or accidental deletion)
- Read-only volume mounts
- Database backup pointing to the same physical volume as the database itself
- Low free space on library drives
- Database symlink instead of real file

Findings are surfaced as a health banner with severity levels and one-click auto-fix where the fix is safe to automate.

---

## Library View + Built-in Player

FableGear includes a split-panel library view backed directly by the Rekordbox database:

- Browse all tracks with BPM, key, duration, and file path
- Browse your filesystem alongside your database tracks in a split view
- Hotplug detection — connected drives appear and disappear without restarting
- Playlist management — create, rename, delete, add and remove tracks, reorder
- Audio playback with waveform display via WaveSurfer
- Export playlists to Pioneer USB drives in the correct PIONEER directory format
- Canonical path integrity checker — compares stored database paths to what's actually on disk and generates a correction plan

---

## FableGo — iOS Companion App

FableGo is an iOS companion app (`ios/FableGo/`) that connects to FableGear over your local network or via Tailscale for remote access.

**What FableGo can do:**
- Browse your music folders remotely
- Trigger server-side downloads with real-time progress over WebSocket
- Browse, create, edit, and delete Rekordbox playlists
- Add and remove tracks from playlists
- Trigger BPM/key analysis jobs remotely
- Browse connected drives and export playlists to Pioneer USB

FableGo uses Bearer token auth (`mobile_token` in `~/.rekordbox-toolkit/config.json`). The FableGear server must be running — Tailscale is optional but enables remote access outside your home network.

FableGo lives in this repo alongside FableGear because it depends directly on FableGear's `/api/mobile/*` API surface — versioning them together keeps the API contract safe.

---

## Under the hood

| Library | Purpose |
|---|---|
| [pyrekordbox](https://github.com/dylanljones/pyrekordbox) | Direct read/write access to the Rekordbox SQLite database (`master.db`) |
| [librosa](https://librosa.org) | BPM detection (beat tracking) and key detection (Krumhansl-Schmuckler on chroma features) |
| [Chromaprint / fpcalc](https://acoustid.org/chromaprint) | Acoustic fingerprinting for duplicate detection |
| [mutagen](https://mutagen.readthedocs.io) | Audio file tag reading and writing (ID3, Vorbis, MP4, etc.) |
| [pyloudnorm](https://github.com/csteinmetz1/pyloudnorm) | EBU R128 integrated loudness measurement |
| [Flask](https://flask.palletsprojects.com) + [Waitress](https://docs.pylonsproject.org/projects/waitress) | Local web server — runs on `localhost:5001`, nothing on the network by default |
| [pywebview](https://pywebview.flowrl.com) | Wraps the UI in a native macOS `WKWebView` window — no browser tab required |
| [flask-sock](https://flask-sock.readthedocs.io) | WebSocket support for real-time progress streaming and FableGo events |

---

## Releasing

```bash
# Tag and publish a release
./scripts/release.sh v2.x.x

# Or with custom release notes
./scripts/release.sh v2.x.x .github/release-notes.md
```

GitHub Actions attaches `FableGear.zip` and `install.sh` to every published release automatically. The release script enforces a clean working tree, branch sync, and tag format before creating anything.

---

## Built by

**Guthrie Entertainment LLC** · Free and open source · [github.com/fabledharbinger0993](https://github.com/fabledharbinger0993)
