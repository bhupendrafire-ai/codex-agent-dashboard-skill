# Codex Agent Dashboard Skill

Local Codex skill for running a live multi-agent orchestration dashboard.

## Contents

- `SKILL.md` - skill instructions and operator workflow.
- `scripts/agent_dashboard.py` - local dashboard generator/server and control-plane CLI.
- `agents/openai.yaml` - skill agent metadata.

## Quick Start

```powershell
py -3 .\scripts\agent_dashboard.py --serve --open
```

The dashboard serves at:

```text
http://127.0.0.1:8765/agent-dashboard.html
```

The current version supports JSON inputs, final-report ingestion, spawned-agent id reconciliation, stale/drift warnings, write-glob overlap warnings, review gates, quiet worktree scans, and second-brain export.
