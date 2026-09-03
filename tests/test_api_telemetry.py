"""
API 일일 사용량 및 쿼터 텔레메트리 단위 테스트 (Exchange & Gemini API Telemetry)
"""

import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from api_telemetry import ExchangeApiTelemetry, get_kst_today_str
from gemini_telemetry import GeminiTelemetry
from dashboard_server import UnifiedDashboardServer


class TestExchangeApiTelemetry(unittest.TestCase):
    def setUp(self):
        self.bithumb_tel = ExchangeApiTelemetry("bithumb")
        self.bithumb_tel.reset()
        self.upbit_tel = ExchangeApiTelemetry("upbit")
        self.upbit_tel.reset()

    def test_record_calls_and_snapshot(self):
        """기본 호출 및 상태 코드 정상 기록 검증"""
        self.bithumb_tel.record_call(method="GET", endpoint="/v1/ticker", status_code=200)
        self.bithumb_tel.record_call(method="POST", endpoint="/v1/orders", status_code=201)
        self.bithumb_tel.record_call(method="GET", endpoint="/v1/orderbook", status_code=429)

        snap = self.bithumb_tel.snapshot()
        self.assertEqual(snap.total_calls, 3)
        self.assertEqual(snap.by_method["GET"], 2)
        self.assertEqual(snap.by_method["POST"], 1)
        self.assertEqual(snap.rate_limited_429, 1)
        self.assertEqual(snap.status_codes[200], 1)
        self.assertEqual(snap.status_codes[429], 1)
        self.assertEqual(snap.last_endpoint, "/v1/orderbook")
        self.assertEqual(snap.last_status, 429)

    def test_upbit_quota_and_groups(self):
        """업비트 그룹 및 잔여 쿼터(sec, min) 기록 검증"""
        self.upbit_tel.record_call(
            method="GET",
            endpoint="/candles/minutes/5",
            status_code=200,
            group="candle",
            remaining_sec=8,
            remaining_min=590,
        )

        snap = self.upbit_tel.snapshot()
        self.assertEqual(snap.total_calls, 1)
        self.assertEqual(snap.by_group.get("candle"), 1)
        self.assertEqual(snap.remaining_sec, 8)
        self.assertEqual(snap.remaining_min, 590)

    def test_date_rollover(self):
        """날짜 변경 시 일일 카운터 자동 초기화(롤오버) 검증"""
        self.bithumb_tel.record_call("GET", "/accounts", 200)
        self.assertEqual(self.bithumb_tel.snapshot().total_calls, 1)

        # 강제로 이전 날짜로 설정 후 새로운 날짜 체크
        with self.bithumb_tel._lock:
            self.bithumb_tel._current_date = "2020-01-01"

        # 다음 호출 시 롤오버 발생
        self.bithumb_tel.record_call("GET", "/ticker", 200)
        snap = self.bithumb_tel.snapshot()
        # 이전 날짜 호출은 초기화되고 새로운 날짜의 1회만 카운트
        self.assertEqual(snap.total_calls, 1)
        self.assertEqual(snap.date, get_kst_today_str())

    def test_thread_safety(self):
        """멀티스레드 동시 호출 시 데이터 정합성 검증"""
        threads = []
        calls_per_thread = 50
        thread_count = 10

        def worker():
            for i in range(calls_per_thread):
                self.bithumb_tel.record_call("GET", f"/api/test/{i}", 200)

        for _ in range(thread_count):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        snap = self.bithumb_tel.snapshot()
        self.assertEqual(snap.total_calls, calls_per_thread * thread_count)
        self.assertEqual(snap.by_method["GET"], calls_per_thread * thread_count)

    def test_singleton_instance_sharing(self):
        """동일 거래소명으로 새 인스턴스를 생성해도 동일한 텔레메트리 카운터를 공유하는지 검증"""
        tel1 = ExchangeApiTelemetry("bithumb")
        tel1.reset()
        tel1.record_call("GET", "/v1/ticker", 200)

        tel2 = ExchangeApiTelemetry("bithumb")
        snap2 = tel2.snapshot()
        self.assertEqual(snap2.total_calls, 1)

        tel2.record_call("POST", "/v1/orders", 200)
        snap1 = tel1.snapshot()
        self.assertEqual(snap1.total_calls, 2)
        self.assertIs(tel1, tel2)


class TestGeminiTelemetry(unittest.TestCase):
    def setUp(self):
        GeminiTelemetry.reset()

    def test_gemini_counters_and_percentages(self):
        """Gemini 성공, 429, 캐시 적중 및 비율 계산 검증"""
        GeminiTelemetry.record_api_success("gemini-3.5-flash-lite", "KRW-BTC")
        GeminiTelemetry.record_api_success("gemini-3.5-flash-lite", "KRW-ETH")
        GeminiTelemetry.record_rate_limited("gemini-3.5-flash-lite", "KRW-XRP")
        GeminiTelemetry.record_cache_hit("KRW-SOL")
        GeminiTelemetry.record_local_fallback("KRW-SOL", "rate limited")

        snap = GeminiTelemetry.snapshot()
        data = snap.to_dict()

        self.assertEqual(data["api_calls"], 3)  # 성공 2 + 429 1
        self.assertEqual(data["api_success"], 2)
        self.assertEqual(data["rate_limited"], 1)
        self.assertEqual(data["cache_hits"], 1)
        self.assertEqual(data["local_fallback"], 1)
        self.assertAlmostEqual(data["success_rate_pct"], 66.7, places=1)
        self.assertGreater(data["quota_limit"], 0)
        self.assertIn("quota_used_pct", data)


class TestDashboardServerApiUsage(unittest.TestCase):
    def test_aggregated_status_includes_api_usage(self):
        """대시보드 통합 상태 응답에 api_usage가 포함되는지 검증"""
        server = UnifiedDashboardServer(port=18999)
        # Mock status responses
        bithumb_mock = {
            "online": True,
            "total_equity": 1000000.0,
            "api_usage": {
                "exchange": {"total_calls": 50, "rate_limited_429": 0},
                "gemini": {"api_calls": 10, "quota_limit": 1500, "cache_hits": 5},
            },
        }
        upbit_mock = {
            "online": True,
            "total_equity": 2000000.0,
            "api_usage": {
                "exchange": {"total_calls": 120, "remaining_sec": 9},
                "gemini": {"api_calls": 15, "quota_limit": 1500, "cache_hits": 8},
            },
        }

        with patch.object(server, "fetch_exchange_status", side_effect=lambda url, ex: bithumb_mock if ex == "bithumb" else upbit_mock):
            agg = server.get_aggregated_status()
            combined = agg["combined"]
            self.assertIn("api_usage", combined)
            self.assertEqual(combined["api_usage"]["bithumb"]["total_calls"], 50)
            self.assertEqual(combined["api_usage"]["upbit"]["total_calls"], 120)
            self.assertEqual(combined["api_usage"]["gemini"]["api_calls"], 25)
            self.assertEqual(combined["api_usage"]["gemini"]["cache_hits"], 13)


if __name__ == "__main__":
    unittest.main()
