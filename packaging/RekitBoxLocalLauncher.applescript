-- FABLEGEARLocalLauncher.applescript
-- Launches FABLEGEAR directly from the local dev repo.
-- No GitHub clone or git pull — safe to use during active development.
-- Swap back to FABLEGEARLauncher.applescript for public releases.

do shell script "bash '/Users/cameronkelly/FABLEDHARBINGER/GIT_REPOS/FABLEGEAR/launch_local.sh' > /dev/null 2>&1 &"
