#!/usr/bin/env python3
"""Verify that a portable contribution contains no downstream-only ancestry."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

try:
    from scripts.validate_downstream_governance import CANONICAL_COMMIT
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from validate_downstream_governance import CANONICAL_COMMIT


DOWNSTREAM_ONLY_PATHS = (
    "downstream-patches.json",
    "downstream-patch-inventory.json",
    "governed-paths.json",
    "FIET-DOWNSTREAM.md",
    ".github/workflows/downstream-governance.yml",
    ".github/workflows/canonical-drift.yml",
    ".github/workflows/canonical-contribution.yml",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def validate_contribution(repo: Path, baseline: str, candidate: str, downstream_tip: str) -> list[str]:
    errors: list[str] = []
    if _git(repo, "merge-base", "--is-ancestor", baseline, candidate).returncode != 0:
        errors.append("candidate is not descended from the canonical baseline")
        return errors
    if _git(repo, "merge-base", "--is-ancestor", downstream_tip, candidate).returncode == 0:
        errors.append("candidate includes downstream-only ancestry")
    shared = _git(repo, "merge-base", candidate, downstream_tip)
    if shared.returncode != 0 or shared.stdout.strip() != baseline:
        errors.append("candidate and downstream release do not diverge at the canonical baseline")
    merges = _git(repo, "rev-list", "--merges", f"{baseline}..{candidate}")
    if merges.returncode != 0 or merges.stdout.strip():
        errors.append("canonical contribution contains merge ancestry")
    changed = _git(repo, "diff", "--name-only", baseline, candidate)
    for path in changed.stdout.splitlines():
        if path in DOWNSTREAM_ONLY_PATHS:
            errors.append(f"canonical contribution includes downstream-only path: {path}")
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--baseline", default=CANONICAL_COMMIT)
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--downstream-tip", default="origin/fiet/v0.43")
    args = parser.parse_args()
    errors = validate_contribution(args.repo_root, args.baseline, args.candidate, args.downstream_tip)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("canonical contribution ancestry is clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
