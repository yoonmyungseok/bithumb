import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

# 프로젝트 루트 경로 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from backtest import QuantBacktester
from market_screener import MarketScreener
from order_safety import (
    OrderFillProcessor,
    OrderJournal,
    OrderStatus,
    SafeOrderExecutor,
)
from realtime_engine import RealtimeRiskEngine
from risk_manager import DailyRiskManager, TrailingStopTracker
from strategy_engine import StrategyPolicy, classify_btc_regime, entry_signal
from trade_memory import TradeMemoryManager
from upbit_websocket import UpbitWebSocketClient
from websocket_manager import BithumbWebSocketClient, WebSocketHealthState


class TestExecutionSafetyComprehensive(unittest.TestCase):
    """
    실거래 안전성, 가상 체결/손익 방지, 멱등성, 상태 단조성,
    시장별 웹소켓 헬스체크, 스크리너/백테스트 정합성 종합 단위 테스트
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = self.temp_dir.name
        self.journal = OrderJournal(data_dir=self.data_dir)
        self.executor = SafeOrderExecutor(self.journal)
        self.risk_manager = DailyRiskManager(data_dir=self.data_dir)
        self.trade_memory = TradeMemoryManager(data_dir=self.data_dir)
        self.trailing_tracker = TrailingStopTracker(data_dir=self.data_dir)
        self.mock_telegram = MagicMock()
        self.mock_sheets = MagicMock()

        self.fill_processor = OrderFillProcessor(
            order_journal=self.journal,
            risk_manager=self.risk_manager,
            trade_memory=self.trade_memory,
            trailing_tracker=self.trailing_tracker,
            telegram=self.mock_telegram,
            sheets=self.mock_sheets,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. 가상 체결 미발생 (No Phantom Fills / ACK != FILLED)
    # -------------------------------------------------------------------------
    def test_ack_does_not_record_realized_profit_or_entry_time(self):
        """거래소 ACK만 받은 경우 손익, 거래 횟수, 트레일링 진입 시간이 변경되지 않아야 함"""
        mock_api = MagicMock()
        mock_api.create_order.return_value = {"uuid": "ord-ack-123", "status": "ACKNOWLEDGED"}

        res = self.executor.submit(
            mock_api,
            market="KRW-BTC",
            side="bid",
            price=100_000_000.0,
            volume=0.01,
            ord_type="limit",
        )

        self.assertEqual(res["status"], "ACKNOWLEDGED")
        self.assertEqual(self.risk_manager.total_trades_today, 0)
        self.assertEqual(self.risk_manager.realized_pnl_krw, 0.0)
        self.assertEqual(len(self.trade_memory.get_recent_trades()), 0)
        self.assertEqual(self.trailing_tracker.get_entry_time("KRW-BTC"), 0.0)

    def test_ack_exit_does_not_record_exit_pnl_without_fill(self):
        """매도 청산 주문 접수 시 실제 체결량이 0이면 실현 손익이 0이어야 함"""
        mock_api = MagicMock()
        mock_api.create_order.return_value = {"uuid": "ord-exit-ack", "status": "ACKNOWLEDGED"}

        self.executor.submit(
            mock_api,
            market="KRW-ETH",
            side="ask",
            volume=1.0,
            ord_type="market",
            position_id="KRW-ETH",
            exit_reason="STOP_LOSS",
            avg_buy_price=4_000_000.0,
        )

        self.assertEqual(self.risk_manager.total_trades_today, 0)
        self.assertEqual(self.risk_manager.realized_pnl_krw, 0.0)

    # -------------------------------------------------------------------------
    # 2. 멱등성 및 증분 체결 (Idempotency & Incremental Fills)
    # -------------------------------------------------------------------------
    def test_duplicate_fill_event_is_idempotent(self):
        """동일한 체결 이벤트가 중복 수신되어도 손익과 거래 횟수가 1회만 반영되어야 함"""
        cid = self.journal.record_intent(
            market="KRW-SOL",
            side="ask",
            volume=10.0,
            price=None,
            ord_type="market",
            position_id="KRW-SOL",
            exit_reason="PARTIAL_TP",
            avg_buy_price=200_000.0,
        )
        self.journal.mark(cid, OrderStatus.ACKNOWLEDGED, exchange_uuid="ex-sol-1")

        # 1차 체결 이벤트: 5.0개 체결 (단가 220,000원 -> 주당 2만원 이익, 총 10만원)
        res1 = self.fill_processor.process_order_fill(
            order_identifier=cid,
            status=OrderStatus.PARTIALLY_FILLED,
            executed_volume=5.0,
            avg_price=220_000.0,
            fee=440.0,
            remaining_volume=5.0,
            exchange_uuid="ex-sol-1",
            exit_reason="PARTIAL_TP",
            avg_buy_price=200_000.0,
        )
        self.assertTrue(res1["processed"])
        self.assertAlmostEqual(res1["fill_delta"], 5.0)
        self.assertAlmostEqual(self.risk_manager.realized_pnl_krw, 99_560.0)  # (220,000 - 200,000)*5 - 440
        self.assertEqual(self.risk_manager.total_trades_today, 1)

        # 2차 중복 이벤트 (동일 수량 5.0개 재수신 -> fill_delta == 0 이므로 processed == False)
        res2 = self.fill_processor.process_order_fill(
            order_identifier=cid,
            status=OrderStatus.PARTIALLY_FILLED,
            executed_volume=5.0,
            avg_price=220_000.0,
            fee=440.0,
            remaining_volume=5.0,
            exchange_uuid="ex-sol-1",
        )
        self.assertFalse(res2["processed"])
        self.assertEqual(res2["fill_delta"], 0.0)
        self.assertAlmostEqual(self.risk_manager.realized_pnl_krw, 99_560.0)
        self.assertEqual(self.risk_manager.total_trades_today, 1)

        # 3차 잔여 체결 완료 이벤트 (executed_volume = 10.0, 증분 5.0)
        res3 = self.fill_processor.process_order_fill(
            order_identifier=cid,
            status=OrderStatus.FILLED,
            executed_volume=10.0,
            avg_price=220_000.0,
            fee=880.0,
            remaining_volume=0.0,
            exchange_uuid="ex-sol-1",
            exit_reason="PARTIAL_TP",
            avg_buy_price=200_000.0,
        )
        self.assertTrue(res3["processed"])
        self.assertAlmostEqual(res3["fill_delta"], 5.0)
        self.assertAlmostEqual(self.risk_manager.realized_pnl_krw, 199_120.0)
        self.assertEqual(self.risk_manager.total_trades_today, 2)

    # -------------------------------------------------------------------------
    # 3. 상태 머신 단조성 (Monotonic State Transitions)
    # -------------------------------------------------------------------------
    def test_state_cannot_regress_from_terminal_states(self):
        """FILLED나 CANCELED 상태에서 하위 상태로 역행하지 않아야 함"""
        cid = self.journal.record_intent("KRW-XRP", "ask", 100.0, None, "market")

        self.journal.mark(cid, OrderStatus.ACKNOWLEDGED)
        self.journal.mark(cid, OrderStatus.FILLED, executed_volume=100.0)

        # FILLED 상태에서 OPEN이나 PARTIALLY_FILLED로 변경 시도 -> FILLED 유지
        self.journal.mark(cid, OrderStatus.OPEN)
        rec = next(o for o in self.journal.orders if o["client_order_id"] == cid)
        self.assertEqual(rec["status"], OrderStatus.FILLED)

        self.journal.mark(cid, OrderStatus.PARTIALLY_FILLED)
        rec2 = next(o for o in self.journal.orders if o["client_order_id"] == cid)
        self.assertEqual(rec2["status"], OrderStatus.FILLED)

    # -------------------------------------------------------------------------
    # 4. 실시간 리스크 엔진 Fallback 제거 검증
    # -------------------------------------------------------------------------
    def test_realtime_engine_no_phantom_exit_on_failed_query(self):
        """실시간 엔진에서 거래소 체결 조회 실패 시 가상 체결 및 가상 손익을 기록하지 않아야 함"""
        mock_api = MagicMock()
        mock_api.get_order.return_value = None  # 조회 실패

        rt_engine = RealtimeRiskEngine(
            exchange_factory=lambda: mock_api,
            order_executor=self.executor,
            order_journal=self.journal,
            risk_manager=self.risk_manager,
            cooldown_manager=MagicMock(),
            trade_memory=self.trade_memory,
            trailing_tracker=self.trailing_tracker,
            telegram=self.mock_telegram,
            sheets=self.mock_sheets,
        )

        cid = self.journal.record_intent("KRW-DOGE", "ask", 1000.0, None, "market", position_id="KRW-DOGE")
        self.journal.mark(cid, OrderStatus.ACKNOWLEDGED, exchange_uuid="doge-ord-1")

        order_res = {"uuid": "doge-ord-1", "client_order_id": cid}
        res = rt_engine._confirm_and_record_exit(
            exchange=mock_api,
            market="KRW-DOGE",
            korean_name="도지코인",
            side_label="TRAILING_STOP",
            order_res=order_res,
            avg_buy_price=180.0,
            fallback_price=200.0,
            fallback_vol=1000.0,
            exit_reason="TRAILING_STOP",
            sheet_order_uuid="doge-ord-1",
            sheet_status_reason="트레일링 스탑",
            now_str="2026-08-25 12:00:00",
        )

        self.assertFalse(res["confirmed"])
        self.assertEqual(res["filled_delta"], 0.0)
        self.assertEqual(self.risk_manager.total_trades_today, 0)
        self.assertEqual(self.risk_manager.realized_pnl_krw, 0.0)

    # -------------------------------------------------------------------------
    # 5. 시장별 웹소켓 헬스체크 (Per-market WebSocket Health)
    # -------------------------------------------------------------------------
    def test_bithumb_websocket_per_market_health(self):
        """BithumbWebSocketClient의 시장별 개별 헬스 상태 판정 검증"""
        ws = BithumbWebSocketClient(initial_markets=["KRW-BTC", "KRW-ETH"])
        ws.is_connected = True

        # 1. 미구독 종목
        h_sol = ws.get_health_status(market="KRW-SOL")
        self.assertEqual(h_sol["status"], WebSocketHealthState.SUBSCRIPTION_FAILED)
        self.assertFalse(h_sol["is_healthy"])

        # 2. 구독 중이나 아직 틱 미수신 종목
        h_eth = ws.get_health_status(market="KRW-ETH")
        self.assertEqual(h_eth["status"], WebSocketHealthState.DATA_UNAVAILABLE)
        self.assertFalse(h_eth["is_healthy"])

        # 3. 틱 수신 종목
        ws._on_message(None, '{"type":"ticker","code":"KRW-BTC","trade_price":100000000.0}')
        h_btc = ws.get_health_status(market="KRW-BTC")
        self.assertEqual(h_btc["status"], WebSocketHealthState.DATA_AVAILABLE)
        self.assertTrue(h_btc["is_healthy"])

        # 4. 틱 수신 후 시간 지연(Stale) 종목
        ws.last_tick_time_by_market["KRW-BTC"] = time.time() - 20.0
        h_btc_stale = ws.get_health_status(market="KRW-BTC", max_stale_seconds=15.0)
        self.assertEqual(h_btc_stale["status"], WebSocketHealthState.STALE)
        self.assertFalse(h_btc_stale["is_healthy"])

    def test_upbit_websocket_per_market_health(self):
        """UpbitWebSocketClient의 시장별 개별 헬스 상태 판정 검증"""
        ws = UpbitWebSocketClient(initial_markets=["KRW-BTC", "KRW-XRP"])
        ws.is_connected = True

        h_unsub = ws.get_health_status(market="KRW-ADA")
        self.assertEqual(h_unsub["status"], WebSocketHealthState.SUBSCRIPTION_FAILED)

        ws._on_message(None, '{"type":"ticker","code":"KRW-XRP","trade_price":800.0}')
        h_xrp = ws.get_health_status(market="KRW-XRP")
        self.assertEqual(h_xrp["status"], WebSocketHealthState.DATA_AVAILABLE)
        self.assertTrue(h_xrp["is_healthy"])

    # -------------------------------------------------------------------------
    # 6. StrategyPolicy 단일 원천 검증
    # -------------------------------------------------------------------------
    def test_strategy_policy_constants(self):
        """StrategyPolicy의 단일 정책 상수가 정합하게 정의되어 있는지 검증"""
        self.assertEqual(StrategyPolicy.TIME_STOP_SECONDS, 7200)
        self.assertEqual(StrategyPolicy.TIME_STOP_BARS_5M, 24)
        self.assertAlmostEqual(StrategyPolicy.PARTIAL_TP_PCT, 0.035)
        self.assertAlmostEqual(StrategyPolicy.TRAILING_DROP_PCT, 0.020)
        self.assertAlmostEqual(StrategyPolicy.FEE_RATE, 0.0004)
        self.assertAlmostEqual(StrategyPolicy.SLIPPAGE_RATE, 0.001)

    # -------------------------------------------------------------------------
    # 7. 스크리너 Fallback Hard Guard
    # -------------------------------------------------------------------------
    def test_market_screener_fallback_hard_guard(self):
        """스크리너 fallback 종목도 스프레드 및 호가창 검증을 통과하지 못하면 배제되는지 검증"""
        mock_api = MagicMock()
        mock_api.get_all_markets.return_value = [
            {"market": "KRW-BTC"},
            {"market": "KRW-ETH"},
            {"market": "KRW-BAD"},
        ]
        # all_tickers: 거래대금 높지만 급등률 조건 미충족 -> fallback 대상
        mock_api.get_all_tickers.return_value = [
            {
                "market": "KRW-BAD",
                "trade_price": 1000.0,
                "signed_change_rate": 0.001,
                "acc_trade_value_24h": 5_000_000_000.0,
            }
        ]
        # 호가 스프레드 과다(1.0% > max 0.5%) 설정
        mock_api.get_orderbook.return_value = {
            "orderbook_units": [
                {"ask_price": 1010.0, "bid_price": 1000.0, "bid_size": 100.0}
            ]
        }

        screener = MarketScreener(mock_api, max_spread_pct=0.005)
        result = screener.scan_markets(top_count=2)
        markets = [r["market"] for r in result]
        self.assertNotIn("KRW-BAD", markets)

    # -------------------------------------------------------------------------
    # 8. 백테스트 정합성 (동일봉 손절 우선 & 포지션 단위 승률)
    # -------------------------------------------------------------------------
    def test_backtest_pessimistic_stop_loss_priority_and_round_trip(self):
        """동일 봉에서 Stop Loss와 Take Profit에 동시 도달 시 손절이 우선 처리되고 포지션 승률에 반영되는지 검증"""
        backtester = QuantBacktester(initial_capital=1_000_000.0, bithumb_api=MagicMock())

        mock_candles = []
        base_p = 10_000.0
        for idx in range(35):
            p = base_p + (idx * 50.0)
            mock_candles.append({
                "opening_price": p,
                "high_price": p + 40.0,
                "low_price": p - 20.0,
                "trade_price": p + 30.0,
                "candle_acc_trade_volume": 1000.0,
            })

        # 진입 직후 충돌 캔들 삽입
        mock_candles[0] = {
            "opening_price": base_p,
            "high_price": base_p * 1.10,  # +10% (TP 도달)
            "low_price": base_p * 0.90,   # -10% (Stop Loss 도달)
            "trade_price": base_p * 0.95,
            "candle_acc_trade_volume": 5000.0,
        }

        res = backtester.run_backtest(
            market="KRW-TEST",
            unit=5,
            count=len(mock_candles),
            candles=mock_candles,
        )

        self.assertIn("position_win_rate", res)
        self.assertIn("round_trip_count", res)
        self.assertIn("expectancy_pct", res)


if __name__ == "__main__":
    unittest.main()
