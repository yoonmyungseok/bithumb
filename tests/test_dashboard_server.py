import json
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


if __name__ == "__main__":
    unittest.main()
