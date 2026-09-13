import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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

    def test_combined_watchlist_sorts_by_alpha_score_across_exchanges(self):
        """통합 Watchlist는 거래소 수집 순서와 무관하게 7대 알파 점수 내림차순이어야 한다."""
        def mock_fetch(_url, exchange_name):
            candidates = (
                [
                    {"market": "KRW-BTC", "alpha_score": 68},
                    {"market": "KRW-ETH", "alpha_score": 91},
                ]
                if exchange_name == "bithumb"
                else [
                    {"market": "KRW-SOL", "alpha_score": 84},
                    # 점수가 없거나 비수치인 후보도 화면에서 안전하게 최하위로 정렬한다.
                    {"market": "KRW-XRP", "alpha_score": "invalid"},
                ]
            )
            return {
                "online": True,
                "exchange": exchange_name,
                "candidates": candidates,
                "positions": [],
                "recent_trades": [],
                "recent_orders": [],
                "safety": {"entry_ready": True, "entry_block_reasons": [], "order_status_counts": {}},
            }

        self.server.fetch_exchange_status = MagicMock(side_effect=mock_fetch)

        candidates = self.server.get_aggregated_status()["combined"]["candidates"]

        self.assertEqual([item["market"] for item in candidates], ["KRW-ETH", "KRW-SOL", "KRW-BTC", "KRW-XRP"])
        self.assertEqual([item["exchange"] for item in candidates], ["bithumb", "upbit", "bithumb", "upbit"])

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

    def test_default_host_binding_from_env_or_default(self):
        """환경변수 DASHBOARD_HOST 설정 시 해당 값을 채택하고, 미설정 시 100.76.22.126을 기본 채택해야 한다."""
        with patch.dict(os.environ, {"DASHBOARD_HOST": "100.76.22.126", "DASHBOARD_PORT": "7979"}, clear=False):
            server = UnifiedDashboardServer()
            self.assertEqual(server.host, "100.76.22.126")
            self.assertEqual(server.port, 7979)

        with patch.dict(os.environ, {}, clear=False):
            if "DASHBOARD_HOST" in os.environ:
                del os.environ["DASHBOARD_HOST"]
            server_default = UnifiedDashboardServer(port=17999)
            self.assertEqual(server_default.host, "100.76.22.126")

    def test_explicit_host_override(self):
        """명시적으로 인자로 넘긴 host는 환경 변수보다 우선하여 바인딩되어야 한다."""
        with patch.dict(os.environ, {"DASHBOARD_HOST": "100.76.22.126"}):
            server = UnifiedDashboardServer(port=17999, host="127.0.0.1")
            self.assertEqual(server.host, "127.0.0.1")

    def test_dashboard_favicon_and_tab_icons(self):
        """파비콘 SVG 파일 존재 여부, HTML 파비콘 링크 및 탭 SVG 아이콘 포함 여부 검증"""
        dashboard_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dashboard"))
        favicon_path = os.path.join(dashboard_dir, "favicon.svg")
        index_html_path = os.path.join(dashboard_dir, "index.html")

        # 1. favicon.svg 파일 존재 및 SVG 태그 유효성 검증
        self.assertTrue(os.path.isfile(favicon_path), "dashboard/favicon.svg must exist")
        with open(favicon_path, "r", encoding="utf-8") as f:
            svg_content = f.read()
        self.assertIn("<svg", svg_content)
        self.assertIn("</svg>", svg_content)

        # 2. index.html에 파비콘 링크 및 탭 SVG 아이콘 포함 검증
        with open(index_html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        self.assertIn('rel="icon"', html_content)
        self.assertIn('href="/favicon.svg"', html_content)
        self.assertIn('id="exchange_combined"', html_content)
        self.assertIn('id="exchange_bithumb"', html_content)
        self.assertIn('id="exchange_upbit"', html_content)
        self.assertIn('id="tab_all"', html_content)
        self.assertIn('id="tab_positions"', html_content)
        self.assertIn('id="tab_candidates"', html_content)

        # 3. _render_unified_html 폴백 템플릿에도 파비콘 링크 포함 검증
        fallback_html = self.server._render_unified_html()
        self.assertIn('href="/favicon.svg"', fallback_html)
        self.assertIn('id="tab-combined"', fallback_html)
        self.assertIn('id="tab-bithumb"', fallback_html)
        self.assertIn('id="tab-upbit"', fallback_html)

    def test_read_recent_lines_backward_chunk_scan_captures_alerts_beyond_first_chunk(self):
        """대량의 INFO 로그가 쌓여 첫 청크 범위를 벗어난 이전 WARNING도 역방향 스캔으로 정상 포착해야 한다."""
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f:
            temp_path = f.name
            # 앞부분에 과거 WARNING 기록
            f.write("2026-09-08 04:00:00 [WARNING] 소켓 연결 끊김 테스트\n")
            # 청크 크기(1024 바이트)를 훌쩍 넘는 대량의 INFO 로그 추가 (약 10KB)
            for i in range(150):
                f.write(f"2026-09-08 05:00:{i%60:02d} [INFO] 사이클 정상 실행 중 {i}\n")

        try:
            # chunk_size를 1024 바이트로 작게 주입하여 역방향 청크 탐색 동작 강제
            lines = UnifiedDashboardServer._read_recent_lines(
                temp_path,
                chunk_size=1024,
                max_bytes=100 * 1024,
                min_alert_matches=1,
            )
            # 앞쪽의 WARNING 라인이 누락되지 않고 수집되었는지 검증
            warning_lines = [line for line in lines if "[WARNING]" in line]
            self.assertEqual(len(warning_lines), 1)
            self.assertIn("소켓 연결 끊김 테스트", warning_lines[0])

            # 존재하지 않는 파일이나 빈 파일도 크래시 없이 빈 리스트 반환 검증
            self.assertEqual(UnifiedDashboardServer._read_recent_lines("non_existent_file.log"), [])
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_recent_24h_filter_markup_and_scripts(self):
        """대시보드 완료 거래 내역 및 실시간 주문 저널의 최근 24시간 필터링 및 UI 표기 검증"""
        project_root = os.path.dirname(os.path.dirname(__file__))

        # 1. dashboard/index.html 검증
        index_html_path = os.path.join(project_root, "dashboard", "index.html")
        with open(index_html_path, "r", encoding="utf-8") as f:
            index_html = f.read()
        self.assertIn("최근 완료 거래 내역", index_html)
        self.assertIn("실시간 주문 저널 (Order Journal)", index_html)
        self.assertIn("비정상 운영 로그", index_html)
        self.assertIn("최근 24시간", index_html)
        self.assertIn("최근 24시간 내 완료된 거래 기록이 없습니다.", index_html)
        self.assertIn("최근 24시간 내 주문 저널 기록이 없습니다.", index_html)

        # 2. dashboard/src/app.js 필터링 및 사건 요약 로직 검증
        app_js_path = os.path.join(project_root, "dashboard", "src", "app.js")
        with open(app_js_path, "r", encoding="utf-8") as f:
            app_js = f.read()
        self.assertIn("function parseTimestampToMs", app_js)
        self.assertIn("function isWithinLast24Hours", app_js)
        self.assertIn("function extractIncidentHeadline", app_js)
        self.assertIn("isWithinLast24Hours(t.timestamp", app_js)
        self.assertIn("isWithinLast24Hours(o.timestamp", app_js)
        self.assertIn("isWithinLast24Hours(a.timestamp", app_js)

        # 3. src/dashboard_server.py 폴백 템플릿 검증
        fallback_html = self.server._render_unified_html()
        self.assertIn("최근 주문 저널 (Order Journal, 최근 24시간)", fallback_html)
        self.assertIn("최근 24시간 내 주문 내역이 없습니다.", fallback_html)
        self.assertIn("diff <= 24 * 3600 * 1000", fallback_html)

        # 4. src/web_server.py 템플릿 검증
        web_server_path = os.path.join(project_root, "src", "web_server.py")
        with open(web_server_path, "r", encoding="utf-8") as f:
            web_server_code = f.read()
        self.assertIn("최근 완료 거래 내역 <span class=\"text-xs font-normal text-slate-400 ml-1\">(최근 24시간)</span>", web_server_code)
        self.assertIn("실시간 주문 저널 <span class=\"text-xs font-normal text-slate-400 ml-1\">(최근 24시간)</span>", web_server_code)
        self.assertIn("최근 24시간 내 완료된 거래 기록이 없습니다.", web_server_code)
        self.assertIn("최근 24시간 내 주문 저널 기록이 없습니다.", web_server_code)

    def test_get_alert_logs_caching(self):
        """get_alert_logs가 짧은 TTL 내에서 디스크 재스캔 없이 캐시된 결과를 즉시 반환하는지 검증"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "trading.log")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("2026-09-09 12:00:00 [WARNING] 첫 번째 경고 로그\n")

            server = UnifiedDashboardServer(alert_log_dir=tmpdir)

            with patch.object(server, "_read_recent_lines", wraps=server._read_recent_lines) as mock_read:
                # 첫 번째 호출: 디스크 읽기 발생
                res1 = server.get_alert_logs("bithumb")
                self.assertEqual(len(res1["alerts"]), 1)
                self.assertEqual(mock_read.call_count, 2)  # trading.log, watchdog.log

                # 두 번째 호출 (즉시): 디스크 읽기 없이 캐시 반환
                res2 = server.get_alert_logs("bithumb")
                self.assertEqual(len(res2["alerts"]), 1)
                self.assertEqual(mock_read.call_count, 2, "2초 TTL 내에는 _read_recent_lines가 재호출되지 않아야 함")

    def test_market_intelligence_ui_components_rendered(self):
        """내장 HTML 템플릿에 거시 시장 인텔리전스 위젯 마크업 및 JS 로직이 포함되어 있는지 검증"""
        html = self.server._render_unified_html()

        # HTML 마크업 검증
        self.assertIn('id="market_intelligence_card"', html)
        self.assertIn('id="mi_regime_badge"', html)
        self.assertIn('id="mi_risk_score"', html)
        self.assertIn('id="mi_cash_ratio"', html)
        self.assertIn('id="mi_summary"', html)
        self.assertIn("거시 시장 인텔리전스 (Groq AI)", html)

        # CSS 뱃지 클래스 검증
        self.assertIn(".badge-danger", html)
        self.assertIn(".badge-warning", html)
        self.assertIn(".badge-secondary", html)
        self.assertIn(".badge-success", html)
        self.assertIn(".badge-info", html)

        # JavaScript updateUI 함수 및 바인딩 검증
        self.assertIn("function updateUI(data)", html)
        self.assertIn("updateUI(latestData)", html)
        self.assertIn("mi.risk_score", html)
        self.assertIn("mi.recommended_cash_ratio", html)
        self.assertIn("mi.market_summary", html)

    def test_aggregated_status_includes_market_intelligence_at_root(self):
        """get_aggregated_status 응답 최상위에 market_intelligence 데이터가 정확히 매핑되는지 검증"""
        sample_mi = {
            "regime": "BULL_TREND",
            "risk_score": 25,
            "recommended_cash_ratio": 0.15,
            "market_summary": "비트코인 4H 강한 상승 추세 지속",
            "action_guideline": "적극 매수 기조 유지",
        }

        def mock_fetch(url, exchange_name):
            return {
                "online": True,
                "exchange": exchange_name,
                "total_equity": 1_000_000.0,
                "krw_available": 500_000.0,
                "daily_start_equity": 1_000_000.0,
                "positions": [],
                "candidates": [],
                "recent_trades": [],
                "recent_orders": [],
                "market_intelligence": sample_mi if exchange_name == "bithumb" else {},
            }

        self.server.fetch_exchange_status = MagicMock(side_effect=mock_fetch)
        status = self.server.get_aggregated_status()

        # 최상위 키 확인
        self.assertIn("market_intelligence", status)
        self.assertIn("market_intelligence_bithumb", status)
        self.assertIn("market_intelligence_upbit", status)

        # 데이터 값 검증
        self.assertEqual(status["market_intelligence"]["regime"], "BULL_TREND")
        self.assertEqual(status["market_intelligence"]["risk_score"], 25)
        self.assertEqual(status["market_intelligence"]["recommended_cash_ratio"], 0.15)
        self.assertEqual(status["market_intelligence_bithumb"]["regime"], "BULL_TREND")
        self.assertEqual(status["market_intelligence_upbit"], {})

    @patch("dashboard_server.MarketIntelligenceService.get_instance")
    def test_aggregated_status_falls_back_to_market_intelligence_service_when_missing(self, mock_get_mi):
        """내부 API 응답에 market_intelligence가 누락되어 있을 때 로컬 캐시 폴백이 정상 작동하는지 검증"""
        fallback_mi = {
            "regime": "BEAR_REGIME",
            "risk_score": 70,
            "recommended_cash_ratio": 0.5,
            "market_summary": "폴백 거시 시장 요약",
            "action_guideline": "방어적 운용",
        }
        mock_service = MagicMock()
        mock_service.get_latest_intelligence.return_value = fallback_mi
        mock_get_mi.return_value = mock_service

        def mock_fetch_without_mi(url, exchange_name):
            return {
                "online": True,
                "exchange": exchange_name,
                "total_equity": 1_000_000.0,
                "krw_available": 500_000.0,
                "daily_start_equity": 1_000_000.0,
                "positions": [],
                "candidates": [],
                "recent_trades": [],
                "recent_orders": [],
                # market_intelligence 필드가 의도적으로 누락됨
            }

        self.server.fetch_exchange_status = MagicMock(side_effect=mock_fetch_without_mi)
        status = self.server.get_aggregated_status()

        self.assertEqual(status["market_intelligence"]["regime"], "BEAR_REGIME")
        self.assertEqual(status["market_intelligence"]["risk_score"], 70)
        self.assertEqual(status["market_intelligence_bithumb"]["regime"], "BEAR_REGIME")
        self.assertEqual(status["market_intelligence_upbit"]["regime"], "BEAR_REGIME")


if __name__ == "__main__":
    unittest.main()

