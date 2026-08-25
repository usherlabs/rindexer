import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from characterize_v043_process import build_manifest, classify_signal_run


class ProcessCharacterizationTests(unittest.TestCase):
    def test_manifest_exercises_live_indexing_and_default_health_port(self):
        manifest = build_manifest("http://127.0.0.1:18545")

        self.assertIn("native_transfers:", manifest)
        self.assertIn("network: anvil", manifest)
        self.assertNotIn("health_port", manifest)
        self.assertNotIn("global:", manifest)

    def test_successful_signal_run_requires_health_and_graceful_zero_exit(self):
        observation, errors = classify_signal_run(
            signal_name="SIGTERM",
            exit_code=0,
            output=(
                "Starting health server on port 8080\n"
                "Received SIGTERM signal gracefully shutting down...\n"
                "Graceful shutdown completed for SIGTERM\n"
            ),
            health_payload={"status": "healthy", "indexing": {"is_running": True}},
        )

        self.assertEqual(errors, [])
        self.assertTrue(observation["default_health_listener_observed"])
        self.assertTrue(observation["engine_signal_handler_observed"])
        self.assertTrue(observation["engine_forced_zero_exit_observed"])

    def test_nonzero_or_missing_shutdown_evidence_fails_closed(self):
        _, errors = classify_signal_run(
            signal_name="SIGINT",
            exit_code=1,
            output="Starting health server on port 8080\n",
            health_payload={"status": "healthy", "indexing": {"is_running": True}},
        )

        self.assertIn("SIGINT process exit was 1, expected 0", errors)
        self.assertIn("SIGINT handler log was not observed", errors)
        self.assertIn("SIGINT graceful completion log was not observed", errors)


if __name__ == "__main__":
    unittest.main()
