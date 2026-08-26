from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.validate_downstream_governance import (
    CANONICAL_COMMIT,
    CANONICAL_TAG,
    CANONICAL_TREE,
    canonical_json_bytes,
    validate_active_ancestry,
    validate_changed_paths,
    validate_governance,
    validate_remote_release_identity,
    validate_transition,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _inventory() -> dict[str, object]:
    return {
        "schema_version": "fiet-rindexer-pre-convergence-inventory/v1",
        "source": {
            "repository": "https://github.com/outerlook/rindexer",
            "branch": "fix/multi-indexed-filter",
            "base_commit": "bd127841c771f0d76f93a0972282f95780cbccdc",
            "head_commit": "e4c4b14b9a0d067da12778ff959053a01c700d88",
        },
        "items": [
            {
                "id": "outerlook-5583ca2-native-transfer-end-block",
                "commit": "5583ca26e3c7422258a298e65cdbad36e7950fe1",
                "parents": ["bd127841c771f0d76f93a0972282f95780cbccdc"],
                "title": "fix: use calculated end_block for native_transfer_block_fetch",
            }
        ],
    }


def _ledger(inventory: dict[str, object]) -> dict[str, object]:
    inventory_hash = hashlib.sha256(canonical_json_bytes(inventory)).hexdigest()
    return {
        "$schema": "./schemas/downstream-patches.schema.json",
        "schema_version": "fiet-rindexer-downstream-patch-ledger/v1",
        "canonical": {
            "repository": "https://github.com/joshstevens19/rindexer",
            "tag": CANONICAL_TAG,
            "commit": CANONICAL_COMMIT,
            "tree": CANONICAL_TREE,
        },
        "inventory": {
            "path": "downstream-patch-inventory.json",
            "canonical_sha256": inventory_hash,
        },
        "release": {
            "branch": "fiet/v0.43",
            "state": "audit",
            "tag": None,
            "peeled_commit": None,
            "peeled_tree": None,
        },
        "governed_paths": "governed-paths.json",
        "patches": [
            {
                "id": "outerlook-5583ca2-native-transfer-end-block",
                "title": "Bound native-transfer historical ranges",
                "owner": "usherlabs",
                "rationale": "Audit the old bounded native-transfer invariant.",
                "audit_disposition": "pending",
                "release_state": "pending",
                "source": {
                    "repository": "https://github.com/outerlook/rindexer",
                    "branch": "fix/multi-indexed-filter",
                    "commits": ["5583ca26e3c7422258a298e65cdbad36e7950fe1"],
                },
                "affected_paths": ["core/src/indexer/native_transfer.rs"],
                "affected_behaviors": ["bounded historical native-transfer scan"],
                "canonical_baseline": {
                    "tag": CANONICAL_TAG,
                    "commit": CANONICAL_COMMIT,
                    "tree": CANONICAL_TREE,
                },
                "requirement_ids": ["rindexer-indexing-integrity:native-transfer-engine-capability-and-maker-policy"],
                "regression_tests": [
                    {
                        "id": "native-transfer-range",
                        "path": "scripts/test_downstream_governance.py",
                        "command": "python3 -m unittest scripts/test_downstream_governance.py",
                    }
                ],
                "upstream": {"status": "not_assessed"},
                "evidence": [],
                "downstream_commits": [],
                "ancestry_markers": [],
            }
        ],
    }


def _governed_paths() -> dict[str, object]:
    return {
        "schema_version": "fiet-rindexer-governed-paths/v1",
        "behavioral_paths": ["core/src", "cli/src", "Cargo.toml", "Cargo.lock"],
        "non_behavioral_exclusions": [
            ".github/workflows/downstream-governance.yml",
            "downstream-patches.json",
            "downstream-patch-inventory.json",
            "governed-paths.json",
            "schemas",
            "scripts",
            "FIET-DOWNSTREAM.md",
        ],
    }


def _fiet_authored_patch() -> dict[str, object]:
    return {
        "id": "fiet-maker-controlled-embedded-lifecycle",
        "title": "Return embedded lifecycle control to Maker",
        "owner": "usherlabs",
        "rationale": "Keep process signals, health, drain deadlines, and exit status caller-owned.",
        "audit_disposition": "ported",
        "release_state": "active",
        "source": {
            "repository": "https://github.com/usherlabs/fiet-maker",
            "branch": "docs/phase-1-schema-specification-vocabulary",
            "commits": ["bba8af53009a665323974b0bb39840297073eadc"],
        },
        "affected_paths": ["core/src/start.rs", "core/src/lib.rs"],
        "affected_behaviors": ["caller-owned embedded lifecycle"],
        "canonical_baseline": {
            "tag": CANONICAL_TAG,
            "commit": CANONICAL_COMMIT,
            "tree": CANONICAL_TREE,
        },
        "requirement_ids": [
            "rindexer-indexing-integrity:maker-controlled-embedded-lifecycle"
        ],
        "regression_tests": [
            {
                "id": "embedded-lifecycle-governance",
                "path": "scripts/test_downstream_governance.py",
                "command": "python3 -m unittest scripts/test_downstream_governance.py",
            }
        ],
        "upstream": {"status": "planned"},
        "evidence": ["scripts/test_qualify_embedded_lifecycle.py"],
        "downstream_commits": ["1" * 40],
        "ancestry_markers": ["FIET-PATCH:maker-controlled-embedded-lifecycle"],
    }


class DownstreamGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inventory = _inventory()
        self.ledger = _ledger(self.inventory)
        self.paths = _governed_paths()

    def test_outerlook_merge_head_has_no_independent_tree_delta(self) -> None:
        receipt_path = (
            REPO_ROOT
            / "characterization"
            / "v0.43"
            / "outerlook-merge-head-audit.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

        self.assertEqual(
            receipt["merge_commit"],
            "e4c4b14b9a0d067da12778ff959053a01c700d88",
        )
        self.assertEqual(
            receipt["parents"],
            [
                "cbb88d2eed9f33792a4be2da5bc2e78c82a398aa",
                "986998e3d98f04fd1a4aa749e9bf9deb88ff63ce",
            ],
        )
        self.assertEqual(receipt["merge_tree"], receipt["second_parent_tree"])
        self.assertFalse(receipt["independent_tree_delta"])

    def test_workflow_characterization_anchor_is_reachable_and_tree_stable(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "downstream-governance.yml").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"Prepare immutable v0\.43 characterization worktree.*?\n\s+([0-9a-f]{40})",
            workflow,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "workflow characterization anchor is missing")
        anchor = match.group(1)

        ancestry = subprocess.run(
            ["git", "-C", REPO_ROOT, "merge-base", "--is-ancestor", anchor, "HEAD"],
            check=False,
        )
        self.assertEqual(ancestry.returncode, 0, "workflow characterization anchor is not in active ancestry")
        tree = subprocess.check_output(
            ["git", "-C", REPO_ROOT, "rev-parse", f"{anchor}^{{tree}}"], text=True
        ).strip()
        self.assertEqual(tree, "dad322becf8dfe7288a3128105234cf2231670bc")

    def test_pending_inventory_is_valid_for_audit_but_not_release(self) -> None:
        self.assertEqual(validate_governance(self.ledger, self.inventory, self.paths, REPO_ROOT), [])
        errors = validate_governance(self.ledger, self.inventory, self.paths, REPO_ROOT, release=True)
        self.assertTrue(any("pending" in error for error in errors), errors)

    def test_candidate_state_rejects_pending_inventory(self) -> None:
        self.ledger["release"]["state"] = "candidate"

        errors = validate_governance(self.ledger, self.inventory, self.paths, REPO_ROOT)

        self.assertTrue(
            any("candidate state is ineligible while patches remain pending" in error for error in errors),
            errors,
        )

    def test_duplicate_patch_ids_are_rejected(self) -> None:
        self.ledger["patches"].append(copy.deepcopy(self.ledger["patches"][0]))
        errors = validate_governance(self.ledger, self.inventory, self.paths, REPO_ROOT)
        self.assertTrue(any("duplicate patch id" in error for error in errors), errors)

    def test_fiet_authored_patch_may_extend_hashed_outerlook_inventory(self) -> None:
        self.ledger["patches"].append(_fiet_authored_patch())

        self.assertEqual(
            validate_governance(
                self.ledger,
                self.inventory,
                self.paths,
                REPO_ROOT,
                verify_repository=False,
            ),
            [],
        )

    def test_non_fiet_extra_patch_is_rejected(self) -> None:
        extra = _fiet_authored_patch()
        extra["id"] = "custom-maker-controlled-lifecycle"
        self.ledger["patches"].append(extra)

        errors = validate_governance(
            self.ledger,
            self.inventory,
            self.paths,
            REPO_ROOT,
            verify_repository=False,
        )
        self.assertTrue(any("non-inventory patch IDs must use fiet-" in error for error in errors), errors)

    def test_missing_inventory_patch_remains_rejected(self) -> None:
        self.ledger["patches"] = []

        errors = validate_governance(
            self.ledger,
            self.inventory,
            self.paths,
            REPO_ROOT,
            verify_repository=False,
        )
        self.assertTrue(any("missing ledger patches for inventory IDs" in error for error in errors), errors)

    def test_outerlook_source_identity_matches_inventory_source(self) -> None:
        self.ledger["patches"][0]["source"]["repository"] = (
            "https://github.com/usherlabs/rindexer"
        )
        self.ledger["patches"][0]["source"]["branch"] = "fiet/v0.43"

        errors = validate_governance(
            self.ledger,
            self.inventory,
            self.paths,
            REPO_ROOT,
            verify_repository=False,
        )
        self.assertTrue(any("source repository must match inventory source" in error for error in errors), errors)
        self.assertTrue(any("source branch must match inventory source" in error for error in errors), errors)

    def test_inventory_hash_and_exact_baseline_are_enforced(self) -> None:
        tampered = copy.deepcopy(self.inventory)
        tampered["items"][0]["title"] = "tampered"
        errors = validate_governance(self.ledger, tampered, self.paths, REPO_ROOT)
        self.assertTrue(any("inventory" in error and "hash" in error for error in errors), errors)

        wrong_baseline = copy.deepcopy(self.ledger)
        wrong_baseline["canonical"]["tree"] = "0" * 40
        errors = validate_governance(wrong_baseline, self.inventory, self.paths, REPO_ROOT)
        self.assertTrue(any("canonical tree" in error for error in errors), errors)

    def test_missing_referenced_regression_test_is_rejected(self) -> None:
        self.ledger["patches"][0]["regression_tests"][0]["path"] = "missing/test.rs"
        errors = validate_governance(self.ledger, self.inventory, self.paths, REPO_ROOT)
        self.assertTrue(any("referenced regression test" in error for error in errors), errors)

    def test_final_disposition_requires_evidence_and_valid_transition(self) -> None:
        final = copy.deepcopy(self.ledger["patches"][0])
        final["audit_disposition"] = "absorbed"
        final["release_state"] = "inactive"
        self.assertTrue(any("evidence" in error for error in validate_transition(self.ledger["patches"][0], final)))

        final["evidence"] = ["evidence/outerlook-5583ca2.json"]
        self.assertEqual(validate_transition(self.ledger["patches"][0], final), [])
        reverted = copy.deepcopy(final)
        reverted["audit_disposition"] = "pending"
        reverted["release_state"] = "pending"
        self.assertTrue(any("final disposition" in error for error in validate_transition(final, reverted)))

    def test_active_port_requires_ancestry_or_squash_marker(self) -> None:
        patch = self.ledger["patches"][0]
        patch["audit_disposition"] = "ported"
        patch["release_state"] = "active"
        patch["evidence"] = ["scripts/test_downstream_governance.py"]
        errors = validate_governance(self.ledger, self.inventory, self.paths, REPO_ROOT)
        self.assertTrue(any("active ancestry" in error for error in errors), errors)

        marker = "FIET-PATCH:outerlook-5583ca2-native-transfer-end-block"
        patch["ancestry_markers"] = [marker]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "--quiet", repo], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.invalid"], check=True)
            (repo / "file").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo, "add", "file"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "--quiet", "-m", "baseline"], check=True)
            baseline = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
            (repo / "file").write_text("ported\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo, "commit", "--quiet", "-am", marker], check=True)
            self.assertEqual(validate_active_ancestry(patch, repo, baseline), [])

    def test_unledgered_behavioral_paths_are_rejected(self) -> None:
        self.assertEqual(
            validate_changed_paths(
                ["scripts/validate_downstream_governance.py", "FIET-DOWNSTREAM.md"],
                self.ledger,
                self.paths,
            ),
            [],
        )
        errors = validate_changed_paths(["core/src/indexer/unowned.rs"], self.ledger, self.paths)
        self.assertTrue(any("unledgered governed path" in error for error in errors), errors)

    def test_release_tag_must_be_immutable_named_and_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "--quiet", repo], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.invalid"], check=True)
            (repo / "file").write_text("qualified\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo, "add", "file"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "--quiet", "-m", "qualified"], check=True)
            commit = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
            tree = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD^{tree}"], text=True).strip()

            release_ledger = copy.deepcopy(self.ledger)
            release_ledger["patches"] = []
            release_ledger["inventory"]["canonical_sha256"] = hashlib.sha256(
                canonical_json_bytes({**self.inventory, "items": []})
            ).hexdigest()
            release_ledger["release"] = {
                "branch": "fiet/v0.43",
                "state": "qualified",
                "tag": "fiet-v0.43.0-1",
                "peeled_commit": commit,
                "peeled_tree": tree,
            }
            empty_inventory = {**self.inventory, "items": []}
            subprocess.run(["git", "-C", repo, "tag", "fiet-v0.43.0-1", commit], check=True)
            self.assertEqual(
                validate_governance(
                    release_ledger,
                    empty_inventory,
                    self.paths,
                    repo,
                    release=True,
                    verify_tag=True,
                    verify_repository=False,
                ),
                [],
            )
            release_ledger["release"]["peeled_tree"] = "0" * 40
            errors = validate_governance(
                release_ledger,
                empty_inventory,
                self.paths,
                repo,
                release=True,
                verify_tag=True,
                verify_repository=False,
            )
            self.assertTrue(any("peeled tree" in error for error in errors), errors)

    def test_remote_release_tag_retargeting_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--quiet", source], check=True)
            subprocess.run(["git", "init", "--quiet", "--bare", remote], check=True)
            subprocess.run(["git", "-C", source, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", source, "config", "user.email", "test@example.invalid"], check=True)
            (source / "file").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "-C", source, "add", "file"], check=True)
            subprocess.run(["git", "-C", source, "commit", "--quiet", "-m", "one"], check=True)
            commit = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
            tree = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD^{tree}"], text=True).strip()
            subprocess.run(["git", "-C", source, "branch", "fiet/v0.43"], check=True)
            subprocess.run(["git", "-C", source, "tag", "fiet-v0.43.0-1"], check=True)
            subprocess.run(["git", "-C", source, "push", "--quiet", str(remote), "fiet/v0.43", "fiet-v0.43.0-1"], check=True)
            self.assertEqual(
                validate_remote_release_identity(source, str(remote), "fiet/v0.43", "fiet-v0.43.0-1", commit, tree),
                [],
            )

            (source / "file").write_text("two\n", encoding="utf-8")
            subprocess.run(["git", "-C", source, "commit", "--quiet", "-am", "two"], check=True)
            subprocess.run(["git", "-C", source, "tag", "--force", "fiet-v0.43.0-1"], check=True)
            subprocess.run(["git", "-C", source, "push", "--quiet", "--force", str(remote), "fiet-v0.43.0-1"], check=True)
            errors = validate_remote_release_identity(
                source, str(remote), "fiet/v0.43", "fiet-v0.43.0-1", commit, tree
            )
            self.assertTrue(any("remote tag" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
