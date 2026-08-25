#!/usr/bin/env python3
"""Validate FIET rindexer downstream source governance and release identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


LEDGER_SCHEMA_VERSION = "fiet-rindexer-downstream-patch-ledger/v1"
INVENTORY_SCHEMA_VERSION = "fiet-rindexer-pre-convergence-inventory/v1"
GOVERNED_PATHS_SCHEMA_VERSION = "fiet-rindexer-governed-paths/v1"
CANONICAL_REPOSITORY = "https://github.com/joshstevens19/rindexer"
CANONICAL_TAG = "v0.43.0"
CANONICAL_COMMIT = "4f441289b83855c357239d2729fb725a56c3060b"
CANONICAL_TREE = "80a2698f6be13949d84d920b01c02125af598d09"
FINAL_DISPOSITIONS = frozenset({"ported", "absorbed", "superseded", "not-behavioral"})
DISPOSITIONS = FINAL_DISPOSITIONS | {"pending"}
RELEASE_STATES = frozenset({"pending", "active", "inactive"})
UPSTREAM_STATES = frozenset({"not_assessed", "planned", "submitted", "merged", "declined", "not_applicable"})
RELEASE_TAG = re.compile(r"^fiet-v0\.43\.0-[1-9][0-9]*$")
GIT_OID = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FIET_PATCH_ID = re.compile(r"^fiet-[a-z0-9]+(?:-[a-z0-9]+)*$")
TRUSTED_FIET_SOURCE_REPOSITORIES = frozenset(
    {
        "https://github.com/usherlabs/fiet-maker",
        "https://github.com/usherlabs/rindexer",
    }
)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, list) else []


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _path_matches(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(f"{prefix}/")


def validate_transition(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    previous_disposition = previous.get("audit_disposition")
    current_disposition = current.get("audit_disposition")
    if previous.get("id") != current.get("id"):
        errors.append("patch id cannot change during a ledger transition")
    if previous_disposition in {"absorbed", "superseded", "not-behavioral"} and current_disposition != previous_disposition:
        errors.append(f"final disposition {previous_disposition} cannot transition to {current_disposition}")
    if previous_disposition == "ported" and current_disposition not in {"ported", "absorbed", "superseded"}:
        errors.append(f"ported disposition cannot transition to {current_disposition}")
    if current_disposition in FINAL_DISPOSITIONS and not _sequence(current.get("evidence")):
        errors.append(f"final disposition {current_disposition} requires evidence")
    return errors


def validate_active_ancestry(patch: Mapping[str, Any], repo_root: Path, baseline: str) -> list[str]:
    patch_id = str(patch.get("id", "<missing>"))
    errors: list[str] = []
    commits = [str(commit) for commit in _sequence(patch.get("downstream_commits"))]
    markers = [str(marker) for marker in _sequence(patch.get("ancestry_markers"))]

    for commit in commits:
        if not GIT_OID.fullmatch(commit):
            errors.append(f"patch {patch_id}: downstream commit is not a full Git OID: {commit}")
            continue
        ancestry = _git(repo_root, "merge-base", "--is-ancestor", commit, "HEAD")
        if ancestry.returncode != 0:
            errors.append(f"patch {patch_id}: downstream commit {commit} is absent from active ancestry")

    marker_found = False
    if markers:
        history = _git(repo_root, "log", "--format=%B%x00", f"{baseline}..HEAD")
        if history.returncode == 0:
            marker_found = any(marker in history.stdout for marker in markers)

    if not commits and not marker_found:
        errors.append(f"patch {patch_id}: active ancestry has neither a retained downstream commit nor a squash marker")
    elif markers and not marker_found and not commits:
        errors.append(f"patch {patch_id}: declared squash marker is absent from active ancestry")
    return errors


def validate_changed_paths(
    changed_paths: Sequence[str],
    ledger: Mapping[str, Any],
    governed_paths: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    behavioral = [str(path) for path in _sequence(governed_paths.get("behavioral_paths"))]
    excluded = [str(path) for path in _sequence(governed_paths.get("non_behavioral_exclusions"))]
    active_paths = [
        str(path)
        for patch in _sequence(ledger.get("patches"))
        if isinstance(patch, Mapping)
        and patch.get("audit_disposition") == "ported"
        and patch.get("release_state") == "active"
        for path in _sequence(patch.get("affected_paths"))
    ]

    for path in changed_paths:
        if not path or any(_path_matches(path, prefix) for prefix in excluded):
            continue
        if not any(_path_matches(path, prefix) for prefix in behavioral):
            continue
        if not any(_path_matches(path, prefix) or _path_matches(prefix, path) for prefix in active_paths):
            errors.append(f"unledgered governed path: {path}")
    return errors


def _validate_release(ledger: Mapping[str, Any], repo_root: Path, verify_tag: bool) -> list[str]:
    errors: list[str] = []
    release = _mapping(ledger.get("release"))
    if release.get("branch") != "fiet/v0.43":
        errors.append("release branch must be fiet/v0.43")
    if release.get("state") != "qualified":
        errors.append("release state must be qualified")
    tag = release.get("tag")
    if not isinstance(tag, str) or not RELEASE_TAG.fullmatch(tag):
        errors.append("release tag must match fiet-v0.43.0-<positive-revision>")
    commit = str(release.get("peeled_commit", ""))
    tree = str(release.get("peeled_tree", ""))
    if not GIT_OID.fullmatch(commit):
        errors.append("release peeled commit must be a full Git OID")
    if not GIT_OID.fullmatch(tree):
        errors.append("release peeled tree must be a full Git OID")
    if not verify_tag or not isinstance(tag, str) or not RELEASE_TAG.fullmatch(tag):
        return errors

    resolved_commit = _git(repo_root, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    resolved_tree = _git(repo_root, "rev-parse", f"refs/tags/{tag}^{{tree}}")
    if resolved_commit.returncode != 0:
        errors.append(f"release tag {tag} does not resolve locally")
        return errors
    if resolved_commit.stdout.strip() != commit:
        errors.append(f"release peeled commit mismatch: {resolved_commit.stdout.strip()} != {commit}")
    if resolved_tree.stdout.strip() != tree:
        errors.append(f"release peeled tree mismatch: {resolved_tree.stdout.strip()} != {tree}")
    return errors


def validate_remote_release_identity(
    repo_root: Path,
    remote: str,
    branch: str,
    tag: str,
    expected_commit: str,
    expected_tree: str,
) -> list[str]:
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        verification_repo = Path(directory) / "release.git"
        initialized = subprocess.run(
            ["git", "init", "--quiet", "--bare", str(verification_repo)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if initialized.returncode != 0:
            return [f"release verification clone initialization failed: {initialized.stderr.strip()}"]
        fetched = subprocess.run(
            [
                "git",
                "-C",
                str(verification_repo),
                "fetch",
                "--quiet",
                "--force",
                remote,
                f"+refs/heads/{branch}:refs/remotes/release/{branch}",
                f"+refs/tags/{tag}:refs/tags/{tag}",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if fetched.returncode != 0:
            return [f"remote release identity fetch failed: {fetched.stderr.strip()}"]

        resolved_commit = subprocess.run(
            ["git", "-C", str(verification_repo), "rev-parse", f"refs/tags/{tag}^{{commit}}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        resolved_tree = subprocess.run(
            ["git", "-C", str(verification_repo), "rev-parse", f"refs/tags/{tag}^{{tree}}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if resolved_commit.returncode != 0:
            errors.append(f"remote tag {tag} does not peel to a commit")
            return errors
        if resolved_commit.stdout.strip() != expected_commit:
            errors.append(f"remote tag commit mismatch: {resolved_commit.stdout.strip()} != {expected_commit}")
        if resolved_tree.stdout.strip() != expected_tree:
            errors.append(f"remote tag peeled tree mismatch: {resolved_tree.stdout.strip()} != {expected_tree}")
        branch_ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(verification_repo),
                "merge-base",
                "--is-ancestor",
                expected_commit,
                f"refs/remotes/release/{branch}",
            ],
            check=False,
        )
        if branch_ancestry.returncode != 0:
            errors.append(f"release tag commit is outside protected branch ancestry: {branch}")
    return errors


def validate_governance(
    ledger: Mapping[str, Any],
    inventory: Mapping[str, Any],
    governed_paths: Mapping[str, Any],
    repo_root: Path,
    *,
    release: bool = False,
    verify_tag: bool = False,
    verify_repository: bool = True,
    previous_ledger: Mapping[str, Any] | None = None,
    changed_paths: Sequence[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if ledger.get("schema_version") != LEDGER_SCHEMA_VERSION:
        errors.append(f"ledger schema_version must be {LEDGER_SCHEMA_VERSION}")
    if inventory.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        errors.append(f"inventory schema_version must be {INVENTORY_SCHEMA_VERSION}")
    if governed_paths.get("schema_version") != GOVERNED_PATHS_SCHEMA_VERSION:
        errors.append(f"governed paths schema_version must be {GOVERNED_PATHS_SCHEMA_VERSION}")

    canonical = _mapping(ledger.get("canonical"))
    expected_canonical = {
        "repository": CANONICAL_REPOSITORY,
        "tag": CANONICAL_TAG,
        "commit": CANONICAL_COMMIT,
        "tree": CANONICAL_TREE,
    }
    for field, expected in expected_canonical.items():
        if canonical.get(field) != expected:
            errors.append(f"canonical {field} must equal {expected}")

    inventory_binding = _mapping(ledger.get("inventory"))
    declared_inventory_hash = inventory_binding.get("canonical_sha256")
    observed_inventory_hash = canonical_sha256(inventory)
    if not isinstance(declared_inventory_hash, str) or not SHA256.fullmatch(declared_inventory_hash):
        errors.append("inventory canonical hash must be SHA-256")
    elif declared_inventory_hash != observed_inventory_hash:
        errors.append(f"inventory hash mismatch: {declared_inventory_hash} != {observed_inventory_hash}")

    items = [item for item in _sequence(inventory.get("items")) if isinstance(item, Mapping)]
    patches = [patch for patch in _sequence(ledger.get("patches")) if isinstance(patch, Mapping)]
    item_ids = [str(item.get("id")) for item in items]
    patch_ids = [str(patch.get("id")) for patch in patches]
    for patch_id in sorted(set(patch_ids)):
        if patch_ids.count(patch_id) > 1:
            errors.append(f"duplicate patch id: {patch_id}")
    missing_inventory_ids = sorted(set(item_ids) - set(patch_ids))
    if missing_inventory_ids:
        errors.append(
            "missing ledger patches for inventory IDs: " + ", ".join(missing_inventory_ids)
        )
    for patch_id in sorted(set(patch_ids) - set(item_ids)):
        if not FIET_PATCH_ID.fullmatch(patch_id):
            errors.append(
                f"non-inventory patch IDs must use fiet-<name>: {patch_id}"
            )

    inventory_by_id = {str(item.get("id")): item for item in items}
    previous_by_id = {
        str(patch.get("id")): patch
        for patch in _sequence(_mapping(previous_ledger).get("patches"))
        if isinstance(patch, Mapping)
    }
    for patch in patches:
        patch_id = str(patch.get("id", "<missing>"))
        disposition = patch.get("audit_disposition")
        release_state = patch.get("release_state")
        if disposition not in DISPOSITIONS:
            errors.append(f"patch {patch_id}: invalid audit disposition {disposition}")
        if release_state not in RELEASE_STATES:
            errors.append(f"patch {patch_id}: invalid release state {release_state}")
        expected_state = "pending" if disposition == "pending" else "active" if disposition == "ported" else "inactive"
        if release_state != expected_state:
            errors.append(f"patch {patch_id}: {disposition} requires release_state={expected_state}")
        if disposition in FINAL_DISPOSITIONS and not _sequence(patch.get("evidence")):
            errors.append(f"patch {patch_id}: final disposition {disposition} requires evidence")

        source = _mapping(patch.get("source"))
        source_repository = source.get("repository")
        source_branch = source.get("branch")
        source_commits = [str(value) for value in _sequence(source.get("commits"))]
        if not source_commits:
            errors.append(f"patch {patch_id}: at least one source commit is required")
        for source_commit in source_commits:
            if not GIT_OID.fullmatch(source_commit):
                errors.append(
                    f"patch {patch_id}: source commit is not a full Git OID: {source_commit}"
                )
        inventory_item = inventory_by_id.get(patch_id)
        if inventory_item is not None:
            inventory_source = _mapping(inventory.get("source"))
            if source_repository != inventory_source.get("repository"):
                errors.append(
                    f"patch {patch_id}: source repository must match inventory source"
                )
            if source_branch != inventory_source.get("branch"):
                errors.append(f"patch {patch_id}: source branch must match inventory source")
            if source_commits != [str(inventory_item.get("commit"))]:
                errors.append(f"patch {patch_id}: source commit must match its inventory item")
        elif FIET_PATCH_ID.fullmatch(patch_id):
            if source_repository not in TRUSTED_FIET_SOURCE_REPOSITORIES:
                errors.append(
                    f"patch {patch_id}: FIET source repository is not trusted: {source_repository}"
                )
            if not isinstance(source_branch, str) or not source_branch:
                errors.append(f"patch {patch_id}: FIET source branch is required")

        baseline = _mapping(patch.get("canonical_baseline"))
        for field in ("tag", "commit", "tree"):
            if baseline.get(field) != expected_canonical[field]:
                errors.append(f"patch {patch_id}: canonical baseline {field} mismatch")

        tests = _sequence(patch.get("regression_tests"))
        if not tests:
            errors.append(f"patch {patch_id}: at least one regression test is required")
        for test in tests:
            test_path = _mapping(test).get("path")
            if not isinstance(test_path, str) or not (repo_root / test_path).is_file():
                errors.append(f"patch {patch_id}: referenced regression test is missing: {test_path}")

        upstream_state = _mapping(patch.get("upstream")).get("status")
        if upstream_state not in UPSTREAM_STATES:
            errors.append(f"patch {patch_id}: invalid upstream status {upstream_state}")

        if disposition == "ported" and release_state == "active" and verify_repository:
            errors.extend(validate_active_ancestry(patch, repo_root, CANONICAL_COMMIT))
        if patch_id in previous_by_id:
            errors.extend(f"patch {patch_id}: {error}" for error in validate_transition(previous_by_id[patch_id], patch))

    if release:
        pending = [str(patch.get("id")) for patch in patches if patch.get("audit_disposition") == "pending"]
        if pending:
            errors.append(f"release is ineligible while patches remain pending: {', '.join(sorted(pending))}")
        errors.extend(_validate_release(ledger, repo_root, verify_tag))

    if verify_repository:
        baseline_commit = _git(repo_root, "cat-file", "-e", f"{CANONICAL_COMMIT}^{{commit}}")
        if baseline_commit.returncode != 0:
            errors.append("exact canonical baseline commit is unavailable in local history")
        else:
            observed_tree = _git(repo_root, "rev-parse", f"{CANONICAL_COMMIT}^{{tree}}")
            if observed_tree.stdout.strip() != CANONICAL_TREE:
                errors.append(f"canonical tree mismatch: {observed_tree.stdout.strip()} != {CANONICAL_TREE}")
            ancestry = _git(repo_root, "merge-base", "--is-ancestor", CANONICAL_COMMIT, "HEAD")
            if ancestry.returncode != 0:
                errors.append("HEAD does not descend from the exact canonical baseline")

    if changed_paths is not None:
        errors.extend(validate_changed_paths(changed_paths, ledger, governed_paths))
    return sorted(set(errors))


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--governed-paths", type=Path)
    parser.add_argument("--previous-ledger", type=Path)
    parser.add_argument("--changed-from", default=CANONICAL_COMMIT)
    parser.add_argument("--skip-changed-paths", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--verify-tag", action="store_true")
    parser.add_argument("--release-remote")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    ledger_path = args.ledger or repo_root / "downstream-patches.json"
    inventory_path = args.inventory or repo_root / "downstream-patch-inventory.json"
    governed_paths_path = args.governed_paths or repo_root / "governed-paths.json"
    previous = _load(args.previous_ledger) if args.previous_ledger else None
    changed_paths: list[str] | None = None
    if not args.skip_changed_paths:
        diff = _git(repo_root, "diff", "--name-only", args.changed_from, "HEAD")
        if diff.returncode != 0:
            print(diff.stderr.strip())
            return 1
        changed_paths = [line for line in diff.stdout.splitlines() if line]

    errors = validate_governance(
        _load(ledger_path),
        _load(inventory_path),
        _load(governed_paths_path),
        repo_root,
        release=args.release,
        verify_tag=args.verify_tag,
        previous_ledger=previous,
        changed_paths=changed_paths,
    )
    if args.release and args.verify_tag and args.release_remote:
        release_details = _mapping(_load(ledger_path).get("release"))
        errors.extend(
            validate_remote_release_identity(
                repo_root,
                args.release_remote,
                str(release_details.get("branch", "")),
                str(release_details.get("tag", "")),
                str(release_details.get("peeled_commit", "")),
                str(release_details.get("peeled_tree", "")),
            )
        )
    if errors:
        for error in errors:
            print(error)
        return 1
    mode = "release" if args.release else "audit"
    print(f"downstream governance is valid in {mode} mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
