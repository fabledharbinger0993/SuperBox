# SuperBox — Claude Context

## What this is
SuperBox is a local rekordbox library management toolkit. Flask backend, single-page HTML/JS/CSS frontend. Runs entirely on the local machine — no internet dependency, no cloud, no AI at runtime. The only network call is the optional git pull on launch.

## Core philosophy
- **Function before aesthetics.** Never sacrifice working behaviour for visual polish.
- **Local-first, always.** No CDN links, no external fonts, no runtime API calls. If it requires internet, it doesn't belong here.
- **Rekordbox is the source of truth.** SuperBox reads and writes the rekordbox database (`master.db`) via pyrekordbox. Every operation that touches the DB or moves files requires explicit user confirmation. Rekordbox must be closed before any write operation.
- **One source of truth.** The canonical codebase lives at `FabledHarbinger/Git Repos/SuperBox/SuperBox`. The GitHub repo (`fabledharbinger0993/SuperBox`) must always reflect this.

## How to start Claude Code on this project
Open Claude Code with working directory set to:
`/Users/cameronkelly/FabledHarbinger/Git Repos/SuperBox/SuperBox`

That's it. This file will orient the session.

## Stack
- **Backend:** Python 3, Flask served by Waitress
- **Database access:** pyrekordbox (reads rekordbox master.db)
- **Audio analysis:** librosa (BPM/key), Chromaprint/fpcalc (acoustic fingerprinting)
- **Frontend:** Single HTML template (`templates/index.html`) + extracted stylesheet (`static/superbox.css`)
- **Launch:** `launch.sh` → sets up venv, pulls latest, starts Waitress on `localhost:5001`, opens browser. Wrapped as a Mac `.app` via Automator for dock access.

## Project structure
```
SuperBox/
├── app.py                  # Flask routes + SSE streaming
├── scanner.py              # Filesystem audio scanner
├── duplicate_detector.py   # Chromaprint fingerprinting + pre-filter by key/BPM/duration
├── audio_processor.py      # BPM/key detection via librosa, writes scan_index.json
├── library_organizer.py    # De-fragger: moves files into Artist/Album/Track hierarchy
├── pruner.py               # Duplicate removal with multi-step confirmation UI
├── importer.py             # Track import into rekordbox DB
├── relocator.py            # Fixes broken file paths in DB
├── playlist_linker.py      # Links tracks to playlists by folder structure
├── audit.py                # Read-only library health check
├── cli.py                  # CLI interface
├── config.py               # App configuration
├── user_config.py          # Per-user settings (music root, excluded folders, etc.)
├── db_connection.py        # pyrekordbox DB connection wrapper
├── brew_updater.py         # Weekly Homebrew update checker (shows banner in UI)
├── icon_utils.py           # Icon helpers
├── key_mapper.py           # Camelot/Open Key mapping
├── templates/
│   └── index.html          # Single-page app (HTML + JS only, CSS extracted)
├── static/
│   ├── superbox.css        # All styles (extracted from index.html for browser caching)
│   └── [icons]             # PNG icons for each tool card
├── launch.sh               # Startup script (Automator-safe: silences stdout for dock icon)
└── requirements.txt
```

## CSS architecture
Styles live in `static/superbox.css`. Key design tokens in `:root`:
- `--log-h: 340px` — log panel height (fixed overlay)
- `--scan-bar-h: 44px` — scan bar height. **All bottom offsets that reference the scan bar use `var(--scan-bar-h)`.** Do not hardcode `44px` in layout calculations.
- Colour semantics: `--safe`, `--caution`, `--warn`, `--danger`, `--accent`

## The log panel / scan bar layout
Both are `position: fixed; bottom: 0`. The scan bar is always visible. The log panel slides up from behind it. Body padding-bottom accounts for both:
```css
body { padding-bottom: calc(var(--log-h) + var(--scan-bar-h) + 4px); }
body.scan-active { padding-bottom: calc(var(--log-h) + var(--scan-bar-h) * 2 + 4px); }
```
Floating buttons (owl, lightbulb, session pills) shift up when `body.log-open` is set.

## Key decisions made (don't undo without reason)
- **`exec > /dev/null 2>&1` in launch.sh is intentional.** Automator treats any stdout as an error. Server logs still go to `superbox.log` via explicit redirect.
- **`git pull --ff-only` on launch is intentional.** Keeps the running app current. `--ff-only` prevents silent merges.
- **No Tailwind, no component frameworks.** Single CSS file, custom tokens, flat structure. Right-sized for a single-page local tool.
- **The multi-step confirmation UI for destructive actions** (prune, organize in assimilate mode) places each confirm button at a different screen corner deliberately — prevents muscle-memory clicking through a dangerous operation.
- **library_organizer.py uses TPE2 over TPE1** for artist folder naming to prevent `Artist feat. Guest` folder explosion. Camelot key prefixes are stripped from artist tags before folder creation.

## Drives in use
- `DJMT` — primary library drive (`/Volumes/DJMT`)
- `Passport` — 4TB NTFS backup/overflow (`/Volumes/Passport`). Does not auto-mount on Mac — run `diskutil mount /dev/disk5s2` if not showing.
- `MARSHALL T` — secondary drive (`/Volumes/MARSHALL T`)

## GitHub
Repo: `https://github.com/fabledharbinger0993/SuperBox`
The public-facing repo must stay current — it's what users download from the website.
Always push to `main` after significant changes.
