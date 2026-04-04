<div align="center">
  <br>
  <p><strong>A free, open-source rekordbox library toolkit for DJs with large, mature libraries.</strong></p>
  <br>

  [![Download](https://img.shields.io/github/v/release/fabledharbinger0993/SuperBox?label=Download%20SuperBox&style=for-the-badge&color=6d28d9&logo=apple&logoColor=white)](https://github.com/fabledharbinger0993/SuperBox/releases/latest)

  <br>
  <sub>macOS · Free · No account required · Rekordbox must be closed for write operations</sub>
</div>

---

## Install

1. Click **Download SuperBox** above
2. Unzip — you get **SuperBox.app**
3. Move it to your Desktop or Applications folder
4. Double-click to launch

> **First launch** opens a Terminal window and automatically installs everything needed — Homebrew, `ffmpeg`, `chromaprint`, and all Python packages. This runs once and takes a few minutes. SuperBox opens in your browser when it's done.

---

## What it does

SuperBox fills the gaps Rekordbox leaves open. It reads and writes the Rekordbox database directly and runs entirely on your local machine — no cloud, no account, no subscription.

### Core pipeline — run in order

| | Tool | What it does |
|--|------|-------------|
| ▣ | **Library Audit** | Two-pass audit: cross-checks the Rekordbox database against your drive (broken paths, orphaned entries, untagged tracks) and walks the filesystem for a full physical inventory. Feeds all downstream tools. |
| 🏷 | **Tag Tracks** | Analyzes the actual audio waveform and writes BPM and musical key into the file tags permanently. Tags survive any database rebuild and work in any software. |
| 🔎 | **Find Duplicates** | Acoustic fingerprinting via Chromaprint. Finds the same recording regardless of filename, bitrate, or format — not filename matching, actual audio content comparison. |
| ✂ | **Prune Duplicates** | Loads the duplicate report and removes selected files. Multi-step confirmation with buttons at different screen corners to prevent accidental clicks. Files go to Trash, not permanent deletion. |
| 📍 | **Fix Broken Paths** | When a drive mounts under a new name or files move, bulk-updates every broken path in the database in one operation. |
| ＋ | **Import** | Adds new audio files to the Rekordbox database. Dry-run mode available. Full database backup created before any write. |
| 🔗 | **Link Playlists** | Maps your folder structure to Rekordbox playlist names automatically after import. |

### Optional tools

| | Tool | What it does |
|--|------|-------------|
| 📢 | **Normalize** | Measures integrated loudness (EBU R128) and re-encodes tracks outside your target. Originals preserved as `.bak` until verified. |
| 🔄 | **Convert Format** | Re-encodes a folder of audio files to a target format before importing. |
| 🗂 | **Organize** | De-fragments your library into `Artist / Album / Track` folder structure using embedded tags. |
| ★ | **Novelty Scanner** | Scans a second drive for tracks not acoustically present in your main library — rescues files from old or backup drives. |

### Pipeline Builder

Chain any combination of tools into one automated run. Choose **auto mode** (runs straight through, each step feeding the next) or **confirm between steps** — pauses after each step with a context-aware gate:

- **↻ Re-do** — replay the same step again
- **✓ Finish** — stop here, call it done
- **⏭ Skip** — skip this step's result and continue to the next
- **⏹ Stop** — abort the pipeline immediately

### Quality of life

- **Drag and drop** — drop any file or folder from Finder onto any path input
- **Session pills** — completed operations tracked for the session, click to re-open output
- **Glossary** — built-in glossary of every technical term used in SuperBox

---

## Requirements

- macOS Monterey 12.0 or later (Apple Silicon or Intel)
- Internet connection on first launch only
- Rekordbox must be **closed** before any write operation

---

## Under the hood

| Library | Purpose |
|---------|---------|
| [librosa](https://librosa.org) | BPM detection (beat tracking) and key detection (Krumhansl-Schmuckler on chroma features) |
| [Chromaprint / fpcalc](https://acoustid.org/chromaprint) | Acoustic fingerprinting for duplicate detection |
| [pyrekordbox](https://github.com/dylanljones/pyrekordbox) | Direct read/write access to the Rekordbox SQLite database |
| [mutagen](https://mutagen.readthedocs.io) | Audio file tag reading and writing (ID3, Vorbis, etc.) |
| [pyloudnorm](https://github.com/csteinmetz1/pyloudnorm) | EBU R128 loudness measurement |
| [Flask](https://flask.palletsprojects.com) + [Waitress](https://docs.pylonsproject.org/projects/waitress) | Local web server — everything runs on localhost, no internet at runtime |

---

## Built by

**Guthrie Entertainment LLC** · Free and open source · [github.com/fabledharbinger0993](https://github.com/fabledharbinger0993)
