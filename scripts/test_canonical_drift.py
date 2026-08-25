from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.report_canonical_drift import compare_canonical


class CanonicalDriftTests(unittest.TestCase):
    def test_report_is_read_only_and_identifies_candidate_absorption_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            canonical = Path(directory) / "canonical"
            subprocess.run(["git", "init", "--quiet", canonical], check=True)
            subprocess.run(["git", "-C", canonical, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", canonical, "config", "user.email", "test@example.invalid"], check=True)
            (canonical / "core/src/indexer").mkdir(parents=True)
            (canonical / "core/src/indexer/native_transfer.rs").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "-C", canonical, "add", "."], check=True)
            subprocess.run(["git", "-C", canonical, "commit", "--quiet", "-m", "baseline"], check=True)
            subprocess.run(["git", "-C", canonical, "tag", "v0.43.0"], check=True)
            baseline = subprocess.check_output(["git", "-C", canonical, "rev-parse", "HEAD"], text=True).strip()
            tree = subprocess.check_output(["git", "-C", canonical, "rev-parse", "HEAD^{tree}"], text=True).strip()

            (canonical / "core/src/indexer/native_transfer.rs").write_text("candidate absorption\n", encoding="utf-8")
            subprocess.run(["git", "-C", canonical, "commit", "--quiet", "-am", "fix: native transfer"], check=True)
            subprocess.run(["git", "-C", canonical, "tag", "v0.44.0"], check=True)
            refs_before = subprocess.check_output(["git", "-C", canonical, "show-ref"], text=True)

            ledger = {
                "canonical": {
                    "repository": str(canonical),
                    "tag": "v0.43.0",
                    "commit": baseline,
                    "tree": tree,
                },
                "patches": [
                    {
                        "id": "native-transfer",
                        "audit_disposition": "ported",
                        "affected_paths": ["core/src/indexer/native_transfer.rs"],
                    }
                ],
            }
            report = compare_canonical(ledger)

            self.assertIn("Latest canonical tag: `v0.44.0`", report)
            self.assertIn("Candidate absorption: `native-transfer`", report)
            self.assertIn("No branch, pull request, tag, merge, or deployment was created.", report)
            refs_after = subprocess.check_output(["git", "-C", canonical, "show-ref"], text=True)
            self.assertEqual(refs_before, refs_after)


if __name__ == "__main__":
    unittest.main()
