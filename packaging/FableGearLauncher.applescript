-- FABLEGEARLauncher.applescript
-- Launches FABLEGEAR via launch.sh using absolute path to canonical repo
-- Works from any .app location (~/Applications/, Dock, etc.)

do shell script "bash '/Volumes/DJMT/FABLEDHARBINGER/GIT_REPOS/FableGear/launch.sh' > /dev/null 2>&1 &"
