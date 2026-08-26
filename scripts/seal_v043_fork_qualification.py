#!/usr/bin/env python3
"""Seal and verify the FIET rindexer v0.43 downstream qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = "fiet-rindexer-v043-fork-qualification-receipt/v1"
RESULTS_SCHEMA_VERSION = "fiet-rindexer-v043-fork-test-results/v1"
CANONICAL_REPOSITORY = "https://github.com/joshstevens19/rindexer"
CANONICAL_TAG = "v0.43.0"
CANONICAL_COMMIT = "4f441289b83855c357239d2729fb725a56c3060b"
CANONICAL_TREE = "80a2698f6be13949d84d920b01c02125af598d09"
GIT_OID = re.compile(r"^[0-9a-f]{40}$")
QUALIFICATION_DIRECTORY = re.compile(r"^qualification/[a-z0-9][a-z0-9.-]*$")
CANDIDATE_BRANCH = re.compile(r"^fiet/[a-z0-9](?:[a-z0-9./-]*[a-z0-9])?$")
REQUIRED_RUNS = frozenset(
    {
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
    }
)
LIVE_GOVERNANCE_PATHS = {
    "inventory": "downstream-patch-inventory.json",
    "ledger": "downstream-patches.json",
    "governed_paths": "governed-paths.json",
}


class ForkQualificationSealError(ValueError):
    """Raised when incomplete or drifting fork evidence cannot be sealed."""


@dataclass(frozen=True)
class QualificationLayout:
    """Safe, attempt-specific paths and source identity for retained evidence."""

    qualification_directory: str
    candidate_branch: str

    @classmethod
    def create(
        cls, qualification_directory: str | Path, candidate_branch: str
    ) -> "QualificationLayout":
        directory = str(qualification_directory)
        posix_directory = PurePosixPath(directory)
        if (
            "\\" in directory
            or posix_directory.is_absolute()
            or ".." in posix_directory.parts
            or not QUALIFICATION_DIRECTORY.fullmatch(directory)
        ):
            raise ForkQualificationSealError(
                "qualification directory must be qualification/<safe-attempt-id>"
            )
        if (
            ".." in candidate_branch
            or "//" in candidate_branch
            or not CANDIDATE_BRANCH.fullmatch(candidate_branch)
        ):
            raise ForkQualificationSealError(
                "candidate branch must be a safe fiet/<branch> name"
            )
        return cls(directory, candidate_branch)

    @property
    def artifact_paths(self) -> dict[str, str]:
        prefix = self.qualification_directory
        return {
            "inventory": f"{prefix}/candidate-downstream-patch-inventory.json",
            "ledger": f"{prefix}/candidate-downstream-patches.json",
            "governed_paths": f"{prefix}/candidate-governed-paths.json",
            "embedded_lifecycle_receipt": (
                f"{prefix}/candidate-embedded-lifecycle-receipt.json"
            ),
            "test_results": f"{prefix}/candidate-test-results.json",
        }

    @property
    def receipt_path(self) -> str:
        return f"{self.qualification_directory}/candidate-fork-qualification-receipt.json"


LEGACY_LAYOUT = QualificationLayout.create(
    "qualification/v0.43", "fiet/candidate-v0.43.0-1"
)
ARTIFACT_PATHS = LEGACY_LAYOUT.artifact_paths


def _load_object(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ForkQualificationSealError(f"{path}: expected a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage_candidate_governance_snapshots(
    repo_root: Path, layout: QualificationLayout = LEGACY_LAYOUT
) -> None:
    """Retain the candidate governance inputs once, without later rewrites."""
    repo_root = repo_root.resolve()
    labels = {
        "inventory": "inventory",
        "ledger": "ledger",
        "governed_paths": "governed paths",
    }
    for name, live_relative in LIVE_GOVERNANCE_PATHS.items():
        live_path = repo_root / live_relative
        snapshot_path = repo_root / layout.artifact_paths[name]
        content = live_path.read_bytes()
        if snapshot_path.exists():
            if snapshot_path.read_bytes() != content:
                raise ForkQualificationSealError(
                    f"refusing to overwrite retained candidate {labels[name]}"
                )
            continue
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_bytes(content)


def _validate_results(results: Mapping[str, Any]) -> None:
    if results.get("schema_version") != RESULTS_SCHEMA_VERSION:
        raise ForkQualificationSealError("unexpected test-results schema version")
    if results.get("hard_gate_failures") != [] or results.get("verdict") != "pass":
        raise ForkQualificationSealError("qualification hard gates failed")

    source = results.get("source_under_test")
    if not isinstance(source, Mapping):
        raise ForkQualificationSealError("source_under_test is missing")
    for field in ("commit", "tree"):
        if not GIT_OID.fullmatch(str(source.get(field, ""))):
            raise ForkQualificationSealError(
                f"downstream source {field} must be a full Git OID"
            )
    if source.get("canonical_baseline_commit") != CANONICAL_COMMIT:
        raise ForkQualificationSealError("canonical baseline commit mismatch")
    if source.get("canonical_baseline_tree") != CANONICAL_TREE:
        raise ForkQualificationSealError("canonical baseline tree mismatch")

    runs = results.get("runs")
    if not isinstance(runs, list):
        raise ForkQualificationSealError("test results runs must be a list")
    passing = {
        run.get("kind")
        for run in runs
        if isinstance(run, Mapping) and run.get("result") == "pass"
    }
    missing = sorted(REQUIRED_RUNS - passing)
    if missing:
        raise ForkQualificationSealError(
            f"missing passing required runs: {', '.join(missing)}"
        )


def _validate_bound_artifacts(
    repo_root: Path, layout: QualificationLayout = LEGACY_LAYOUT
) -> None:
    artifact_paths = layout.artifact_paths
    ledger = _load_object(repo_root / artifact_paths["ledger"])
    release = ledger.get("release")
    if not isinstance(release, Mapping) or release.get("state") != "candidate":
        raise ForkQualificationSealError(
            "qualification requires a candidate-state downstream ledger"
        )
    patches = ledger.get("patches")
    if not isinstance(patches, list) or any(
        isinstance(patch, Mapping) and patch.get("audit_disposition") == "pending"
        for patch in patches
    ):
        raise ForkQualificationSealError(
            "qualification requires a ledger with no pending patch"
        )

    lifecycle = _load_object(
        repo_root / artifact_paths["embedded_lifecycle_receipt"]
    )
    if lifecycle.get("verdict") != "pass":
        raise ForkQualificationSealError(
            "embedded lifecycle qualification did not pass"
        )


def build_fork_qualification_receipt(
    repo_root: Path,
    results_path: Path,
    layout: QualificationLayout = LEGACY_LAYOUT,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    results_path = results_path.resolve()
    expected_results_path = (
        repo_root / layout.artifact_paths["test_results"]
    ).resolve()
    if results_path != expected_results_path:
        raise ForkQualificationSealError(
            f"test results must use the retained attempt path: {layout.artifact_paths['test_results']}"
        )
    results = _load_object(results_path)
    _validate_results(results)

    artifacts: dict[str, dict[str, str]] = {}
    for name, relative_path in layout.artifact_paths.items():
        path = repo_root / relative_path
        if not path.is_file():
            raise ForkQualificationSealError(
                f"retained artifact is missing: {relative_path}"
            )
        artifacts[name] = {"path": relative_path, "sha256": _sha256(path)}

    _validate_bound_artifacts(repo_root, layout)

    source = results["source_under_test"]
    return {
        "$schema": "../../../schemas/v043-fork-qualification-receipt.schema.json",
        "schema_version": SCHEMA_VERSION,
        "sealed_at": results["created_at"],
        "canonical_source": {
            "repository": CANONICAL_REPOSITORY,
            "tag": CANONICAL_TAG,
            "commit": CANONICAL_COMMIT,
            "tree": CANONICAL_TREE,
        },
        "downstream_source": {
            "repository": "https://github.com/usherlabs/rindexer",
            "branch": layout.candidate_branch,
            "commit": source["commit"],
            "tree": source["tree"],
        },
        "toolchain": results["toolchain"],
        "qualification_runs": results["runs"],
        "artifacts": artifacts,
        "verdict": "qualified",
        "release_authorized": False,
    }


def verify_fork_qualification_receipt(
    repo_root: Path,
    receipt: Mapping[str, Any],
    layout: QualificationLayout | None = None,
) -> list[str]:
    repo_root = repo_root.resolve()
    errors: list[str] = []
    if receipt.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    canonical = receipt.get("canonical_source")
    if not isinstance(canonical, Mapping):
        errors.append("canonical_source is missing")
    else:
        if canonical.get("commit") != CANONICAL_COMMIT:
            errors.append("canonical commit mismatch")
        if canonical.get("tree") != CANONICAL_TREE:
            errors.append("canonical tree mismatch")
    downstream = receipt.get("downstream_source")
    if not isinstance(downstream, Mapping):
        errors.append("downstream_source is missing")
    else:
        for field in ("commit", "tree"):
            if not GIT_OID.fullmatch(str(downstream.get(field, ""))):
                errors.append(f"downstream {field} is not a full Git OID")
    if receipt.get("verdict") != "qualified":
        errors.append("receipt verdict must be qualified")
    if receipt.get("release_authorized") is not False:
        errors.append("qualification receipt must not authorize release promotion")

    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return sorted(set(errors + ["artifact bindings are missing"]))
    if layout is None:
        inventory = artifacts.get("inventory")
        inventory_path = (
            inventory.get("path") if isinstance(inventory, Mapping) else None
        )
        branch = downstream.get("branch") if isinstance(downstream, Mapping) else None
        try:
            if not isinstance(inventory_path, str) or not isinstance(branch, str):
                raise ForkQualificationSealError(
                    "receipt cannot determine its qualification layout"
                )
            inventory_posix = PurePosixPath(inventory_path)
            if inventory_posix.name != "candidate-downstream-patch-inventory.json":
                raise ForkQualificationSealError("inventory path mismatch")
            layout = QualificationLayout.create(str(inventory_posix.parent), branch)
        except ForkQualificationSealError as error:
            return sorted(set(errors + [str(error)]))
    if (
        isinstance(downstream, Mapping)
        and downstream.get("branch") != layout.candidate_branch
    ):
        errors.append("downstream branch does not match qualification layout")

    artifact_paths = layout.artifact_paths
    for name, expected_path in artifact_paths.items():
        binding = artifacts.get(name)
        if not isinstance(binding, Mapping):
            errors.append(f"{name} binding is missing")
            continue
        if binding.get("path") != expected_path:
            errors.append(f"{name} path mismatch")
            continue
        path = repo_root / expected_path
        if not path.is_file():
            errors.append(f"{name} artifact is missing")
            continue
        if binding.get("sha256") != _sha256(path):
            errors.append(f"{name} SHA-256 mismatch")

    results_path = repo_root / artifact_paths["test_results"]
    if results_path.is_file():
        try:
            results = _load_object(results_path)
            _validate_results(results)
            source = results["source_under_test"]
            if receipt.get("sealed_at") != results.get("created_at"):
                errors.append("sealed_at does not match bound test results")
            if receipt.get("toolchain") != results.get("toolchain"):
                errors.append("toolchain does not match bound test results")
            if receipt.get("qualification_runs") != results.get("runs"):
                errors.append("qualification_runs do not match bound test results")
            if isinstance(downstream, Mapping):
                if downstream.get("commit") != source.get("commit"):
                    errors.append("downstream commit does not match bound test results")
                if downstream.get("tree") != source.get("tree"):
                    errors.append("downstream tree does not match bound test results")
        except ForkQualificationSealError as error:
            errors.append(str(error))
    try:
        _validate_bound_artifacts(repo_root, layout)
    except ForkQualificationSealError as error:
        errors.append(str(error))
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--qualification-dir",
        type=Path,
        default=Path(LEGACY_LAYOUT.qualification_directory),
    )
    parser.add_argument(
        "--candidate-branch", default=LEGACY_LAYOUT.candidate_branch
    )
    parser.add_argument("--results", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    try:
        layout = QualificationLayout.create(
            args.qualification_dir, args.candidate_branch
        )
        results_argument = args.results or Path(layout.artifact_paths["test_results"])
        output_argument = args.output or Path(layout.receipt_path)
        results_path = (
            results_argument
            if results_argument.is_absolute()
            else repo_root / results_argument
        )
        output_path = (
            output_argument
            if output_argument.is_absolute()
            else repo_root / output_argument
        )
        if args.verify:
            errors = verify_fork_qualification_receipt(
                repo_root, _load_object(output_path), layout
            )
            if errors:
                for error in errors:
                    print(error)
                return 1
            print(f"fork qualification receipt is valid: {output_path}")
            return 0

        stage_candidate_governance_snapshots(repo_root, layout)
        receipt = build_fork_qualification_receipt(repo_root, results_path, layout)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(f"wrote fork qualification receipt: {output_path}")
        return 0
    except (ForkQualificationSealError, OSError, json.JSONDecodeError) as error:
        print(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
