# RekitBox Private AI Workflow

This workflow is private-repo only and is intentionally excluded from the public repo mirror.

## Goals

- Run an AI maintenance loop from inside the same venv used by RekitBox.
- Default to local Ollama (offline-first).
- Support optional internet provider mode by setting an API key.
- Optionally auto-sync vetted private changes into the public repo while keeping AI files out.

## Files

- scripts/agent_workflow.sh
- scripts/rekit_agent.py
- scripts/sync_public_repo.sh
- .public-sync-excludes
- .rekitbox-agent.env.example

## Setup

1. Ensure scripts are executable:

```bash
chmod +x scripts/agent_workflow.sh scripts/sync_public_repo.sh scripts/rekit_agent.py
```

1. Create local env config:

```bash
cp .rekitbox-agent.env.example .rekitbox-agent.env
source .rekitbox-agent.env
```

1. If using Ollama, start Ollama and pull a coding model:

```bash
ollama pull qwen2.5-coder:7b
```

1. Select a prompt profile (optional):

```bash
export REKIT_AGENT_PROFILE=cl   # or: default, is, reviewpr
```

## Run modes

Dry run (recommended first):

```bash
source .rekitbox-agent.env
./scripts/agent_workflow.sh once
```

Show current runtime settings (includes selected profile):

```bash
./scripts/agent_workflow.sh status
```

Background loop:

```bash
source .rekitbox-agent.env
./scripts/agent_workflow.sh start
./scripts/agent_workflow.sh status
./scripts/agent_workflow.sh stop
```

Enable automatic patch apply + commit + push only after validation:

```bash
export REKIT_AGENT_AUTO_APPLY=1
export REKIT_AGENT_AUTO_COMMIT=1
export REKIT_AGENT_AUTO_PUSH=1
./scripts/agent_workflow.sh once
```

## Private to public mirror

Initialize local clone of public repo (once):

```bash
./scripts/sync_public_repo.sh init
```

Sync private repo state into public repo and push:

```bash
./scripts/sync_public_repo.sh once
```

AI files are excluded by .public-sync-excludes so the public app remains AI-free.

## Safety notes

- Keep REKIT_AGENT_AUTO_APPLY=0 until model quality is proven for your codebase.
- Keep REKIT_AGENT_AUTO_PUSH=0 if you want a manual review step before publishing.
- Run Rekordbox closed for all operations that mutate the Rekordbox DB.
