from __future__ import annotations

import argparse
import contextlib
import fnmatch
import functools
import hashlib
import html
import http.server
import json
import os
import pathlib
import re
import subprocess
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone


DEFAULT_DIR = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home())) / "CodexAgentDashboard"
DEFAULT_STATUS = DEFAULT_DIR / "agent-status.json"
DEFAULT_HTML = DEFAULT_DIR / "agent-dashboard.html"
DEFAULT_OPEN_STATE = DEFAULT_DIR / "open-state.json"
DEFAULT_SNAPSHOT_DIR = DEFAULT_DIR / "snapshots"
DEFAULT_CONCURRENCY_LIMIT = 6
DEFAULT_MANUAL_MINUTES_PER_AGENT = 45
DEFAULT_COORDINATION_MINUTES_PER_AGENT = 8
DEFAULT_FOCUS_BLOCK_MINUTES = 25
IMPACT_READ_ONLY_WEIGHT = 0.35
IMPACT_META_WORK_WEIGHT = 0.65
IMPACT_NO_EDIT_WEIGHT = 0.50
DEFAULT_STALE_MINUTES = {
    "planned": 120,
    "queued": 120,
    "running": 30,
    "completed": 60,
    "needs-review": 60,
    "blocked": 240,
}
DEFAULT_SCAN_IGNORE_PATTERNS = [
    ".git/**",
    "**/.git/**",
    "bin/**",
    "**/bin/**",
    "obj/**",
    "**/obj/**",
    "node_modules/**",
    "**/node_modules/**",
    ".next/**",
    "**/.next/**",
    "dist/**",
    "**/dist/**",
    "build/**",
    "**/build/**",
    ".venv/**",
    "venv/**",
    "**/.venv/**",
    "**/venv/**",
    "__pycache__/**",
    "**/__pycache__/**",
    ".pytest_cache/**",
    "**/.pytest_cache/**",
    ".mypy_cache/**",
    "**/.mypy_cache/**",
    ".cache/**",
    "**/.cache/**",
    "*.zip",
    "*.7z",
    "*.tar",
    "*.gz",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.pdf",
    "*.bin",
    "*.dll",
    "*.exe",
    "*.pdb",
    "*.obj",
]
DEFAULT_VAULT = pathlib.Path(
    os.environ.get("SECOND_BRAIN_VAULT")
    or pathlib.Path(os.environ.get("USERPROFILE", pathlib.Path.home())) / "Documents" / "second-brain"
)
LIFECYCLE_STATES = [
    "planned",
    "queued",
    "running",
    "completed",
    "needs-review",
    "reviewed",
    "blocked",
    "failed",
    "merged",
    "closed",
]
REVIEW_READY_STATES = {"completed", "needs-review"}
ACTIVE_STATES = {"running"}
QUEUE_STATES = {"planned", "queued", "needs-review"}
DONE_STATES = {"reviewed", "merged", "closed"}
BLOCKED_STATES = {"blocked", "failed"}
IMPACT_PROGRESS_WEIGHTS = {
    "planned": 0.15,
    "queued": 0.20,
    "running": 0.50,
    "completed": 0.85,
    "needs-review": 0.90,
    "reviewed": 1.00,
    "merged": 1.00,
    "closed": 1.00,
    "blocked": 0.35,
    "failed": 0.25,
}
RECIPES = {
    "explorer-swarm": {
        "title": "Quick Code Search",
        "purpose": "Answer separate code questions before building.",
        "agentType": "explorer",
        "defaultStatus": "queued",
        "outputs": ["specific findings", "file references", "risks", "recommended next work items"],
    },
    "implementation-workers": {
        "title": "Build Workers",
        "purpose": "Split code changes into separate work areas.",
        "agentType": "worker",
        "defaultStatus": "queued",
        "outputs": ["changed files", "tests run", "blockers", "next step"],
    },
    "test-fix-wave": {
        "title": "Test Fix Group",
        "purpose": "Assign focused test failures or build failures to workers.",
        "agentType": "worker",
        "defaultStatus": "queued",
        "outputs": ["failing command", "cause", "patch", "check command"],
    },
    "pr-review-response": {
        "title": "PR Comment Fix Group",
        "purpose": "Fix separate PR review comments without overlapping edits.",
        "agentType": "worker",
        "defaultStatus": "queued",
        "outputs": ["comment fixed", "files changed", "review note", "tests"],
    },
    "migration-refactor-split": {
        "title": "Migration Split",
        "purpose": "Split a migration by package, API area, or data model.",
        "agentType": "worker",
        "defaultStatus": "queued",
        "outputs": ["area migrated", "compatibility notes", "tests", "follow-up risks"],
    },
    "bug-investigation-ladder": {
        "title": "Bug Investigation Steps",
        "purpose": "Run reproduce, trace, likely cause, fix, and retest steps.",
        "agentType": "default",
        "defaultStatus": "queued",
        "outputs": ["reproduction", "suspected cause", "patch path", "retest proof"],
    },
    "release-readiness-matrix": {
        "title": "Release Checklist",
        "purpose": "Assign release checks such as installer, smoke test, rollback, docs, and parity.",
        "agentType": "worker",
        "defaultStatus": "queued",
        "outputs": ["check status", "evidence", "blockers", "release advice"],
    },
}
HEARTBEAT_CONTRACT_TEMPLATE = """Dashboard heartbeat contract

You are part of a multi-agent Codex run with a live local dashboard.
Publish public status updates only. Do not include private reasoning, secrets, credentials, or raw personal data.

Agent protocol:
- Ownership scope: {ownership}
- Allowed files/modules: {allowed_files}
- Do not touch: {do_not_touch}
- Expected outputs: {expected_outputs}
- Heartbeat cadence: {heartbeat_cadence}
- Test expectations: {test_expectations}
- Handoff format: changed files, verification, blockers, next owner.
- Blocker format: smallest actionable blocker, evidence, and suggested owner.

Use this command when your public activity changes:
py -3 C:\\Users\\Piculiar\\.codex\\skills\\agent-dashboard\\scripts\\agent_dashboard.py --keep-existing --event "{agent_name}|read|Reading the relevant code and docs|<specific files/modules>"

Use this command when your agent row should change:
py -3 C:\\Users\\Piculiar\\.codex\\skills\\agent-dashboard\\scripts\\agent_dashboard.py --keep-existing --agent "{agent_name}|{agent_id}|running|<current public summary>|<owned files/modules>|<changed files separated by ;>|<tests>|<blockers>|<handoff>"

Expected heartbeat moments:
- When you start and identify your ownership scope.
- After meaningful reads that change direction.
- After edits, with files or modules named.
- When tests/checks start and finish.
- When blocked, with the smallest useful blocker.
- At final handoff, with changed files, verification, blockers, and next owner.

Recommended event kinds: read, edit, test, blocked, handoff, status.
Keep each heartbeat one line and user-facing.
"""


class ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


class LiveDashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, status_path: pathlib.Path, **kwargs):
        self.status_path = status_path
        super().__init__(*args, **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_GET(self) -> None:
        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/api/status":
            payload = load_existing(self.status_path)
            body = json.dumps(ensure_control_plane(payload), indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        route = route_from_path(clean_path)
        if route:
            payload = load_existing(self.status_path)
            view, agent_ref = route
            body = render_html(payload, view=view, agent_ref=agent_ref).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        super().do_GET()

    def do_POST(self) -> None:
        clean_path = self.path.split("?", 1)[0]
        if clean_path != "/action":
            self.send_error(404, "Unknown dashboard action")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8")
        form = urllib.parse.parse_qs(body)
        redirect_to = form_value(form, "returnTo") or self.headers.get("Referer") or "/overview"
        action = form_value(form, "action")
        agent_key = form_value(form, "agent")
        command_id = form_value(form, "command")
        command_state = form_value(form, "state")
        note = form_value(form, "note")

        with status_lock(self.status_path):
            payload = ensure_control_plane(load_existing(self.status_path))
            if action == "set-command-state":
                set_command_state(payload, command_id, command_state or "dismissed", note)
            else:
                handle_control_action(payload, action, agent_key, note)
            write_payload(self.status_path, payload)

        self.send_response(303)
        self.send_header("Location", redirect_to)
        self.end_headers()


def parse_agent(raw: str) -> dict:
    fields = [field.strip() for field in raw.split("|")]
    fields += [""] * (9 - len(fields))
    name, agent_id, status, summary, ownership, changed_files, tests, blockers, handoff = fields[:9]
    return {
        "name": name or "Unnamed agent",
        "id": agent_id,
        "status": (status or "running").lower(),
        "summary": summary,
        "ownership": ownership,
        "changedFiles": split_list(changed_files),
        "tests": tests,
        "blockers": blockers,
        "handoff": handoff,
        "updatedAt": utc_now(),
    }


def parse_planned_agent(raw: str) -> dict:
    fields = [field.strip() for field in raw.split("|")]
    fields += [""] * (11 - len(fields))
    name, summary, ownership, allowed_files, do_not_touch, expected_outputs, tests, priority, wave, recipe, status = fields[:11]
    planned_status = (status or "planned").lower()
    return normalize_agent(
        {
            "name": name or "Unnamed planned agent",
            "id": "",
            "status": planned_status if planned_status in LIFECYCLE_STATES else "planned",
            "summary": summary,
            "ownership": ownership,
            "allowedFiles": split_list(allowed_files),
            "writeGlobs": split_list(allowed_files),
            "doNotTouch": split_list(do_not_touch),
            "expectedOutputs": split_list(expected_outputs),
            "changedFiles": [],
            "tests": tests,
            "blockers": "",
            "handoff": "",
            "priority": priority,
            "wave": wave,
            "recipe": recipe,
            "updatedAt": utc_now(),
        }
    )


def parse_event(raw: str) -> dict:
    fields = [field.strip() for field in raw.split("|")]
    fields += [""] * (5 - len(fields))
    agent, kind, message, detail, timestamp = fields[:5]
    return {
        "agent": agent,
        "kind": (kind or "status").lower(),
        "message": message,
        "detail": detail,
        "timestamp": timestamp or utc_now(),
    }


def split_list(value: str) -> list[str]:
    if not value:
        return []
    separators = [";", ","]
    items = [value]
    for separator in separators:
        if separator in value:
            items = value.split(separator)
            break
    return [item.strip() for item in items if item.strip()]


def text_from_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value).strip()


def list_from_value(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return split_list(text) if text else []


def positive_int(value: object, default: int = 0) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def bool_from_value(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on", "read-only", "readonly"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def first_value(data: dict, keys: list[str], default: object = "") -> object:
    for key in keys:
        if key in data and data.get(key) is not None:
            value = data.get(key)
            if not (isinstance(value, str) and not value.strip()):
                return value
    return default


def load_json_value(source: str, *, force_file: bool = False) -> object:
    source_text = str(source or "").strip()
    if not source_text:
        raise ValueError("Empty JSON input")

    path_text = source_text[1:] if source_text.startswith("@") else source_text
    read_file = force_file or source_text.startswith("@")
    if not read_file and not source_text.lstrip().startswith(("{", "[")):
        try:
            read_file = pathlib.Path(path_text).expanduser().exists()
        except OSError:
            read_file = False

    label = path_text if read_file else "inline JSON"
    try:
        text = pathlib.Path(path_text).expanduser().read_text(encoding="utf-8") if read_file else source_text
    except OSError as exc:
        raise ValueError(f"Could not read JSON file {path_text}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON input from {label}: {exc.msg}") from exc


def json_records_from_inputs(
    raw_values: list[str],
    file_values: list[str],
    container_keys: list[str],
) -> list[dict]:
    records: list[dict] = []
    for value in raw_values:
        records.extend(extract_json_records(load_json_value(value), container_keys))
    for value in file_values:
        records.extend(extract_json_records(load_json_value(value, force_file=True), container_keys))
    return records


def extract_json_records(value: object, container_keys: list[str]) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in container_keys:
            child = value.get(key)
            if isinstance(child, list):
                return [item for item in child if isinstance(item, dict)]
            if isinstance(child, dict):
                return [child]
        return [value]
    raise ValueError("JSON input must be an object, an array of objects, or an object containing a record array")


def parse_agent_json(data: dict) -> dict:
    agent = dict(data)
    name = text_from_value(first_value(data, ["name", "agent", "agentName"], "Unnamed agent"))
    agent_id = text_from_value(first_value(data, ["id", "agentId", "threadId", "sessionId"], ""))
    status = text_from_value(first_value(data, ["status", "lifecycle"], "running")).lower()
    agent["name"] = name or "Unnamed agent"
    agent["id"] = agent_id
    agent["status"] = status or "running"
    agent["summary"] = text_from_value(first_value(data, ["summary", "message", "title"], agent.get("summary", "")))
    agent["ownership"] = text_from_value(first_value(data, ["ownership", "scope", "owner"], agent.get("ownership", "")))
    agent["changedFiles"] = list_from_value(first_value(data, ["changedFiles", "changed_files", "filesChanged", "files"], agent.get("changedFiles", [])))
    agent["allowedFiles"] = list_from_value(first_value(data, ["allowedFiles", "allowed_files", "allowed", "allowedGlobs"], agent.get("allowedFiles", [])))
    agent["writeGlobs"] = list_from_value(first_value(data, ["writeGlobs", "write_globs", "writeScope", "write_scope"], agent.get("writeGlobs", [])))
    agent["doNotTouch"] = list_from_value(first_value(data, ["doNotTouch", "do_not_touch", "blockedFiles", "blocked_files"], agent.get("doNotTouch", [])))
    agent["expectedOutputs"] = list_from_value(first_value(data, ["expectedOutputs", "expected_outputs", "outputs"], agent.get("expectedOutputs", [])))
    agent["tests"] = text_from_value(first_value(data, ["tests", "testsRun", "verification", "checks"], agent.get("tests", "")))
    agent["blockers"] = text_from_value(first_value(data, ["blockers", "blocker", "blockerStatus", "risks"], agent.get("blockers", "")))
    agent["handoff"] = text_from_value(first_value(data, ["handoff", "nextHandoff", "next_owner", "nextOwner"], agent.get("handoff", "")))
    agent["readOnly"] = bool_from_value(first_value(data, ["readOnly", "read_only", "readonly", "readMode"], agent.get("readOnly", False)))
    manual_minutes = positive_int(first_value(data, ["manualMinutes", "manual_minutes", "estimatedManualMinutes", "estimated_manual_minutes"], agent.get("manualMinutes", 0)))
    coordination_minutes = positive_int(first_value(data, ["coordinationMinutes", "coordination_minutes", "agentMinutes", "agent_minutes"], agent.get("coordinationMinutes", 0)))
    if manual_minutes:
        agent["manualMinutes"] = manual_minutes
    if coordination_minutes:
        agent["coordinationMinutes"] = coordination_minutes
    agent["updatedAt"] = text_from_value(first_value(data, ["updatedAt", "timestamp", "reportedAt"], agent.get("updatedAt", ""))) or utc_now()
    if data.get("noBlockers") is True and not agent.get("blockers"):
        agent["blockers"] = "None reported"
    return normalize_agent(agent)


def parse_planned_agent_json(data: dict) -> dict:
    planned = dict(data)
    if not text_from_value(first_value(planned, ["status", "lifecycle"], "")):
        planned["status"] = "planned"
    agent = parse_agent_json(planned)
    if agent_status(agent) not in LIFECYCLE_STATES:
        agent["status"] = "planned"
    return normalize_agent(agent)


def parse_event_json(data: dict) -> dict:
    return {
        "agent": text_from_value(first_value(data, ["agent", "agentName", "name"], "")),
        "kind": text_from_value(first_value(data, ["kind", "type", "status"], "status")).lower(),
        "message": text_from_value(first_value(data, ["message", "summary", "title"], "")),
        "detail": text_from_value(first_value(data, ["detail", "details", "body"], "")),
        "timestamp": text_from_value(first_value(data, ["timestamp", "updatedAt", "createdAt"], "")) or utc_now(),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def status_lock(path: pathlib.Path, timeout_seconds: float = 10.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout_seconds
    handle = None
    while handle is None:
        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, f"{os.getpid()} {utc_now()}".encode("utf-8"))
        except FileExistsError:
            try:
                is_stale = time.time() - lock_path.stat().st_mtime > 60
                if is_stale:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for dashboard status lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if handle is not None:
            os.close(handle)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def load_existing(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"version": 1, "title": "Codex Agent Dashboard", "agents": []}
    try:
        return ensure_control_plane(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "title": "Codex Agent Dashboard", "agents": []}


def write_payload(path: pathlib.Path, payload: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp_path, path)
    return payload


def write_status(path: pathlib.Path, title: str, agents: list[dict], events: list[dict], base: dict | None = None) -> dict:
    payload = ensure_control_plane(base or {})
    payload.update({
        "version": 1,
        "schemaVersion": 2,
        "title": title,
        "generatedAt": utc_now(),
        "note": "Private reasoning is not shown. This page shows each agent's status, actions taken, changed files, tests, blockers, and next steps.",
        "agents": agents,
        "events": events,
    })
    return write_payload(path, ensure_control_plane(payload))


def form_value(form: dict[str, list[str]], key: str, default: str = "") -> str:
    values = form.get(key)
    if not values:
        return default
    return values[0].strip()


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-").lower()
    return cleaned or hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def normalize_list_field(agent: dict, key: str) -> None:
    value = agent.get(key)
    agent[key] = list_from_value(value)


def glob_scope_root(pattern: str) -> str:
    normalized = normalize_path_for_match(pattern).strip().lstrip("./")
    if not normalized:
        return ""
    wildcard_index = min([idx for idx in [normalized.find("*"), normalized.find("?"), normalized.find("[")] if idx >= 0] or [-1])
    if wildcard_index >= 0:
        normalized = normalized[:wildcard_index]
    normalized = normalized.rstrip("/")
    if "/" in normalized and not pathlib.PurePosixPath(normalized).suffix:
        return normalized
    if "/" in normalized:
        return normalized.rsplit("/", 1)[0]
    return normalized


def scopes_overlap(left: str, right: str) -> bool:
    left_root = glob_scope_root(left)
    right_root = glob_scope_root(right)
    if not left_root or not right_root:
        return False
    return (
        left_root == right_root
        or right_root.startswith(left_root.rstrip("/") + "/")
        or left_root.startswith(right_root.rstrip("/") + "/")
    )


def active_for_ownership(agent: dict) -> bool:
    return agent_status(agent) not in {"reviewed", "merged", "closed", "failed"}


def agent_is_read_only(agent: dict) -> bool:
    return bool_from_value(agent.get("readOnly"), False)


def refresh_ownership_warnings(agents: list[dict]) -> None:
    for agent in agents:
        warnings: list[str] = []
        if active_for_ownership(agent) and not agent_is_read_only(agent) and not agent.get("writeGlobs"):
            warnings.append("allowed edit paths are missing")
        if active_for_ownership(agent) and agent_is_read_only(agent) and not (agent.get("allowedFiles") or agent.get("ownership")):
            warnings.append("read-only agent is missing the files it may read")
        agent["ownershipWarnings"] = warnings

    for index, agent in enumerate(agents):
        if not active_for_ownership(agent) or agent_is_read_only(agent):
            continue
        write_globs = agent.get("writeGlobs") if isinstance(agent.get("writeGlobs"), list) else []
        for other in agents[index + 1:]:
            if not active_for_ownership(other) or agent_is_read_only(other):
                continue
            other_globs = other.get("writeGlobs") if isinstance(other.get("writeGlobs"), list) else []
            for left in write_globs:
                for right in other_globs:
                    if scopes_overlap(str(left), str(right)):
                        message = f"edit path overlaps with {other.get('name')}: {left} <> {right}"
                        other_message = f"edit path overlaps with {agent.get('name')}: {right} <> {left}"
                        if message not in agent["ownershipWarnings"]:
                            agent["ownershipWarnings"].append(message)
                        if other_message not in other["ownershipWarnings"]:
                            other["ownershipWarnings"].append(other_message)


def normalize_agent(agent: dict) -> dict:
    status = agent_status(agent)
    if status not in LIFECYCLE_STATES:
        status = "running"
    agent["status"] = status
    agent.setdefault("lifecycle", status)
    agent.setdefault("priority", "")
    agent.setdefault("wave", "")
    agent.setdefault("recipe", "")
    agent.setdefault("allowedFiles", [])
    agent.setdefault("writeGlobs", [])
    agent.setdefault("doNotTouch", [])
    agent.setdefault("expectedOutputs", [])
    agent["readOnly"] = agent_is_read_only(agent)
    for list_key in ["allowedFiles", "writeGlobs", "doNotTouch", "expectedOutputs", "changedFiles", "ownershipWarnings"]:
        normalize_list_field(agent, list_key)
    if not agent.get("writeGlobs") and agent.get("allowedFiles"):
        agent["writeGlobs"] = list(agent.get("allowedFiles", []))
    agent.setdefault("heartbeatCadence", "on start, meaningful read, edit, test, blocker, and final update")
    agent.setdefault("testExpectations", agent.get("tests") or "")
    agent.setdefault("review", {"state": review_state_for(status), "notes": "", "reviewedAt": ""})
    agent.setdefault("worktree", {})
    agent.setdefault("commands", [])
    if agent.get("manualMinutes"):
        agent["manualMinutes"] = positive_int(agent.get("manualMinutes"), 0)
    if agent.get("coordinationMinutes"):
        agent["coordinationMinutes"] = positive_int(agent.get("coordinationMinutes"), 0)
    return agent


def review_state_for(status: str) -> str:
    if status in {"completed", "needs-review"}:
        return "pending"
    if status in {"reviewed", "merged", "closed"}:
        return "passed"
    if status in {"blocked", "failed"}:
        return "blocked"
    return "not-ready"


def ensure_control_plane(payload: dict) -> dict:
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("version", 1)
    payload.setdefault("schemaVersion", 2)
    payload.setdefault("title", "Codex Agent Dashboard")
    payload.setdefault("generatedAt", utc_now())
    payload.setdefault("note", "Private reasoning is not shown. This page shows each agent's status, actions taken, changed files, tests, blockers, and next steps.")
    payload.setdefault("agents", [])
    payload.setdefault("events", [])
    payload.setdefault("commands", [])
    payload.setdefault("memoryExports", [])
    payload.setdefault("recipes", RECIPES)
    payload.setdefault("impact", {})
    payload.setdefault("workflow", {})
    impact = payload["impact"] if isinstance(payload.get("impact"), dict) else {}
    impact.setdefault("manualMinutesPerAgent", DEFAULT_MANUAL_MINUTES_PER_AGENT)
    impact.setdefault("coordinationMinutesPerAgent", DEFAULT_COORDINATION_MINUTES_PER_AGENT)
    impact.setdefault("focusBlockMinutes", DEFAULT_FOCUS_BLOCK_MINUTES)
    impact.setdefault("note", "Estimated from agent work items, status, and time spent coordinating.")
    payload["impact"] = impact
    workflow = payload["workflow"] if isinstance(payload.get("workflow"), dict) else {}
    workflow.setdefault("objective", "")
    workflow.setdefault("status", "active")
    workflow.setdefault("concurrencyLimit", DEFAULT_CONCURRENCY_LIMIT)
    workflow.setdefault("activeWave", "")
    workflow.setdefault("worktree", "")
    workflow.setdefault("createdAt", payload.get("generatedAt") or utc_now())
    payload["workflow"] = workflow
    payload["agents"] = [normalize_agent(agent) for agent in payload.get("agents", []) if isinstance(agent, dict)]
    refresh_ownership_warnings(payload["agents"])
    payload["events"] = [event for event in payload.get("events", []) if isinstance(event, dict)]
    payload["commands"] = [command for command in payload.get("commands", []) if isinstance(command, dict)]
    return payload


def heartbeat_contract(
    agent_name: str,
    agent_id: str,
    ownership: str = "specified in your spawn prompt",
    allowed_files: str = "specified in your spawn prompt",
    do_not_touch: str = "anything outside your ownership scope",
    expected_outputs: str = "changed files, verification, blockers, and next step",
    heartbeat_cadence: str = "on start, meaningful read, edit, test, blocker, and final update",
    test_expectations: str = "run focused checks for your scope or report why blocked",
) -> str:
    return HEARTBEAT_CONTRACT_TEMPLATE.format(
        agent_name=agent_name or "AGENT_NAME",
        agent_id=agent_id,
        ownership=ownership,
        allowed_files=allowed_files,
        do_not_touch=do_not_touch,
        expected_outputs=expected_outputs,
        heartbeat_cadence=heartbeat_cadence,
        test_expectations=test_expectations,
    )


def merge_agents(existing_agents: list[object], new_agents: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    name_to_key: dict[str, str] = {}

    for agent in existing_agents:
        if not isinstance(agent, dict):
            continue
        key = str(agent.get("id") or agent.get("name") or "")
        if not key:
            continue
        merged[key] = agent
        name = str(agent.get("name") or "").strip().lower()
        if name:
            name_to_key[name] = key

    for agent in new_agents:
        key = str(agent.get("id") or agent.get("name") or "")
        if not key:
            continue
        name = str(agent.get("name") or "").strip().lower()
        old_key = name_to_key.get(name) if name else None
        if old_key and old_key != key:
            old_agent = merged.pop(old_key, {})
            merged[key] = {**old_agent, **agent}
        else:
            merged[key] = agent
        if name:
            name_to_key[name] = key

    return list(merged.values())


def add_event(payload: dict, agent: str, kind: str, message: str, detail: str = "") -> None:
    payload.setdefault("events", []).append(
        {
            "agent": agent,
            "kind": kind,
            "message": message,
            "detail": detail,
            "timestamp": utc_now(),
        }
    )


def add_command(payload: dict, agent: str, kind: str, message: str, prompt: str = "") -> None:
    payload.setdefault("commands", []).append(
        {
            "id": hashlib.sha1(f"{agent}|{kind}|{message}|{utc_now()}".encode("utf-8")).hexdigest()[:12],
            "agent": agent,
            "kind": kind,
            "message": message,
            "prompt": prompt,
            "createdAt": utc_now(),
            "state": "pending",
        }
    )


def set_agent_status(agent: dict, status: str, note: str = "") -> None:
    status = status if status in LIFECYCLE_STATES else agent_status(agent)
    agent["status"] = status
    agent["lifecycle"] = status
    agent["updatedAt"] = utc_now()
    review = agent.get("review") if isinstance(agent.get("review"), dict) else {}
    review["state"] = review_state_for(status)
    if note:
        review["notes"] = note
    if status in {"reviewed", "merged", "closed"}:
        review["reviewedAt"] = review.get("reviewedAt") or utc_now()
    agent["review"] = review


def review_gate_issues(agent: dict) -> list[str]:
    issues: list[str] = []
    changed_files = agent.get("changedFiles") if isinstance(agent.get("changedFiles"), list) else []
    if not agent_is_read_only(agent) and not changed_files:
        issues.append("changed files are missing")
    if not text_from_value(agent.get("tests")):
        issues.append("tests or checks are missing")
    if not text_from_value(agent.get("blockers")):
        issues.append("blocker status is missing (write none if nothing is stuck)")
    if not text_from_value(agent.get("handoff")):
        issues.append("next step is missing")
    return issues


def mark_agent_reviewed(payload: dict, agent: dict, note: str = "") -> bool:
    issues = review_gate_issues(agent)
    review = agent.get("review") if isinstance(agent.get("review"), dict) else {}
    if issues:
        review["state"] = "blocked"
        review["gateIssues"] = issues
        review["notes"] = note or "Cannot mark checked until the missing details are added."
        agent["review"] = review
        set_agent_status(agent, "needs-review", review["notes"])
        agent["review"]["state"] = "blocked"
        agent["review"]["gateIssues"] = issues
        detail = "; ".join(issues)
        add_event(payload, agent.get("name") or "agent", "review", "Review needs more details", detail)
        add_command(
            payload,
            agent.get("name") or "agent",
            "request-evidence",
            "Add missing review details before marking checked",
            "Ask the agent for changed files, tests/checks, blocker status, and the next step.",
        )
        return False
    review["state"] = "passed"
    review["gateIssues"] = []
    agent["review"] = review
    set_agent_status(agent, "reviewed", note)
    add_event(payload, agent.get("name") or "agent", "reviewed", "Marked checked", note)
    return True


def reconcile_agent_id(payload: dict, agent_ref_value: str, actual_id: str, actual_name: str = "") -> dict | None:
    agent = find_agent(payload.get("agents", []), agent_ref_value)
    if not agent and actual_name:
        agent = find_agent(payload.get("agents", []), actual_name)
    if not actual_id.strip():
        add_event(payload, "Orchestrator", "blocked", "Agent session ID link skipped", "missing session ID")
        return None
    if not agent:
        agent = normalize_agent(
            {
                "name": actual_name or agent_ref_value or "Spawned agent",
                "id": actual_id.strip(),
                "status": "running",
                "summary": "Agent session ID linked after launch.",
                "ownership": "",
                "updatedAt": utc_now(),
            }
        )
        payload.setdefault("agents", []).append(agent)
    prior_id = str(agent.get("id") or "").strip()
    aliases = list_from_value(agent.get("aliases"))
    for alias in [agent_ref_value, prior_id]:
        alias = str(alias or "").strip()
        if alias and alias not in aliases and alias != actual_id:
            aliases.append(alias)
    if actual_name:
        agent["name"] = actual_name
    agent["id"] = actual_id.strip()
    agent["aliases"] = aliases
    agent["updatedAt"] = utc_now()
    add_event(payload, agent.get("name") or actual_id, "status", "Linked agent session ID", actual_id.strip())
    return agent


def ingest_final_report(payload: dict, report: dict) -> dict:
    update = parse_agent_json(report)
    reported_status = text_from_value(first_value(report, ["status", "lifecycle"], "")).lower()
    final_status = reported_status if reported_status in {"blocked", "failed", "needs-review"} else "needs-review"
    update["status"] = final_status

    agent = find_agent(payload.get("agents", []), update.get("id") or "") or find_agent(payload.get("agents", []), update.get("name") or "")
    if not agent:
        agent = update
        payload.setdefault("agents", []).append(agent)
        set_agent_status(agent, final_status, "Final report added; waiting for lead review.")
    else:
        for key in [
            "name",
            "id",
            "summary",
            "ownership",
            "tests",
            "blockers",
            "handoff",
            "priority",
            "wave",
            "recipe",
        ]:
            value = update.get(key)
            if text_from_value(value):
                agent[key] = value
        if any(key in report for key in ["readOnly", "read_only", "readonly", "readMode"]):
            agent["readOnly"] = agent_is_read_only(update)
        for key in ["changedFiles", "allowedFiles", "writeGlobs", "doNotTouch", "expectedOutputs"]:
            value = update.get(key)
            if isinstance(value, list) and value:
                agent[key] = value
        set_agent_status(agent, final_status, "Final report added; waiting for lead review.")

    agent["lastFinalReportAt"] = utc_now()
    agent["finalReport"] = {key: value for key, value in report.items() if key != "events"}
    agent["updatedAt"] = utc_now()
    add_event(payload, agent.get("name") or update.get("id") or "agent", "handoff", "Final report added", agent.get("summary") or "")

    report_events = report.get("events")
    if isinstance(report_events, list):
        for event in report_events:
            if isinstance(event, dict):
                payload.setdefault("events", []).append(parse_event_json(event))
    return agent


def parse_datetime_value(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def age_label(minutes: float) -> str:
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = int(minutes // 60)
    remainder = int(minutes % 60)
    return f"{hours}h {remainder}m" if remainder else f"{hours}h"


def last_agent_activity_at(payload: dict, agent: dict) -> datetime | None:
    candidates = [
        parse_datetime_value(agent.get("updatedAt")),
        parse_datetime_value(agent.get("lastFinalReportAt")),
    ]
    for event in payload.get("events", []):
        if isinstance(event, dict) and event_matches_agent(event, agent):
            candidates.append(parse_datetime_value(event.get("timestamp")))
    parsed = [candidate for candidate in candidates if candidate is not None]
    return max(parsed) if parsed else None


def stale_agent_warnings(payload: dict, agent: dict) -> list[str]:
    warnings: list[str] = []
    status = agent_status(agent)
    threshold = DEFAULT_STALE_MINUTES.get(status)
    if threshold:
        last_seen = last_agent_activity_at(payload, agent)
        if not last_seen:
            warnings.append(f"{plain_label(status, STATUS_LABELS)} has no time-stamped update")
        else:
            age_minutes = (datetime.now().astimezone() - last_seen).total_seconds() / 60
            if age_minutes > threshold:
                warnings.append(f"{plain_label(status, STATUS_LABELS)} has not posted an update for {age_label(age_minutes)}")
    if status == "running" and not text_from_value(agent.get("id")):
        warnings.append("running agent is missing its session ID")
    if status in {"completed", "needs-review"} and not text_from_value(agent.get("lastFinalReportAt")):
        warnings.append("final report has not been added")
    return warnings


def dashboard_warnings(payload: dict, agents: list[dict]) -> list[str]:
    warnings: list[str] = []
    for agent in agents:
        name = str(agent.get("name") or agent.get("id") or "agent")
        for warning in stale_agent_warnings(payload, agent):
            warnings.append(f"{name}: {warning}")
        for warning in agent.get("ownershipWarnings", []) if isinstance(agent.get("ownershipWarnings"), list) else []:
            warnings.append(f"{name}: {warning}")
        if agent_status(agent) == "reviewed":
            issues = review_gate_issues(agent)
            if issues:
                warnings.append(f"{name}: marked reviewed but details are missing ({'; '.join(issues)})")
    return warnings


def pending_commands(payload: dict) -> list[dict]:
    return [
        command for command in payload.get("commands", [])
        if isinstance(command, dict) and str(command.get("state") or "pending").lower() == "pending"
    ]


def command_age_minutes(command: dict) -> float | None:
    created_at = parse_datetime_value(command.get("createdAt"))
    if not created_at:
        return None
    return (datetime.now().astimezone() - created_at).total_seconds() / 60


def set_command_state(payload: dict, command_id: str, state: str, note: str = "") -> bool:
    allowed_states = {"pending", "done", "dismissed", "failed"}
    normalized_state = state.strip().lower()
    if normalized_state not in allowed_states:
        normalized_state = "dismissed"
    for command in payload.get("commands", []):
        if not isinstance(command, dict):
            continue
        if str(command.get("id") or "") == command_id:
            command["state"] = normalized_state
            command["updatedAt"] = utc_now()
            if note:
                command["note"] = note
            add_event(
                payload,
                str(command.get("agent") or "Orchestrator"),
                "status",
                f"Action marked {plain_label(normalized_state)}",
                note or str(command.get("message") or command_id),
            )
            return True
    add_event(payload, "Orchestrator", "blocked", "Action update ignored", f"Unknown action id: {command_id}")
    return False


def health_summary(payload: dict) -> dict:
    payload = ensure_control_plane(payload)
    agents = payload.get("agents", [])
    counts = count_agents(agents)
    warnings = dashboard_warnings(payload, agents)
    final_report_gaps = [
        agent for agent in agents
        if agent_status(agent) in REVIEW_READY_STATES and not text_from_value(agent.get("lastFinalReportAt"))
    ]
    write_scope_gaps = [
        agent for agent in agents
        if active_for_ownership(agent) and not agent_is_read_only(agent) and not agent.get("writeGlobs")
    ]
    missing_ids = [
        agent for agent in agents
        if agent_status(agent) in {"running", "completed", "needs-review"} and not text_from_value(agent.get("id"))
    ]
    commands = pending_commands(payload)
    stale_commands = [
        command for command in commands
        if (command_age_minutes(command) or 0) >= 60
    ]
    blocker_map: dict[str, list[str]] = {}
    for agent in agents:
        blocker = text_from_value(agent.get("blockers"))
        if not blocker or blocker.lower() in {"none", "none reported", "no blockers"}:
            continue
        blocker_map.setdefault(blocker, []).append(str(agent.get("name") or agent.get("id") or "agent"))
    return {
        "counts": counts,
        "warningCount": len(warnings),
        "warnings": warnings,
        "finalReportGaps": final_report_gaps,
        "writeScopeGaps": write_scope_gaps,
        "missingIds": missing_ids,
        "pendingCommands": commands,
        "staleCommands": stale_commands,
        "blockers": blocker_map,
        "impact": compute_impact(payload, agents, counts),
        "worktree": payload.get("worktree", {}),
        "workflow": payload.get("workflow", {}),
    }


def render_doctor_report(payload: dict) -> str:
    summary = health_summary(payload)
    counts = summary["counts"]
    impact = summary["impact"]
    workflow = summary["workflow"] if isinstance(summary.get("workflow"), dict) else {}
    worktree = summary["worktree"] if isinstance(summary.get("worktree"), dict) else {}
    lines = [
        "# Dashboard Health Check",
        f"- Objective: {workflow.get('objective') or 'Not recorded'}",
        f"- Agents: {sum(counts.values())}",
        f"- Status counts: " + ", ".join(f"{plain_label(state, STATUS_LABELS)}={count}" for state, count in counts.items() if count),
        f"- Warnings: {summary['warningCount']}",
        f"- Waiting actions: {len(summary['pendingCommands'])}",
        f"- Old waiting actions: {len(summary['staleCommands'])}",
        f"- Estimated saved time: {format_minutes(int(impact.get('savedMinutes', 0)))} ({impact.get('rank')})",
    ]
    if worktree:
        changed = "yes" if worktree.get("uncommitted") else "no"
        lines.append(f"- Work folder: {worktree.get('path') or 'not scanned'}; has uncommitted changes: {changed}")

    lines.extend(["", "## Missing Info"])
    gap_lines = [
        ("Final reports needed", summary["finalReportGaps"]),
        ("Edit paths needed", summary["writeScopeGaps"]),
        ("Session IDs needed", summary["missingIds"]),
    ]
    for label, agents in gap_lines:
        if agents:
            names = ", ".join(str(agent.get("name") or agent.get("id") or "agent") for agent in agents[:12])
            extra = f" (+{len(agents) - 12} more)" if len(agents) > 12 else ""
            lines.append(f"- {label}: {names}{extra}")
        else:
            lines.append(f"- {label}: none")

    if summary["warnings"]:
        lines.extend(["", "## Warnings"])
        for warning in summary["warnings"][:20]:
            lines.append(f"- {warning}")
        if len(summary["warnings"]) > 20:
            lines.append(f"- ...{len(summary['warnings']) - 20} more")

    if summary["pendingCommands"]:
        lines.extend(["", "## Waiting Actions"])
        for command in summary["pendingCommands"][:12]:
            age = command_age_minutes(command)
            age_text = age_label(age) if age is not None else "unknown age"
            lines.append(f"- {command.get('id')}: {command.get('agent')} / {plain_label(command.get('kind'), KIND_LABELS)} / {age_text} / {command.get('message')}")

    if summary["blockers"]:
        lines.extend(["", "## What Is Stuck"])
        for blocker, agents in list(summary["blockers"].items())[:12]:
            owner_text = ", ".join(agents[:4])
            extra = f" (+{len(agents) - 4} more)" if len(agents) > 4 else ""
            lines.append(f"- {owner_text}{extra}: {blocker}")

    lines.extend(["", "## Suggested Next Steps"])
    if summary["finalReportGaps"]:
        first = summary["finalReportGaps"][0]
        lines.append(f"- Make a final report template: --print-final-report-template \"{first.get('name') or first.get('id')}\"")
    if summary["writeScopeGaps"]:
        first = summary["writeScopeGaps"][0]
        lines.append(f"- Add edit paths or mark readOnly=true for: {first.get('name') or first.get('id')}")
    if summary["staleCommands"]:
        first = summary["staleCommands"][0]
        lines.append(f"- Clear old waiting action: --set-command-state \"{first.get('id')}|dismissed|superseded or completed\"")
    if not (summary["finalReportGaps"] or summary["writeScopeGaps"] or summary["staleCommands"]):
        lines.append("- Nothing needs fixing right now; keep updates and final reports current.")
    return "\n".join(lines).strip() + "\n"


def build_final_report_template(payload: dict, agent_ref_value: str) -> dict:
    payload = ensure_control_plane(payload)
    agent = find_agent(payload.get("agents", []), agent_ref_value) if agent_ref_value else None
    if not agent:
        agent = normalize_agent({"name": agent_ref_value or "AGENT_NAME", "status": "completed"})
    return {
        "name": agent.get("name") or agent_ref_value or "AGENT_NAME",
        "id": agent.get("id") or "",
        "status": "completed",
        "summary": agent.get("summary") or "<public result summary>",
        "ownership": agent.get("ownership") or "<owned files/modules>",
        "readOnly": agent_is_read_only(agent),
        "changedFiles": agent.get("changedFiles") if isinstance(agent.get("changedFiles"), list) else [],
        "allowedFiles": agent.get("allowedFiles") if isinstance(agent.get("allowedFiles"), list) else [],
        "writeGlobs": agent.get("writeGlobs") if isinstance(agent.get("writeGlobs"), list) else [],
        "doNotTouch": agent.get("doNotTouch") if isinstance(agent.get("doNotTouch"), list) else [],
        "expectedOutputs": agent.get("expectedOutputs") if isinstance(agent.get("expectedOutputs"), list) else ["changed files", "tests", "blockers", "next step"],
        "tests": agent.get("tests") or "<verification run or reason tests were not run>",
        "blockers": agent.get("blockers") or "None reported",
        "handoff": agent.get("handoff") or "<next owner or action>",
        "events": [
            {
                "agent": agent.get("name") or agent_ref_value or "AGENT_NAME",
                "kind": "handoff",
                "message": "Final report ready",
                "detail": "Changed files, verification, blockers, and next step provided.",
            }
        ],
    }


def archive_run_snapshot(payload: dict, snapshot_dir: pathlib.Path = DEFAULT_SNAPSHOT_DIR) -> pathlib.Path:
    payload = ensure_control_plane(payload)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = snapshot_dir / f"agent-status-{stamp}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload.setdefault("snapshots", []).append({"path": str(path), "timestamp": utc_now()})
    payload["generatedAt"] = utc_now()
    add_event(payload, "Orchestrator", "handoff", "Saved dashboard snapshot", str(path))
    return path


def promote_next_agents(payload: dict, note: str = "") -> int:
    workflow = payload.get("workflow", {})
    limit = int(workflow.get("concurrencyLimit") or DEFAULT_CONCURRENCY_LIMIT)
    running = len(filter_agents(payload.get("agents", []), {"running"}))
    available = max(0, limit - running)
    promoted = 0
    for agent in payload.get("agents", []):
        if promoted >= available:
            break
        if agent_status(agent) in {"planned", "queued"}:
            set_agent_status(agent, "running", note)
            add_event(payload, agent.get("name") or "agent", "status", "Started next group", note)
            prompt = build_spawn_prompt(agent)
            add_command(payload, agent.get("name") or "agent", "spawn", "Start this agent with the update plan", prompt)
            promoted += 1
    return promoted


def build_spawn_prompt(agent: dict) -> str:
    protocol = heartbeat_contract(
        str(agent.get("name") or "AGENT_NAME"),
        str(agent.get("id") or ""),
        ownership=str(agent.get("ownership") or "specified in this prompt"),
        allowed_files="; ".join(agent.get("allowedFiles") if isinstance(agent.get("allowedFiles"), list) else []) or str(agent.get("ownership") or ""),
        do_not_touch="; ".join(agent.get("doNotTouch") if isinstance(agent.get("doNotTouch"), list) else []) or "anything outside your ownership scope",
        expected_outputs="; ".join(agent.get("expectedOutputs") if isinstance(agent.get("expectedOutputs"), list) else []) or "changed files, verification, blockers, and next step",
        heartbeat_cadence=str(agent.get("heartbeatCadence") or "on milestones"),
        test_expectations=str(agent.get("testExpectations") or agent.get("tests") or "run focused verification"),
    )
    return (
        f"You are {agent.get('name')}. Ownership: {agent.get('ownership') or 'unspecified'}.\n"
        f"Summary: {agent.get('summary') or 'No summary provided'}.\n\n"
        f"{protocol}"
    )


def handle_control_action(payload: dict, action: str, agent_key: str, note: str = "") -> None:
    payload = ensure_control_plane(payload)
    agent = find_agent(payload.get("agents", []), agent_key) if agent_key else None
    agent_name = str(agent.get("name") if agent else agent_key or "Orchestrator")

    if action == "promote-next":
        count = promote_next_agents(payload, note)
        add_event(payload, "Orchestrator", "status", f"Started {count} waiting agents", note)
        return

    if action == "export-memory":
        path = export_second_brain(payload, note)
        add_event(payload, "Orchestrator", "handoff", "Saved run summary to second brain", str(path))
        return

    if not agent:
        add_event(payload, "Orchestrator", "blocked", f"Action ignored: unknown agent {agent_key}", action)
        return

    if action == "mark-reviewed":
        mark_agent_reviewed(payload, agent, note)
    elif action == "needs-review":
        set_agent_status(agent, "needs-review", note)
        add_event(payload, agent_name, "review", "Marked needs more info", note)
    elif action == "mark-merged":
        set_agent_status(agent, "merged", note)
        add_event(payload, agent_name, "merged", "Marked merged", note)
    elif action == "close":
        set_agent_status(agent, "closed", note)
        add_event(payload, agent_name, "closed", "Marked closed", note)
        add_command(payload, agent_name, "close-agent", "Close this completed Codex agent", f"close_agent target={agent.get('id')}")
    elif action == "block":
        set_agent_status(agent, "blocked", note)
        agent["blockers"] = note or agent.get("blockers", "")
        add_event(payload, agent_name, "blocked", "Marked blocked", note)
    elif action == "fail":
        set_agent_status(agent, "failed", note)
        add_event(payload, agent_name, "failed", "Marked failed", note)
    elif action == "request-status":
        prompt = "Please publish a public dashboard update: current status, changed files, tests, blockers, and next step. Do not include private reasoning."
        add_command(payload, agent_name, "request-status", "Ask for a public status update", prompt)
        add_event(payload, agent_name, "status", "Status requested", note)
    elif action == "interrupt":
        prompt = "Pause current work and publish a short public status update plus any blockers or next-step needs. Do not include private reasoning."
        add_command(payload, agent_name, "interrupt", "Pause agent for status or redirect", prompt)
        add_event(payload, agent_name, "status", "Pause request queued", note)
    elif action == "queue-follow-up":
        add_command(payload, agent_name, "follow-up", "Add follow-up work", note or "Follow up on this agent's next step.")
        add_event(payload, agent_name, "handoff", "Follow-up added", note)
    else:
        add_event(payload, agent_name, "status", f"Unknown action: {action}", note)

    payload["generatedAt"] = utc_now()


def run_git(worktree: pathlib.Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(worktree),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or "").strip()


def normalize_path_for_match(value: str) -> str:
    return value.replace("\\", "/").strip().lower()


def file_matches_scope(path: str, scopes: list[str]) -> bool:
    normalized = normalize_path_for_match(path)
    for scope in scopes:
        scope_text = normalize_path_for_match(scope).rstrip("*")
        if not scope_text:
            continue
        if normalized.startswith(scope_text) or scope_text in normalized:
            return True
    return False


def parse_git_status(output: str) -> list[str]:
    files: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1].strip()
        files.append(path)
    return files


def ignored_by_scan(path: str, patterns: list[str]) -> bool:
    normalized = normalize_path_for_match(path).lstrip("./")
    for pattern in patterns:
        normalized_pattern = normalize_path_for_match(pattern).lstrip("./")
        if not normalized_pattern:
            continue
        if fnmatch.fnmatch(normalized, normalized_pattern):
            return True
        if normalized_pattern.endswith("/**") and normalized.startswith(normalized_pattern[:-3].rstrip("/") + "/"):
            return True
    return False


def scan_worktree(payload: dict, worktree: str, ignore_patterns: list[str] | None = None) -> dict:
    worktree_path = pathlib.Path(worktree).expanduser()
    status_output = run_git(worktree_path, ["status", "--short"])
    all_changed_files = parse_git_status(status_output)
    effective_ignores = [*DEFAULT_SCAN_IGNORE_PATTERNS, *(ignore_patterns or [])]
    changed_files = [path for path in all_changed_files if not ignored_by_scan(path, effective_ignores)]
    ignored_files = [path for path in all_changed_files if ignored_by_scan(path, effective_ignores)]
    diff_stat = run_git(worktree_path, ["diff", "--stat"])
    shortstat = run_git(worktree_path, ["diff", "--shortstat"])

    payload.setdefault("workflow", {})["worktree"] = str(worktree_path)
    payload["worktree"] = {
        "path": str(worktree_path),
        "scannedAt": utc_now(),
        "changedFiles": changed_files,
        "ignoredFiles": ignored_files,
        "ignorePatterns": effective_ignores,
        "statusShort": status_output,
        "diffStat": diff_stat,
        "shortStat": shortstat,
        "uncommitted": bool(changed_files),
    }

    claimed: dict[str, list[str]] = {}
    for agent in payload.get("agents", []):
        scopes: list[str] = []
        if isinstance(agent.get("allowedFiles"), list):
            scopes.extend(str(item) for item in agent.get("allowedFiles", []))
        if isinstance(agent.get("writeGlobs"), list):
            scopes.extend(str(item) for item in agent.get("writeGlobs", []))
        if agent.get("ownership"):
            scopes.extend(split_list(str(agent.get("ownership"))))
        if isinstance(agent.get("changedFiles"), list):
            scopes.extend(str(item) for item in agent.get("changedFiles", []))
        touches = [path for path in changed_files if file_matches_scope(path, scopes)]
        outside = []
        if isinstance(agent.get("changedFiles"), list):
            outside = [
                path for path in agent.get("changedFiles", [])
                if not file_matches_scope(str(path), scopes)
            ]
        for path in touches:
            claimed.setdefault(path, []).append(str(agent.get("name") or agent.get("id") or "agent"))
        agent["worktree"] = {
            "path": str(worktree_path),
            "scannedAt": payload["worktree"]["scannedAt"],
            "matchedChangedFiles": touches,
            "ownershipViolations": outside,
            "uncommitted": bool(touches),
            "diffSummary": shortstat or diff_stat[:500],
        }

    overlaps = {path: owners for path, owners in claimed.items() if len(owners) > 1}
    for agent in payload.get("agents", []):
        owned_overlaps = {
            path: owners for path, owners in overlaps.items()
            if str(agent.get("name") or agent.get("id") or "agent") in owners
        }
        worktree_info = agent.get("worktree") if isinstance(agent.get("worktree"), dict) else {}
        worktree_info["overlapRisk"] = owned_overlaps
        worktree_info["handoffReady"] = handoff_ready(agent)
        agent["worktree"] = worktree_info

    payload["worktree"]["overlapRisk"] = overlaps
    payload["generatedAt"] = utc_now()
    add_event(payload, "Orchestrator", "status", "Checked file changes", str(worktree_path))
    return payload["worktree"]


def handoff_ready(agent: dict) -> str:
    status = agent_status(agent)
    if status in BLOCKED_STATES:
        return "blocked"
    if status in {"reviewed", "merged", "closed"}:
        return "checked"
    if status in REVIEW_READY_STATES:
        if not review_gate_issues(agent):
            return "ready to check"
        return "needs missing details"
    return "not ready yet"


def protocol_issues(agent: dict) -> list[str]:
    issues: list[str] = []
    read_only = agent_is_read_only(agent)
    if not str(agent.get("ownership") or "").strip():
        issues.append("work area is missing")
    if not agent.get("allowedFiles"):
        issues.append("allowed files are missing")
    if not read_only and not agent.get("writeGlobs"):
        issues.append("allowed edit paths are missing")
    if not agent.get("expectedOutputs"):
        issues.append("expected results are missing")
    if not str(agent.get("heartbeatCadence") or "").strip():
        issues.append("update timing is missing")
    if not str(agent.get("testExpectations") or agent.get("tests") or "").strip():
        issues.append("test plan is missing")
    if not read_only and not agent.get("doNotTouch"):
        issues.append("off-limits files are missing")
    return issues


def protocol_state(agent: dict) -> str:
    return "pass" if not protocol_issues(agent) else "needs-protocol"


def render_protocol_summary(agent: dict) -> str:
    issues = protocol_issues(agent)
    if not issues:
        return '<p class="ok-text">Update plan complete</p>'
    return render_list(issues)


def render_memory_summary(payload: dict) -> str:
    agents = payload.get("agents", [])
    impact = compute_impact(payload, agents, count_agents(agents))
    lines = [
        "## Codex agent dashboard run",
        f"- Exported: {compact_time(utc_now())}",
        f"- Dashboard: http://127.0.0.1:8765/agent-dashboard.html",
        f"- Objective: {payload.get('workflow', {}).get('objective') or 'Not recorded'}",
        f"- Agents launched: {len(agents)}",
        f"- Estimated time saved: {format_minutes(int(impact.get('savedMinutes', 0)))}",
        f"- Saved time level: {impact.get('rank')}",
        "",
        "### Agent status",
    ]
    for state in LIFECYCLE_STATES:
        count = len([agent for agent in agents if agent_status(agent) == state])
        if count:
            lines.append(f"- {plain_label(state, STATUS_LABELS)}: {count}")
    decision_events = [event for event in payload.get("events", []) if str(event.get("kind") or "").lower() in {"decision", "reviewed", "merged"}]
    if decision_events:
        lines.extend(["", "### Decisions made"])
        for event in decision_events[-12:]:
            lines.append(f"- {event.get('message')} ({event.get('agent')})")
    lines.extend(["", "### Agent next steps"])
    artifact_lines: list[str] = []
    lesson_lines: list[str] = []
    for agent in agents:
        lines.append(f"- {agent.get('name')}: {agent_status(agent)}")
        if agent.get("summary"):
            lines.append(f"  - Summary: {agent.get('summary')}")
        if agent.get("changedFiles"):
            changed = ", ".join(str(item) for item in agent.get("changedFiles", [])[:8])
            lines.append(f"  - Changed files: {changed}")
            artifact_lines.append(f"- {agent.get('name')}: {changed}")
        if agent.get("tests"):
            lines.append(f"  - Tests: {agent.get('tests')}")
        if agent.get("blockers"):
            lines.append(f"  - Blockers: {agent.get('blockers')}")
            lesson_lines.append(f"- {agent.get('name')}: blocker - {agent.get('blockers')}")
        if agent.get("handoff"):
            lines.append(f"  - Next step: {agent.get('handoff')}")
            lesson_lines.append(f"- {agent.get('name')}: next step - {agent.get('handoff')}")
    if artifact_lines:
        lines.extend(["", "### Artifacts and files", *artifact_lines])
    if lesson_lines:
        lines.extend(["", "### Lessons and follow-ups", *lesson_lines])
    recent_events = payload.get("events", [])[-12:]
    if recent_events:
        lines.extend(["", "### Recent activity"])
        for event in recent_events:
            lines.append(f"- {compact_time(event.get('timestamp'))} | {event.get('agent')} | {event.get('kind')}: {event.get('message')}")
    return "\n".join(lines).strip() + "\n"


def export_second_brain(payload: dict, note: str = "") -> pathlib.Path:
    vault = DEFAULT_VAULT
    daily_dir = vault / "Daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    daily_path = daily_dir / f"{datetime.now().date().isoformat()}.md"
    summary = render_memory_summary(payload)
    if note:
        summary += f"\nNote: {note}\n"
    existing = daily_path.read_text(encoding="utf-8") if daily_path.exists() else ""
    marker = f"\n\n{summary}"
    daily_path.write_text(existing.rstrip() + marker, encoding="utf-8")
    payload.setdefault("memoryExports", []).append({"path": str(daily_path), "timestamp": utc_now()})
    payload["generatedAt"] = utc_now()
    return daily_path


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def status_class(status: str) -> str:
    normalized = (status or "").lower()
    if normalized in {"reviewed", "merged", "closed"}:
        return "ok"
    if normalized in {"completed", "needs-review"}:
        return "warn"
    if normalized in {"blocked", "failed"}:
        return "bad"
    if normalized in {"planned", "queued"}:
        return "warn"
    return "run"


def compact_time(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        else:
            parsed = parsed.astimezone()
        zone = parsed.tzname() or parsed.strftime("%z")
        return f"{parsed:%Y-%m-%d %H:%M:%S} {zone}".strip()
    except ValueError:
        return text.replace("T", " ")


def route_from_path(path: str) -> tuple[str, str] | None:
    normalized = path.rstrip("/") or "/"
    if normalized in {"/", "/agent-dashboard.html", "/overview"}:
        return ("overview", "")
    if normalized == "/agents":
        return ("agents", "")
    if normalized == "/activity":
        return ("activity", "")
    if normalized == "/queue":
        return ("queue", "")
    if normalized == "/workflow":
        return ("workflow", "")
    if normalized == "/review":
        return ("review", "")
    if normalized == "/diffs":
        return ("diffs", "")
    if normalized == "/doctor":
        return ("doctor", "")
    if normalized == "/recipes":
        return ("recipes", "")
    if normalized == "/memory":
        return ("memory", "")
    if normalized.startswith("/agent/"):
        return ("agent", urllib.parse.unquote(normalized.removeprefix("/agent/")))
    return None


def agent_ref(agent: dict) -> str:
    value = str(agent.get("id") or agent.get("name") or "agent")
    return urllib.parse.quote(value, safe="")


def agent_href(agent: dict) -> str:
    return f"/agent/{agent_ref(agent)}"


def agent_status(agent: dict) -> str:
    return str(agent.get("status") or "running").lower()


STATUS_LABELS = {
    "needs-review": "needs review",
}

KIND_LABELS = {
    "handoff": "next step",
    "request-evidence": "more info",
    "request-status": "status update",
    "close-agent": "close agent",
}


def plain_label(value: object, labels: dict[str, str] | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.lower()
    if labels and normalized in labels:
        return labels[normalized]
    return normalized.replace("_", " ").replace("-", " ")


def view_class(view: str, current: str) -> str:
    if current == "agent" and view == "agents":
        return "active"
    return "active" if view == current else ""


def render_list(items: list[str]) -> str:
    if not items:
        return '<span class="muted">None reported</span>'
    return '<div class="file-list">' + "".join(f"<code>{esc(item)}</code>" for item in items) + "</div>"


def render_warning_list(warnings: list[str]) -> str:
    if not warnings:
        return '<section class="empty">No warnings.</section>'
    return '<div class="warning-list">' + "".join(f'<div class="warning">{esc(warning)}</div>' for warning in warnings) + "</div>"


def agent_warning_items(payload: dict, agent: dict) -> list[str]:
    warnings = stale_agent_warnings(payload, agent)
    ownership = agent.get("ownershipWarnings") if isinstance(agent.get("ownershipWarnings"), list) else []
    warnings.extend(str(item) for item in ownership)
    if agent_status(agent) == "reviewed":
        issues = review_gate_issues(agent)
        if issues:
            warnings.append(f"review details missing: {'; '.join(issues)}")
    return warnings


def render_agent_warnings(payload: dict, agent: dict) -> str:
    warnings = agent_warning_items(payload, agent)
    if not warnings:
        return ""
    return f"""
      <div class="agent-warning">
        <label>Needs attention</label>
        {render_warning_list(warnings)}
      </div>
    """


def render_agent_nav(agents: list[dict]) -> str:
    if not agents:
        return '<div class="nav-empty">No agents yet</div>'
    rows = []
    for index, agent in enumerate(agents):
        name = esc(agent.get("name"))
        status = str(agent.get("status") or "running")
        status_label = esc(plain_label(status, STATUS_LABELS))
        summary = esc(agent.get("summary") or agent.get("ownership") or status)
        rows.append(
            f"""
            <a class="agent-nav {status_class(status)}" href="{agent_href(agent)}">
              <span class="dot"></span>
              <span class="nav-text">
                <strong>{name}</strong>
                <em>{summary}</em>
              </span>
              <span class="nav-state">{status_label}</span>
            </a>
            """
        )
    return "".join(rows)


def render_events(payload: dict, agents: list[dict]) -> str:
    raw_events = payload.get("events") if isinstance(payload.get("events"), list) else []
    events = [event for event in raw_events if isinstance(event, dict)]
    if not events:
        events = [
            {
                "agent": agent.get("name"),
                "kind": agent.get("status") or "running",
                "message": agent.get("summary") or "Agent is active.",
                "detail": agent.get("ownership") or "",
                "timestamp": agent.get("updatedAt") or payload.get("generatedAt"),
            }
            for agent in agents
        ]
    if not events:
        return '<section class="empty">No public activity has been written yet.</section>'

    rows = []
    for event in reversed(events[-30:]):
        rows.append(
            f"""
            <div class="event">
              <div class="event-meta">
                <span>{esc(compact_time(event.get("timestamp")))}</span>
                <span>{esc(event.get("agent"))}</span>
                <b>{esc(plain_label(event.get("kind"), KIND_LABELS))}</b>
              </div>
              <div class="event-message">{esc(event.get("message"))}</div>
              <div class="event-detail">{esc(event.get("detail"))}</div>
            </div>
            """
        )
    return "".join(rows)


def event_matches_agent(event: dict, agent: dict) -> bool:
    event_agent = str(event.get("agent") or "").strip().lower()
    if not event_agent:
        return False
    candidates = [
        str(agent.get("name") or "").strip().lower(),
        str(agent.get("id") or "").strip().lower(),
    ]
    return any(
        candidate and (event_agent == candidate or event_agent in candidate or candidate in event_agent)
        for candidate in candidates
    )


def render_agent_activity(payload: dict, agent: dict) -> str:
    raw_events = payload.get("events") if isinstance(payload.get("events"), list) else []
    events = [event for event in raw_events if isinstance(event, dict) and event_matches_agent(event, agent)]
    if not events:
        return '<div class="mini-empty">No updates yet</div>'
    rows = []
    for event in reversed(events[-5:]):
        rows.append(
            f"""
            <div class="mini-event">
              <div class="mini-meta">
                <span>{esc(compact_time(event.get("timestamp")))}</span>
                <b>{esc(plain_label(event.get("kind"), KIND_LABELS))}</b>
              </div>
              <div class="mini-message">{esc(event.get("message"))}</div>
              <div class="mini-detail">{esc(event.get("detail"))}</div>
            </div>
            """
        )
    return "".join(rows)


def render_agent_rows(payload: dict, agents: list[dict]) -> str:
    if not agents:
        return '<section class="empty">No agents have been written to the dashboard yet.</section>'

    rows = []
    for index, agent in enumerate(agents):
        status = str(agent.get("status") or "running")
        status_label = esc(plain_label(status, STATUS_LABELS))
        rows.append(
            f"""
            <article id="agent-{index}" class="agent-row {status_class(status)}">
              <div class="row-head">
                <div>
                  <h3>{esc(agent.get("name"))}</h3>
                  <p class="mono">{esc(agent.get("id")) or "no agent id"}</p>
                </div>
                <span class="status">{status_label}</span>
              </div>
              <p class="summary">{esc(agent.get("summary")) or '<span class="muted">No public summary yet</span>'}</p>
              {render_agent_warnings(payload, agent)}
              <div class="detail-grid">
                <section>
                  <label>Work area</label>
                  <p>{esc(agent.get("ownership")) or '<span class="muted">Unspecified</span>'}</p>
                </section>
                <section>
                  <label>Allowed edit paths</label>
                  {render_list(agent.get("writeGlobs") if isinstance(agent.get("writeGlobs"), list) else [])}
                </section>
                <section>
                  <label>Update plan</label>
                  {render_protocol_summary(agent)}
                </section>
                <section>
                  <label>Changed files</label>
                  {render_list(agent.get("changedFiles") if isinstance(agent.get("changedFiles"), list) else [])}
                </section>
                <section>
                  <label>Tests</label>
                  <p>{esc(agent.get("tests")) or '<span class="muted">None reported</span>'}</p>
                </section>
                <section>
                  <label>Stuck on</label>
                  <p>{esc(agent.get("blockers")) or '<span class="muted">None reported</span>'}</p>
                </section>
              </div>
              <div class="handoff">
                <label>Next step</label>
                <p>{esc(agent.get("handoff")) or '<span class="muted">None yet</span>'}</p>
              </div>
              <div class="activity">
                <label>Recent update</label>
                {render_agent_activity(payload, agent)}
              </div>
            </article>
            """
        )
    return "".join(rows)


def render_view_tabs(current: str) -> str:
    tabs = [
        ("overview", "Overview", "/overview"),
        ("workflow", "Work Plan", "/workflow"),
        ("agents", "Agents", "/agents"),
        ("review", "Check Work", "/review"),
        ("diffs", "File Changes", "/diffs"),
        ("doctor", "Health", "/doctor"),
        ("activity", "Activity", "/activity"),
        ("queue", "Waiting", "/queue"),
        ("recipes", "Templates", "/recipes"),
        ("memory", "Notes", "/memory"),
    ]
    return '<nav class="view-tabs">' + "".join(
        f'<a class="{view_class(view, current)}" href="{href}">{label}</a>'
        for view, label, href in tabs
    ) + "</nav>"


def impact_config(payload: dict) -> dict:
    config = payload.get("impact") if isinstance(payload.get("impact"), dict) else {}
    return {
        "manualMinutesPerAgent": max(1, positive_int(config.get("manualMinutesPerAgent"), DEFAULT_MANUAL_MINUTES_PER_AGENT)),
        "coordinationMinutesPerAgent": positive_int(config.get("coordinationMinutesPerAgent"), DEFAULT_COORDINATION_MINUTES_PER_AGENT),
        "focusBlockMinutes": max(1, positive_int(config.get("focusBlockMinutes"), DEFAULT_FOCUS_BLOCK_MINUTES)),
        "note": text_from_value(config.get("note")) or "Estimated from agent work items, status, and time spent coordinating.",
    }


def impact_rank(saved_minutes: int) -> str:
    if saved_minutes >= 600:
        return "huge time save"
    if saved_minutes >= 300:
        return "large time save"
    if saved_minutes >= 120:
        return "solid time save"
    if saved_minutes >= 30:
        return "good start"
    return "just started"


def meaningful_changed_files(agent: dict) -> list[str]:
    empty_markers = {"", "none", "n/a", "na", "no edits", "no changes", "read-only", "readonly"}
    return [
        item for item in list_from_value(agent.get("changedFiles"))
        if item.strip().lower() not in empty_markers
    ]


def default_manual_effort_weight(agent: dict) -> float:
    text = " ".join([
        text_from_value(agent.get("name")),
        text_from_value(agent.get("summary")),
        text_from_value(agent.get("ownership")),
    ]).lower()
    weights = [1.0]
    if bool_from_value(agent.get("readOnly")) or any(token in text for token in ("scout", "read-only", "readonly", "qa scan")):
        weights.append(IMPACT_READ_ONLY_WEIGHT)
    if "orchestrator" in text:
        weights.append(IMPACT_META_WORK_WEIGHT)
    if not meaningful_changed_files(agent):
        weights.append(IMPACT_NO_EDIT_WEIGHT)
    return min(weights)


def format_minutes(minutes: int) -> str:
    minutes = max(0, int(minutes))
    hours = minutes // 60
    remainder = minutes % 60
    if hours and remainder:
        return f"{hours}h {remainder}m"
    if hours:
        return f"{hours}h"
    return f"{remainder}m"


def compute_impact(payload: dict, agents: list[dict], counts: dict[str, int]) -> dict:
    config = impact_config(payload)
    manual_total = 0.0
    coordination_total = 0.0
    effective_slices = 0.0
    explicit_estimates = 0

    for agent in agents:
        status = agent_status(agent)
        progress = IMPACT_PROGRESS_WEIGHTS.get(status, 0.5)
        manual_is_explicit = bool(agent.get("manualMinutes"))
        coordination_is_explicit = bool(agent.get("coordinationMinutes"))
        manual_minutes = positive_int(agent.get("manualMinutes"), config["manualMinutesPerAgent"])
        coordination_minutes = positive_int(agent.get("coordinationMinutes"), config["coordinationMinutesPerAgent"])
        if manual_is_explicit or coordination_is_explicit:
            explicit_estimates += 1
        manual_weight = progress if manual_is_explicit else progress * default_manual_effort_weight(agent)
        manual_total += manual_minutes * manual_weight
        coordination_total += coordination_minutes * progress
        effective_slices += manual_minutes * manual_weight / config["manualMinutesPerAgent"]

    manual_minutes_total = int(round(manual_total))
    coordination_minutes_total = int(round(coordination_total))
    saved_minutes = max(0, manual_minutes_total - coordination_minutes_total)
    focus_blocks = saved_minutes // config["focusBlockMinutes"]
    done_count = counts.get("reviewed", 0) + counts.get("merged", 0) + counts.get("closed", 0)
    review_ready = counts.get("completed", 0) + counts.get("needs-review", 0)
    running = counts.get("running", 0)
    clean_review_ready = len([
        agent for agent in agents
        if agent_status(agent) in {"completed", "needs-review", "reviewed", "merged", "closed"}
        and not review_gate_issues(agent)
    ])
    warnings = dashboard_warnings(payload, agents)

    badges: list[dict[str, str]] = []
    if agents:
        badges.append({"title": "Tracking Started", "detail": f"{len(agents)} work items tracked"})
    if running >= 2:
        badges.append({"title": "Several Agents Working", "detail": f"{running} agents active"})
    if clean_review_ready:
        badges.append({"title": "Clear Updates", "detail": f"{clean_review_ready} agents ready to check"})
    if done_count:
        badges.append({"title": "Work Checked", "detail": f"{done_count} work items done"})
    if agents and not warnings:
        badges.append({"title": "Looks Clear", "detail": "No dashboard warnings"})
    if saved_minutes >= 120:
        badges.append({"title": "Focus Time Saved", "detail": f"{focus_blocks} focus blocks saved"})

    return {
        "manualMinutes": manual_minutes_total,
        "coordinationMinutes": coordination_minutes_total,
        "savedMinutes": saved_minutes,
        "focusBlocks": focus_blocks,
        "focusBlockMinutes": config["focusBlockMinutes"],
        "effectiveSlices": round(effective_slices, 1),
        "rank": impact_rank(saved_minutes),
        "badges": badges[:6],
        "assumption": (
            f"{config['manualMinutesPerAgent']}m baseline lowered for read-only, lead, or no-edit work; "
            f"minus {config['coordinationMinutesPerAgent']}m coordination per tracked item"
        ),
        "note": config["note"],
        "explicitEstimates": explicit_estimates,
        "reviewReady": review_ready,
    }


def render_stats(counts: dict[str, int], impact: dict | None = None) -> str:
    saved = format_minutes(int((impact or {}).get("savedMinutes", 0)))
    return f"""
      <section class="stats">
        <div class="stat"><span>Planned</span><strong>{counts.get("planned", 0)}</strong></div>
        <div class="stat run"><span>Running</span><strong>{counts["running"]}</strong></div>
        <div class="stat warn"><span>To Check</span><strong>{counts.get("needs-review", 0) + counts.get("completed", 0)}</strong></div>
        <div class="stat bad"><span>Blocked</span><strong>{counts["blocked"]}</strong></div>
        <div class="stat ok"><span>Done</span><strong>{counts.get("reviewed", 0) + counts.get("merged", 0) + counts.get("closed", 0)}</strong></div>
        <div class="stat impact"><span>Saved</span><strong>{esc(saved)}</strong></div>
      </section>
    """


def render_panel(title: str, detail: str, body: str) -> str:
    return f"""
      <section class="panel">
        <div class="panel-head"><h3>{esc(title)}</h3><span class="muted">{esc(detail)}</span></div>
        <div class="panel-body">{body}</div>
      </section>
    """


def render_impact_panel(impact: dict) -> str:
    badges = impact.get("badges") if isinstance(impact.get("badges"), list) else []
    badge_markup = "".join(
        f"""
        <div class="impact-badge">
          <strong>{esc(badge.get("title"))}</strong>
          <span>{esc(badge.get("detail"))}</span>
        </div>
        """
        for badge in badges if isinstance(badge, dict)
    ) or '<span class="muted">Start tracking agents to see progress badges.</span>'
    body = f"""
      <div class="impact-grid">
        <section class="impact-hero">
          <label>Estimated time saved</label>
          <strong>{esc(format_minutes(int(impact.get("savedMinutes", 0))))}</strong>
          <p>{esc(impact.get("rank"))} - {esc(impact.get("assumption"))}</p>
        </section>
        <section>
          <label>Manual work avoided</label>
          <p>{esc(format_minutes(int(impact.get("manualMinutes", 0))))}</p>
        </section>
        <section>
          <label>Time coordinating</label>
          <p>{esc(format_minutes(int(impact.get("coordinationMinutes", 0))))}</p>
        </section>
        <section>
          <label>Focus blocks saved</label>
          <p>{esc(impact.get("focusBlocks", 0))} x {esc(impact.get("focusBlockMinutes", DEFAULT_FOCUS_BLOCK_MINUTES))}m</p>
        </section>
        <section>
          <label>Work counted</label>
          <p>{esc(impact.get("effectiveSlices", 0))}</p>
        </section>
      </div>
      <div class="impact-badges">{badge_markup}</div>
      <p class="impact-note">{esc(impact.get("note"))}</p>
    """
    return render_panel("Time Saved Estimate", "rough estimate", body)


def filter_agents(agents: list[dict], statuses: set[str]) -> list[dict]:
    return [agent for agent in agents if agent_status(agent) in statuses]


def count_agents(agents: list[dict]) -> dict[str, int]:
    counts = {state: 0 for state in LIFECYCLE_STATES}
    for agent in agents:
        status = agent_status(agent)
        counts[status] = counts.get(status, 0) + 1
    counts["running"] = counts.get("running", 0)
    counts["queued"] = counts.get("queued", 0)
    counts["completed"] = counts.get("completed", 0)
    counts["blocked"] = counts.get("blocked", 0) + counts.get("failed", 0)
    return counts


def find_agent(agents: list[dict], ref: str) -> dict | None:
    decoded = urllib.parse.unquote(ref or "").strip().lower()
    if not decoded:
        return agents[0] if agents else None
    for agent in agents:
        candidates = [
            str(agent.get("id") or "").strip().lower(),
            str(agent.get("name") or "").strip().lower(),
        ]
        aliases = agent.get("aliases") if isinstance(agent.get("aliases"), list) else []
        candidates.extend(str(alias or "").strip().lower() for alias in aliases)
        if decoded in candidates:
            return agent
    return None


def render_agent_table(agents: list[dict]) -> str:
    if not agents:
        return '<section class="empty">No matching agents.</section>'
    rows = []
    for agent in agents:
        status = agent_status(agent)
        status_label = esc(plain_label(status, STATUS_LABELS))
        rows.append(
            f"""
            <a class="agent-line {status_class(status)}" href="{agent_href(agent)}">
              <span class="dot"></span>
              <span>
                <strong>{esc(agent.get("name"))}</strong>
                <em>{esc(agent.get("summary")) or "No summary yet"}</em>
              </span>
              <b>{status_label}</b>
            </a>
            """
        )
    return '<div class="agent-lines">' + "".join(rows) + "</div>"


def render_overview(payload: dict, agents: list[dict], counts: dict[str, int]) -> str:
    active_agents = filter_agents(agents, {"running", "queued", "needs-review"})
    blocked_agents = filter_agents(agents, {"blocked", "failed"})
    recent = render_events(payload, agents)
    warnings = dashboard_warnings(payload, agents)
    impact = compute_impact(payload, agents, counts)
    return (
        render_stats(counts, impact)
        + render_impact_panel(impact)
        + (render_panel("Needs Attention", f"{len(warnings)} warnings", render_warning_list(warnings)) if warnings else "")
        + render_panel("Active Agents", f"{len(active_agents)} active or waiting", render_agent_table(active_agents))
        + (render_panel("Blocked", f"{len(blocked_agents)} need attention", render_agent_table(blocked_agents)) if blocked_agents else "")
        + render_panel("Recent Activity", "newest first", recent)
    )


def render_agents_view(payload: dict, agents: list[dict]) -> str:
    return render_panel("All Agents", f"{len(agents)} total", render_agent_rows(payload, agents))


def render_activity_view(payload: dict, agents: list[dict]) -> str:
    return render_panel("Recent Updates", "latest public updates", render_events(payload, agents))


def render_queue_view(agents: list[dict]) -> str:
    queued = filter_agents(agents, {"queued", "needs-review"})
    running = filter_agents(agents, {"running"})
    completed = filter_agents(agents, {"completed", "merged"})
    body = (
        '<div class="queue-grid">'
        + render_panel("Waiting Or Ready To Check", f"{len(queued)} waiting", render_agent_table(queued))
        + render_panel("Running Now", f"{len(running)} active", render_agent_table(running))
        + render_panel("Done And Ready To Close", f"{len(completed)} finished", render_agent_table(completed))
        + "</div>"
    )
    return body


def render_action_form(action: str, label: str, agent: dict | None = None, note: str = "") -> str:
    agent_value = esc(agent.get("id") or agent.get("name")) if agent else ""
    note_input = f'<input type="hidden" name="note" value="{esc(note)}">' if note else ""
    return f"""
      <form class="inline-action" method="post" action="/action">
        <input type="hidden" name="action" value="{esc(action)}">
        <input type="hidden" name="agent" value="{agent_value}">
        {note_input}
        <button type="submit">{esc(label)}</button>
      </form>
    """


def render_command_state_form(command_id: str, state: str, label: str, note: str = "") -> str:
    return f"""
      <form class="inline-action" method="post" action="/action">
        <input type="hidden" name="action" value="set-command-state">
        <input type="hidden" name="command" value="{esc(command_id)}">
        <input type="hidden" name="state" value="{esc(state)}">
        <input type="hidden" name="note" value="{esc(note)}">
        <button type="submit">{esc(label)}</button>
      </form>
    """


def render_copy_button(label: str, value: str) -> str:
    return f'<button type="button" class="copy-button" data-copy="{esc(value)}">{esc(label)}</button>'


def render_pending_commands(payload: dict) -> str:
    commands = [command for command in payload.get("commands", []) if isinstance(command, dict) and command.get("state") == "pending"]
    if not commands:
        return '<section class="empty">No waiting actions.</section>'
    rows = []
    for command in reversed(commands[-20:]):
        prompt = str(command.get("prompt") or command.get("message") or "")
        rows.append(
            f"""
            <div class="command-row">
              <div>
                <strong>{esc(command.get("message"))}</strong>
                <p>{esc(command.get("agent"))} &middot; {esc(plain_label(command.get("kind"), KIND_LABELS))} &middot; {esc(compact_time(command.get("createdAt")))}</p>
              </div>
              <div class="action-bar">
                {render_copy_button("Copy", prompt)}
                {render_command_state_form(str(command.get("id") or ""), "dismissed", "Dismiss", "Dismissed from dashboard")}
              </div>
            </div>
            """
        )
    return '<div class="command-list">' + "".join(rows) + "</div>"


def render_workflow_view(payload: dict, agents: list[dict], counts: dict[str, int]) -> str:
    workflow = payload.get("workflow", {})
    limit = workflow.get("concurrencyLimit", DEFAULT_CONCURRENCY_LIMIT)
    running = len(filter_agents(agents, {"running"}))
    capacity = max(0, int(limit) - running)
    warnings = dashboard_warnings(payload, agents)
    impact = compute_impact(payload, agents, counts)
    body = f"""
      <div class="workflow-grid">
        <section>
          <label>Objective</label>
          <p>{esc(workflow.get("objective")) or '<span class="muted">Not recorded</span>'}</p>
        </section>
        <section>
          <label>Agent limit</label>
          <p>{running} running / {esc(limit)} limit &middot; {capacity} slots open</p>
        </section>
        <section>
          <label>Work folder</label>
          <p>{esc(workflow.get("worktree")) or '<span class="muted">Not scanned</span>'}</p>
        </section>
        <section>
          <label>Current group</label>
          <p>{esc(workflow.get("activeWave")) or '<span class="muted">Unspecified</span>'}</p>
        </section>
      </div>
      <div class="action-bar">
        {render_action_form("promote-next", "Start next group")}
        {render_action_form("export-memory", "Save summary to notes")}
      </div>
    """
    return (
        render_stats(counts, impact)
        + render_impact_panel(impact)
        + (render_panel("Needs Attention", f"{len(warnings)} warnings", render_warning_list(warnings)) if warnings else "")
        + render_panel("Work Plan", "who runs next", body)
        + render_panel("Waiting Actions", "copy into agent chats", render_pending_commands(payload))
    )


def render_review_view(payload: dict, agents: list[dict]) -> str:
    needs_review = filter_agents(agents, {"completed", "needs-review"})
    reviewed = filter_agents(agents, {"reviewed", "merged", "closed"})
    rows = []
    for agent in needs_review:
        gate_issues = review_gate_issues(agent)
        rows.append(
            f"""
            <div class="review-row">
              <div>
                <strong>{esc(agent.get("name"))}</strong>
                <p>{esc(agent.get("summary")) or "No summary yet"}</p>
                <p class="muted">{esc(agent.get("tests")) or "No tests reported"} &middot; {esc(agent.get("blockers")) or "No blockers reported"}</p>
                {(f'<div class="gate-issues">{render_warning_list(gate_issues)}</div>' if gate_issues else '<p class="ok-text">Ready to check</p>')}
              </div>
              <div class="row-actions">
                {render_action_form("mark-reviewed", "Mark checked", agent)}
                {render_action_form("needs-review", "Needs more info", agent)}
                {render_action_form("close", "Close", agent)}
              </div>
            </div>
            """
        )
    pending = "".join(rows) if rows else '<section class="empty">No completed agents are waiting to be checked.</section>'
    return render_panel("Ready To Check", f"{len(needs_review)} waiting", pending) + render_panel("Checked Or Closed", f"{len(reviewed)} checked", render_agent_table(reviewed))


def render_diffs_view(payload: dict, agents: list[dict]) -> str:
    worktree = payload.get("worktree") if isinstance(payload.get("worktree"), dict) else {}
    rows = []
    for agent in agents:
        info = agent.get("worktree") if isinstance(agent.get("worktree"), dict) else {}
        overlap_risk = info.get("overlapRisk") if isinstance(info.get("overlapRisk"), dict) else {}
        warnings = agent_warning_items(payload, agent)
        rows.append(
            f"""
            <div class="diff-row">
              <div>
                <strong>{esc(agent.get("name"))}</strong>
                <p>{esc(info.get("handoffReady") or handoff_ready(agent))}</p>
                {render_warning_list(warnings) if warnings else ""}
              </div>
              <div>
                <label>Files this agent changed</label>
                {render_list(info.get("matchedChangedFiles") if isinstance(info.get("matchedChangedFiles"), list) else [])}
              </div>
              <div>
                <label>Files outside work area</label>
                {render_list(info.get("ownershipViolations") if isinstance(info.get("ownershipViolations"), list) else [])}
                <label>May conflict with</label>
                {render_list(list(overlap_risk.keys()))}
              </div>
            </div>
            """
        )
    changed_count = len(worktree.get('changedFiles', [])) if isinstance(worktree.get('changedFiles'), list) else 0
    ignored_count = len(worktree.get('ignoredFiles', [])) if isinstance(worktree.get('ignoredFiles'), list) else 0
    head = f"Work folder: {worktree.get('path') or 'not scanned'} - {changed_count} changed files, {ignored_count} ignored"
    return render_panel("File Change Check", head, "".join(rows) if rows else '<section class="empty">No file-change data yet.</section>')


def render_agent_diff_summary(agent: dict) -> str:
    info = agent.get("worktree") if isinstance(agent.get("worktree"), dict) else {}
    if not info:
        return '<div class="mini-empty">No file-change check yet.</div>'
    overlap_risk = info.get("overlapRisk") if isinstance(info.get("overlapRisk"), dict) else {}
    return f"""
      <div class="detail-grid">
        <section>
          <label>Ready for next step</label>
          <p>{esc(info.get("handoffReady") or handoff_ready(agent))}</p>
        </section>
        <section>
          <label>Change summary</label>
          <p>{esc(info.get("diffSummary")) or '<span class="muted">No change summary</span>'}</p>
        </section>
        <section>
          <label>Files this agent changed</label>
          {render_list(info.get("matchedChangedFiles") if isinstance(info.get("matchedChangedFiles"), list) else [])}
        </section>
        <section>
          <label>Files outside work area</label>
          {render_list(info.get("ownershipViolations") if isinstance(info.get("ownershipViolations"), list) else [])}
        </section>
        <section>
          <label>May conflict with</label>
          {render_list(list(overlap_risk.keys()))}
        </section>
      </div>
    """


def render_recipes_view(payload: dict) -> str:
    rows = []
    recipes = payload.get("recipes") if isinstance(payload.get("recipes"), dict) else RECIPES
    for key, recipe in recipes.items():
        outputs = recipe.get("outputs") if isinstance(recipe.get("outputs"), list) else []
        prompt = f"Use the {recipe.get('title')} recipe. Purpose: {recipe.get('purpose')}. Expected outputs: {', '.join(outputs)}."
        rows.append(
            f"""
            <div class="recipe-row">
              <div>
                <strong>{esc(recipe.get("title"))}</strong>
                <p>{esc(recipe.get("purpose"))}</p>
                <p class="muted">{esc(key)} &middot; {esc(plain_label(recipe.get("agentType")))}</p>
              </div>
              {render_copy_button("Copy template prompt", prompt)}
            </div>
            """
        )
    return render_panel("Agent Templates", f"{len(rows)} templates", "".join(rows))


def render_memory_view(payload: dict) -> str:
    exports = payload.get("memoryExports") if isinstance(payload.get("memoryExports"), list) else []
    body = f"""
      <section class="memory-preview">
        <label>Notes preview</label>
        <pre>{esc(render_memory_summary(payload))}</pre>
      </section>
      <div class="action-bar">{render_action_form("export-memory", "Add to daily note")}</div>
    """
    if exports:
        body += "<div class=\"memory-exports\">" + "".join(
            f"<p>{esc(compact_time(item.get('timestamp')))} &middot; <code>{esc(item.get('path'))}</code></p>"
            for item in exports[-5:] if isinstance(item, dict)
        ) + "</div>"
    return render_panel("Second Brain Notes", "saved run summary", body)


def render_doctor_view(payload: dict) -> str:
    summary = health_summary(payload)
    gap_count = (
        len(summary["finalReportGaps"])
        + len(summary["writeScopeGaps"])
        + len(summary["missingIds"])
        + len(summary["staleCommands"])
    )
    return render_panel("Dashboard Health Check", f"{gap_count} items to fix", f"<pre>{esc(render_doctor_report(payload))}</pre>")


def render_agent_detail(payload: dict, agents: list[dict], agent_ref_value: str) -> str:
    agent = find_agent(agents, agent_ref_value)
    if not agent:
        return render_panel("Agent Not Found", "unknown route", '<section class="empty">That agent is not in the current dashboard state.</section>')
    status = agent_status(agent)
    status_label = esc(plain_label(status, STATUS_LABELS))
    return f"""
      <section class="agent-detail {status_class(status)}">
        <div class="detail-hero">
          <div>
            <p class="eyebrow">Agent Details</p>
            <h3>{esc(agent.get("name"))}</h3>
            <p class="mono">{esc(agent.get("id")) or "no agent id"}</p>
          </div>
          <span class="status">{status_label}</span>
        </div>
        <p class="summary">{esc(agent.get("summary")) or '<span class="muted">No public summary yet</span>'}</p>
        {render_agent_warnings(payload, agent)}
        <div class="detail-grid">
          <section>
            <label>Work area</label>
            <p>{esc(agent.get("ownership")) or '<span class="muted">Unspecified</span>'}</p>
          </section>
          <section>
            <label>Allowed edit paths</label>
            {render_list(agent.get("writeGlobs") if isinstance(agent.get("writeGlobs"), list) else [])}
          </section>
          <section>
            <label>Update plan</label>
            {render_protocol_summary(agent)}
          </section>
          <section>
            <label>Changed files</label>
            {render_list(agent.get("changedFiles") if isinstance(agent.get("changedFiles"), list) else [])}
          </section>
          <section>
            <label>Tests</label>
            <p>{esc(agent.get("tests")) or '<span class="muted">None reported</span>'}</p>
          </section>
          <section>
            <label>Stuck on</label>
            <p>{esc(agent.get("blockers")) or '<span class="muted">None reported</span>'}</p>
          </section>
        </div>
        <div class="handoff">
          <label>Next step</label>
          <p>{esc(agent.get("handoff")) or '<span class="muted">None yet</span>'}</p>
        </div>
        <div class="activity">
          <label>Actions</label>
          <div class="action-bar">
            {render_action_form("request-status", "Ask for update", agent)}
            {render_action_form("interrupt", "Pause and ask", agent)}
            {render_action_form("queue-follow-up", "Add follow-up", agent, "Follow up on this agent's next step.")}
            {render_action_form("mark-reviewed", "Mark checked", agent)}
            {render_action_form("close", "Close", agent)}
          </div>
        </div>
        <div class="activity">
          <label>File change check</label>
          {render_agent_diff_summary(agent)}
        </div>
        <div class="activity">
          <label>Recent update</label>
          {render_agent_activity(payload, agent)}
        </div>
      </section>
    """


def render_main_view(payload: dict, agents: list[dict], counts: dict[str, int], view: str, agent_ref_value: str) -> str:
    if view == "workflow":
        return render_workflow_view(payload, agents, counts)
    if view == "agents":
        return render_agents_view(payload, agents)
    if view == "review":
        return render_review_view(payload, agents)
    if view == "diffs":
        return render_diffs_view(payload, agents)
    if view == "doctor":
        return render_doctor_view(payload)
    if view == "activity":
        return render_activity_view(payload, agents)
    if view == "queue":
        return render_queue_view(agents)
    if view == "recipes":
        return render_recipes_view(payload)
    if view == "memory":
        return render_memory_view(payload)
    if view == "agent":
        return render_agent_detail(payload, agents, agent_ref_value)
    return render_overview(payload, agents, counts)


def render_html(payload: dict, view: str = "overview", agent_ref: str = "") -> str:
    agents = payload.get("agents") if isinstance(payload.get("agents"), list) else []
    counts = count_agents(agents)
    active_count = counts.get("running", 0) + counts.get("queued", 0) + counts.get("planned", 0)
    generated_at = compact_time(payload.get("generatedAt"))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>{esc(payload.get("title"))}</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      background: #0f1012;
      color: #f3f4f7;
      --bg: #0f1012;
      --surface: #141519;
      --surface-2: #181a20;
      --surface-3: #202229;
      --line: #2a2d35;
      --line-soft: rgba(255,255,255,.07);
      --muted: #9298a3;
      --muted-2: #6f7682;
      --text: #f3f4f7;
      --text-soft: #d7dbe3;
      --green: #57d681;
      --blue: #79aefc;
      --yellow: #e9b85d;
      --red: #ff7373;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      min-height: 100vh;
      font-size: 13px;
    }}
    .shell {{
      display: grid;
      grid-template-columns: clamp(178px, 25vw, 236px) minmax(0, 1fr);
      min-height: 100vh;
    }}
    aside {{
      border-right: 1px solid var(--line);
      background: #101115;
      padding: 14px 10px;
      position: sticky;
      top: 0;
      height: 100vh;
      overflow: auto;
    }}
    h1 {{
      margin: 0;
      font-size: 13px;
      font-weight: 650;
      letter-spacing: 0;
    }}
    .workspace {{
      color: var(--muted);
      font-size: 11px;
      margin: 3px 0 14px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .side-head {{
      padding: 2px 8px 10px;
      border-bottom: 1px solid var(--line-soft);
      margin-bottom: 8px;
    }}
    .agent-nav {{
      display: grid;
      grid-template-columns: 10px minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      text-decoration: none;
      color: var(--text);
      padding: 8px;
      border-radius: 8px;
      margin-bottom: 3px;
      border: 1px solid transparent;
      min-height: 44px;
    }}
    .agent-nav:hover {{
      background: var(--surface);
      border-color: var(--line);
    }}
    .dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--blue);
      box-shadow: 0 0 0 3px rgba(121,174,252,.1);
    }}
    .agent-nav.ok .dot {{ background: var(--green); }}
    .agent-nav.warn .dot {{ background: var(--yellow); }}
    .agent-nav.bad .dot {{ background: var(--red); }}
    .nav-text strong {{
      display: block;
      font-size: 12px;
      font-weight: 560;
      line-height: 1.25;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .nav-text em {{
      color: var(--muted);
      display: block;
      font-size: 11px;
      font-style: normal;
      line-height: 1.25;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .nav-state {{
      color: var(--muted-2);
      font-size: 10px;
      text-transform: uppercase;
      align-self: start;
      margin-top: 1px;
    }}
    .content {{
      min-width: 0;
      padding: 14px 16px 22px;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      padding-bottom: 12px;
      margin-bottom: 12px;
    }}
    .title-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }}
    .title-row h2 {{
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0;
    }}
    .eyebrow {{
      color: var(--muted-2);
      font-size: 11px;
      margin: 0 0 4px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    .live-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid rgba(87,214,129,.45);
      background: rgba(87,214,129,.08);
      color: #b8f1ca;
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 12px;
      white-space: nowrap;
    }}
    .live-pill span {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
    }}
    .sub {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }}
    .view-tabs {{
      display: flex;
      gap: 4px;
      margin: 12px 0 0;
      border: 1px solid var(--line);
      background: #101116;
      border-radius: 8px;
      padding: 4px;
      width: fit-content;
      max-width: 100%;
      overflow-x: auto;
    }}
    .view-tabs a {{
      color: var(--muted);
      text-decoration: none;
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 12px;
      white-space: nowrap;
    }}
    .view-tabs a:hover {{
      color: var(--text);
      background: var(--surface-2);
    }}
    .view-tabs a.active {{
      color: var(--text);
      background: var(--surface-3);
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(6, minmax(66px, 1fr));
      gap: 8px;
      margin: 12px 0;
    }}
    .stat {{
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 8px;
      padding: 9px 10px;
      min-width: 0;
    }}
    .stat span {{
      color: var(--muted);
      font-size: 11px;
    }}
    .stat strong {{
      display: block;
      margin-top: 2px;
      font-size: 19px;
      font-weight: 620;
    }}
    .stat.run strong {{ color: var(--blue); }}
    .stat.warn strong {{ color: var(--yellow); }}
    .stat.ok strong {{ color: var(--green); }}
    .stat.bad strong {{ color: var(--red); }}
    .stat.impact strong {{ color: #b8f1ca; }}
    .impact-grid {{
      display: grid;
      grid-template-columns: 1.35fr repeat(4, minmax(110px, 1fr));
      gap: 10px;
      padding: 12px;
    }}
    .impact-grid section {{
      border: 1px solid var(--line-soft);
      background: #101116;
      border-radius: 8px;
      padding: 10px;
      min-width: 0;
    }}
    .impact-hero {{
      background: linear-gradient(135deg, rgba(87,214,129,.12), rgba(121,174,252,.1)) !important;
      border-color: rgba(87,214,129,.35) !important;
    }}
    .impact-hero strong {{
      display: block;
      margin: 2px 0 4px;
      color: #b8f1ca;
      font-size: 28px;
      line-height: 1.05;
    }}
    .impact-grid section p {{
      color: var(--text-soft);
      font-size: 16px;
      font-weight: 620;
    }}
    .impact-hero p {{
      color: var(--muted) !important;
      font-size: 12px !important;
      font-weight: 400 !important;
      line-height: 1.35;
    }}
    .impact-badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      padding: 0 12px 12px;
    }}
    .impact-badge {{
      border: 1px solid rgba(121,174,252,.32);
      background: rgba(121,174,252,.08);
      border-radius: 8px;
      padding: 7px 9px;
      min-width: 128px;
    }}
    .impact-badge strong {{
      display: block;
      color: var(--text-soft);
      font-size: 12px;
    }}
    .impact-badge span {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      margin-top: 2px;
    }}
    .impact-note {{
      margin: -2px 12px 12px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .panel {{
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 8px;
      overflow: hidden;
      margin-bottom: 10px;
    }}
    .panel-head {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #111216;
      gap: 10px;
    }}
    .panel-head h3 {{
      margin: 0;
      font-size: 13px;
      font-weight: 620;
      letter-spacing: 0;
    }}
    .panel-body {{
      padding: 0;
    }}
    .agent-lines {{
      display: grid;
      gap: 0;
    }}
    .agent-line {{
      display: grid;
      grid-template-columns: 12px minmax(0, 1fr) auto;
      align-items: center;
      gap: 9px;
      padding: 10px 12px;
      color: var(--text);
      text-decoration: none;
      border-bottom: 1px solid rgba(42,46,56,.75);
    }}
    .agent-line:last-child {{
      border-bottom: 0;
    }}
    .agent-line:hover {{
      background: #171920;
    }}
    .agent-line strong {{
      display: block;
      font-size: 13px;
      font-weight: 610;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .agent-line em {{
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
      font-style: normal;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .agent-line b {{
      color: var(--muted-2);
      font-size: 10px;
      font-weight: 560;
      text-transform: uppercase;
    }}
    .queue-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}
    .queue-grid .panel {{
      margin-bottom: 0;
    }}
    .workflow-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      padding: 12px;
    }}
    .action-bar {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      padding: 0 12px 12px;
    }}
    .inline-action {{
      margin: 0;
      display: inline-flex;
    }}
    button, .copy-button {{
      border: 1px solid var(--line);
      background: var(--surface-2);
      color: var(--text);
      border-radius: 7px;
      padding: 6px 9px;
      font: inherit;
      font-size: 12px;
      cursor: pointer;
    }}
    button:hover, .copy-button:hover {{
      background: var(--surface-3);
      border-color: #3b404c;
    }}
    .command-list, .memory-exports {{
      display: grid;
    }}
    .command-row, .review-row, .diff-row, .recipe-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      padding: 11px 12px;
      border-bottom: 1px solid rgba(42,46,56,.75);
    }}
    .command-row:last-child, .review-row:last-child, .diff-row:last-child, .recipe-row:last-child {{
      border-bottom: 0;
    }}
    .command-row strong, .review-row strong, .diff-row strong, .recipe-row strong {{
      display: block;
      font-size: 13px;
    }}
    .command-row p, .review-row p, .diff-row p, .recipe-row p {{
      margin: 3px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }}
    .row-actions {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .diff-row {{
      grid-template-columns: minmax(150px, .8fr) minmax(0, 1fr) minmax(0, 1fr);
      align-items: start;
    }}
    .memory-preview {{
      padding: 12px;
    }}
    pre {{
      white-space: pre-wrap;
      margin: 8px 0 0;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #101116;
      color: var(--text-soft);
      font: 11px Consolas, "SFMono-Regular", ui-monospace, monospace;
      max-height: 420px;
      overflow: auto;
    }}
    .event {{
      padding: 10px 12px 10px 15px;
      border-bottom: 1px solid rgba(42,46,56,.75);
      position: relative;
    }}
    .event:last-child {{ border-bottom: 0; }}
    .event::before {{
      content: "";
      position: absolute;
      left: 0;
      top: 10px;
      bottom: 10px;
      width: 2px;
      border-radius: 2px;
      background: var(--blue);
    }}
    .event-meta {{
      display: flex;
      gap: 8px;
      color: var(--muted);
      font-size: 11px;
      flex-wrap: wrap;
    }}
    .event-meta b {{
      color: var(--blue);
      font-weight: 560;
    }}
    .event-message {{
      margin-top: 5px;
      font-size: 13px;
      color: var(--text-soft);
    }}
    .event-detail {{
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .agent-row {{
      border-top: 1px solid var(--line);
      padding: 13px 12px;
    }}
    .agent-row:first-child {{ border-top: 0; }}
    .agent-detail {{
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 8px;
      padding: 14px;
    }}
    .detail-hero {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: flex-start;
      border-bottom: 1px solid var(--line);
      padding-bottom: 12px;
      margin-bottom: 12px;
    }}
    .detail-hero h3 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      letter-spacing: 0;
    }}
    .row-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }}
    .row-head h3 {{
      margin: 0;
      font-size: 14px;
      font-weight: 620;
      letter-spacing: 0;
    }}
    .mono {{
      color: var(--muted);
      font: 11px Consolas, "SFMono-Regular", ui-monospace, monospace;
      margin: 4px 0 0;
      word-break: break-all;
    }}
    .status {{
      border: 1px solid var(--line);
      background: var(--surface-2);
      color: var(--text);
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 11px;
      white-space: nowrap;
    }}
    .agent-row.ok .status {{ border-color: rgba(79,209,123,.55); color: var(--green); }}
    .agent-row.warn .status {{ border-color: rgba(230,180,80,.55); color: var(--yellow); }}
    .agent-row.bad .status {{ border-color: rgba(255,107,107,.55); color: var(--red); }}
    .summary {{
      color: var(--text-soft);
      margin: 10px 0;
      line-height: 1.45;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
    }}
    section label, .handoff label {{
      display: block;
      color: var(--muted);
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: .04em;
      margin-bottom: 5px;
    }}
    section p, .handoff p {{
      margin: 0;
      font-size: 12px;
      line-height: 1.4;
    }}
    .handoff {{
      border-top: 1px solid rgba(42,46,56,.7);
      margin-top: 10px;
      padding-top: 9px;
    }}
    .activity {{
      border-top: 1px solid rgba(42,46,56,.7);
      margin-top: 10px;
      padding-top: 9px;
    }}
    .mini-event {{
      border: 1px solid var(--line-soft);
      background: #111318;
      border-radius: 8px;
      padding: 8px;
      margin-top: 6px;
    }}
    .mini-meta {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 11px;
    }}
    .mini-meta b {{
      color: var(--blue);
      font-weight: 560;
    }}
    .mini-message {{
      margin-top: 4px;
      font-size: 12px;
      color: var(--text-soft);
    }}
    .mini-detail {{
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .mini-empty {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 6px;
    }}
    .agent-warning {{
      border: 1px solid rgba(233,184,93,.36);
      background: rgba(233,184,93,.07);
      border-radius: 8px;
      padding: 8px;
      margin: 8px 0 10px;
    }}
    .agent-warning label {{
      color: #f0cb82;
    }}
    .warning-list {{
      display: grid;
      gap: 5px;
      padding: 10px 12px;
    }}
    .agent-warning .warning-list, .gate-issues .warning-list, .diff-row .warning-list {{
      padding: 6px 0 0;
    }}
    .warning {{
      border-left: 2px solid var(--yellow);
      color: #f1d79b;
      background: rgba(233,184,93,.06);
      padding: 6px 8px;
      font-size: 12px;
      line-height: 1.35;
    }}
    .gate-issues {{
      margin-top: 7px;
    }}
    .file-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }}
    code {{
      background: #101116;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 3px 5px;
      color: #cbd4e2;
      font: 11px Consolas, "SFMono-Regular", ui-monospace, monospace;
      min-width: 0;
      max-width: 100%;
      overflow-wrap: anywhere;
    }}
    .muted {{
      color: var(--muted);
    }}
    .ok-text {{
      color: var(--green);
    }}
    .empty {{
      padding: 16px;
      color: var(--muted);
    }}
    .nav-empty {{
      color: var(--muted);
      font-size: 13px;
      padding: 10px;
    }}
    @media (max-width: 860px) {{
      .shell {{ grid-template-columns: 1fr; }}
      aside {{ position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--line); }}
      .side-head {{ border-bottom: 0; }}
      .stats, .detail-grid, .queue-grid, .workflow-grid, .impact-grid, .diff-row, .command-row, .review-row, .recipe-row {{ grid-template-columns: 1fr; }}
      .row-actions {{ justify-content: flex-start; }}
    }}
    @media (min-width: 560px) and (max-width: 760px) {{
      .stats {{ grid-template-columns: repeat(4, minmax(58px, 1fr)); }}
      .stat {{ padding: 8px; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="side-head">
        <h1>Codex Agents</h1>
        <div class="workspace">{len(agents)} agents &middot; {active_count} active</div>
      </div>
      {render_agent_nav(agents)}
    </aside>
    <main class="content">
      <header>
        <div class="title-row">
          <div>
            <p class="eyebrow">Agent Dashboard</p>
            <h2>{esc(payload.get("title"))}</h2>
            <p class="sub">Updated {esc(generated_at)} &middot; refreshes every 5 seconds</p>
          </div>
          <div class="live-pill"><span></span>Live</div>
        </div>
        <p class="sub">{esc(payload.get("note"))}</p>
        {render_view_tabs(view)}
      </header>
      {render_main_view(payload, agents, counts, view, agent_ref)}
    </main>
  </div>
  <script>
    document.addEventListener("click", async (event) => {{
      const button = event.target.closest("[data-copy]");
      if (!button) return;
      try {{
        await navigator.clipboard.writeText(button.getAttribute("data-copy") || "");
        const original = button.textContent;
        button.textContent = "Copied";
        setTimeout(() => {{ button.textContent = original; }}, 1200);
      }} catch (error) {{
        button.textContent = "Copy failed";
      }}
    }});
  </script>
</body>
</html>
"""


def open_page(path: pathlib.Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_url(url: str) -> None:
    if os.name == "nt":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        webbrowser.open(url)


def load_open_state(path: pathlib.Path = DEFAULT_OPEN_STATE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_open_state(url: str, path: pathlib.Path = DEFAULT_OPEN_STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": url,
        "openedAt": utc_now(),
        "note": "Used to avoid opening duplicate dashboard browser tabs.",
    }
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def should_open_url(url: str, force_open: bool, path: pathlib.Path = DEFAULT_OPEN_STATE) -> bool:
    if force_open:
        return True
    state = load_open_state(path)
    if state.get("url") != url:
        return True
    opened_at = str(state.get("openedAt") or "")
    try:
        opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - opened.astimezone(timezone.utc)).total_seconds()
        return age_seconds > 12 * 60 * 60
    except ValueError:
        return False


def open_url_once(url: str, force_open: bool = False) -> bool:
    if not should_open_url(url, force_open):
        return False
    open_url(url)
    write_open_state(url)
    return True


def serve_dashboard(status_path: pathlib.Path, html_path: pathlib.Path, host: str, port: int, open_browser: bool, force_open: bool) -> int:
    handler = functools.partial(
        LiveDashboardHandler,
        directory=str(html_path.parent),
        status_path=status_path,
    )
    with ReusableThreadingHTTPServer((host, port), handler) as server:
        actual_port = server.server_address[1]
        url = f"http://{host}:{actual_port}/{html_path.name}"
        print(url, flush=True)
        print(str(html_path), flush=True)
        print(str(status_path), flush=True)
        if open_browser:
            opened = open_url_once(url, force_open)
            if not opened:
                print("dashboard browser tab already marked open; skipped opening another tab", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update a local Codex sub-agent dashboard.")
    parser.add_argument("--title", default="Codex Agent Dashboard")
    parser.add_argument("--agent", action="append", default=[], help="name|id|status|summary|ownership|changedFiles|tests|blockers|handoff")
    parser.add_argument("--agent-json", action="append", default=[], help="Agent JSON object/array, or @path to JSON.")
    parser.add_argument("--agent-json-file", action="append", default=[], help="Path to agent JSON object/array.")
    parser.add_argument("--event", action="append", default=[], help="agent|kind|message|detail|timestamp")
    parser.add_argument("--event-json", action="append", default=[], help="Event JSON object/array, or @path to JSON.")
    parser.add_argument("--event-json-file", action="append", default=[], help="Path to event JSON object/array.")
    parser.add_argument("--status-file", default=str(DEFAULT_STATUS))
    parser.add_argument("--html-file", default=str(DEFAULT_HTML))
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--force-open", action="store_true", help="Open a new browser tab even if the dashboard URL was already opened recently.")
    parser.add_argument("--keep-existing", action="store_true", help="Merge provided agents into existing status by id/name.")
    parser.add_argument("--serve", action="store_true", help="Run a local live dashboard server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--doctor", action="store_true", help="Print a dashboard health report with warnings, items to fix, blockers, and suggested actions.")
    parser.add_argument("--print-heartbeat-contract", action="store_true", help="Print the prompt block to include when spawning a future agent.")
    parser.add_argument("--agent-name", default="AGENT_NAME", help="Agent name to use with --print-heartbeat-contract.")
    parser.add_argument("--agent-id", default="", help="Agent id to use with --print-heartbeat-contract when already known.")
    parser.add_argument("--print-final-report-template", default="", help="Print a JSON final-report template for an agent id or name.")
    parser.add_argument("--plan-agent", action="append", default=[], help="name|summary|ownership|allowedFiles|doNotTouch|expectedOutputs|tests|priority|wave|recipe|status")
    parser.add_argument("--plan-agent-json", action="append", default=[], help="Planned-agent JSON object/array, or @path to JSON.")
    parser.add_argument("--plan-agent-json-file", action="append", default=[], help="Path to planned-agent JSON object/array.")
    parser.add_argument("--set-status", action="append", default=[], help="agent-id-or-name|status|note")
    parser.add_argument("--set-command-state", action="append", default=[], help="command-id|state|note, where state is pending, done, dismissed, or failed")
    parser.add_argument("--control-action", action="append", default=[], help="agent-id-or-name|action|note")
    parser.add_argument("--reconcile-agent-id", action="append", default=[], help="planned-agent-name-or-id|actual-agent-id|optional-display-name")
    parser.add_argument("--final-report-json", action="append", default=[], help="Final report JSON object/array, or @path to JSON.")
    parser.add_argument("--final-report-json-file", action="append", default=[], help="Path to final report JSON object/array.")
    parser.add_argument("--workflow-objective", default="", help="Set the run objective.")
    parser.add_argument("--concurrency-limit", type=int, default=0, help="Set the active sub-agent concurrency limit.")
    parser.add_argument("--manual-minutes-per-agent", type=int, default=0, help="Manual-work baseline for the impact/time-saved estimate.")
    parser.add_argument("--coordination-minutes-per-agent", type=int, default=-1, help="Agent orchestration overhead for the impact/time-saved estimate.")
    parser.add_argument("--focus-block-minutes", type=int, default=0, help="Focus-block length used by the impact scoreboard.")
    parser.add_argument("--impact-note", default="", help="Explain the assumptions behind the impact/time-saved estimate.")
    parser.add_argument("--scan-worktree", default="", help="Scan a git worktree for diff/worktree intelligence.")
    parser.add_argument("--scan-ignore", action="append", default=[], help="Additional git status path/glob pattern to ignore during --scan-worktree.")
    parser.add_argument("--archive-run-snapshot", action="store_true", help="Write an immutable timestamped copy of the current dashboard JSON under the local snapshots folder.")
    parser.add_argument("--export-second-brain", action="store_true", help="Append a run summary to the second-brain daily note.")
    parser.add_argument("--print-memory-summary", action="store_true", help="Print the second-brain run summary without writing it.")
    parser.add_argument("--print-recipe", default="", help="Print a reusable deployment recipe prompt by key.")
    args = parser.parse_args()
    status_path = pathlib.Path(args.status_file)
    html_path = pathlib.Path(args.html_file)

    if args.print_heartbeat_contract:
        print(heartbeat_contract(args.agent_name, args.agent_id))
        return 0

    if args.print_recipe:
        recipe = RECIPES.get(args.print_recipe)
        if not recipe:
            print(f"Unknown recipe: {args.print_recipe}")
            return 2
        outputs = ", ".join(recipe.get("outputs", []))
        print(f"{recipe['title']}\nPurpose: {recipe['purpose']}\nAgent type: {recipe['agentType']}\nExpected outputs: {outputs}")
        return 0

    if args.doctor:
        print(render_doctor_report(load_existing(status_path)))
        return 0

    if args.print_final_report_template:
        print(json.dumps(build_final_report_template(load_existing(status_path), args.print_final_report_template), indent=2))
        return 0

    if args.print_memory_summary:
        print(render_memory_summary(load_existing(status_path)))
        return 0

    try:
        agent_json_records = json_records_from_inputs(args.agent_json, args.agent_json_file, ["agents", "agent"])
        planned_json_records = json_records_from_inputs(args.plan_agent_json, args.plan_agent_json_file, ["agents", "plannedAgents", "planned_agents", "plan"])
        event_json_records = json_records_from_inputs(args.event_json, args.event_json_file, ["events", "activity"])
        final_report_records = json_records_from_inputs(args.final_report_json, args.final_report_json_file, ["reports", "finalReports", "final_reports", "agents"])
    except ValueError as error:
        parser.error(str(error))

    with status_lock(status_path):
        existing = load_existing(status_path)
        agents = [parse_agent(raw) for raw in args.agent] + [parse_agent_json(record) for record in agent_json_records]
        planned_agents = [parse_planned_agent(raw) for raw in args.plan_agent] + [parse_planned_agent_json(record) for record in planned_json_records]
        events = [parse_event(raw) for raw in args.event] + [parse_event_json(record) for record in event_json_records]

        if args.keep_existing:
            existing_agents = existing.get("agents", []) if isinstance(existing.get("agents"), list) else []
            agents = merge_agents(existing_agents, [*agents, *planned_agents])
            prior_events = existing.get("events", []) if isinstance(existing.get("events"), list) else []
            events = [*prior_events, *events] if events else prior_events
        elif planned_agents:
            agents = [*agents, *planned_agents]
        elif not agents:
            agents = existing.get("agents", []) if isinstance(existing.get("agents"), list) else []
            prior_events = existing.get("events", []) if isinstance(existing.get("events"), list) else []
            events = [*prior_events, *events] if events else prior_events

        payload = write_status(status_path, args.title, agents, events, base=existing)

        if args.workflow_objective:
            payload.setdefault("workflow", {})["objective"] = args.workflow_objective
        if args.concurrency_limit:
            payload.setdefault("workflow", {})["concurrencyLimit"] = max(1, args.concurrency_limit)
        if args.manual_minutes_per_agent:
            payload.setdefault("impact", {})["manualMinutesPerAgent"] = max(1, args.manual_minutes_per_agent)
        if args.coordination_minutes_per_agent >= 0:
            payload.setdefault("impact", {})["coordinationMinutesPerAgent"] = max(0, args.coordination_minutes_per_agent)
        if args.focus_block_minutes:
            payload.setdefault("impact", {})["focusBlockMinutes"] = max(1, args.focus_block_minutes)
        if args.impact_note:
            payload.setdefault("impact", {})["note"] = args.impact_note
        for raw in args.reconcile_agent_id:
            fields = [field.strip() for field in raw.split("|")]
            fields += [""] * (3 - len(fields))
            agent_key, actual_id, actual_name = fields[:3]
            reconcile_agent_id(payload, agent_key, actual_id, actual_name)
        for report in final_report_records:
            ingest_final_report(payload, report)
        for raw in args.set_status:
            fields = [field.strip() for field in raw.split("|")]
            fields += [""] * (3 - len(fields))
            agent_key, status, note = fields[:3]
            agent = find_agent(payload.get("agents", []), agent_key)
            if agent:
                if status.lower() == "reviewed":
                    mark_agent_reviewed(payload, agent, note)
                else:
                    set_agent_status(agent, status, note)
                    add_event(payload, agent.get("name") or agent_key, "status", f"Status set to {status}", note)
        for raw in args.set_command_state:
            fields = [field.strip() for field in raw.split("|")]
            fields += [""] * (4 - len(fields))
            command_id, state, note = fields[0], fields[1], fields[2]
            set_command_state(payload, command_id, state, note)
        for raw in args.control_action:
            fields = [field.strip() for field in raw.split("|")]
            fields += [""] * (3 - len(fields))
            agent_key, action, note = fields[:3]
            handle_control_action(payload, action, agent_key, note)
        if args.scan_worktree:
            scan_worktree(payload, args.scan_worktree, args.scan_ignore)
        if args.archive_run_snapshot:
            archive_run_snapshot(payload)
        if args.export_second_brain:
            export_second_brain(payload)
        write_payload(status_path, ensure_control_plane(payload))
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(payload), encoding="utf-8")

    if args.serve:
        return serve_dashboard(status_path, html_path, args.host, args.port, args.open, args.force_open)

    if args.open:
        open_url_once(str(html_path), args.force_open)

    print(str(html_path))
    print(str(status_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
