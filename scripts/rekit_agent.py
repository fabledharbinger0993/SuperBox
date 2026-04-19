#!/usr/bin/env python3
"""Small provider bridge for RekitBox AI workflow.

Supports:
  - Ollama chat API (default, local/offline-first)
  - OpenAI-compatible chat completions endpoint (optional internet mode)

This script only returns structured JSON instructions. Patch application,
commits, and pushes are controlled by scripts/agent_workflow.sh.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_SYSTEM_PROMPT = (
    "You are a careful maintenance agent for a local Python project. "
    "Given repository diagnostics, suggest only minimal, safe fixes. "
    "Respond as strict JSON only with keys: summary, patch, commit_message, confidence, notes. "
    "patch must be unified diff text or an empty string."
)

DEFAULT_PROMPT_PROFILE = os.getenv("REKIT_AGENT_PROFILE", "default")
PROFILE_TO_FILE = {
    "cl": "cl.md",
    "is": "is.md",
    "reviewpr": "reviewpr.md",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_prompt_profile_root() -> Path:
    return _repo_root() / "agent_assets" / "fabledclaw_snapshot" / ".pi" / "prompts"


def _resolve_profile_prompt(profile: str, profile_root: Path) -> tuple[str, str, str]:
    if profile == "default":
        return profile, "", "default"

    filename = PROFILE_TO_FILE.get(profile)
    if not filename:
        raise ValueError(f"unsupported prompt profile: {profile}")

    prompt_path = profile_root / filename
    if not prompt_path.is_file():
        raise ValueError(f"prompt profile file not found: {prompt_path}")

    text = prompt_path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"prompt profile file is empty: {prompt_path}")

    return profile, text, str(prompt_path)


def _http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 90) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("model returned empty text")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("model output did not contain a JSON object")
    return json.loads(match.group(0))


def _ollama_chat(model: str, prompt: str, system_prompt: str) -> str:
    url = os.getenv("REKIT_OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "options": {
            "temperature": 0.1,
        },
    }
    data = _http_post_json(url, payload, headers={})
    message = (data.get("message") or {}).get("content", "")
    if not message:
        raise ValueError("ollama response did not include message.content")
    return message


def _openai_compatible_chat(model: str, prompt: str, system_prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for provider=openai")

    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if base_url.endswith("/"):
        base_url = base_url[:-1]
    url = f"{base_url}/chat/completions"

    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    data = _http_post_json(url, payload, headers=headers)
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("openai-compatible response did not include choices")
    message = (choices[0].get("message") or {}).get("content", "")
    if not message:
        raise ValueError("openai-compatible response did not include message.content")
    return message


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate structured fix instructions from diagnostics")
    parser.add_argument("--context", required=True, help="Path to diagnostics/context text file")
    parser.add_argument("--output", required=True, help="Path to write structured JSON response")
    parser.add_argument("--provider", default=os.getenv("REKIT_AGENT_PROVIDER", "ollama"), choices=["ollama", "openai"])
    parser.add_argument("--model", default=os.getenv("REKIT_AGENT_MODEL", "qwen2.5-coder:7b"))
    parser.add_argument("--system-prompt", default=os.getenv("REKIT_AGENT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT))
    parser.add_argument("--prompt-profile", default=DEFAULT_PROMPT_PROFILE, choices=["default", "cl", "is", "reviewpr"])
    parser.add_argument("--prompt-profile-root", default=str(_default_prompt_profile_root()))
    args = parser.parse_args()

    with open(args.context, "r", encoding="utf-8") as fh:
        prompt = fh.read()

    prompt_profile_name, prompt_profile_text, prompt_profile_source = _resolve_profile_prompt(
        args.prompt_profile,
        Path(args.prompt_profile_root),
    )
    resolved_system_prompt = args.system_prompt
    if prompt_profile_text:
        resolved_system_prompt = f"{prompt_profile_text}\n\n{resolved_system_prompt}"

    try:
        if args.provider == "ollama":
            raw = _ollama_chat(args.model, prompt, resolved_system_prompt)
        else:
            raw = _openai_compatible_chat(args.model, prompt, resolved_system_prompt)

        parsed = _extract_json_object(raw)
        normalized = {
            "summary": str(parsed.get("summary", "")).strip(),
            "patch": str(parsed.get("patch", "")),
            "commit_message": str(parsed.get("commit_message", "agent: subtle maintenance fix")).strip()
            or "agent: subtle maintenance fix",
            "confidence": str(parsed.get("confidence", "medium")).strip() or "medium",
            "notes": str(parsed.get("notes", "")).strip(),
            "provider": args.provider,
            "model": args.model,
            "prompt_profile": prompt_profile_name,
            "prompt_profile_source": prompt_profile_source,
        }
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, json.JSONDecodeError) as exc:
        normalized = {
            "summary": f"agent unavailable: {exc}",
            "patch": "",
            "commit_message": "agent: no-op",
            "confidence": "none",
            "notes": "No patch generated.",
            "provider": args.provider,
            "model": args.model,
            "prompt_profile": prompt_profile_name,
            "prompt_profile_source": prompt_profile_source,
            "error": str(exc),
        }

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(normalized, fh, indent=2)
        fh.write("\n")

    print(normalized.get("summary", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())