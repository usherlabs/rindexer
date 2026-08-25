#!/usr/bin/env python3
"""Seal and verify the immutable FIET characterization of canonical rindexer v0.43."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "fiet-rindexer-v043-characterization-receipt/v1"
RESULTS_SCHEMA_VERSION = "fiet-rindexer-vanilla-test-results/v1"
CANONICAL_COMMIT = "4f441289b83855c357239d2729fb725a56c3060b"
CANONICAL_TREE = "80a2698f6be13949d84d920b01c02125af598d09"
RUNTIME_PATHS = ["Cargo.toml", "Cargo.lock", "core/Cargo.toml", "core/src"]
REQUIRED_RUNS = frozenset(
    {"workspace_default_features", "workspace_all_features", "docker_e2e"}
)
ARTIFACT_PATHS = {
    "source_receipt": "characterization/v0.43/vanilla-baseline-source-receipt.json",
    "process_receipt": "characterization/v0.43/vanilla-baseline-process-receipt.json",
    "failure_injection_receipt": (
        "characterization/v0.43/vanilla-baseline-failure-injection-receipt.json"
    ),
    "test_results": "characterization/v0.43/vanilla-baseline-test-results.json",
}


class CharacterizationSealError(ValueError):
    """Raised when incomplete or drifting characterization evidence cannot be sealed."""


def _load_object(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise CharacterizationSealError(f"{path}: expected a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_results(results: Mapping[str, Any]) -> None:
    if results.get("schema_version") != RESULTS_SCHEMA_VERSION:
        raise CharacterizationSealError("unexpected test-results schema version")
    if results.get("remaining_before_immutable_characterization_receipt") != []:
        raise CharacterizationSealError("characterization campaign is incomplete")
    if results.get("verdict") != "pass":
        raise CharacterizationSealError("characterization campaign did not pass")

    source = results.get("source_under_test")
    if not isinstance(source, Mapping):
        raise CharacterizationSealError("source_under_test is missing")
    if source.get("canonical_commit") != CANONICAL_COMMIT:
        raise CharacterizationSealError("canonical commit does not match v0.43.0")
    if source.get("canonical_root_tree") != CANONICAL_TREE:
        raise CharacterizationSealError("canonical tree does not match v0.43.0")
    if source.get("runtime_paths_byte_identical_to_canonical") is not True:
        raise CharacterizationSealError("runtime paths are not byte-identical to canonical")
    if source.get("runtime_paths") != RUNTIME_PATHS:
        raise CharacterizationSealError("runtime path inventory changed")

    runs = results.get("runs")
    if not isinstance(runs, list):
        raise CharacterizationSealError("test results runs must be a list")
    passing = {
        run.get("kind")
        for run in runs
        if isinstance(run, Mapping) and run.get("result") == "pass"
    }
    missing = sorted(REQUIRED_RUNS - passing)
    if missing:
        raise CharacterizationSealError(
            f"missing passing required runs: {', '.join(missing)}"
        )


def build_characterization_receipt(
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
            raise CharacterizationSealError(f"retained artifact is missing: {relative_path}")
        artifacts[name] = {"path": relative_path, "sha256": _sha256(path)}

    return {
        "$schema": "../../../schemas/v043-characterization-receipt.schema.json",
        "schema_version": SCHEMA_VERSION,
        "sealed_at": results["created_at"],
        "canonical_source": {
            "repository": "https://github.com/joshstevens19/rindexer",
            "tag": "v0.43.0",
            "commit": CANONICAL_COMMIT,
            "tree": CANONICAL_TREE,
            "runtime_paths": RUNTIME_PATHS,
            "runtime_paths_byte_identical": True,
        },
        "toolchain": results["toolchain"],
        "qualification_runs": results["runs"],
        "artifacts": artifacts,
        "verdict": "characterized",
        "release_authorized": False,
    }


def verify_characterization_receipt(
    repo_root: Path, receipt: Mapping[str, Any]
) -> list[str]:
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
        if canonical.get("runtime_paths") != RUNTIME_PATHS:
            errors.append("runtime path inventory mismatch")
        if canonical.get("runtime_paths_byte_identical") is not True:
            errors.append("canonical runtime identity is not asserted")
    if receipt.get("verdict") != "characterized":
        errors.append("receipt verdict must be characterized")
    if receipt.get("release_authorized") is not False:
        errors.append("characterization receipt must not authorize a release")

    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return errors + ["artifact bindings are missing"]
    for name, expected_path in ARTIFACT_PATHS.items():
        binding = artifacts.get(name)
        if not isinstance(binding, Mapping):
            errors.append(f"{name} binding is missing")
            continue
        if binding.get("path") != expected_path:
            errors.append(f"{name} path mismatch")
            continue
        path = repo_root.resolve() / expected_path
        if not path.is_file():
            errors.append(f"{name} artifact is missing")
            continue
        observed_hash = _sha256(path)
        if binding.get("sha256") != observed_hash:
            errors.append(f"{name} SHA-256 mismatch")

    results_path = repo_root.resolve() / ARTIFACT_PATHS["test_results"]
    if results_path.is_file():
        try:
            results = _load_object(results_path)
            _validate_results(results)
            if receipt.get("sealed_at") != results.get("created_at"):
                errors.append("sealed_at does not match bound test results")
            if receipt.get("toolchain") != results.get("toolchain"):
                errors.append("toolchain does not match bound test results")
            if receipt.get("qualification_runs") != results.get("runs"):
                errors.append("qualification_runs do not match bound test results")
        except CharacterizationSealError as error:
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
        default=Path("characterization/v0.43/vanilla-baseline-test-results.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("characterization/v0.43/vanilla-baseline-receipt.json"),
    )
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    results_path = args.results if args.results.is_absolute() else repo_root / args.results
    output_path = args.output if args.output.is_absolute() else repo_root / args.output
    try:
        if args.verify:
            errors = verify_characterization_receipt(repo_root, _load_object(output_path))
            if errors:
                for error in errors:
                    print(error)
                return 1
            print(f"characterization receipt is valid: {output_path}")
            return 0

        receipt = build_characterization_receipt(repo_root, results_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(f"wrote characterization receipt: {output_path}")
        return 0
    except (CharacterizationSealError, OSError, json.JSONDecodeError) as error:
        print(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
