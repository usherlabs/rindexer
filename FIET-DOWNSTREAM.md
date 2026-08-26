# FIET downstream source governance

`usherlabs/rindexer` has two deliberately different branch families:

- `master` is a fast-forward-only mirror/reference for canonical `joshstevens19/rindexer`. FIET behavior never lands there.
- `fiet/v<release>` is a governed downstream release line created directly from one verified canonical tag, commit, and root tree. Pull requests and passing release/provenance checks are mandatory; repository review approval is optional. `fiet/v0.43` begins at canonical `v0.43.0` commit `4f441289b83855c357239d2729fb725a56c3060b` and tree `80a2698f6be13949d84d920b01c02125af598d09`.
- `fiet/<patch>-v<release>` is a narrow downstream patch branch. It targets the matching governed `fiet/v<release>` branch and carries a ledger entry, regression evidence, and an `FIET-PATCH:<id>` squash marker when the exact patch commit will not survive merging.
- `upstream/<patch>` is a portable contribution branch created from the applicable canonical tag. It must not contain the downstream governance commits or any other FIET-only ancestry.

## Canonical release adoption

Adopting a new canonical line always starts a new `fiet/v<release>` branch at that release's exact verified baseline. Never merge, rebase, or fast-forward a new canonical release wholesale into an already modified downstream release branch. Instead, rerun the patch audit against the untouched new baseline and carry forward only invariants that still fail.

The mirror procedure is:

1. Fetch canonical heads and tags without modifying a downstream release branch.
2. Verify the selected tag's peeled commit and root tree in a clean clone.
3. Fast-forward `master` only when it remains an unmodified canonical reference.
4. Create a new `fiet/v<release>` from the verified canonical identity.
5. Re-characterize every ledger item before applying downstream behavior.

## Patch ledger and release eligibility

`downstream-patch-inventory.json` is the immutable, hashed pre-convergence inventory. `downstream-patches.json` records the audit disposition, release state, ownership, source, affected paths and behavior, FIET requirement identities, regression tests, upstream state, evidence, and active ancestry for each item.

`pending` is valid while characterization is underway but is never release-eligible. A final disposition is one of:

- `ported`: canonical still violates the invariant and a narrow active patch is retained;
- `absorbed`: untouched canonical behavior passes the retained invariant regression;
- `superseded`: a different canonical mechanism satisfies the invariant;
- `not-behavioral`: the inventoried item contributes no independent runtime behavior.

Run audit validation during ordinary patch work:

```bash
python3 scripts/validate_downstream_governance.py
```

Release validation additionally rejects pending audit items and verifies the immutable tag's peeled identity:

```bash
python3 scripts/validate_downstream_governance.py --release --verify-tag
```

Qualified tags use `fiet-v<canonical-version>-<revision>`, beginning with `fiet-v0.43.0-1`. A tag is never retargeted. A failed release keeps its exact tag, commit, tree, ledger, and test evidence for forensic comparison.

Each replacement qualification uses its own retained evidence directory. The
original `fiet-v0.43.0-1` evidence remains under `qualification/v0.43`; the
`fiet-v0.43.0-2` attempt uses `qualification/v0.43.0-2`. Seal a replacement
without overwriting an earlier attempt by naming both its directory and the
actual source branch:

```bash
python3 scripts/seal_v043_fork_qualification.py \
  --qualification-dir qualification/v0.43.0-2 \
  --candidate-branch fiet/exact-cursor-seed-no-start-v0.43
```

The sealer accepts only `qualification/<safe-attempt-id>` directories and
`fiet/*` candidate branches. It binds the exact attempt paths, hashes, commit,
tree, and branch. Historical v1 receipts remain read-only verifiable with the
legacy defaults.

## Qualification toolchain

Downstream CI and release workflows use the exact Rust toolchain recorded in
`rust-toolchain.toml`, including its `clippy` and `rustfmt` components. The
v0.43 line is pinned to Rust 1.97.0. Untouched v0.43 passes its strict Clippy
gate on that toolchain; Rust 1.98 introduces
`result_large_err` diagnostics for existing public error types when CI enables
`-D warnings`. A toolchain update therefore requires its own non-behavioral CI
qualification instead of silently changing the release gate.

## Canonical drift and contributions

The scheduled canonical drift workflow is read-only. It reports newer releases, identities, changed paths, and possible patch absorption without creating or changing branches, pull requests, tags, or deployments.

For a portable upstream contribution, create `upstream/<patch>` directly from the recorded canonical tag, apply only the portable code and tests, and run:

```bash
python3 scripts/verify_canonical_contribution.py \
  --downstream-tip origin/fiet/v0.43
```

Only after the canonical contribution is accepted and a new canonical release is qualified may a downstream ledger item transition to `absorbed` or `superseded`.
