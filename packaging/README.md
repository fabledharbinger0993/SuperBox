# RekitBox Mac .app Launcher

This folder contains scripts to build a native Mac .app wrapper for RekitBox with a custom dock icon.

## Files
- `RekitBoxLauncher.applescript`: AppleScript source for the launcher.
- `build_applescript_app.sh`: Script to compile the .app and set the icon.
- `rekitbox-app-icon.png`: Custom dock icon (must be present).

## Build Instructions

1. Ensure you have Xcode command line tools installed (`xcode-select --install`).
2. Run the build script:

    ```sh
    cd packaging
    bash build_applescript_app.sh
    ```

3. The resulting `RekitBox.app` can be moved to `/Applications` or the Dock.

## Behavior
- On launch, the app runs `launch.sh` from the repo root.
- Homebrew and RekitBox update checks run silently; if offline, the current version opens.
- Closing the window quits the app and venv.

---

For advanced customization, edit the AppleScript or build script as needed.
