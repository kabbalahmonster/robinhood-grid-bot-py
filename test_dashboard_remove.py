import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "dashboard-remove.py"
SPEC = importlib.util.spec_from_file_location("dashboard_remove", SCRIPT)
dashboard_remove = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard_remove)


class TestDashboardRemove(unittest.TestCase):
    def test_builds_encoded_removal_url(self):
        self.assertEqual(
            dashboard_remove.removal_url("https://doomdash.ca/api/status", "OLD BOT/1"),
            "https://doomdash.ca/api/bots/OLD%20BOT%2F1",
        )

    def test_preserves_dashboard_path_prefix(self):
        self.assertEqual(
            dashboard_remove.removal_url("https://example.test/doom/api/status", "OLD"),
            "https://example.test/doom/api/bots/OLD",
        )

    def test_rejects_insecure_remote_url(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            dashboard_remove.removal_url("http://doomdash.ca/api/status", "OLD")

    def test_allows_local_http_for_testing(self):
        self.assertEqual(
            dashboard_remove.removal_url("http://127.0.0.1:5000/api/status", "OLD"),
            "http://127.0.0.1:5000/api/bots/OLD",
        )


if __name__ == "__main__":
    unittest.main()
