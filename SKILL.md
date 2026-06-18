---
name: agent-dashboard
description: Create, update, and open a local Codex-style multi-view dashboard for Codex sub-agent orchestration. Use when spawning or deploying Codex sub-agents, or when the user wants to see running agents, public activity updates, agent ownership, status, blockers, changed files, tests, or handoffs in a browser while a multi-agent task is active.
---

# Agent Dashboard

Use this skill to maintain a temporary local sidecar dashboard for Codex sub-agents.

## Quick Start

Run the bundled script:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --serve --open
```

`--open` is idempotent for the dashboard URL. It records the URL in `%LOCALAPPDATA%\CodexAgentDashboard\open-state.json` and skips opening another browser tab when the same dashboard was opened recently. Use `--force-open` only when the user explicitly wants a fresh tab/window.

The script writes:

- `%LOCALAPPDATA%\CodexAgentDashboard\agent-dashboard.html`
- `%LOCALAPPDATA%\CodexAgentDashboard\agent-status.json`

With `--serve`, the dashboard is served from `http://127.0.0.1:8765/agent-dashboard.html` by default and auto-refreshes every 5 seconds from the current JSON. The server also exposes routed views at `/overview`, `/workflow`, `/agents`, `/review`, `/diffs`, `/doctor`, `/activity`, `/queue`, `/recipes`, `/memory`, and `/agent/<id-or-name>`. To update visible status, rerun the script with current agent data and public activity events, or write a compatible `agent-status.json`.

The dashboard only shows agents reported by the orchestrator. If the current runtime does not expose a list-all-agents API, say that clearly in the dashboard summary or public activity log.

## Codex Side Panel

When deploying agents from Codex desktop, keep the dashboard visible in the in-app browser side panel at:

```text
http://127.0.0.1:8765/agent-dashboard.html
```

If an in-app browser navigation tool is available, navigate it to that URL when the first agent is deployed. If no such tool is exposed, emit the URL as a short status line and use `--serve --open` as the fallback. The Python server can open the OS default browser, but it cannot force the Codex side panel open by itself.

Do not pass `--open` for every agent launch, status update, wave promotion, or heartbeat. Start or reuse the server once, then update dashboard data with `--keep-existing` commands. If the side panel is already on the dashboard URL, leave it alone.

## Updating Agents

Prefer JSON for anything longer than a tiny heartbeat. Pipe-separated input still works for quick one-line updates, but JSON is the robust path for long paths, summaries, blocker text, and fields that can contain `|`.

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing `
  --agent-json '{"name":"CATIA/BOM","id":"019...","status":"running","summary":"Hardening worker contract","ownership":"EngineBridge worker files","changedFiles":["src/EngineBridge/Worker.cs"],"tests":"dotnet test src/EngineBridge.Tests","blockers":"None reported","handoff":"Awaiting lead review"}' `
  --event-json '{"agent":"CATIA/BOM","kind":"edit","message":"Patched worker contract","detail":"Added fixture-backed axis propagation command"}'
```

For larger updates, use `--agent-json-file`, `--event-json-file`, `--plan-agent-json-file`, or `--final-report-json-file`.

Pass one `--agent` value per agent:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing `
  --agent "CATIA/BOM|019...|running|Hardening worker contract|EngineBridge worker files" `
  --agent "WPF UX|019...|blocked|Waiting for engine contract|App XAML/ViewModels" `
  --event "CATIA/BOM|edit|Patched worker contract|Added fixture-backed axis propagation command"
```

When the live server is already running, use update commands without `--serve`; the open browser page will refresh from the updated JSON.

Pipe-separated fields are:

```text
name|id|status|summary|ownership|changedFiles|tests|blockers|handoff
```

Only the first five fields are expected. Leave later fields blank when unknown. For planned agents entered with pipe syntax, `allowedFiles` is also treated as the initial write-glob set.

Recommended statuses:

- `planned`
- `running`
- `queued`
- `completed`
- `needs-review`
- `reviewed`
- `blocked`
- `failed`
- `merged`
- `closed`

## Control Plane

Use the control-plane features when running multi-agent coding work.

Plan agents before spawning them:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing `
  --workflow-objective "Implement release readiness fixes" `
  --plan-agent "Installer|Harden installer gate|eng/installer|eng/;src/Installer|src/Auth|changed files;tests;handoff|installer smoke|P1|wave-1|release-readiness-matrix|queued"
```

For robust planning, prefer JSON and include `writeGlobs` explicitly:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing `
  --plan-agent-json '{"name":"Installer","summary":"Harden installer gate","ownership":"Installer/Auth boundary","allowedFiles":["src/Installer","src/Auth"],"writeGlobs":["src/Installer/**","src/Auth/**"],"doNotTouch":["src/Billing/**"],"expectedOutputs":["changed files","tests","handoff"],"tests":"installer smoke","priority":"P1","wave":"wave-1","recipe":"release-readiness-matrix","status":"queued"}'
```

For read-only scouts, set `readOnly:true` and keep `allowedFiles`/`ownership` specific. Read-only agents do not need `writeGlobs` or changed files to pass the review gate, but they still need verification, blocker status, and a handoff.

Promote the next queued wave according to the dashboard concurrency limit:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing --control-action "|promote-next|Fill open runtime slots"
```

Set lifecycle state or review gate state:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing `
  --set-status "Installer|needs-review|Tests passed; awaiting lead review"
```

`reviewed` is gated. The dashboard will refuse to mark an agent reviewed until it has changed files, tests or verification, explicit blocker evidence, and a handoff. If there are no blockers, the agent should write `None reported` rather than leaving the field blank.

After a real Codex session is spawned, reconcile the actual id back onto the planned row:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing `
  --reconcile-agent-id "Installer|019ed...|Installer"
