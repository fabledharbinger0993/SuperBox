<div align="center">
  <br>
  <p><strong>A free, open-source rekordbox library toolkit for DJs with large, mature libraries.</strong></p>
  <br>

  [![Download](https://img.shields.io/github/v/release/fabledharbinger0993/RekitBox?label=Download%20RekitBox&style=for-the-badge&color=6d28d9&logo=apple&logoColor=white)](https://github.com/fabledharbinger0993/RekitBox/releases/latest)

  <br>
  <sub>macOS · Free · No account required · Rekordbox must be closed for write operations</sub>
</div>

---

## Install

1. Click **Download RekitBox** above
2. Unzip — you get **RekitBox.app**
3. Move it to your Desktop or Applications folder
4. Right-click → **Open** → **Open** (required once — see Gatekeeper note below)
5. A setup window installs any missing dependencies, then RekitBox opens as a native app

> **First launch** opens a Terminal window and automatically installs everything needed — Homebrew, `ffmpeg`, `chromaprint`, and all Python packages. This runs once and takes a few minutes. After that RekitBox opens as a **standalone native window** — no browser required.

> **"RekitBox is damaged" or "cannot be opened"?** This is macOS Gatekeeper — it blocks apps that aren't signed with an Apple Developer certificate. To allow it:
> 1. Right-click `RekitBox.app` → **Open** → **Open Anyway** in the dialog that appears
>
> Or via System Settings:
> 1. Go to **System Settings → Privacy & Security**
> 2. Scroll down — you'll see *"RekitBox was blocked from use"*
> 3. Click **Open Anyway**

---

## What it does

RekitBox fills the gaps Rekordbox leaves open. It reads and writes the Rekordbox database directly and runs entirely on your local machine — no cloud, no account, no subscription.

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

- **Drag and drop** — every path field on every card accepts a folder dropped directly from Finder. A glowing purple zone appears on hover; the path populates instantly.
- **Browse buttons** — every path field also has a Browse… button that opens the native macOS folder picker
- **Library indicator pill** — each tool shows a pinned 📍 pill marking your current rekordbox library root so you always know what you're operating on
- **Session pills** — completed operations tracked for the session, click to re-open output
- **Glossary** — built-in glossary of every technical term used in RekitBox

---

## Requirements

- macOS Monterey 12.0 or later (Apple Silicon or Intel)
- Internet connection on first launch only
- Rekordbox must be **closed** before any write operation

---

## RekitGo + RekitBox repo strategy

RekitGo is intentionally kept in this same repository (`ios/RekitGo`) rather than split into a separate repo.

Why this is the current default:
- RekitGo depends directly on RekitBox's `/api/mobile/*` surface and auth flow
- Backend/mobile changes are easier to ship safely when versioned together
- One repo keeps release coordination and API compatibility checks in one place

If RekitGo eventually needs an independent release cadence, separate contributor model, or public SDK-style API contract, splitting it into its own repo would make sense.

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
| [pywebview](https://pywebview.flowrl.com) | Wraps WKWebView in a native macOS window — no browser required |
| [PyInstaller](https://pyinstaller.org) | Bundles Python + all dependencies into a self-contained `RekitBox.app` |

---

## Built by

**Guthrie Entertainment LLC** · Free and open source · [github.com/fabledharbinger0993](https://github.com/fabledharbinger0993)
