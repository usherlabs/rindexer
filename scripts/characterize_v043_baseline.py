#!/usr/bin/env python3
"""Characterize the exact, untouched canonical rindexer v0.43 runtime.

This harness is intentionally diagnostic.  It records both capabilities and
known gaps before any FIET behavioral patch is applied, and binds every source
observation to the canonical commit/tree.  It may run from a later test-only or
governance commit as long as the canonical baseline remains in its ancestry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "fiet-rindexer-vanilla-characterization/v1"
CANONICAL_COMMIT = "4f441289b83855c357239d2729fb725a56c3060b"
CANONICAL_ROOT_TREE = "80a2698f6be13949d84d920b01c02125af598d09"

REQUIRED_OBSERVATION_IDS = [
    "start_details_requires_watch",
    "default_health_port",
    "rust_indexing_starts_health_listener",
    "engine_process_signals",
    "engine_shutdown_calls_exit_zero",
    "event_cursor_primary_key",
    "event_cursor_has_detail_key",
    "latest_block_is_separate_coarse_table",
    "bloom_negative_emits_empty_batch",
    "empty_batch_reaches_cursor_update",
    "cursor_write_failure_reaches_caller",
    "atomic_postgres_cursor_is_opt_in",
    "rust_callbacks_are_atomic_by_default",
    "filter_mode_selects_first_alternative",
    "address_mode_selects_first_matching_alternative",
    "one_alternative_supports_multi_value_topics",
    "retry_boundaries_have_existing_regressions",
    "unexpected_event_stream_eof_returns_success",
    "native_processor_closed_channel_sleeps",
    "native_transfer_supports_bounded_range",
    "reorg_absent_is_disabled",
    "reorg_present_defaults_enabled",
    "reorg_internal_relations_are_unconditional",
    "hypersync_absent_is_disabled",
    "hypersync_some_replaces_historical_log_provider",
]


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _read(root: Path, relative: str) -> str:
    return _git(root, "show", f"{CANONICAL_COMMIT}:{relative}")


def _sha256(root: Path, relative: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{CANONICAL_COMMIT}:{relative}"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def _contains_all(source: str, *needles: str) -> bool:
    return all(needle in source for needle in needles)


def _observation(
    observation_id: str,
    observed: Any,
    evidence: list[str],
    implication: str,
) -> dict[str, Any]:
    return {
        "id": observation_id,
        "observed": observed,
        "evidence": evidence,
        "implication": implication,
    }


def characterize_repository(root: Path) -> dict[str, Any]:
    root = root.resolve()
    canonical_tree = _git(root, "show", "-s", "--format=%T", CANONICAL_COMMIT)
    if canonical_tree != CANONICAL_ROOT_TREE:
        raise RuntimeError(
            f"canonical root tree mismatch: expected {CANONICAL_ROOT_TREE}, got {canonical_tree}"
        )

    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", CANONICAL_COMMIT, "HEAD"],
        cwd=root,
        check=False,
    ).returncode == 0
    if not ancestor:
        raise RuntimeError(f"canonical baseline {CANONICAL_COMMIT} is not in HEAD ancestry")

    start = _read(root, "core/src/start.rs")
    global_manifest = _read(root, "core/src/manifest/global.rs")
    db_generate = _read(root, "core/src/database/generate.rs")
    pg_generate = _read(root, "core/src/database/postgres/generate.rs")
    last_synced = _read(root, "core/src/indexer/last_synced.rs")
    fetch_logs = _read(root, "core/src/indexer/fetch_logs.rs")
    process = _read(root, "core/src/indexer/process.rs")
    event_filter = _read(root, "core/src/event/rindexer_event_filter.rs")
    contract_manifest = _read(root, "core/src/manifest/contract.rs")
    pg_client = _read(root, "core/src/database/postgres/client.rs")
    no_code = _read(root, "core/src/indexer/no_code.rs")
    generator = _read(root, "core/src/generator/events_bindings.rs")
    indexer_start = _read(root, "core/src/indexer/start.rs")
    native_transfer = _read(root, "core/src/indexer/native_transfer.rs")
    network_manifest = _read(root, "core/src/manifest/network.rs")
    provider = _read(root, "core/src/provider.rs")

    cursor_ddl = (
        'CREATE TABLE IF NOT EXISTS rindexer_internal.{table_name} '
        '("network" TEXT PRIMARY KEY, "last_synced_block" NUMERIC);'
    )
    cursor_update = (
        "UPDATE rindexer_internal.{table_name} SET last_synced_block = {to_block} "
        "WHERE network = '{network}' AND {to_block} > last_synced_block"
    )

    observations = [
        _observation(
            "start_details_requires_watch",
            "pub watch: bool" in start,
            ["core/src/start.rs:StartDetails.watch"],
            "Maker v0.35 call sites require an explicit v0.43 watch value.",
        ),
        _observation(
            "default_health_port",
            8080 if "pub fn default_health_port() -> u16 {\n    8080\n}" in global_manifest else None,
            ["core/src/manifest/global.rs:default_health_port"],
            "The vanilla listener conflicts with services already using port 8080.",
        ),
        _observation(
            "rust_indexing_starts_health_listener",
            _contains_all(
                start,
                "let health_server_handle = if details.indexing_details.is_some()",
                "start_health_server(health_port, manifest_clone, postgres_client).await",
            ),
            ["core/src/start.rs:health_server_handle"],
            "Maker needs an embedded mode that suppresses this listener.",
        ),
        _observation(
            "engine_process_signals",
            ["SIGTERM", "SIGINT", "SIGQUIT"]
            if _contains_all(
                start,
                "SignalKind::terminate()",
                "SignalKind::interrupt()",
                "SignalKind::quit()",
            )
            else [],
            ["core/src/start.rs:shutdown_handle"],
            "The vanilla engine owns process signals instead of its embedding caller.",
        ),
        _observation(
            "engine_shutdown_calls_exit_zero",
            _contains_all(start, "async fn handle_shutdown", "std::process::exit(0)"),
            ["core/src/start.rs:handle_shutdown"],
            "The engine can overwrite Maker's drain-timeout exit status.",
        ),
        _observation(
            "event_cursor_primary_key",
            ["network"] if cursor_ddl in db_generate else [],
            ["core/src/database/generate.rs:event cursor DDL"],
            "Untouched v0.43 cursor identity is event/network scoped.",
        ),
        _observation(
            "event_cursor_has_detail_key",
            "detail_key" in cursor_ddl,
            ["core/src/database/generate.rs:event cursor DDL"],
            "Untouched v0.43 cannot represent exact per-manifest-detail coverage.",
        ),
        _observation(
            "latest_block_is_separate_coarse_table",
            _contains_all(
                db_generate,
                "rindexer_internal.latest_block",
                '"network" TEXT PRIMARY KEY, "block" NUMERIC',
            ),
            ["core/src/database/generate.rs:latest_block DDL"],
            "latest_block is a network watermark and is not exact event coverage.",
        ),
        _observation(
            "bloom_negative_emits_empty_batch",
            _contains_all(
                fetch_logs,
                "bloom_check_applies",
                "!is_relevant_block",
                "logs: Vec::new()",
                "from_block,\n                                        to_block",
            ),
            ["core/src/indexer/fetch_logs.rs:live_indexing_stream Bloom branch"],
            "Bloom-negative work enters the ordered consumer path without eth_getLogs.",
        ),
        _observation(
            "empty_batch_reaches_cursor_update",
            _contains_all(
                process,
                "let should_update_progress = if fn_data.is_empty()",
                "update_progress_and_last_synced_task(config, to_block",
            ),
            ["core/src/indexer/process.rs:trigger_event"],
            "The v0.43 Bloom path fixes the old in-memory-only empty-block advance, but only at event/network scope.",
        ),
        _observation(
            "cursor_write_failure_reaches_caller",
            not _contains_all(
                last_synced,
                "pub async fn update_progress_and_last_synced_task",
                "if let Err(e) = result",
                'error!("Error updating db last synced block',
                "on_complete();",
            ),
            ["core/src/indexer/last_synced.rs:update_progress_and_last_synced_task"],
            "Cursor persistence failures are logged and completion still advances; exact ordered coverage needs a fallible result.",
        ),
        _observation(
            "atomic_postgres_cursor_is_opt_in",
            _contains_all(pg_client, "pub async fn insert_bulk_with_cursor")
            and _contains_all(no_code, "let atomic_pg_cursor", "if atomic_pg_cursor"),
            [
                "core/src/database/postgres/client.rs:insert_bulk_with_cursor",
                "core/src/indexer/no_code.rs:atomic_pg_cursor",
            ],
            "Only the qualified no-code PostgreSQL raw-row path opts into atomic batch/cursor persistence.",
        ),
        _observation(
            "rust_callbacks_are_atomic_by_default",
            not _contains_all(
                generator,
                "insert_bulk_with_cursor",
                "&& !l.trim_start().starts_with(\"//\")",
            ),
            ["core/src/generator/events_bindings.rs:atomic cursor opt-in hint test"],
            "Generated/custom Rust callbacks remain at-least-once unless explicitly migrated.",
        ),
        _observation(
            "filter_mode_selects_first_alternative",
            ".indexed_filters\n                .map(|indexed_filter| indexed_filter.clone().into())" in event_filter
            and ".and_then(|f| f.first().cloned())" in contract_manifest,
            [
                "core/src/manifest/contract.rs:ContractDetails::indexing_contract_setup",
                "core/src/event/rindexer_event_filter.rs:new_filter",
            ],
            "A later filter-mode alternative is silently ignored on untouched v0.43.",
        ),
        _observation(
            "address_mode_selects_first_matching_alternative",
            _contains_all(
                event_filter,
                "address_details.indexed_filters.iter().find_map",
                "indexed_filters.iter().find(|&n| n.event_name == event_name)",
            ),
            ["core/src/event/rindexer_event_filter.rs:new_address_filter"],
            "Multiple same-event alternatives in one address detail collapse to the first match.",
        ),
        _observation(
            "one_alternative_supports_multi_value_topics",
            _contains_all(
                contract_manifest,
                "pub indexed_1: Option<Vec<String>>",
                "pub indexed_2: Option<Vec<String>>",
                "pub indexed_3: Option<Vec<String>>",
                "collect::<Vec<_>>().into()",
            ),
            ["core/src/manifest/contract.rs:EventInputIndexedFilters::into"],
            "Maker's one-entry Swap filter can retain multiple pool IDs in topic1.",
        ),
        _observation(
            "retry_boundaries_have_existing_regressions",
            _contains_all(
                fetch_logs,
                "retry_alchemy_block_range_parsing",
                "retry_response_too_big_halves_range",
                "retry_equal_from_to_returns_none",
                "live_indexing_caps_get_logs_to_configured_max_block_range",
            ),
            ["core/src/indexer/fetch_logs.rs:retry and live-range tests"],
            "Canonical includes useful range tests, but Outerlook boundary invariants still require audit.",
        ),
        _observation(
            "unexpected_event_stream_eof_returns_success",
            _contains_all(
                process,
                "while let Some(result) = logs_stream.next().await",
                "while let Some(joined) = in_flight.next().await",
                "Ok(())",
            ),
            ["core/src/indexer/process.rs:process_event_logs"],
            "Unexpected live stream EOF is indistinguishable from requested completion.",
        ),
        _observation(
            "native_processor_closed_channel_sleeps",
            _contains_all(
                native_transfer,
                "let recv = if buffer.is_empty()",
                "if recv == 0",
                "sleep(Duration::from_secs(1)).await;\n            continue;",
            ),
            ["core/src/indexer/native_transfer.rs:native_transfer_block_processor"],
            "Unexpected producer-channel closure does not terminate or fail the processor.",
        ),
        _observation(
            "native_transfer_supports_bounded_range",
            _contains_all(
                native_transfer,
                "end_block: Option<U64>",
                "let to_block = end_block.map(|end| block.min(end)).unwrap_or(block)",
                "Finished HISTORICAL INDEXING",
            ),
            ["core/src/indexer/native_transfer.rs:native_transfer_block_fetch"],
            "The engine has explicit bounded historical capability independent of Maker's live-only policy.",
        ),
        _observation(
            "reorg_absent_is_disabled",
            _contains_all(
                indexer_start,
                "n.reorg_handling.as_ref().and_then(|cfg|",
                "if cfg.enabled",
            ),
            ["core/src/indexer/start.rs:reorg_configs"],
            "An omitted reorg_handling mapping creates no coordinator.",
        ),
        _observation(
            "reorg_present_defaults_enabled",
            _contains_all(
                network_manifest,
                '#[serde(default = "default_reorg_enabled")]',
                "fn default_reorg_enabled() -> bool {\n    true\n}",
            ),
            ["core/src/manifest/network.rs:ReorgHandlingConfig"],
            "A present mapping with omitted enabled field activates reorg handling.",
        ),
        _observation(
            "reorg_internal_relations_are_unconditional",
            _contains_all(
                db_generate,
                "generate_reorg_block_hashes_table_sql()",
                "generate_derived_op_log_table_sql()",
            )
            and _contains_all(
                pg_generate,
                "rindexer_internal.reorg_block_hashes",
                "rindexer_internal.derived_op_log",
            ),
            [
                "core/src/database/generate.rs:generate_postgres_indexer_setup",
                "core/src/database/postgres/generate.rs:reorg DDL",
            ],
            "Schema migration must inventory reorg relations even while Maker disables runtime handling.",
        ),
        _observation(
            "hypersync_absent_is_disabled",
            _contains_all(
                network_manifest,
                "pub hypersync: Option<HypersyncConfig>",
                "Bool(false) | serde_yaml::Value::Null => Ok(None)",
                "assert!(network.hypersync.is_none())",
            ),
            ["core/src/manifest/network.rs:HypersyncConfig and default test"],
            "Omitting HyperSync preserves direct RPC historical log sourcing.",
        ),
        _observation(
            "hypersync_some_replaces_historical_log_provider",
            _contains_all(
                provider,
                "match &network.hypersync",
                "Some(hypersync_config) =>",
                "crate::hypersync::create_hypersync_provider",
            ),
            ["core/src/provider.rs:create_network_providers"],
            "Any configured HyperSync value changes the historical getLogs source and needs separate qualification.",
        ),
    ]

    observation_ids = [item["id"] for item in observations]
    if observation_ids != REQUIRED_OBSERVATION_IDS:
        raise RuntimeError("characterization observation inventory is incomplete or reordered")

    source_paths = [
        "core/src/start.rs",
        "core/src/manifest/global.rs",
        "core/src/database/generate.rs",
        "core/src/database/postgres/generate.rs",
        "core/src/indexer/last_synced.rs",
        "core/src/indexer/fetch_logs.rs",
        "core/src/indexer/process.rs",
        "core/src/event/rindexer_event_filter.rs",
        "core/src/manifest/contract.rs",
        "core/src/database/postgres/client.rs",
        "core/src/indexer/no_code.rs",
        "core/src/generator/events_bindings.rs",
        "core/src/indexer/start.rs",
        "core/src/indexer/native_transfer.rs",
        "core/src/manifest/network.rs",
        "core/src/provider.rs",
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "source_identity": {
            "repository": "https://github.com/usherlabs/rindexer",
            "canonical_repository": "https://github.com/joshstevens19/rindexer",
            "canonical_tag": "v0.43.0",
            "canonical_commit": CANONICAL_COMMIT,
            "canonical_root_tree": canonical_tree,
            "canonical_is_ancestor": ancestor,
            "observed_head": _git(root, "rev-parse", "HEAD"),
            "observed_head_tree": _git(root, "show", "-s", "--format=%T", "HEAD"),
        },
        "source_hashes": {path: _sha256(root, path) for path in source_paths},
        "required_observation_ids": REQUIRED_OBSERVATION_IDS,
        "observations": observations,
        "verdict": "characterized",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    receipt = characterize_repository(args.repository)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
