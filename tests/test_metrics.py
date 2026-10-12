import threading
import unittest

from agent_qa.metrics import MetricsRegistry, _escape_label


class MetricsRegistryTests(unittest.TestCase):
    def test_render_includes_metric_metadata_and_sorted_samples(self):
        registry = MetricsRegistry()
        registry.record("PUT", "/ready", 405, 0.25)
        registry.record("GET", "/ready", 200, 0.5)
        registry.record("GET", "/ready", 200, 0.25)

        rendered = registry.render(3, "abc123")
        self.assertTrue(rendered.endswith("\n"))
        self.assertNotIn("\n\n", rendered)
        self.assertEqual(
            rendered,
            "\n".join(
                [
                    "# HELP agent_qa_build_info Build information.",
                    "# TYPE agent_qa_build_info gauge",
                    'agent_qa_build_info{git_sha="abc123"} 1',
                    "# HELP agent_qa_http_request_duration_seconds "
                    "HTTP request duration in seconds.",
                    "# TYPE agent_qa_http_request_duration_seconds summary",
                    'agent_qa_http_request_duration_seconds_count{method="GET",'
                    'route="/ready"} 2',
                    'agent_qa_http_request_duration_seconds_count{method="PUT",'
                    'route="/ready"} 1',
                    'agent_qa_http_request_duration_seconds_sum{method="GET",'
                    'route="/ready"} 0.75',
                    'agent_qa_http_request_duration_seconds_sum{method="PUT",'
                    'route="/ready"} 0.25',
                    "# HELP agent_qa_http_requests_total Total HTTP requests.",
                    "# TYPE agent_qa_http_requests_total counter",
                    'agent_qa_http_requests_total{method="GET",route="/ready",'
                    'status="200"} 2',
                    'agent_qa_http_requests_total{method="PUT",route="/ready",'
                    'status="405"} 1',
                    "# HELP agent_qa_orders Current number of orders.",
                    "# TYPE agent_qa_orders gauge",
                    "agent_qa_orders 3",
                    "",
                ]
            ),
        )

    def test_labels_escape_backslash_quote_and_newline(self):
        registry = MetricsRegistry()
        registry.record('G"ET', 'line\n"\\path', 500, 0.125)

        rendered = registry.render(0, 'sha"\\\n')
        self.assertEqual(
            _escape_label('back\\slash "quote"\nline'),
            'back\\\\slash \\"quote\\"\\nline',
        )
        self.assertIn('git_sha="sha\\"\\\\\\n"', rendered)
        self.assertIn(
            'method="OTHER",route="line\\n\\"\\\\path",status="500"',
            rendered,
        )
        self.assertTrue(rendered.endswith("\n"))

    def test_unrecognized_methods_share_a_bounded_series(self):
        registry = MetricsRegistry()
        for index in range(100):
            registry.record(f"CUSTOM-{index}", "/ready", 200, 0.01)
        registry.record("GET", "/ready", 200, 0.5)

        rendered = registry.render(0, "test")
        self.assertIn(
            'agent_qa_http_requests_total{method="GET",route="/ready",'
            'status="200"} 1',
            rendered,
        )
        self.assertIn(
            'agent_qa_http_requests_total{method="OTHER",route="/ready",'
            'status="200"} 100',
            rendered,
        )
        self.assertNotIn("CUSTOM-", rendered)
        self.assertEqual(rendered.count('method="'), 6)
        self.assertLess(len(rendered), 1000)

    def test_render_uses_a_consistent_request_snapshot(self):
        registry = MetricsRegistry()
        registry.record("GET", "/metrics", 200, 0.1)
        snapshot = registry.render(0, "test")
        self.assertIn(
            'agent_qa_http_requests_total{method="GET",route="/metrics",'
            'status="200"} 1',
            snapshot,
        )
        self.assertIn("agent_qa_http_requests_total", snapshot)

    def test_concurrent_recording_is_thread_safe(self):
        registry = MetricsRegistry()
        thread_count = 8
        records_per_thread = 250
        threads = [
            threading.Thread(
                target=lambda: [
                    registry.record("GET", "/ready", 200, 0.01)
                    for _ in range(records_per_thread)
                ]
            )
            for _ in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        rendered = registry.render(0, "test")
        self.assertIn(
            'agent_qa_http_requests_total{method="GET",route="/ready",'
            'status="200"} 2000',
            rendered,
        )
        self.assertIn(
            'agent_qa_http_request_duration_seconds_count{method="GET",'
            'route="/ready"} 2000',
            rendered,
        )
        self.assertIn(
            'agent_qa_http_request_duration_seconds_sum{method="GET",'
            'route="/ready"} 20.0',
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
