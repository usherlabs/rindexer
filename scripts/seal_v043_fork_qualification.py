#!/usr/bin/env python3
"""Seal and verify the FIET rindexer v0.43 downstream qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "fiet-rindexer-v043-fork-qualification-receipt/v1"
RESULTS_SCHEMA_VERSION = "fiet-rindexer-v043-fork-test-results/v1"
CANONICAL_REPOSITORY = "https://github.com/joshstevens19/rindexer"
CANONICAL_TAG = "v0.43.0"
CANONICAL_COMMIT = "4f441289b83855c357239d2729fb725a56c3060b"
CANONICAL_TREE = "80a2698f6be13949d84d920b01c02125af598d09"
GIT_OID = re.compile(r"^[0-9a-f]{40}$")
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
ARTIFACT_PATHS = {
    "inventory": "downstream-patch-inventory.json",
    "ledger": "downstream-patches.json",
    "governed_paths": "governed-paths.json",
    "embedded_lifecycle_receipt": (
        "qualification/v0.43/candidate-embedded-lifecycle-receipt.json"
    ),
    "test_results": "qualification/v0.43/candidate-test-results.json",
}


class ForkQualificationSealError(ValueError):
    """Raised when incomplete or drifting fork evidence cannot be sealed."""


def _load_object(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ForkQualificationSealError(f"{path}: expected a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _validate_bound_artifacts(repo_root: Path) -> None:
    ledger = _load_object(repo_root / ARTIFACT_PATHS["ledger"])
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
        repo_root / ARTIFACT_PATHS["embedded_lifecycle_receipt"]
    )
    if lifecycle.get("verdict") != "pass":
        raise ForkQualificationSealError(
            "embedded lifecycle qualification did not pass"
        )


def build_fork_qualification_receipt(
    repo_root: Path, results_path: Path
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    results_path = results_path.resolve()
    results = _load_object(results_path)
    _validate_results(results)

    artifacts: dict[str, dict[str, str]] = {}
    for name, relative_path in ARTIFACT_PATHS.items():
        path = repo_root / relative_path
        if not path.is_file():
            raise ForkQualificationSealError(
                f"retained artifact is missing: {relative_path}"
            )
        artifacts[name] = {"path": relative_path, "sha256": _sha256(path)}

    # The unit fixture intentionally uses small stand-in artifacts; production
    # sealing validates the retained ledger and lifecycle receipt themselves.
    ledger = _load_object(repo_root / ARTIFACT_PATHS["ledger"])
    if ledger.get("schema_version") is not None:
        _validate_bound_artifacts(repo_root)

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
            "branch": "fiet/candidate-v0.43.0-1",
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
    repo_root: Path, receipt: Mapping[str, Any]
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
    for name, expected_path in ARTIFACT_PATHS.items():
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

    results_path = repo_root / ARTIFACT_PATHS["test_results"]
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
        _validate_bound_artifacts(repo_root)
    except ForkQualificationSealError as error:
        # Unit fixtures use stand-in artifacts without production schema fields.
        ledger_path = repo_root / ARTIFACT_PATHS["ledger"]
        ledger = _load_object(ledger_path) if ledger_path.is_file() else {}
        if ledger.get("schema_version") is not None:
            errors.append(str(error))
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=Path("qualification/v0.43/candidate-test-results.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("qualification/v0.43/candidate-fork-qualification-receipt.json"),
    )
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    results_path = args.results if args.results.is_absolute() else repo_root / args.results
    output_path = args.output if args.output.is_absolute() else repo_root / args.output
    try:
        if args.verify:
            errors = verify_fork_qualification_receipt(
                repo_root, _load_object(output_path)
            )
            if errors:
                for error in errors:
                    print(error)
                return 1
            print(f"fork qualification receipt is valid: {output_path}")
            return 0

        receipt = build_fork_qualification_receipt(repo_root, results_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(f"wrote fork qualification receipt: {output_path}")
        return 0
    except (ForkQualificationSealError, OSError, json.JSONDecodeError) as error:
        print(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
