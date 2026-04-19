# FabledClaw Intake Roadmap

This is the first stable intake lane for bringing selected FabledClaw agent components into RekitBox private automation.

## Stage 1: Snapshot And Catalog

Run:

```bash
./scripts/import_fabledclaw_assets.sh
```

Output path:

- `agent_assets/fabledclaw_snapshot/`
- `agent_assets/fabledclaw_snapshot/MANIFEST.txt`

## Stage 2: Functional Mapping

Map each imported area to RekitBox agent workflow responsibilities:

- `.pi/prompts/*` -> system and task prompts for `scripts/rekit_agent.py`
- `.pi/extensions/*` -> future helper utilities for diffing, redraw, and file operations
- `auto-reply/*` -> command routing and trigger patterns for autonomous workflow mode
- `context-engine/*` -> context assembly and delegation model before model call
- `bindings/*` -> typed interfaces for internal orchestration records
- `assets/chrome-extension/*` -> optional remote control or browser-assist layer (deferred)
- `channels/*` -> channel/session state machine and delivery guard patterns
- `commands/*` -> command parser, auth choice flow, and provider routing patterns
- `copilot-proxy/*` -> provider bridge contract shape for plugin-backed providers
- `cron/*` -> heartbeat, scheduling, and isolated run lane patterns
- `diffs/*` -> diff rendering and review-centric tool structure
- `flows/*` -> setup doctor/model-picker flow patterns for guided onboarding
- `helpers/*` -> reusable contracts and test harness utilities for safe integration
- `ollama/*` -> local provider plugin patterns and model/setup defaults
- `media/*` -> media ingestion, MIME handling, and bounded fetch/store safety patterns
- `interactive/*` -> structured interactive payload envelope patterns
- `process/*` -> command queue, exec supervision, and restart recovery patterns
- `routing/*` -> account/session route resolution and session-key continuity patterns
- `sglang/*` -> alternate provider plugin shape compatible with provider contract seams
- `signal/*` -> channel plugin architecture and runtime monitor patterns (deferred)
- `skills/*` -> skill catalog patterns and tool-specific operating guidance (reference only)
- `slack/*` -> mature channel plugin implementation patterns for actions/streaming/setup
- `zai/*` -> provider plugin registration and runtime contract pattern
- `.env.example` -> environment precedence and provider/channel key conventions
- `entry.*.test.ts` -> CLI startup/respawn fast-path safeguards
- `library.test.ts` -> lazy runtime boundary guardrails for dynamic imports
- `logging.ts` -> logging subsystem barrel/re-export architecture pattern
- `openclaw.podman.env` -> container runtime env contract and token/bind defaults
- `pnpm-workspace.yaml` -> monorepo package/workspace boundary layout reference
- `channel-web.ts` -> channel barrel surface and lazy boundary resolution pattern

## Stage 3: Controlled Activation In RekitBox

Initial integration order:

1. Import prompt templates into a RekitBox-specific prompt registry.
2. Add command-routing shim for a small, safe command set (`status`, `diagnose`, `propose-fix`).
3. Keep write actions gated behind explicit safety flags already used in `agent_workflow.sh`.

## Safety Rules

- Public repo sync must continue excluding all private AI internals.
- Default mode remains local-first (Ollama).
- Internet-backed provider remains optional and disabled by default.
- No DB-writing Rekordbox operations should be auto-executed by the agent.
