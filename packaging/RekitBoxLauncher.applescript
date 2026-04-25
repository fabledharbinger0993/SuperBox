-- RekitBoxLauncher.applescript
-- Launches RekitBox via launch.sh using absolute path to canonical repo
-- Works from any .app location (~/Applications/, Dock, etc.)

do shell script "bash '/Users/cameronkelly/FABLEDHARBINGER/GIT_REPOS/RekitBox/launch.sh' > /dev/null 2>&1 &"
