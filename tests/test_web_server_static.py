import os
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error
import json

# Add src to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from web_server import DashboardWebServer


class TestWebServerStatic(unittest.TestCase):
    def setUp(self):
        self.mock_data = {
            "total_equity": 1050000.0,
            "krw_available": 500000.0,
            "daily_start_equity": 1000000.0,
            "daily_pnl_krw": 50000.0,
            "daily_pnl_pct": 5.0,
            "realized_pnl_krw": 30000.0,
            "total_trades": 5,
            "win_trades": 4,
            "win_rate": 80.0,
            "fear_and_greed": "Greed (65)",
            "bot_state": "🟢 정상 가동 중",
            "btc_regime": "NORMAL",
            "positions": [],
            "candidates": []
        }
        self.action_called = None
        self.port = 17985

    def tearDown(self):
        if hasattr(self, "server") and self.server:
            self.server.stop()
            time.sleep(0.1)

    def _get_status(self):
        return dict(self.mock_data)

    def _handle_action(self, action_name: str) -> str:
        self.action_called = action_name
        return f"Action {action_name} executed"

    def test_cors_and_status_api(self):
        self.server = DashboardWebServer(
            port=self.port,
            host="127.0.0.1",
            data_provider=self._get_status,
            action_handler=self._handle_action,
            title="Test Bot",
        )
        self.server.start()
        time.sleep(0.2)

        # 1. Test GET /api/status
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/status")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
            self.assertIn("application/json", resp.headers.get("Content-Type"))
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["total_equity"], 1050000.0)
            self.assertEqual(data["title"], "Test Bot")

        # 2. Test OPTIONS preflight
        req_options = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/status", method="OPTIONS")
        with urllib.request.urlopen(req_options) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
            self.assertIn("GET", resp.headers.get("Access-Control-Allow-Methods"))

    def test_static_file_serving(self):
        self.server = DashboardWebServer(
            port=self.port,
            host="127.0.0.1",
            data_provider=self._get_status,
            action_handler=self._handle_action,
            title="Test Static Bot",
        )
        self.server.start()
        time.sleep(0.2)

        # Test GET / (index.html)
        req_root = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(req_root) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type"))
            content = resp.read().decode("utf-8")
            self.assertIn("AI 퀀트 트레이딩 Pro", content)

        # Test GET /src/styles.css
        req_css = urllib.request.Request(f"http://127.0.0.1:{self.port}/src/styles.css")
        with urllib.request.urlopen(req_css) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/css", resp.headers.get("Content-Type"))
            content = resp.read().decode("utf-8")
            self.assertIn("card-glass", content)

        # Test GET /src/app.js
        req_js = urllib.request.Request(f"http://127.0.0.1:{self.port}/src/app.js")
        with urllib.request.urlopen(req_js) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("javascript", resp.headers.get("Content-Type"))
            content = resp.read().decode("utf-8")
            self.assertIn("fetchStatus", content)

    def test_post_action(self):
        self.server = DashboardWebServer(
            port=self.port,
            host="127.0.0.1",
            data_provider=self._get_status,
            action_handler=self._handle_action,
        )
        self.server.start()
        time.sleep(0.2)

        req_post = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/action/pause",
            data=b"",
            method="POST"
        )
        with urllib.request.urlopen(req_post) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["success"])
            self.assertEqual(self.action_called, "pause")

    def test_fallback_when_no_static_dir(self):
        self.server = DashboardWebServer(
            port=self.port,
            host="127.0.0.1",
            data_provider=self._get_status,
            static_dir="/non/existent/path/here",
            title="Fallback Bot",
        )
        self.server.start()
        time.sleep(0.2)

        req_root = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(req_root) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type"))
            content = resp.read().decode("utf-8")
            self.assertIn("Fallback Bot", content)


if __name__ == "__main__":
    unittest.main()
