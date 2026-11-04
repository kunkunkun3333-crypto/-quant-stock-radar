"""Offline regression tests for V6.0 health diagnostics (no network calls)."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v6_healthcheck", ROOT / "tools" / "v6_healthcheck.py")
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)


class HealthcheckTests(unittest.TestCase):
    def test_v6_repo_core_sources_parse(self):
        result = health.run(ROOT)
        self.assertEqual(result["failures"], 0, result["checks"])
        self.assertEqual(len(result["checks"]), len(health.REQUIRED))
        self.assertTrue(all(c["status"] == "pass" for c in result["checks"]))

    def test_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            result = health.run(Path(folder))
            self.assertGreater(result["failures"], 0)

    def test_syntax_error_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in health.REQUIRED:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x = 1\n" if target.suffix == ".py" else "", encoding="utf-8")
            (root / "v52_test" / "app_v52_test.py").write_text("def broken(:\n", encoding="utf-8")
            result = health.run(root)
            bad = next(x for x in result["checks"] if x["check"] == "v52_test/app_v52_test.py")
            self.assertEqual(bad["status"], "fail")

    def test_http_status_categories(self):
        self.assertEqual(health.classify_http(200), "ok")
        self.assertEqual(health.classify_http(401), "unauthorized")
        self.assertEqual(health.classify_http(403), "forbidden")
        self.assertEqual(health.classify_http(429), "rate_limited")
        self.assertEqual(health.classify_http(503), "server_error")

    def test_live_diagnostic_is_opt_in(self):
        with patch.object(health, "probe_url", side_effect=AssertionError("network called")):
            result = health.run(ROOT, live=False)
            self.assertEqual(result["failures"], 0)

    def test_http_failure_is_a_warning_not_a_false_success(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "limit", {}, None)
        with patch.object(health.urllib.request, "urlopen", side_effect=error):
            result = health.probe_url("https://example.invalid")
        self.assertEqual(result["category"], "rate_limited")
        self.assertEqual(result["status"], "unavailable")

    def test_probe_reports_network_error_without_credentials(self):
        with patch.object(health.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("offline")):
            result = health.probe_url("https://example.invalid")
        self.assertEqual(result["category"], "network_error")
        self.assertNotIn("offline", str(result))

    def test_v6_entry_delegates_to_original_v52_app(self):
        source = (ROOT / "v52_test" / "app_v52_test.py").read_text(encoding="utf-8")
        self.assertIn("runpy.run_path", source)
        self.assertIn("app_v52.py", source)


if __name__ == "__main__":
    unittest.main()
