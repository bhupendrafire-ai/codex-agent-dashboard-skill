<p align="center">
  <img src="docs/dashboard-preview.svg" alt="Codex Agent Dashboard preview" width="100%">
</p>

<h1 align="center">Codex Agent Dashboard Skill</h1>

<p align="center">
  A local-first mission control dashboard for running serious multi-agent Codex coding workflows.
</p>

<p align="center">
  <a href="https://github.com/bhupendrafire-ai/codex-agent-dashboard-skill">
    <img alt="Codex skill" src="https://img.shields.io/badge/Codex-skill-111827?style=for-the-badge">
  </a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img alt="Local first" src="https://img.shields.io/badge/local--first-dashboard-16A34A?style=for-the-badge">
  <img alt="Status" src="https://img.shields.io/badge/status-active-2563EB?style=for-the-badge">
</p>

---

## Why This Exists

Codex can run complex work across many agents, but large agent swarms get messy fast:

- Which agents are running?
- Who owns which files?
- Which sessions are blocked?
- Which changes are ready for review?
- Which agents finished but never reported final evidence?
- Where did that handoff go?

This skill turns those loose threads into a live local cockpit: agents, lifecycle state, ownership, public activity, worktree intelligence, review gates, stale warnings, and second-brain export in one browser view.

It is intentionally not a replacement for the lead agent's judgment. It is the instrument panel that keeps the lead agent from flying blind.

## Highlights

| Capability | What it does |
| --- | --- |
| Live sidecar dashboard | Serves a local auto-refreshing dashboard at `http://127.0.0.1:8765/agent-dashboard.html`. |
| Multi-view cockpit | Includes `/overview`, `/workflow`, `/agents`, `/review`, `/diffs`, `/activity`, `/queue`, `/recipes`, `/memory`, and `/agent/<id>`. |
| Agent lifecycle tracking | Supports `planned`, `queued`, `running`, `completed`, `needs-review`, `reviewed`, `blocked`, `failed`, `merged`, and `closed`. |
| Robust JSON input | Uses JSON/file inputs for long summaries, paths, blockers, events, planned agents, and final reports. |
| Spawn reconciliation | Maps planned agents to real Codex session ids after `spawn_agent` returns. |
| Review gates | Prevents `reviewed` status unless changed files, tests, blocker evidence, and handoff are present. |
| Worktree intelligence | Scans git status, matches changes to owners, flags ownership violations, and ignores noisy build/cache artifacts. |
| Drift warnings | Surfaces stale running sessions, missing final reports, missing ids, and overlapping write globs. |
| Impact scoreboard | Estimates time saved, focus blocks recovered, and unlocks lightweight workflow badges. |
| Second-brain export | Appends a durable run summary to an Obsidian-style daily note. |

## Quick Start

From this repo:

```powershell
py -3 .\scripts\agent_dashboard.py --serve --open
```

Default local URL:

```text
http://127.0.0.1:8765/agent-dashboard.html
```

The script writes its live state under:

```text
%LOCALAPPDATA%\CodexAgentDashboard\
```

## The Basic Loop

1. Start or reuse the live dashboard.
2. Plan agents before spawning them.
3. Give each agent a heartbeat contract.
4. Reconcile each real spawned session id back to its planned row.
5. Let agents publish public heartbeats at meaningful milestones.
6. Ingest final reports when agents finish.
7. Use the review gate before marking work reviewed.
8. Export the run summary when the swarm is done.

## Impact Scoreboard

The dashboard includes a little motivation engine for humans: estimated time saved, manual effort avoided, coordination cost, focus blocks recovered, and small badges for healthy orchestration habits.

By default it uses a conservative estimate:

- `45m` manual work per full agent slice
- `8m` coordination overhead per full agent slice
- `25m` per focus block

Tune it for your own workflow:

```powershell
py -3 .\scripts\agent_dashboard.py --keep-existing `
  --manual-minutes-per-agent 60 `
  --coordination-minutes-per-agent 10 `
  --focus-block-minutes 25 `
  --impact-note "Estimate uses one focused manual implementation slice per planned agent."
```

Agents can also report per-slice estimates in JSON:

```json
{
  "name": "Installer",
  "manualMinutes": 90,
  "coordinationMinutes": 12
}
```

The estimate is meant to make progress feel visible. It is not billing data, and the dashboard says what assumptions it used.

## Plan Agents

Use JSON for real work. Pipe strings still exist for tiny updates, but JSON survives long paths and text containing `|`.

```powershell
py -3 .\scripts\agent_dashboard.py --keep-existing `
  --workflow-objective "Harden release readiness" `
  --plan-agent-json '{
    "name": "Installer",
    "summary": "Harden installer gate",
    "ownership": "Installer/Auth boundary",
    "allowedFiles": ["src/Installer", "src/Auth"],
    "writeGlobs": ["src/Installer/**", "src/Auth/**"],
    "doNotTouch": ["src/Billing/**"],
    "expectedOutputs": ["changed files", "tests", "blockers", "handoff"],
    "tests": "installer smoke",
    "priority": "P1",
    "wave": "wave-1",
    "recipe": "release-readiness-matrix",
    "status": "queued"
  }'
```

