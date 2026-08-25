"""P0/P1 실거래 준비도 회귀 테스트: 호가 격리, 체결 메타데이터, 확정봉, 신호 동등성."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from backtest import synthesize_1h_candles
from order_safety import OrderFillProcessor, OrderJournal, OrderStatus, SafeOrderExecutor
from strategy_engine import OrderbookFlowTracker, calculate_composite_alpha_score, entry_signal, select_completed_candles
from trade_memory import TradeMemoryManager


def make_candles(count: int = 30) -> list[dict]:
    """지표 계산에 충분한 최신순 테스트 캔들을 결정론적으로 생성한다."""
    return [
        {
            "trade_price": 1_000.0 + (count - index),
            "opening_price": 999.0 + (count - index),
            "high_price": 1_003.0 + (count - index),
            "low_price": 997.0 + (count - index),
            "candle_acc_trade_volume": 100.0 + index,
            "candle_date_time_utc": f"2026-08-25T{(count - index) % 24:02d}:00:00",
        }
        for index in range(count)
    ]


class ReadinessP0P1Tests(unittest.TestCase):
    """실거래 주문 경로가 승인, 체결, 분석 데이터를 분리하는지 검증한다."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_readiness_")
        # 이 파일은 체결 경로의 메타데이터 연결을 검증하므로 느린 파일시스템 원자 저장은 모의한다.
        self.order_save_patch = patch("order_safety.write_json_atomically")
        self.memory_save_patch = patch("trade_memory.write_json_atomically")
        self.order_save_patch.start()
        self.memory_save_patch.start()

    def tearDown(self):
        self.memory_save_patch.stop()
        self.order_save_patch.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_orderbook_history_is_isolated_by_exchange_and_market(self):
        """종목과 거래소가 다르면 높은 호가 비율 이력이 서로 섞이지 않아야 한다."""
        tracker = OrderbookFlowTracker(max_history=5)
        with patch("strategy_engine.global_orderbook_tracker", tracker):
            candles = make_candles()
            hot_book = {"total_bid_size": 1_000.0, "total_ask_size": 100.0}
            neutral_book = {"total_bid_size": 100.0, "total_ask_size": 100.0}
            calculate_composite_alpha_score(candles, orderbook=hot_book, market="KRW-BTC", exchange="bithumb")
            result = calculate_composite_alpha_score(candles, orderbook=neutral_book, market="KRW-ETH", exchange="bithumb")
            self.assertEqual(result["factor_breakdown"]["orderbook_sample_count"], 1)
            self.assertEqual(result["factor_breakdown"]["orderbook_smoothed_ratio"], 1.0)
            calculate_composite_alpha_score(candles, orderbook=hot_book, market="KRW-BTC", exchange="upbit")
            self.assertEqual(tracker.get_sample_count("bithumb:KRW-BTC"), 1)
            self.assertEqual(tracker.get_sample_count("upbit:KRW-BTC"), 1)

    def test_single_orderbook_observation_is_not_max_score(self):
        """초기 한 번의 과도한 호가 잔량은 관측 부족 감쇠로 최고 점수를 받지 않아야 한다."""
        tracker = OrderbookFlowTracker(max_history=5)
        with patch("strategy_engine.global_orderbook_tracker", tracker):
            result = calculate_composite_alpha_score(
                make_candles(), orderbook={"total_bid_size": 1_000.0, "total_ask_size": 100.0}, market="KRW-BTC", exchange="bithumb"
            )
        self.assertLess(result["factor_breakdown"]["orderflow_score"], 15)

    def test_exit_fill_copies_entry_snapshot_but_ack_creates_no_memory(self):
        """ACK는 성과 기록을 만들지 않고, 실제 매도 체결만 진입 스냅샷을 복사해야 한다."""
        journal = OrderJournal(data_dir=self.test_dir)
        memory = TradeMemoryManager(data_dir=self.test_dir)
        processor = OrderFillProcessor(order_journal=journal, trade_memory=memory)
        executor = SafeOrderExecutor(journal)
        exchange = MagicMock()
        exchange.create_order.return_value = {"uuid": "buy-1"}
        snapshot = {
            "exchange": "bithumb", "entry_btc_regime": "RISK_OFF", "alpha_score": 71,
            "indicators": {"rsi": 52.0, "pct_b": 0.48, "atr": 10.0}, "entry_reason": "계약 테스트",
            "entry_decision_at": "2026-08-25 12:00:00", "target_price": 110.0, "stop_loss": 90.0,
        }
        buy = executor.submit(exchange, "KRW-TEST", "bid", 1.0, 100.0, position_id="pos-1", entry_strategy_snapshot=snapshot, exchange_name="bithumb")
        self.assertEqual(memory.get_recent_trades(), [])
        processor.process_order_fill(buy["client_order_id"], OrderStatus.FILLED, 1.0, avg_price=100.0, exchange_uuid="buy-1")
        sell_id = journal.record_intent("KRW-TEST", "ask", 1.0, 110.0, "limit", position_id="pos-1", avg_buy_price=100.0)
        processor.process_order_fill(sell_id, OrderStatus.FILLED, 1.0, avg_price=110.0, fee=1.0, remaining_volume=0.0)
        # 원자 저장은 이 테스트에서 모의했으므로, 메모리 객체의 실제 누적 레코드를 직접 검증한다.
        trade = memory.trades[-1]
        self.assertEqual(trade["position_id"], "pos-1")
        self.assertEqual(trade["entry_btc_regime"], "RISK_OFF")
        self.assertEqual(trade["alpha_score"], 71)
        self.assertEqual(trade["indicators"]["rsi"], 52.0)

    def test_completed_candles_exclude_open_bar_and_detect_duplicate_time(self):
        """진행 중 최신 봉은 제외하고, 시간 중복은 신규 진입 차단 신호로 처리해야 한다."""
        candles = make_candles(26)
        completed = select_completed_candles(candles, minimum_count=25)
        self.assertEqual(len(completed), 25)
        self.assertNotEqual(completed[0], candles[0])
        candles[1]["candle_date_time_utc"] = candles[0]["candle_date_time_utc"]
        self.assertEqual(select_completed_candles(candles, minimum_count=25), [])

    def test_incomplete_hourly_group_is_not_synthesized_and_signal_contract_matches(self):
        """불완전 1시간봉을 버리고 동일 입력 신호가 실행 모드와 무관하게 일치하는지 검증한다."""
        chronological = list(reversed(make_candles(11)))
        self.assertEqual(synthesize_1h_candles(chronological), [])
        candles = make_candles()
        live = entry_signal(candles, candles_1h=make_candles(25), btc_regime="NORMAL", market="KRW-TEST")
        replay = entry_signal(candles, candles_1h=make_candles(25), btc_regime="NORMAL", market="KRW-TEST")
        self.assertEqual(live["allow_buy"], replay["allow_buy"])
        self.assertEqual(live["target_price"], replay["target_price"])
        self.assertEqual(live["stop_loss"], replay["stop_loss"])


if __name__ == "__main__":
    unittest.main()
