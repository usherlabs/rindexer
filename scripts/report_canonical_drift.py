#!/usr/bin/env python3
"""Produce a read-only report comparing the recorded and latest canonical releases."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


STABLE_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _path_matches(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(f"{prefix}/") or prefix.startswith(f"{path}/")


def compare_canonical(ledger: Mapping[str, Any]) -> str:
    canonical = ledger["canonical"]
    repository = str(canonical["repository"])
    recorded_tag = str(canonical["tag"])
    recorded_commit = str(canonical["commit"])
    recorded_tree = str(canonical["tree"])

    with tempfile.TemporaryDirectory() as directory:
        bare = Path(directory) / "canonical.git"
        subprocess.run(["git", "init", "--quiet", "--bare", bare], check=True)
        _git(bare, "fetch", "--quiet", "--force", repository, "+refs/tags/*:refs/tags/*")
        observed_commit = _git(bare, "rev-parse", f"refs/tags/{recorded_tag}^{{commit}}")
        observed_tree = _git(bare, "rev-parse", f"refs/tags/{recorded_tag}^{{tree}}")
        if observed_commit != recorded_commit or observed_tree != recorded_tree:
            raise ValueError("recorded canonical tag no longer matches its recorded commit and tree")

        tags = [
            tag
            for tag in _git(bare, "for-each-ref", "--format=%(refname:strip=2)", "refs/tags").splitlines()
            if STABLE_TAG.fullmatch(tag)
        ]
        if not tags:
            raise ValueError("canonical repository has no stable release tags")
        tags.sort(key=lambda tag: tuple(int(part) for part in tag.removeprefix("v").split(".")))
        latest_tag = tags[-1]
        latest_commit = _git(bare, "rev-parse", f"refs/tags/{latest_tag}^{{commit}}")
        latest_tree = _git(bare, "rev-parse", f"refs/tags/{latest_tag}^{{tree}}")
        if latest_commit != recorded_commit:
            ancestry = subprocess.run(
                ["git", "-C", str(bare), "merge-base", "--is-ancestor", recorded_commit, latest_commit],
                check=False,
            )
            if ancestry.returncode != 0:
                raise ValueError("latest canonical release is not descended from the recorded baseline")
            changed_paths = _git(bare, "diff", "--name-only", recorded_commit, latest_commit).splitlines()
            commits = _git(bare, "log", "--reverse", "--pretty=- `%h` %s", f"{recorded_commit}..{latest_commit}")
        else:
            changed_paths = []
            commits = ""

    lines = [
        "# Canonical rindexer drift report",
        "",
        f"- Recorded canonical tag: `{recorded_tag}`",
        f"- Recorded commit: `{recorded_commit}`",
        f"- Recorded tree: `{recorded_tree}`",
        f"- Latest canonical tag: `{latest_tag}`",
        f"- Latest commit: `{latest_commit}`",
        f"- Latest tree: `{latest_tree}`",
        f"- Review candidate: **{'yes' if latest_commit != recorded_commit else 'no'}**",
        "",
    ]
    if not changed_paths:
        lines.append("The recorded canonical release is current.")
    else:
        lines.extend(["## Candidate patch absorption", ""])
        for patch in ledger.get("patches", []):
            matching = sorted(
                path
                for path in changed_paths
                if any(_path_matches(path, prefix) for prefix in patch.get("affected_paths", []))
            )
            if matching:
                lines.append(f"- Candidate absorption: `{patch['id']}` ({', '.join(f'`{path}`' for path in matching)})")
        lines.extend(["", "## Canonical commits", "", commits, "", "## Changed paths", ""])
        lines.extend(f"- `{path}`" for path in changed_paths)
    lines.extend(["", "No branch, pull request, tag, merge, or deployment was created.", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ledger", type=Path, default=Path(__file__).resolve().parents[1] / "downstream-patches.json")
    args = parser.parse_args()
    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    report = compare_canonical(ledger)
    args.output.write_text(report, encoding="utf-8")
    print(f"canonical drift report written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