```

When an agent finishes, ingest its final report instead of manually retyping the row:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing `
  --final-report-json-file .\agent-final-report.json
```

Final reports set the agent to `needs-review` by default, append a public handoff event, record `lastFinalReportAt`, and preserve the evidence needed by the review gate.

Generate a final-report template from the current row before handoff:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --print-final-report-template "Installer"
```

## Doctor and Hygiene

Run the dashboard doctor before reviews, handoffs, or closing a swarm:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --doctor
```

The doctor summarizes lifecycle counts, drift warnings, missing final reports, missing write scopes, missing active ids, stale pending commands, critical blockers, impact estimate, and suggested next commands. The same health report is visible in the dashboard at `/doctor`.

Dismiss or resolve a stale pending orchestrator command after acting on it:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing `
  --set-command-state "1738a5216292|dismissed|Superseded by newer handoff"
```

Archive an immutable timestamped copy of the current dashboard state:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing --archive-run-snapshot
```

Scan a git worktree for diff/worktree intelligence:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing --scan-worktree H:\CADMation_NXT
```

The scanner ignores noisy folders and artifacts by default, including `.git`, `bin`, `obj`, `node_modules`, build/dist folders, caches, virtualenvs, and common binary/media artifacts. Add run-specific ignores with repeated `--scan-ignore` flags.

Export a durable run summary to the second-brain daily note:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing --export-second-brain
```

Set `SECOND_BRAIN_VAULT` to override the default vault path for tests or alternate vaults.

Preview the same summary without writing:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --print-memory-summary
```

The local dashboard UI can mark agents reviewed, close them, promote the next wave, copy pending orchestrator commands, preview second-brain summaries, and generate follow-up/status/interrupt command prompts. Actions that require Codex tools, such as `spawn_agent`, `send_input`, or `close_agent`, are represented as pending commands for the orchestrator to execute because the local Python server cannot call Codex tools directly.

## Impact Scoreboard

The dashboard estimates time saved from agent-assisted work and shows it in the overview/workflow scoreboards. The estimate is intentionally approximate: it multiplies tracked agent slices by a manual-work baseline, weights each slice by lifecycle progress, discounts default estimates for scouts/read-only/meta/no-edit slices, then subtracts orchestration overhead for every tracked slice.

Tune the assumptions for a run:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --keep-existing `
  --manual-minutes-per-agent 60 `
  --coordination-minutes-per-agent 10 `
  --focus-block-minutes 25 `
  --impact-note "Estimate uses one focused manual implementation slice per planned agent."
```

Agents may also report per-slice estimates in JSON with `manualMinutes` and `coordinationMinutes`; explicit estimates are treated as already scoped and are not discounted by the default scout/meta heuristics. Keep the estimate user-facing and honest; it is motivation, not accounting.

## Recipes

Reusable deployment recipes are built in:

- `explorer-swarm`
- `implementation-workers`
- `test-fix-wave`
- `pr-review-response`
- `migration-refactor-split`
- `bug-investigation-ladder`
- `release-readiness-matrix`

Print a recipe prompt:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --print-recipe implementation-workers
```

## Public Activity

Use `--event` to show visible progress. This is not private reasoning; it is a public activity log.

```text
agent|kind|message|detail|timestamp
```

Recommended kinds:

- `read`
- `edit`
- `test`
- `blocked`
- `handoff`
- `merged`

The dashboard also shows recent matching heartbeat events inside each agent card, so agents should use their exact dashboard name in the `agent` field.

## Future Agent Spawn Contract

Whenever spawning a future Codex sub-agent:

1. Start or reuse the live dashboard before the first spawn.
2. Seed the dashboard with a `queued` or `running` row for each planned agent.
3. Include the heartbeat contract in the spawned agent prompt.
4. Declare planned `writeGlobs` so overlap warnings are visible before work starts.
5. After `spawn_agent` returns the actual id, run `--reconcile-agent-id`.
6. Ask agents to publish heartbeats directly with the dashboard script at meaningful milestones.
7. At final handoff, ingest a JSON final report with changed files, tests, blocker evidence, and handoff.

Generate the heartbeat contract for a prompt with:

```powershell
py -3 C:\Users\Piculiar\.codex\skills\agent-dashboard\scripts\agent_dashboard.py --print-heartbeat-contract --agent-name "WPF UX"
```

The contract tells agents to publish public updates only. They must not write private reasoning, secrets, credentials, or raw personal data into the dashboard.

## Orchestrator Workflow

1. Create or update the dashboard when agents are spawned.
2. Include the heartbeat contract in every agent prompt.
3. Rerun the script whenever an agent reports changed files, tests, blockers, handoff, or visible progress.
4. Reconcile actual spawned ids onto planned rows as soon as the tool returns them.
5. Ingest final reports; do not mark reviewed from memory alone.
6. Watch stale/drift warnings for missing ids, missing final reports, old heartbeats, and write-glob overlap.
7. Keep the status honest: if the dashboard cannot read live runtime state directly, say so in the summary.
8. Close the loop by marking agents `reviewed`, `merged`, `closed`, or `blocked` after evidence-backed review.

The webpage is a visibility aid, not an authoritative source. The authoritative evidence remains tool output, changed files, tests, blocker evidence, handoffs, and agent final reports.

## Development Verification

When changing this skill, run:

```powershell
py -3 -m unittest discover -s tests -v
py -3 -m py_compile scripts\agent_dashboard.py tests\test_agent_dashboard.py
```

Also smoke the live dashboard with `--doctor`, `--print-final-report-template`, and the `/doctor` route after starting or reusing the local server.
