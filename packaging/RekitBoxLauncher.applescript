-- RekitBoxLauncher.applescript
-- Launches RekitBox via launch.sh, for use in a .app wrapper

do shell script "cd \"" & POSIX path of (path to me as text) & "../../ && ./launch.sh > /dev/null 2>&1 &"
