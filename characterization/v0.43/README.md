# Untouched v0.43 characterization

This directory records diagnostic observations made before FIET applies any
runtime patch to canonical rindexer v0.43.0. The source identity is fixed at
commit `4f441289b83855c357239d2729fb725a56c3060b` and root tree
`80a2698f6be13949d84d920b01c02125af598d09`.

Run the source characterization and its regression tests with:

```bash
python3 -m unittest scripts/test_characterize_v043_baseline.py
python3 scripts/characterize_v043_baseline.py \
  --output characterization/v0.43/vanilla-baseline-source-receipt.json
cargo test --locked \
  --manifest-path characterization/v0.43/public-api-probe/Cargo.toml
cargo build -p rindexer_cli
# Restore canonical Cargo.lock after Cargo normalizes stale workspace-package versions.
git restore --source=HEAD -- Cargo.lock
python3 scripts/characterize_v043_process.py \
  --binary target/debug/rindexer_cli \
  --anvil "$(command -v anvil)" \
  --output characterization/v0.43/vanilla-baseline-process-receipt.json
```

The retained source receipt is diagnostic, not a release receipt. It records
both capabilities and known gaps. It must be combined with the pinned Rust
unit/integration, process, and Docker E2E results before OpenSpec task 3.12 is
complete. No result in this directory authorizes a behavioral patch by itself.

`vanilla-baseline-failure-injection-receipt.json` binds the existing atomic
PostgreSQL cursor regressions and the external legacy Rust-callback crash-window
probe. The expected redelivery is not classified as a failure; a custom handler
must own idempotent normative persistence until it opts into an atomic cursor API.

`vanilla-baseline-test-results.json` accumulates the executable test layers.
Its `remaining_before_immutable_characterization_receipt` list is normative for
the characterization campaign and prevents a partial green result from being
mistaken for the sealed baseline receipt.

After every required run passes, seal and independently verify the retained
evidence with:

```bash
python3 scripts/seal_v043_characterization.py
python3 scripts/seal_v043_characterization.py --verify
```

The resulting `vanilla-baseline-receipt.json` binds the exact canonical source,
toolchain, qualification runs, and SHA-256 of every subordinate artifact. It is
an immutable characterization gate only; `release_authorized` is always false.
