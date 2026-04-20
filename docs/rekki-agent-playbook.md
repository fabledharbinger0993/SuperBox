# Rekki Agent Playbook

## Why this exists

This document turns the Rekki concept into an implementation contract for this repo.

## Architecture (Current)

- Native shell: main.py
- Backend/API: app.py
- UI: templates/index.html + static/rekitbox.js
- Local agent bridge: scripts/rekit_agent.py + scripts/agent_workflow.sh
- Prompt profiles: agent_assets/fabledclaw_snapshot/.pi/prompts/

## Local-first model strategy

1. Primary: Ollama (localhost)
1. Optional fallback: OpenAI-compatible endpoint via environment key
1. Public runtime remains AI-free unless explicitly enabled in private workflow

## Reason -> Act -> Observe pattern

1. Reason

- Read runtime context from /api/rekki/context.
- Determine if user intent is read-only or write-capable.

1. Act

- Read-only first: summarize state, explain risks, suggest the minimal next action.
- For write-capable operations: produce a checklist and require explicit user confirmation.

1. Observe

- Re-query context/status.
- Report what changed and what did not.

## Rekordbox safety contract

- Treat Rekordbox database as source of truth.
- Assume Rekordbox must be closed for writes.
- Always prefer dry-run/preview before write actions.
- Never silently mutate DB or filesystem from autonomous assistant output.

## Harmonic helper rules (Camelot)

For key nA/nB where n in 1..12:

- same: nA -> nA, nB -> nB
- adjacent: nA -> (n+1)A or (n-1)A, same for B
- relative: nA <-> nB

Use this as recommendation logic, not automatic playlist mutation.

## Suggested practical feature queue

1. Compatible-track read-only query (BPM + key compatibility)
1. Prep-audit summary (missing BPM/key tags)
1. Session planner assistant (genre + energy + harmonic path)
1. Optional human-confirmed action suggestions tied to existing tool cards

## Operational checklist before testing Rekki

1. Activate venv
1. Confirm Ollama is reachable
1. Confirm selected model is available
1. Launch RekitBox
1. Open Rekki panel and run read-only prompts first
