-- RekitBoxAgentLauncher.applescript
-- Launches RekitBox Agent via launch_agent.sh using absolute path to canonical repo
-- Works from any .app location (~/Applications/, Dock, etc.)

do shell script "bash '/Users/cameronkelly/FABLEDHARBINGER/GIT_REPOS/RekitBox/launch_agent.sh' > /dev/null 2>&1 &"
