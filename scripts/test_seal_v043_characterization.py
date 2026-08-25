import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from seal_v043_characterization import (  # noqa: E402
    CharacterizationSealError,
    build_characterization_receipt,
    verify_characterization_receipt,
)


class SealV043CharacterizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary_directory.name)
        characterization = self.repo_root / "characterization" / "v0.43"
        characterization.mkdir(parents=True)

        self.artifacts = {
            "source_receipt": characterization / "vanilla-baseline-source-receipt.json",
            "process_receipt": characterization / "vanilla-baseline-process-receipt.json",
            "failure_injection_receipt": characterization
            / "vanilla-baseline-failure-injection-receipt.json",
        }
        for name, path in self.artifacts.items():
            path.write_text(json.dumps({"artifact": name}) + "\n", encoding="utf-8")

        self.results_path = characterization / "vanilla-baseline-test-results.json"
        self.results = {
            "schema_version": "fiet-rindexer-vanilla-test-results/v1",
            "created_at": "2026-08-25T08:00:00Z",
            "source_under_test": {
                "canonical_commit": "4f441289b83855c357239d2729fb725a56c3060b",
                "canonical_root_tree": "80a2698f6be13949d84d920b01c02125af598d09",
                "runtime_paths_byte_identical_to_canonical": True,
                "runtime_paths": ["Cargo.toml", "Cargo.lock", "core/Cargo.toml", "core/src"],
            },
            "toolchain": {"rustc": "rustc 1.97.0", "target": "x86_64-unknown-linux-gnu"},
            "runs": [
                {"kind": "workspace_default_features", "result": "pass"},
                {"kind": "workspace_all_features", "result": "pass"},
                {"kind": "docker_e2e", "result": "pass"},
            ],
            "remaining_before_immutable_characterization_receipt": [],
            "verdict": "pass",
        }
        self._write_results()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_results(self):
        self.results_path.write_text(
            json.dumps(self.results, indent=2) + "\n", encoding="utf-8"
        )

    def test_seal_binds_canonical_identity_and_every_retained_artifact(self):
        receipt = build_characterization_receipt(self.repo_root, self.results_path)

        self.assertEqual(
            receipt["schema_version"],
            "fiet-rindexer-v043-characterization-receipt/v1",
        )
        self.assertEqual(receipt["verdict"], "characterized")
        self.assertFalse(receipt["release_authorized"])
        self.assertEqual(
            receipt["canonical_source"]["commit"],
            "4f441289b83855c357239d2729fb725a56c3060b",
        )
        self.assertEqual(
            set(receipt["artifacts"]),
            {
                "source_receipt",
                "process_receipt",
                "failure_injection_receipt",
                "test_results",
            },
        )
        self.assertEqual(
            verify_characterization_receipt(self.repo_root, receipt), []
        )

    def test_incomplete_campaign_cannot_be_sealed(self):
        self.results["remaining_before_immutable_characterization_receipt"] = [
            "Docker E2E suite"
        ]
        self.results["verdict"] = "partial_pass"
        self._write_results()

        with self.assertRaisesRegex(CharacterizationSealError, "campaign is incomplete"):
            build_characterization_receipt(self.repo_root, self.results_path)

    def test_changed_artifact_invalidates_a_sealed_receipt(self):
        receipt = build_characterization_receipt(self.repo_root, self.results_path)
        self.artifacts["process_receipt"].write_text(
            json.dumps({"artifact": "tampered"}) + "\n", encoding="utf-8"
        )

        errors = verify_characterization_receipt(self.repo_root, receipt)
        self.assertTrue(any("process_receipt SHA-256 mismatch" in error for error in errors))

    def test_receipt_summary_must_match_bound_test_results(self):
        receipt = build_characterization_receipt(self.repo_root, self.results_path)
        receipt["qualification_runs"] = receipt["qualification_runs"][:-1]

        errors = verify_characterization_receipt(self.repo_root, receipt)
        self.assertIn("qualification_runs do not match bound test results", errors)

    def test_runtime_drift_cannot_be_sealed(self):
        self.results["source_under_test"]["runtime_paths_byte_identical_to_canonical"] = False
        self._write_results()

        with self.assertRaisesRegex(CharacterizationSealError, "runtime paths are not byte-identical"):
            build_characterization_receipt(self.repo_root, self.results_path)

    def test_missing_required_run_cannot_be_sealed(self):
        incomplete = copy.deepcopy(self.results)
        incomplete["runs"] = incomplete["runs"][:-1]
        self.results = incomplete
        self._write_results()

        with self.assertRaisesRegex(CharacterizationSealError, "missing passing required runs"):
            build_characterization_receipt(self.repo_root, self.results_path)


if __name__ == "__main__":
    unittest.main()
