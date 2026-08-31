import json
import os
import unittest
from unittest.mock import MagicMock, patch

from dashboard_server import UnifiedDashboardServer


class UnifiedDashboardServerTests(unittest.TestCase):
    """통합 대시보드 게이트웨이 서버 단위 테스트"""

    def setUp(self):
        self.server = UnifiedDashboardServer(
            port=17999,
            host="127.0.0.1",
            bithumb_api_url="http://127.0.0.1:17979",
            upbit_api_url="http://127.0.0.1:17980",
        )

    def test_both_exchanges_online_aggregation(self):
        """빗썸과 업비트가 모두 온라인일 때 자산, 손익, 포지션이 정확히 합산되는지 검증"""
        mock_bithumb_resp = {
            "title": "Bithumb Trading Core",
            "total_equity": 1_000_000.0,
            "krw_available": 500_000.0,
            "daily_start_equity": 1_000_000.0,
            "daily_pnl_krw": 0.0,
            "realized_pnl_krw": 25_000.0,
            "status": "ACTIVE",
            "positions": [{"market": "KRW-BTC", "korean_name": "비트코인", "balance": 0.005, "avg_buy_price": 100000000.0, "current_price": 105000000.0}],
            "candidates": [{"market": "KRW-ETH", "korean_name": "이더리움", "alpha_score": 75}],
            "recent_trades": [{"market": "KRW-BTC", "pnl_krw": 25000.0}],
            "recent_orders": [{"market": "KRW-BTC", "side": "bid", "price": 100000000.0}],
            "fear_and_greed": "65점 (탐욕)",
        }
        mock_upbit_resp = {
            "title": "Upbit Trading Core",
            "total_equity": 2_000_000.0,
            "krw_available": 1_200_000.0,
            "daily_start_equity": 2_000_000.0,
            "daily_pnl_krw": 0.0,
            "realized_pnl_krw": 35_000.0,
            "status": "ACTIVE",
            "positions": [{"market": "KRW-ETH", "korean_name": "이더리움", "balance": 0.2, "avg_buy_price": 4000000.0, "current_price": 4200000.0}],
            "candidates": [{"market": "KRW-SOL", "korean_name": "솔라나", "alpha_score": 80}],
            "recent_trades": [{"market": "KRW-ETH", "pnl_krw": 35000.0}],
            "recent_orders": [{"market": "KRW-ETH", "side": "bid", "price": 4000000.0}],
            "fear_and_greed": "65점 (탐욕)",
        }

        def mock_fetch(url, exchange_name):
            if "17979" in url or exchange_name == "bithumb":
                data = dict(mock_bithumb_resp)
                data["online"] = True
                data["exchange"] = "bithumb"
                return data
            else:
                data = dict(mock_upbit_resp)
                data["online"] = True
                data["exchange"] = "upbit"
                return data

        self.server.fetch_exchange_status = MagicMock(side_effect=mock_fetch)

        agg = self.server.get_aggregated_status()
        combined = agg["combined"]

        self.assertEqual(combined["total_equity"], 3_000_000.0)
        self.assertEqual(combined["krw_available"], 1_700_000.0)
        self.assertEqual(combined["realized_pnl_krw"], 60_000.0)
        self.assertEqual(len(combined["positions"]), 2)
        self.assertTrue(combined["bithumb_online"])
        self.assertTrue(combined["upbit_online"])

    def test_single_exchange_offline_graceful_handling(self):
        """한쪽 거래소가 오프라인일 때도 크래시 없이 정상 거래소 데이터와 오프라인 상태가 표시되는지 검증"""
        def mock_fetch(url, exchange_name):
            if exchange_name == "bithumb":
                return {
                    "online": True,
                    "exchange": "bithumb",
                    "total_equity": 1_000_000.0,
                    "krw_available": 1_000_000.0,
                    "realized_pnl_krw": 0.0,
                    "positions": [],
                    "recent_orders": [],
                }
            else:
                return {
                    "online": False,
                    "status": "OFFLINE",
                    "exchange": "upbit",
                    "total_equity": 0.0,
                    "krw_available": 0.0,
                    "realized_pnl_krw": 0.0,
                    "positions": [],
                    "recent_orders": [],
                    "message": "오프라인",
                }

        self.server.fetch_exchange_status = MagicMock(side_effect=mock_fetch)

        agg = self.server.get_aggregated_status()
        combined = agg["combined"]

        self.assertEqual(combined["total_equity"], 1_000_000.0)
        self.assertTrue(combined["bithumb_online"])
        self.assertFalse(combined["upbit_online"])
        self.assertFalse(combined["safety"]["entry_ready"])
        self.assertIn("업비트 봇 오프라인", combined["safety"]["entry_block_reasons"])

    def test_combined_safety_requires_all_exchanges_to_be_entry_ready(self):
        """통합 탭은 한 거래소라도 대사 또는 시세 상태가 불명확하면 매수를 차단한다."""
        def mock_fetch(_url, exchange_name):
            is_bithumb = exchange_name == "bithumb"
            return {
                "online": True,
                "exchange": exchange_name,
                "total_equity": 1_000_000.0,
                "krw_available": 500_000.0,
                "positions": [],
                "candidates": [],
                "recent_trades": [],
                "recent_orders": [],
                "safety": {
                    "entry_ready": is_bithumb,
                    "entry_block_reasons": [] if is_bithumb else ["체결 대사 진행 주문 1건"],
                    "order_status_counts": {"FILLED": 2} if is_bithumb else {"RECONCILIATION_PENDING": 1},
                },
            }

        self.server.fetch_exchange_status = MagicMock(side_effect=mock_fetch)
        safety = self.server.get_aggregated_status()["combined"]["safety"]

        self.assertFalse(safety["entry_ready"])
        self.assertIn("업비트: 체결 대사 진행 주문 1건", safety["entry_block_reasons"])
        self.assertEqual(safety["order_status_counts"]["FILLED"], 2)
        self.assertEqual(safety["order_status_counts"]["RECONCILIATION_PENDING"], 1)

    def test_action_token_is_optional_but_enforced_when_configured(self):
        """기존 로컬 운용은 유지하되 환경 변수로 원격 제어 토큰을 강제할 수 있어야 한다."""
        with patch.dict("os.environ", {"DASHBOARD_ACTION_TOKEN": "test-action-token"}, clear=False):
            protected_server = UnifiedDashboardServer(port=17999)

        self.assertFalse(protected_server.is_action_authorized(""))
        self.assertFalse(protected_server.is_action_authorized("wrong-token"))
        self.assertTrue(protected_server.is_action_authorized("test-action-token"))

    def test_combined_safety_includes_exchange_feed_health(self):
        """통합 탭은 거래소별 시세 스트림 상태를 보존하고 일부 장애를 DEGRADED로 표시한다."""
        def mock_fetch(_url, exchange_name):
            healthy = exchange_name == "bithumb"
            return {
                "online": True,
                "exchange": exchange_name,
                "positions": [],
                "candidates": [],
                "recent_trades": [],
                "recent_orders": [],
                "safety": {
                    "entry_ready": healthy,
                    "entry_block_reasons": [] if healthy else ["시세 스트림 비정상 (STALE)"],
                    "order_status_counts": {},
                    "feed": {"is_healthy": healthy, "status": "DATA_AVAILABLE" if healthy else "STALE"},
                },
            }

        self.server.fetch_exchange_status = MagicMock(side_effect=mock_fetch)
        feed = self.server.get_aggregated_status()["combined"]["safety"]["feed"]

        self.assertFalse(feed["is_healthy"])
        self.assertEqual(feed["status"], "DEGRADED")
        self.assertEqual(feed["by_exchange"]["bithumb"]["status"], "DATA_AVAILABLE")
        self.assertEqual(feed["by_exchange"]["upbit"]["status"], "STALE")

    @patch("requests.Session.post")
    def test_forward_action_routing(self, mock_post):
        """거래소 타겟별 원격 액션 라우팅 검증"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "message": "일시정지 완료"}
        mock_post.return_value = mock_resp

        # 1. 빗썸 전용 액션
        res_bt = self.server.forward_action("pause", "bithumb")
        self.assertTrue(res_bt["success"])
        self.assertIn("bithumb", res_bt["results"])
        self.assertNotIn("upbit", res_bt["results"])

        # 2. 업비트 전용 액션
        res_up = self.server.forward_action("pause", "upbit")
        self.assertTrue(res_up["success"])
        self.assertIn("upbit", res_up["results"])
        self.assertNotIn("bithumb", res_up["results"])

        # 3. 전체 액션
        res_all = self.server.forward_action("pause", "all")
        self.assertTrue(res_all["success"])
        self.assertIn("bithumb", res_all["results"])
        self.assertIn("upbit", res_all["results"])

    def test_alert_logs_returns_only_warning_or_higher_and_masks_secret(self):
        """경고 이상 로그만 조회하고 인증 값은 브라우저 응답에서 제거한다."""
        server = UnifiedDashboardServer(port=17999)
        # 파일 접근 자체는 별도 유틸리티로 분리했으므로 필터와 마스킹 계약을 단위 검증한다.
        server._read_recent_lines = MagicMock(side_effect=[
            [
                "2026-08-31 10:00:00 [INFO] 정상 상태",
                "2026-08-31 10:01:00 [WARNING] 지연 감지",
                "2026-08-31 10:02:00 [ERROR] token=secret-value 주문 실패",
            ],
            [], [], [], [],
        ])
        result = server.get_alert_logs("bithumb")

        self.assertEqual([item["level"] for item in result["alerts"]], ["ERROR", "WARNING"])
        self.assertEqual(result["exchange"], "bithumb")
        self.assertNotIn("secret-value", result["alerts"][0]["message"])
        self.assertIn("token=***", result["alerts"][0]["message"])

    def test_alert_logs_filter_sources_by_exchange_tab(self):
        """거래소 탭에서는 타 거래소의 로그가 섞이지 않아야 한다."""
        server = UnifiedDashboardServer(port=17999)
        log_lines = {
            "trading.log": ["2026-08-31 10:00:00 [WARNING] 빗썸 경고"],
            "trading_upbit.log": ["2026-08-31 10:01:00 [ERROR] 업비트 오류"],
            "watchdog.log": ["[2026-08-31 10:02:00] [WARNING] [WATCHDOG] 빗썸 감시 경고"],
            "watchdog_upbit.log": ["[2026-08-31 10:03:00] [CRITICAL] [UPBIT-WATCHDOG] 업비트 감시 오류"],
            "dashboard.log": ["2026-08-31 10:04:00 [ERROR] [DASHBOARD] 통합 오류"],
        }
        server._read_recent_lines = MagicMock(side_effect=lambda path: log_lines[os.path.basename(path)])

        bithumb = server.get_alert_logs("bithumb")
        self.assertEqual({item["source"] for item in bithumb["alerts"]}, {"빗썸 봇", "빗썸 워치독"})
        self.assertTrue(all("업비트" not in item["message"] for item in bithumb["alerts"]))

        upbit = server.get_alert_logs("upbit")
        self.assertEqual({item["source"] for item in upbit["alerts"]}, {"업비트 봇", "업비트 워치독"})
        self.assertTrue(all("빗썸" not in item["message"] for item in upbit["alerts"]))


if __name__ == "__main__":
    unittest.main()
