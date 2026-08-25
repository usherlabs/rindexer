from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.verify_canonical_contribution import validate_contribution


class CanonicalContributionTests(unittest.TestCase):
    def test_canonical_based_patch_passes_and_downstream_ancestry_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "--quiet", repo], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.invalid"], check=True)
            (repo / "file").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo, "add", "file"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "--quiet", "-m", "baseline"], check=True)
            baseline = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()

            subprocess.run(["git", "-C", repo, "switch", "--quiet", "-c", "downstream"], check=True)
            (repo / "governance").write_text("downstream only\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo, "add", "governance"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "--quiet", "-m", "downstream"], check=True)
            downstream_tip = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()

            subprocess.run(["git", "-C", repo, "switch", "--quiet", "-c", "upstream/good", baseline], check=True)
            (repo / "file").write_text("portable\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo, "commit", "--quiet", "-am", "portable"], check=True)
            good = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
            self.assertEqual(validate_contribution(repo, baseline, good, downstream_tip), [])

            subprocess.run(["git", "-C", repo, "switch", "--quiet", "-c", "upstream/bad", downstream_tip], check=True)
            (repo / "file").write_text("portable after downstream\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo, "commit", "--quiet", "-am", "portable"], check=True)
            bad = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
            errors = validate_contribution(repo, baseline, bad, downstream_tip)
            self.assertTrue(any("downstream-only ancestry" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
