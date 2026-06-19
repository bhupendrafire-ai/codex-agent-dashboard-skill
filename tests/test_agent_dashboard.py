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
        self.assertNotIn("allowed edit paths are missing", "\n".join(agent_dashboard.dashboard_warnings(payload, payload["agents"])))

    def test_worker_missing_write_globs_is_reported(self) -> None:
        worker = agent_dashboard.parse_agent_json({
            "name": "Worker",
            "status": "running",
            "ownership": "src worker files",
            "changedFiles": ["pending"],
            "tests": "unit tests pending",
            "blockers": "None reported yet",
            "handoff": "Patch pending",
        })
        payload = self.payload(worker)

        warnings = "\n".join(agent_dashboard.dashboard_warnings(payload, payload["agents"]))
        self.assertIn("Worker: allowed edit paths are missing", warnings)

    def test_review_ready_row_evidence_replaces_noisy_final_report_gap(self) -> None:
        worker = agent_dashboard.parse_agent_json({
            "name": "PDF Arc Export",
            "status": "needs-review",
            "summary": "Closed PDF exporter gap for authored Arc entities",
            "ownership": "CadPdfDrawingExporter.cs;CadPdfDrawingExporterTests.cs",
            "changedFiles": [
                "src/CADMation.Core/Drafting/CadPdfDrawingExporter.cs",
                "tests/CADMation.Tests/CadPdfDrawingExporterTests.cs",
            ],
            "tests": "CadPdfDrawingExporterTests 5/5",
            "blockers": "None reported",
            "handoff": "Ready for final integration review",
        })
        payload = self.payload(worker)
        summary = agent_dashboard.health_summary(payload)

        warnings = "\n".join(summary["warnings"])
        self.assertEqual([], summary["finalReportGaps"])
        self.assertEqual([], summary["writeScopeGaps"])
        self.assertEqual([], summary["missingIds"])
        self.assertNotIn("final report", warnings)
        self.assertNotIn("allowed edit paths are missing", warnings)
        self.assertEqual(
            [
                "src/CADMation.Core/Drafting/CadPdfDrawingExporter.cs",
                "tests/CADMation.Tests/CadPdfDrawingExporterTests.cs",
            ],
            payload["agents"][0]["writeGlobs"],
        )

    def test_read_only_handoff_evidence_does_not_need_fake_write_globs(self) -> None:
        scout = agent_dashboard.parse_agent_json({
            "name": "NXT Command Coverage",
            "status": "needs-review",
            "summary": "Read-only command coverage matrix and migration wave plan completed",
            "ownership": "NXT Drafting command surface audit",
            "changedFiles": ["None"],
            "tests": "Read-only source/doc inspection; no tests run in this pass",
            "blockers": "No repo-local blocker for this audit.",
            "handoff": "Next owner should split implementation waves by core model and tests/docs.",
        })
        payload = self.payload(scout)
        summary = agent_dashboard.health_summary(payload)

        warnings = "\n".join(summary["warnings"])
        self.assertTrue(payload["agents"][0]["readOnly"])
        self.assertEqual([], agent_dashboard.review_gate_issues(payload["agents"][0]))
        self.assertEqual([], summary["finalReportGaps"])
        self.assertEqual([], summary["writeScopeGaps"])
        self.assertEqual([], summary["missingIds"])
        self.assertNotIn("final report", warnings)
        self.assertNotIn("allowed edit paths are missing", warnings)

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
        review_gap = agent_dashboard.parse_agent_json({
            "name": "Review Gap",
            "status": "needs-review",
            "ownership": "src worker files",
        })
        worker = agent_dashboard.parse_agent_json({
            "name": "Worker",
            "status": "running",
            "ownership": "src worker files",
            "changedFiles": ["pending"],
            "tests": "unit tests pending",
            "blockers": "None reported yet",
            "handoff": "Patch pending",
        })
        payload = self.payload(review_gap, worker, commands=[{
            "id": "cmd1",
            "agent": "Worker",
            "kind": "request-status",
            "message": "Request status",
            "createdAt": "2020-01-01T00:00:00+00:00",
            "state": "pending",
        }])

        report = agent_dashboard.render_doctor_report(payload)

        self.assertIn("Final reports needed: Review Gap", report)
        self.assertIn("Edit paths needed: Worker", report)
        self.assertIn("Old waiting actions: 1", report)
        self.assertIn("--print-final-report-template \"Review Gap\"", report)
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

    def test_impact_estimate_discounts_scouts_and_meta_work(self) -> None:
        worker = agent_dashboard.parse_agent_json({
            "name": "Worker",
            "status": "merged",
            "changedFiles": ["src/worker.py"],
            "tests": "unit tests passed",
            "blockers": "None reported",
            "handoff": "Merged",
        })
        scout = agent_dashboard.parse_agent_json({
            "name": "Release Evidence Scout",
            "status": "closed",
            "readOnly": True,
            "changedFiles": ["None"],
            "tests": "Read-only scan",
            "blockers": "None reported",
            "handoff": "Recommendation handed off",
        })
        orchestrator = agent_dashboard.parse_agent_json({
            "name": "Lead Orchestrator - Release Pack",
            "status": "merged",
            "changedFiles": ["eng/release.ps1"],
            "tests": "release tests passed",
            "blockers": "None reported",
            "handoff": "Pack merged",
        })
        payload = self.payload(worker, scout, orchestrator)

        impact = agent_dashboard.compute_impact(payload, payload["agents"], agent_dashboard.count_agents(payload["agents"]))

        self.assertEqual(90, impact["manualMinutes"])
        self.assertEqual(24, impact["coordinationMinutes"])
        self.assertEqual(66, impact["savedMinutes"])
        self.assertEqual(2.0, impact["effectiveSlices"])
        self.assertIn("lowered for read-only, lead, or no-edit work", impact["assumption"])

    def test_explicit_impact_estimates_are_not_discounted_by_slice_type(self) -> None:
        scout = agent_dashboard.parse_agent_json({
            "name": "Read Only Scout",
            "status": "merged",
            "readOnly": True,
            "manualMinutes": 30,
            "coordinationMinutes": 5,
            "tests": "Read-only scan",
            "blockers": "None reported",
            "handoff": "Recommendation handed off",
        })
        payload = self.payload(scout)

        impact = agent_dashboard.compute_impact(payload, payload["agents"], agent_dashboard.count_agents(payload["agents"]))

        self.assertEqual(30, impact["manualMinutes"])
        self.assertEqual(5, impact["coordinationMinutes"])
        self.assertEqual(25, impact["savedMinutes"])
        self.assertEqual(0.7, impact["effectiveSlices"])

    def test_doctor_tells_lead_to_stop_spawning_when_only_external_blockers_remain(self) -> None:
        agents = [
            agent_dashboard.parse_agent_json({
                "name": f"Gate {index}",
                "status": "merged",
                "changedFiles": [f"eng/gate{index}.ps1"],
                "tests": "focused tests passed",
                "blockers": "External live evidence is still missing: signed installer proof and clean-PC smoke.",
                "blockerType": "external",
                "handoff": "Operator must attach returned evidence, then rerun the final gate.",
            })
            for index in range(4)
        ]
        payload = self.payload(*agents)

        report = agent_dashboard.render_doctor_report(payload)
        convergence = agent_dashboard.health_summary(payload)["convergence"]

        self.assertTrue(convergence["onlyExternal"])
        self.assertEqual("external evidence needed", convergence["status"])
        self.assertIn("Convergence: external evidence needed", report)
        self.assertIn("external/live=4", report)
        self.assertIn("Stop spawning more repo-local agents", report)

    def test_local_blocker_prevents_external_only_stop_advice(self) -> None:
        local = agent_dashboard.parse_agent_json({
            "name": "Local Fix",
            "status": "blocked",
            "changedFiles": ["src/gate.py"],
            "tests": "unit test failure reproduced",
            "blockers": "Local implementation gap: failing test in src/gate.py.",
            "blockerType": "local",
            "handoff": "Patch src/gate.py and rerun unit tests.",
        })
        external = agent_dashboard.parse_agent_json({
            "name": "Signing",
            "status": "merged",
            "changedFiles": ["eng/signing.ps1"],
            "tests": "signing handoff tests passed",
            "blockers": "External signing certificate is missing.",
            "blockerType": "external",
            "handoff": "Operator provides certificate.",
        })
        payload = self.payload(local, external)

        report = agent_dashboard.render_doctor_report(payload)
        convergence = agent_dashboard.health_summary(payload)["convergence"]

        self.assertFalse(convergence["onlyExternal"])
        self.assertEqual("local work remains", convergence["status"])
        self.assertIn("local=1, external/live=1", report)
        self.assertNotIn("Stop spawning more repo-local agents", report)

    def test_unclear_blockers_trigger_clarification_before_more_waves(self) -> None:
        agents = [
            agent_dashboard.parse_agent_json({
                "name": f"Gate {index}",
                "status": "merged",
                "changedFiles": [f"docs/gate{index}.md"],
                "tests": "docs checked",
                "blockers": "Package evidence is absent and owner is unknown.",
                "handoff": "Lead should decide who owns the missing evidence.",
            })
            for index in range(3)
        ]
        payload = self.payload(*agents)

        report = agent_dashboard.render_doctor_report(payload)
        convergence = agent_dashboard.health_summary(payload)["convergence"]

        self.assertEqual("clarify blocker ownership", convergence["status"])
        self.assertIn("clarify blocker ownership", report)
        self.assertIn("Clarify mixed or unclear blockers", report)
        self.assertNotIn("Nothing needs fixing right now", report)


if __name__ == "__main__":
    unittest.main()
