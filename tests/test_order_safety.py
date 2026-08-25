import os
import sys
import tempfile
import types
import unittest

try:
    import requests
except ModuleNotFoundError:  # Keep unit tests runnable before optional runtime deps are installed.
    class RequestException(Exception):
        pass

    class Timeout(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    requests = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(
            RequestException=RequestException,
            Timeout=Timeout,
            ConnectionError=ConnectionError,
        )
    )
    sys.modules["requests"] = requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from order_safety import (
    AmbiguousOrderError,
    CooldownManager,
    OrderJournal,
    RiskGuard,
    SafeOrderExecutor,
    calculate_risk_position_size,
    get_dynamic_portfolio_tiers,
)


class TimeoutBithumb:
    def create_order(self, *args, **kwargs):
        raise requests.exceptions.Timeout("response lost")


class SuccessBithumb:
    def create_order(self, *args, **kwargs):
        return {"uuid": "exchange-1"}

    def get_order(self, uuid):
        return {"uuid": uuid, "state": "done"}


class OrderSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal = OrderJournal(os.path.join(self.temp_dir.name, "orders.json"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_timeout_is_recorded_and_blocks_a_duplicate_buy(self):
        executor = SafeOrderExecutor(self.journal)
        with self.assertRaises(AmbiguousOrderError):
            executor.submit(TimeoutBithumb(), "KRW-BTC", "bid", volume=1, price=10000)
        self.assertEqual(self.journal.orders[-1]["status"], "UNKNOWN")
        self.assertTrue(self.journal.has_unresolved_market("KRW-BTC"))

    def test_exchange_completion_unblocks_the_market(self):
        executor = SafeOrderExecutor(self.journal)
        executor.submit(SuccessBithumb(), "KRW-BTC", "bid", volume=1, price=10000)
        self.assertTrue(self.journal.has_unresolved_market("KRW-BTC"))
        self.assertEqual(self.journal.reconcile_exchange_statuses(SuccessBithumb().get_order), 1)
        self.assertEqual(self.journal.orders[-1]["status"], "FILLED")
        self.assertFalse(self.journal.has_unresolved_market("KRW-BTC"))

    def test_exchange_partial_fill_updates_status_to_partially_filled(self):
        executor = SafeOrderExecutor(self.journal)
        executor.submit(SuccessBithumb(), "KRW-BTC", "bid", volume=1, price=10000)
        
        def mock_get_order(_uuid):
            return {"uuid": _uuid, "status": "trade", "executed_volume": "0.5", "remaining_volume": "0.5"}
        
        self.assertEqual(self.journal.reconcile_exchange_statuses(mock_get_order), 1)
        self.assertEqual(self.journal.orders[-1]["status"], "PARTIALLY_FILLED")
        self.assertTrue(self.journal.has_unresolved_market("KRW-BTC"))

    def test_private_fill_event_updates_journal_by_client_order_id(self):
        client_id = self.journal.record_intent("KRW-BTC", "bid", 1, 10000, "limit")
        self.assertTrue(self.journal.apply_private_order_event({
            "client_order_id": client_id,
            "state": "trade",
            "order_id": "order-1",
            "executed_volume": "0.4",
            "remaining_volume": "0.6",
        }))
        order = self.journal.orders[-1]
        self.assertEqual(order["status"], "PARTIALLY_FILLED")
        self.assertEqual(order["exchange_order_id"], "order-1")

    def test_risk_guard_enforces_position_and_exposure_limits(self):
        guard = RiskGuard(5000, max_open_positions=2, max_position_pct=0.35, max_total_exposure_pct=0.85, max_order_krw=0)
        self.assertEqual(guard.validate_buy("KRW-BTC", 400_000, 900_000, 1_000_000, []), (False, "종목당 비중 한도 초과"))
        self.assertEqual(guard.validate_buy("KRW-BTC", 100_000, 900_000, 1_000_000, ["KRW-ETH", "KRW-XRP"]), (False, "동시 보유 종목 수 한도 초과"))
        self.assertEqual(guard.validate_buy("KRW-BTC", 100_000, 900_000, 1_000_000, []), (True, "OK"))

    def test_cooldown_manager(self):
        cd = CooldownManager(
            default_sl_cooldown=100.0,
            default_tp_cooldown=50.0,
            default_time_stop_cooldown=100.0,
            data_dir=self.temp_dir.name,
        )
        cd.record_exit("KRW-BTC", "STOP_LOSS", exit_price=100.0)
        is_cd, rem = cd.is_in_cooldown("KRW-BTC")
        self.assertTrue(is_cd)
        self.assertGreater(rem, 0.0)

        # 쿨다운 중에는 check_reentry_allowed가 False
        allowed, reason = cd.check_reentry_allowed("KRW-BTC", 100.5)
        self.assertFalse(allowed)
        self.assertIn("쿨다운 대기 중", reason)

        is_cd_eth, _ = cd.is_in_cooldown("KRW-ETH")
        self.assertFalse(is_cd_eth)

    def test_whipsaw_reentry_prevention_scenario(self):
        """316원 타임스탑 매도 후 317원 재매수 시도와 같은 휩쏘 횡보 재진입 차단 검증"""
        cd = CooldownManager(
            default_sl_cooldown=0.0,  # 즉시 만료로 설정하여 갭 필터만 독립 검증
            default_tp_cooldown=0.0,
            default_time_stop_cooldown=0.0,
            data_dir=self.temp_dir.name,
        )

        # 1. 타임스탑 316원 청산 기록
        cd.record_exit("KRW-CSIX", "TIME_STOP", exit_price=316.0)

        # 317원 (+0.32% 박스권 횡보) 시도 -> 차단되어야 함
        allowed, reason = cd.check_reentry_allowed("KRW-CSIX", 317.0, min_gap_pct=0.015)
        self.assertFalse(allowed)
        self.assertIn("박스권 횡보 구간", reason)
        self.assertIn("휩쏘 재진입 방지", reason)

        # 315원 (-0.32% 박스권 횡보) 시도 -> 차단되어야 함
        allowed_down, reason_down = cd.check_reentry_allowed("KRW-CSIX", 315.0, min_gap_pct=0.015)
        self.assertFalse(allowed_down)
        self.assertIn("박스권 횡보 구간", reason_down)

        # 322원 (+1.9% 상방 돌파) 시도 -> 승인되어야 함
        allowed_up, reason_up = cd.check_reentry_allowed("KRW-CSIX", 322.0, min_gap_pct=0.015)
        self.assertTrue(allowed_up)
        self.assertEqual(reason_up, "OK")

        # 2. 손절 316원 청산 기록
        cd.record_exit("KRW-DOGE", "STOP_LOSS", exit_price=316.0)

        # 317원 (+0.32% 미미한 반등) 시도 -> 차단되어야 함
        allowed_sl, reason_sl = cd.check_reentry_allowed("KRW-DOGE", 317.0, min_gap_pct=0.015)
        self.assertFalse(allowed_sl)
        self.assertIn("상방 돌파", reason_sl)

        # 322원 (+1.9% 돌파) 시도 -> 승인
        allowed_sl_ok, _ = cd.check_reentry_allowed("KRW-DOGE", 322.0, min_gap_pct=0.015)
        self.assertTrue(allowed_sl_ok)

    def test_calculate_risk_position_size(self):
        # 1,000,000 total equity, entry=100,000, SL=98,000 (2% stop), risk 1% = 10,000 max loss
        # Expected position = 10,000 / (~0.0218) ~= 450,000 -> capped at 35% (350,000)
        size = calculate_risk_position_size(
            total_equity=1_000_000.0,
            entry_price=100_000.0,
            stop_loss=98_000.0,
            risk_fraction=0.01,
            max_position_pct=0.35,
        )
    def test_is_managed_order_filters_external_orders(self):
        client_id = self.journal.record_intent("KRW-BTC", "bid", 1, 10000, "limit")
        self.journal.mark(client_id, "OPEN", exchange_uuid="uuid-bot-1")

        # 봇이 생성한 주문 ID / UUID는 True
        self.assertTrue(self.journal.is_managed_order(client_id))
        self.assertTrue(self.journal.is_managed_order("uuid-bot-1"))

        # 수동으로 생성된 외부 주문은 False
        self.assertFalse(self.journal.is_managed_order("uuid-manual-external-999"))
        self.assertFalse(self.journal.is_managed_order(""))

    def test_dynamic_portfolio_tiers(self):
        # 1. 소액 (< 30만 원) -> 2종목, 50%, 4개 스크리닝
        slots, pos_pct, top_n = get_dynamic_portfolio_tiers(27_680.0)
        self.assertEqual(slots, 2)
        self.assertEqual(pos_pct, 0.50)
        self.assertEqual(top_n, 4)

        # 2. 중소액 (30만 ~ 100만 원) -> 3종목, 35%, 5개 스크리닝
        slots, pos_pct, top_n = get_dynamic_portfolio_tiers(500_000.0)
        self.assertEqual(slots, 3)
        self.assertEqual(pos_pct, 0.35)
        self.assertEqual(top_n, 5)

        # 3. 100만 원 이상 -> 4종목, 25%, 6개 스크리닝
        slots, pos_pct, top_n = get_dynamic_portfolio_tiers(1_500_000.0)
        self.assertEqual(slots, 4)
        self.assertEqual(pos_pct, 0.25)
        self.assertEqual(top_n, 6)


if __name__ == "__main__":
    unittest.main()
