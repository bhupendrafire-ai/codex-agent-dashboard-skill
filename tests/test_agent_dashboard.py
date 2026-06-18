from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "agent_dashboard.py"
SPEC = importlib.util.spec_from_file_location("agent_dashboard", MODULE_PATH)
agent_dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(agent_dashboard)


class AgentDashboardControlPlaneTests(unittest.TestCase):
    def payload(self, *agents: dict, commands: list[dict] | None = None) -> dict:
        return agent_dashboard.ensure_control_plane({
            "agents": list(agents),
            "events": [],
            "commands": commands or [],
        })

    def test_read_only_review_gate_does_not_require_changed_files_or_write_globs(self) -> None:
        agent = agent_dashboard.parse_agent_json({
            "name": "Read Only Scout",
            "status": "needs-review",
            "readOnly": True,
            "ownership": "release docs",
            "allowedFiles": ["docs/**"],
            "expectedOutputs": ["findings", "handoff"],
            "tests": "Read-only rg scan; no tests run",
            "blockers": "None reported",
            "handoff": "Recommendation handed to lead",
        })
        payload = self.payload(agent)

        self.assertEqual([], agent_dashboard.review_gate_issues(payload["agents"][0]))
        self.assertNotIn("missing planned write globs", "\n".join(agent_dashboard.dashboard_warnings(payload, payload["agents"])))

    def test_worker_missing_write_globs_is_reported(self) -> None:
        worker = agent_dashboard.parse_agent_json({
            "name": "Worker",
            "status": "needs-review",
            "ownership": "src worker files",
            "changedFiles": ["src/worker.py"],
            "tests": "unit tests passed",
            "blockers": "None reported",
            "handoff": "Ready for review",
        })
        payload = self.payload(worker)

        warnings = "\n".join(agent_dashboard.dashboard_warnings(payload, payload["agents"]))
        self.assertIn("Worker: missing planned write globs", warnings)

    def test_ingest_final_report_preserves_existing_read_only_flag_when_unspecified(self) -> None:
        scout = agent_dashboard.parse_agent_json({
            "name": "Scout",
            "status": "running",
            "readOnly": True,
            "ownership": "docs",
            "allowedFiles": ["docs/**"],
        })
        payload = self.payload(scout)

        agent_dashboard.ingest_final_report(payload, {
            "name": "Scout",
            "summary": "Scan complete",
            "tests": "Read-only scan",
            "blockers": "None reported",
            "handoff": "Lead can implement recommendation",
        })

        self.assertTrue(payload["agents"][0]["readOnly"])
        self.assertEqual([], agent_dashboard.review_gate_issues(payload["agents"][0]))

    def test_final_report_template_uses_existing_agent_evidence(self) -> None:
        worker = agent_dashboard.parse_agent_json({
            "name": "Worker",
            "id": "abc123",
            "status": "needs-review",
            "summary": "Patched worker",
            "ownership": "src worker files",
            "writeGlobs": ["src/**"],
            "changedFiles": ["src/worker.py"],
            "tests": "unit tests passed",
            "blockers": "None reported",
            "handoff": "Lead review",
        })
        template = agent_dashboard.build_final_report_template(self.payload(worker), "Worker")

        self.assertEqual("Worker", template["name"])
        self.assertEqual("abc123", template["id"])
        self.assertEqual("completed", template["status"])
        self.assertEqual(["src/worker.py"], template["changedFiles"])
        self.assertEqual("None reported", template["blockers"])

    def test_doctor_report_surfaces_hygiene_gaps_and_stale_commands(self) -> None:
        worker = agent_dashboard.parse_agent_json({
            "name": "Worker",
            "status": "needs-review",
            "ownership": "src worker files",
            "changedFiles": ["src/worker.py"],
            "tests": "unit tests passed",
            "blockers": "None reported",
            "handoff": "Ready for review",
        })
        payload = self.payload(worker, commands=[{
            "id": "cmd1",
            "agent": "Worker",
            "kind": "request-status",
            "message": "Request status",
            "createdAt": "2020-01-01T00:00:00+00:00",
            "state": "pending",
        }])

        report = agent_dashboard.render_doctor_report(payload)

        self.assertIn("Final reports missing: Worker", report)
        self.assertIn("Write scopes missing: Worker", report)
        self.assertIn("Stale pending commands: 1", report)
        self.assertIn("--print-final-report-template \"Worker\"", report)
        self.assertIn("--set-command-state \"cmd1|dismissed|superseded or completed\"", report)

    def test_set_command_state_updates_pending_command(self) -> None:
        payload = self.payload(commands=[{
            "id": "cmd1",
            "agent": "Worker",
            "kind": "request-status",
            "message": "Request status",
            "createdAt": "2020-01-01T00:00:00+00:00",
            "state": "pending",
        }])

        changed = agent_dashboard.set_command_state(payload, "cmd1", "dismissed", "superseded")

        self.assertTrue(changed)
        self.assertEqual("dismissed", payload["commands"][0]["state"])
        self.assertEqual(1, len(payload["events"]))

    def test_archive_run_snapshot_writes_timestamped_copy(self) -> None:
        payload = self.payload(agent_dashboard.parse_agent_json({"name": "Worker", "status": "running"}))
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = agent_dashboard.archive_run_snapshot(payload, pathlib.Path(temp_dir))

            self.assertTrue(snapshot.exists())
            loaded = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual("Worker", loaded["agents"][0]["name"])
            self.assertIn("snapshots", payload)


if __name__ == "__main__":
    unittest.main()
