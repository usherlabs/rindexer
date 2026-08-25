import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from characterize_v043_baseline import characterize_repository  # noqa: E402


class CharacterizeV043BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.receipt = characterize_repository(REPOSITORY_ROOT)
        cls.observations = {
            observation["id"]: observation for observation in cls.receipt["observations"]
        }

    def observed(self, observation_id):
        return self.observations[observation_id]["observed"]

    def test_exact_canonical_source_identity_is_retained(self):
        identity = self.receipt["source_identity"]
        self.assertEqual(
            identity["canonical_commit"],
            "4f441289b83855c357239d2729fb725a56c3060b",
        )
        self.assertEqual(
            identity["canonical_root_tree"],
            "80a2698f6be13949d84d920b01c02125af598d09",
        )
        self.assertTrue(identity["canonical_is_ancestor"])

    def test_embedded_lifecycle_is_not_maker_safe(self):
        self.assertTrue(self.observed("start_details_requires_watch"))
        self.assertEqual(self.observed("default_health_port"), 8080)
        self.assertTrue(self.observed("rust_indexing_starts_health_listener"))
        self.assertEqual(
            self.observed("engine_process_signals"), ["SIGTERM", "SIGINT", "SIGQUIT"]
        )
        self.assertTrue(self.observed("engine_shutdown_calls_exit_zero"))

    def test_cursor_is_network_scoped_and_progress_errors_are_not_propagated(self):
        self.assertEqual(
            self.observed("event_cursor_primary_key"), ["network"]
        )
        self.assertFalse(self.observed("event_cursor_has_detail_key"))
        self.assertTrue(self.observed("latest_block_is_separate_coarse_table"))
        self.assertTrue(self.observed("bloom_negative_emits_empty_batch"))
        self.assertTrue(self.observed("empty_batch_reaches_cursor_update"))
        self.assertFalse(self.observed("cursor_write_failure_reaches_caller"))
        self.assertTrue(self.observed("atomic_postgres_cursor_is_opt_in"))
        self.assertFalse(self.observed("rust_callbacks_are_atomic_by_default"))

    def test_filter_alternatives_are_first_only(self):
        self.assertTrue(self.observed("filter_mode_selects_first_alternative"))
        self.assertTrue(self.observed("address_mode_selects_first_matching_alternative"))
        self.assertTrue(self.observed("one_alternative_supports_multi_value_topics"))

    def test_stream_native_reorg_and_hypersync_baseline(self):
        self.assertTrue(self.observed("unexpected_event_stream_eof_returns_success"))
        self.assertTrue(self.observed("native_processor_closed_channel_sleeps"))
        self.assertTrue(self.observed("native_transfer_supports_bounded_range"))
        self.assertTrue(self.observed("reorg_absent_is_disabled"))
        self.assertTrue(self.observed("reorg_present_defaults_enabled"))
        self.assertTrue(self.observed("reorg_internal_relations_are_unconditional"))
        self.assertTrue(self.observed("hypersync_absent_is_disabled"))
        self.assertTrue(self.observed("hypersync_some_replaces_historical_log_provider"))

    def test_receipt_is_diagnostic_and_complete(self):
        self.assertEqual(self.receipt["schema_version"], "fiet-rindexer-vanilla-characterization/v1")
        self.assertEqual(self.receipt["verdict"], "characterized")
        self.assertEqual(len(self.observations), 25)
        self.assertEqual(set(self.observations), set(self.receipt["required_observation_ids"]))


if __name__ == "__main__":
    unittest.main()
