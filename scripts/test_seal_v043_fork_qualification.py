import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from seal_v043_fork_qualification import (  # noqa: E402
    ForkQualificationSealError,
    QualificationLayout,
    build_fork_qualification_receipt,
    stage_candidate_governance_snapshots,
    verify_fork_qualification_receipt,
)


class SealV043ForkQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary_directory.name)
        qualification = self.repo_root / "qualification" / "v0.43"
        qualification.mkdir(parents=True)

        self.live_paths = {
            "inventory": self.repo_root / "downstream-patch-inventory.json",
            "ledger": self.repo_root / "downstream-patches.json",
            "governed_paths": self.repo_root / "governed-paths.json",
        }
        self.artifact_paths = {
            "inventory": qualification / "candidate-downstream-patch-inventory.json",
            "ledger": qualification / "candidate-downstream-patches.json",
            "governed_paths": qualification / "candidate-governed-paths.json",
            "embedded_lifecycle_receipt": qualification
            / "candidate-embedded-lifecycle-receipt.json",
        }
        artifact_values = {
            "inventory": {"artifact": "inventory"},
            "ledger": {
                "schema_version": "fiet-rindexer-downstream-patch-ledger/v1",
                "release": {"state": "candidate"},
                "patches": [{"audit_disposition": "ported"}],
            },
            "governed_paths": {"artifact": "governed_paths"},
            "embedded_lifecycle_receipt": {"verdict": "pass"},
        }
        for name, path in self.live_paths.items():
            path.write_text(json.dumps(artifact_values[name]) + "\n", encoding="utf-8")
        stage_candidate_governance_snapshots(self.repo_root)
        self.artifact_paths["embedded_lifecycle_receipt"].write_text(
            json.dumps(artifact_values["embedded_lifecycle_receipt"]) + "\n",
            encoding="utf-8",
        )

        self.results_path = qualification / "candidate-test-results.json"
        self.results = {
            "schema_version": "fiet-rindexer-v043-fork-test-results/v1",
            "created_at": "2026-08-25T12:00:00Z",
            "source_under_test": {
                "commit": "1" * 40,
                "tree": "2" * 40,
                "canonical_baseline_commit": "4f441289b83855c357239d2729fb725a56c3060b",
                "canonical_baseline_tree": "80a2698f6be13949d84d920b01c02125af598d09",
            },
            "toolchain": {
                "rustc": "rustc 1.97.0",
                "cargo_nextest": "cargo-nextest 0.9.143",
                "host": "x86_64-unknown-linux-gnu",
            },
            "runs": [
                {"kind": kind, "command": kind, "result": "pass"}
                for kind in (
                    "governance_schema_and_ancestry",
                    "clippy_workspace_all_targets",
                    "rindexer_package",
                    "exact_cursor_postgres",
                    "callback_failure_ordering",
                    "embedded_lifecycle_process",
                    "unexpected_live_eof_process",
                    "workspace_default_features",
                    "workspace_all_features",
                    "docker_e2e",
                )
            ],
            "hard_gate_failures": [],
            "verdict": "pass",
        }
        self._write_results()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_results(self):
        self.results_path.write_text(
            json.dumps(self.results, indent=2) + "\n", encoding="utf-8"
        )

    def test_seal_binds_candidate_baseline_artifacts_and_runs(self):
        receipt = build_fork_qualification_receipt(
            self.repo_root, self.results_path
        )

        self.assertEqual(
            receipt["schema_version"],
            "fiet-rindexer-v043-fork-qualification-receipt/v1",
        )
        self.assertEqual(receipt["verdict"], "qualified")
        self.assertFalse(receipt["release_authorized"])
        self.assertEqual(receipt["downstream_source"]["commit"], "1" * 40)
        self.assertEqual(
            set(receipt["artifacts"]),
            {
                "inventory",
                "ledger",
                "governed_paths",
                "embedded_lifecycle_receipt",
                "test_results",
            },
        )
        self.assertEqual(
            verify_fork_qualification_receipt(self.repo_root, receipt), []
        )

    def test_missing_required_run_cannot_be_sealed(self):
        self.results["runs"] = self.results["runs"][:-1]
        self._write_results()

        with self.assertRaisesRegex(
            ForkQualificationSealError, "missing passing required runs: docker_e2e"
        ):
            build_fork_qualification_receipt(self.repo_root, self.results_path)

    def test_hard_gate_failure_cannot_be_sealed(self):
        self.results["hard_gate_failures"] = ["unexpected live EOF exited zero"]
        self.results["verdict"] = "fail"
        self._write_results()

        with self.assertRaisesRegex(
            ForkQualificationSealError, "qualification hard gates failed"
        ):
            build_fork_qualification_receipt(self.repo_root, self.results_path)

    def test_artifact_drift_invalidates_receipt(self):
        receipt = build_fork_qualification_receipt(
            self.repo_root, self.results_path
        )
        self.artifact_paths["ledger"].write_text(
            json.dumps({"artifact": "tampered"}) + "\n", encoding="utf-8"
        )

        errors = verify_fork_qualification_receipt(self.repo_root, receipt)
        self.assertIn("ledger SHA-256 mismatch", errors)

    def test_later_live_release_metadata_does_not_rewrite_candidate_snapshot(self):
        receipt = build_fork_qualification_receipt(
            self.repo_root, self.results_path
        )
        live_ledger = json.loads(self.live_paths["ledger"].read_text())
        live_ledger["release"]["state"] = "qualified"
        self.live_paths["ledger"].write_text(
            json.dumps(live_ledger) + "\n", encoding="utf-8"
        )

        self.assertEqual(
            verify_fork_qualification_receipt(self.repo_root, receipt), []
        )
        with self.assertRaisesRegex(
            ForkQualificationSealError, "refusing to overwrite retained candidate ledger"
        ):
            stage_candidate_governance_snapshots(self.repo_root)

    def test_receipt_runs_must_match_test_results(self):
        receipt = build_fork_qualification_receipt(
            self.repo_root, self.results_path
        )
        receipt["qualification_runs"] = copy.deepcopy(
            receipt["qualification_runs"][:-1]
        )

        errors = verify_fork_qualification_receipt(self.repo_root, receipt)
        self.assertIn("qualification_runs do not match bound test results", errors)

    def test_replacement_attempt_uses_independent_retained_artifacts(self):
        legacy_inventory = self.artifact_paths["inventory"].read_bytes()
        layout = QualificationLayout.create(
            "qualification/v0.43.0-2",
            "fiet/exact-cursor-seed-no-start-v0.43",
        )

        stage_candidate_governance_snapshots(self.repo_root, layout)
        replacement_lifecycle = self.repo_root / layout.artifact_paths[
            "embedded_lifecycle_receipt"
        ]
        replacement_lifecycle.write_text(
            json.dumps({"verdict": "pass"}) + "\n", encoding="utf-8"
        )
        replacement_results = self.repo_root / layout.artifact_paths["test_results"]
        replacement_results.write_text(
            json.dumps(self.results, indent=2) + "\n", encoding="utf-8"
        )

        receipt = build_fork_qualification_receipt(
            self.repo_root, replacement_results, layout
        )

        self.assertEqual(
            receipt["downstream_source"]["branch"],
            "fiet/exact-cursor-seed-no-start-v0.43",
        )
        self.assertEqual(
            receipt["artifacts"]["inventory"]["path"],
            "qualification/v0.43.0-2/candidate-downstream-patch-inventory.json",
        )
        self.assertEqual(
            self.artifact_paths["inventory"].read_bytes(), legacy_inventory
        )
        self.assertEqual(
            verify_fork_qualification_receipt(self.repo_root, receipt), []
        )

    def test_qualification_directory_must_be_safe_and_repository_relative(self):
        for unsafe_path in (
            "/tmp/qualification",
            "../qualification/v0.43.0-2",
            "qualification/../v0.43.0-2",
            "qualification",
        ):
            with self.subTest(path=unsafe_path), self.assertRaises(
                ForkQualificationSealError
            ):
                QualificationLayout.create(
                    unsafe_path, "fiet/exact-cursor-seed-no-start-v0.43"
                )

    def test_candidate_branch_must_be_a_safe_fiet_branch(self):
        for unsafe_branch in (
            "main",
            "fiet/../main",
            "fiet/a..b",
            "fiet/Bad Branch",
        ):
            with self.subTest(branch=unsafe_branch), self.assertRaises(
                ForkQualificationSealError
            ):
                QualificationLayout.create("qualification/v0.43.0-2", unsafe_branch)


if __name__ == "__main__":
    unittest.main()
