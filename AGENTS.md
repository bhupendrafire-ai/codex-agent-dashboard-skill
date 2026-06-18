# Workspace Rules

- Whenever Codex changes files in this workspace (`H:\CodexAgentDashboard`), make the corresponding change in the active global Codex skill install at `C:\Users\Piculiar\.codex\skills\agent-dashboard` as part of the same task.
- Treat this workspace as the source of truth for development, and keep the global skill copy synchronized so Codex uses the updated skill immediately.
- This is standing approval for sync writes limited to `C:\Users\Piculiar\.codex\skills\agent-dashboard`. Ask before touching any other global machine state.
- Preserve existing user configuration and merge changes idempotently.
