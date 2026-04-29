# FableGear

**A free, open-source rekordbox library toolkit for DJs with large, mature libraries.**

[![Download](https://img.shields.io/github/v/release/fabledharbinger0993/FableGear?label=Download%20FableGear&style=for-the-badge&color=6d28d9&logo=apple&logoColor=white)](https://github.com/fabledharbinger0993/FableGear/releases/latest)

macOS · Free · No account required · Rekordbox must be closed for write operations

---

## Install

1. Click **Download FableGear** above
2. Unzip — you get **FableGear.app**
3. Move it to your Desktop or Applications folder
4. Right-click → **Open** → **Open** (required once — see Gatekeeper note below)
5. A setup window installs any missing dependencies, then FableGear opens as a native app

> **First launch** opens a Terminal window and automatically installs everything needed — Homebrew, `ffmpeg`, `chromaprint`, and all Python packages. This runs once and takes a few minutes. After that FableGear opens as a **standalone native window** — no browser required.

**"FableGear is damaged" or "cannot be opened"?**
This is macOS Gatekeeper — it blocks apps that aren't signed with an Apple Developer certificate.

To allow it, do one of these:

1. Right-click `FableGear.app` → **Open** → **Open Anyway** in the dialog that appears.
2. Go to **System Settings → Privacy & Security**, scroll down until you see *"FableGear was blocked from use"*, then click **Open Anyway**.

---

## What it does

FableGear fills the gaps Rekordbox leaves open. It reads and writes the Rekordbox database directly and runs entirely on your local machine — no cloud, no account, no subscription.

### Core pipeline — run in order

| Icon | Tool | What it does |
| --- | --- | --- |
| ▣ | **Library Audit** | Two-pass audit: cross-checks the Rekordbox database against your drive (broken paths, orphaned entries, untagged tracks) and walks the filesystem for a full physical inventory. Feeds all downstream tools. |
| 🏷 | **Tag Tracks** | Analyzes the actual audio waveform and writes BPM and musical key into the file tags permanently. Tags survive any database rebuild and work in any software. |
| 🔎 | **Find Duplicates** | Acoustic fingerprinting via Chromaprint. Finds the same recording regardless of filename, bitrate, or format — not filename matching, actual audio content comparison. |
| ✂ | **Prune Duplicates** | Loads the duplicate report and removes selected files. Multi-step confirmation with buttons at different screen corners to prevent accidental clicks. Files go to Trash, not permanent deletion. |
| 📍 | **Fix Broken Paths** | When a drive mounts under a new name or files move, bulk-updates every broken path in the database in one operation. |
| ＋ | **Import** | Adds new audio files to the Rekordbox database. Dry-run mode available. Full database backup created before any write. |
| 🔗 | **Link Playlists** | Maps your folder structure to Rekordbox playlist names automatically after import. |

### Optional tools

| Icon | Tool | What it does |
| --- | --- | --- |
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
- **Glossary** — built-in glossary of every technical term used in FableGear

---

## Requirements

- macOS Monterey 12.0 or later (Apple Silicon or Intel)
- Internet connection on first launch only
- Rekordbox must be **closed** before any write operation

---

## FableGo + FableGear repo strategy

FableGo is intentionally kept in this same repository (`ios/FableGo`) rather than split into a separate repo.

Why this is the current default:

- FableGo depends directly on FableGear's `/api/mobile/*` surface and auth flow
- Backend/mobile changes are easier to ship safely when versioned together
- One repo keeps release coordination and API compatibility checks in one place

If FableGo eventually needs an independent release cadence, separate contributor model, or public SDK-style API contract, splitting it into its own repo would make sense.

---

## Developer Auto-Sync (VS Code)

This repo now supports optional local auto-sync to GitHub when opened in VS Code:

- On folder open, VS Code starts a managed autosync daemon (`scripts/autosync.sh start`).
- Autosync only runs on allowed branches (default: `main`) and skips while git is mid-merge/rebase/cherry-pick.
- Before pushing, autosync fetches/rebases to avoid push-loop conflicts.
- Releases remain manual. Nothing in auto-sync creates tags or releases.
- Published releases always get `FableGear.zip` attached automatically via `.github/workflows/release-zip.yml`.

### One-time setup

```bash
cd "/Users/cameronkelly/FabledHarbinger/Git Repos/FableGear"
chmod +x scripts/autosync.sh scripts/release.sh
```

### Manual controls

Run a single autosync cycle immediately:

```bash
./scripts/autosync.sh once
```

Start autosync daemon:

```bash
./scripts/autosync.sh start
```

Check autosync daemon status:

```bash
./scripts/autosync.sh status
```

Stop autosync daemon:

```bash
./scripts/autosync.sh stop
```

Run autosync in foreground (debug mode):

```bash
AUTOSYNC_INTERVAL=5 AUTOSYNC_BRANCHES=main ./scripts/autosync.sh watch
```

Autosync state/logs are stored in `.git/autosync/`.

### Release command (manual only)

Create a release with generated notes:

```bash
./scripts/release.sh v2.0.13
```

Create a release with a notes file:

```bash
./scripts/release.sh v2.0.13 .github/release-notes.md
```

`scripts/release.sh` now enforces safe release preconditions:

- clean working tree
- current branch matches release branch (default `main`)
- local `main` matches `origin/main`
- tag format validation (`vX.Y.Z`)
- waits until `FableGear.zip` is confirmed attached (or fails on timeout)

### Private AI workflow (optional)

Private repository automation can run from inside the FableGear venv:

```bash
./scripts/agent_workflow.sh once
./scripts/agent_workflow.sh start
./scripts/agent_workflow.sh status
./scripts/agent_workflow.sh stop
```

Mirror private changes into the public repo (AI files excluded):

```bash
./scripts/sync_public_repo.sh once
```

See `docs/agent-workflow.md` for full setup and safety switches.

### Agent edition package (separate venv)

FableGear can now be packaged in two tracks:

- `FableGear.zip` -> regular runtime (`launch.sh`, `venv`)
- `FableGear-Agent.zip` -> agent runtime (`launch_agent.sh`, `venv-agent`)

Build the agent package locally:

```bash
./build_agent_release.sh
```

The agent installer launches `launch_agent.sh` and provisions an isolated
`venv-agent`, so your standard FableGear environment remains untouched.

---

## Under the hood

| Library | Purpose |
| --- | --- |
| [librosa](https://librosa.org) | BPM detection (beat tracking) and key detection (Krumhansl-Schmuckler on chroma features) |
| [Chromaprint / fpcalc](https://acoustid.org/chromaprint) | Acoustic fingerprinting for duplicate detection |
| [pyrekordbox](https://github.com/dylanljones/pyrekordbox) | Direct read/write access to the Rekordbox SQLite database |
| [mutagen](https://mutagen.readthedocs.io) | Audio file tag reading and writing (ID3, Vorbis, etc.) |
| [pyloudnorm](https://github.com/csteinmetz1/pyloudnorm) | EBU R128 loudness measurement |
| [Flask](https://flask.palletsprojects.com) + [Waitress](https://docs.pylonsproject.org/projects/waitress) | Local web server — everything runs on localhost, no internet at runtime |
| [pywebview](https://pywebview.flowrl.com) | Wraps WKWebView in a native macOS window — no browser required |
| [PyInstaller](https://pyinstaller.org) | Bundles Python + all dependencies into a self-contained `FableGear.app` |

---

## Built by

**Guthrie Entertainment LLC** · Free and open source · [github.com/fabledharbinger0993](https://github.com/fabledharbinger0993)
