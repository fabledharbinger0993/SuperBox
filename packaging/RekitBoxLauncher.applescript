-- FABLEGEARLauncher.applescript
-- Launches FABLEGEAR via launch.sh using absolute path to canonical repo
-- Works from any .app location (~/Applications/, Dock, etc.)

do shell script "bash '/Users/cameronkelly/FABLEDHARBINGER/GIT_REPOS/FABLEGEAR/launch.sh' > /dev/null 2>&1 &"