## Publish Heartbeats

Agents should publish public progress only: no private reasoning, secrets, credentials, or raw personal data.

```powershell
py -3 .\scripts\agent_dashboard.py --keep-existing `
  --event-json '{
    "agent": "Installer",
    "kind": "test",
    "message": "Installer smoke passed",
    "detail": "Validated auth handoff and rollback path"
  }'
```

Update an agent row:

```powershell
py -3 .\scripts\agent_dashboard.py --keep-existing `
  --agent-json '{
    "name": "Installer",
    "id": "019...",
    "status": "running",
    "summary": "Patching installer release gate",
    "ownership": "Installer/Auth boundary",
    "changedFiles": ["src/Installer/Gate.cs"],
    "tests": "dotnet test src/Installer.Tests",
    "blockers": "None reported",
    "handoff": "Lead should review installer gate diff"
  }'
```

## Reconcile Spawned Session IDs

When a Codex spawn call returns the real session id:

```powershell
py -3 .\scripts\agent_dashboard.py --keep-existing `
  --reconcile-agent-id "Installer|019ed...|Installer"
```

That keeps planned rows, events, and future handoffs tied to the real running session.

## Ingest Final Reports

Final reports are the cleanest way to keep the dashboard from drifting away from truth.

```powershell
py -3 .\scripts\agent_dashboard.py --keep-existing `
  --final-report-json-file .\agent-final-report.json
```

Example final report:

```json
{
  "name": "Installer",
  "id": "019ed...",
  "status": "completed",
  "summary": "Installer release gate is hardened",
  "changedFiles": ["src/Installer/Gate.cs", "src/Auth/AuthProbe.cs"],
  "tests": "dotnet test src/Installer.Tests",
  "blockers": "None reported",
  "handoff": "Ready for lead review; watch rollback copy in Gate.cs",
  "events": [
    {
      "agent": "Installer",
      "kind": "handoff",
      "message": "Final report ready",
      "detail": "Changed files, verification, blockers, and handoff provided"
    }
  ]
}
```

Final reports set the agent to `needs-review` by default and record `lastFinalReportAt`.

## Review Gate

An agent cannot be marked `reviewed` unless it has:

- changed files
- tests or verification
- blocker evidence, even if the answer is `None reported`
- a handoff

This is deliberately strict. The dashboard should make integration safer, not just prettier.

## Worktree Scan

Scan a git worktree for changed files, ownership matches, overlap risk, and handoff readiness:

```powershell
py -3 .\scripts\agent_dashboard.py --keep-existing `
  --scan-worktree H:\CADMation_NXT
```

The scanner ignores noisy folders and artifacts by default:

- `.git`
- `bin`
- `obj`
- `node_modules`
- `.next`
- `dist`
- `build`
- `.venv`
- `__pycache__`
- `.pytest_cache`
- `.mypy_cache`
- common binary and media artifacts

Add run-specific ignores:

```powershell
py -3 .\scripts\agent_dashboard.py --keep-existing `
  --scan-worktree H:\CADMation_NXT `
  --scan-ignore "artifacts/**" `
  --scan-ignore "*.snap"
```

## Built-In Recipes

The skill includes reusable deployment recipes:

- `explorer-swarm`
- `implementation-workers`
- `test-fix-wave`
- `pr-review-response`
- `migration-refactor-split`
- `bug-investigation-ladder`
- `release-readiness-matrix`

Print one:

```powershell
py -3 .\scripts\agent_dashboard.py --print-recipe release-readiness-matrix
```

## Second-Brain Export

Append a durable run summary to your daily note:

```powershell
py -3 .\scripts\agent_dashboard.py --keep-existing --export-second-brain
```

Override the default vault path:

```powershell
$env:SECOND_BRAIN_VAULT = "C:\Users\you\Documents\second-brain"
```

Preview without writing:

```powershell
py -3 .\scripts\agent_dashboard.py --print-memory-summary
```

## Side Panel Etiquette

`--open` is idempotent for the dashboard URL. It records the open URL under:

```text
%LOCALAPPDATA%\CodexAgentDashboard\open-state.json
```

Use `--open` once when starting the dashboard. Do not pass it for every heartbeat or agent launch.

## Project Layout

```text
.
+-- SKILL.md
+-- agents/
|   +-- openai.yaml
+-- scripts/
|   +-- agent_dashboard.py
+-- docs/
    +-- dashboard-preview.svg
```

## Philosophy

Good agentic coding is not just more agents. It is better coordination.

This dashboard is built around a few practical beliefs:

- Public status beats private guessing.
- Ownership should be declared before edits begin.
- Review should require evidence.
- A stale dashboard should admit it is stale.
- The lead agent still owns judgment.

## Status

This is an active personal Codex skill. It is useful today, opinionated by design, and likely to evolve as Codex orchestration surfaces expose richer runtime hooks.
