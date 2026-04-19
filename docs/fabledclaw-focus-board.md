# FabledClaw Focus Board (Minimal First Slice)

This board is intentionally small. Ignore everything else for now.

## What To Ignore Right Now

- Skip full channel runtime, cron service internals, and extension surfaces.
- Skip macOS app internals.
- Skip broad plugin onboarding complexity.

## First Slice Only (7 Files)

1. `agent_assets/fabledclaw_snapshot/.pi/prompts/cl.md`
2. `agent_assets/fabledclaw_snapshot/.pi/prompts/is.md`
3. `agent_assets/fabledclaw_snapshot/.pi/prompts/reviewpr.md`
4. `agent_assets/fabledclaw_snapshot/commands/agent.ts`
5. `agent_assets/fabledclaw_snapshot/flows/provider-flow.ts`
6. `scripts/rekit_agent.py`
7. `scripts/agent_workflow.sh`

## Immediate Goal

Build only one capability:

- Prompt profile selection (cl, is, reviewpr) in the RekitBox private AI workflow.

No scheduling. No channel delivery. No auto-push changes.

## Acceptance Criteria For This Slice

- Agent runner accepts a prompt profile name.
- Prompt profile content is prepended as the system prompt.
- Default profile remains safe and local-first.
- Existing safety flags continue to gate apply/commit/push.

## Next After This Works

- Add one lightweight command-routing shim.
- Add one diagnostics-only cron heartbeat.

Only proceed when first-slice behavior is stable.

## Core Gaps To Import Only If Needed

You do not need these for first-slice prompt profile wiring.

- For `logging.ts` reuse:
  - `logging/console.ts`
  - `logging/levels.ts`
  - `logging/logger.ts`
  - `logging/subsystem.ts`
- For `channel-web.ts` runtime parity:
  - `plugins/runtime/runtime-whatsapp-boundary.ts`
- For `entry.*.test.ts` to be runnable:
  - `entry.ts`
  - `entry.respawn.ts`
  - `version.ts`
  - `cli/` (minimum files used by entry tests)
  - `infra/` (minimum files used by entry tests)
- For `library.test.ts` to be runnable:
  - `library.ts`

If you want to keep imports minimal, defer all of the above until the first slice is passing.
