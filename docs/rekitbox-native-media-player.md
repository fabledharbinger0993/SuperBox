# FableGear-Native Minimal Media Player

## Purpose

- Allow users to preview and cue local audio files in the library/playlist builder

## Features

- Play/pause, seek, volume
- Simple waveform or progress bar (optional)
- Hotcue set/preview (if possible)
- Support for common audio formats (mp3, wav, aiff, flac)
- Lightweight, no external dependencies beyond standard Python/JS audio APIs

## UI Integration

- Embedded in track detail or playlist view
- Minimal controls: play, pause, seek, volume
- Optional: waveform visualization using JS (e.g., wavesurfer.js)

## Backend

- Serve audio files via Flask static route
- No DRM or cloud streaming; local files only

## Safety

- Never modify audio files
- Only allow playback of files in user library

---

This plan enables safe, local audio preview/cueing for FableGear-native workflows.