import unittest

from scripts.qualify_embedded_lifecycle import build_manifest, classify_run


class EmbeddedLifecycleQualificationTests(unittest.TestCase):
    def test_manifest_uses_explicit_health_port_without_watch_or_graphql(self):
        manifest = build_manifest("http://127.0.0.1:18545", 18080)

        self.assertIn("health_port: 18080", manifest)
        self.assertNotIn("watch", manifest)
        self.assertNotIn("graphql", manifest.lower())

    def test_success_requires_caller_signal_drain_and_engine_completion(self):
        observation, errors = classify_run(
            signal_name="SIGTERM",
            exit_code=0,
            output=(
                "FIET_EMBEDDED_CALLER_SIGNAL=SIGTERM\n"
                "FIET_EMBEDDED_DRAIN=complete\n"
                "FIET_EMBEDDED_ENGINE=complete\n"
            ),
            health_bound=False,
            force_timeout=False,
        )

        self.assertEqual(errors, [])
        self.assertTrue(observation["caller_signal_observed"])
        self.assertFalse(observation["engine_signal_handler_observed"])

    def test_drain_timeout_preserves_caller_nonzero_exit(self):
        observation, errors = classify_run(
            signal_name="SIGINT",
            exit_code=42,
            output="FIET_EMBEDDED_CALLER_SIGNAL=SIGINT\nFIET_EMBEDDED_DRAIN=timeout\n",
            health_bound=False,
            force_timeout=True,
        )

        self.assertEqual(errors, [])
        self.assertEqual(observation["expected_exit_code"], 42)

    def test_engine_signal_or_health_ownership_fails(self):
        _, errors = classify_run(
            signal_name="SIGTERM",
            exit_code=0,
            output=(
                "FIET_EMBEDDED_CALLER_SIGNAL=SIGTERM\n"
                "FIET_EMBEDDED_DRAIN=complete\n"
                "FIET_EMBEDDED_ENGINE=complete\n"
                "Received SIGTERM signal gracefully shutting down\n"
            ),
            health_bound=True,
            force_timeout=False,
        )

        self.assertIn("embedded rindexer bound its configured health port", errors)
        self.assertIn("engine-owned process signal handler was observed", errors)


if __name__ == "__main__":
    unittest.main()
