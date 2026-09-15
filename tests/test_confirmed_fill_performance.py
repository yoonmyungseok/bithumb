"""확정 체결 기준 성과 집계·운영 리포트 회귀 테스트."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from confirmed_fill_performance import (
    build_confirmed_fill_report,
    build_trade_legs_from_journal,
    compare_with_daily_stats,
    kst_date_from_ts,
    summarize_trade_legs,
)
from order_safety.types import OrderStatus


def _buy_order(**overrides):
    base = {
        "client_order_id": "buy-1",
        "position_id": "pos-1",
        "market": "KRW-BTC",
        "side": "bid",
        "status": OrderStatus.FILLED,
        "avg_price": 100.0,
        "processed_executed_volume": 1.0,
        "processed_fee": 0.05,
        "slippage_bps": 2.0,
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_100.0,
        "entry_strategy_snapshot": {
            "entry_btc_regime": "NORMAL",
            "strategy_mode": "SCALP",
            "entry_reason": "ALPHA_BREAKOUT",
        },
        "exchange": "bithumb",
    }
    base.update(overrides)
    return base


def _sell_order(**overrides):
    base = {
        "client_order_id": "sell-1",
        "position_id": "pos-1",
        "market": "KRW-BTC",
        "side": "ask",
        "status": OrderStatus.FILLED,
        "avg_price": 110.0,
        "avg_buy_price": 100.0,
        "processed_executed_volume": 1.0,
        "processed_fee": 0.055,
        "slippage_bps": -1.0,
        "exit_reason": "TRAILING_STOP",
        "created_at": 1_700_000_500.0,
        "updated_at": 1_700_000_600.0,
        "last_event_at": 1_700_000_600.0,
        "exchange": "bithumb",
    }
    base.update(overrides)
    return base


class ConfirmedFillPerformanceTests(unittest.TestCase):
    def test_ack_only_buy_excluded_from_legs(self):
        orders = [
            _buy_order(
                client_order_id="ack-only",
                processed_executed_volume=0.0,
                processed_fee=0.0,
                status=OrderStatus.ACKNOWLEDGED,
                executed_volume=1.0,
            ),
            _sell_order(processed_executed_volume=0.0, status=OrderStatus.OPEN),
        ]
        legs = build_trade_legs_from_journal(orders, exchange="bithumb")
        self.assertEqual(legs, [])

    def test_canceled_order_without_fill_excluded(self):
        orders = [
            _buy_order(status=OrderStatus.CANCELED, processed_executed_volume=0.0),
            _sell_order(status=OrderStatus.CANCELED, processed_executed_volume=0.0),
        ]
        legs = build_trade_legs_from_journal(orders, exchange="bithumb")
        self.assertEqual(legs, [])

    def test_partial_fill_and_split_take_profit(self):
        orders = [
            _buy_order(processed_executed_volume=10.0, processed_fee=1.0),
            _sell_order(
                client_order_id="tp1",
                processed_executed_volume=4.0,
                processed_fee=0.4,
                exit_reason="PARTIAL_TP_1",
                last_event_at=1_700_001_000.0,
            ),
            _sell_order(
                client_order_id="tp2",
                processed_executed_volume=6.0,
                processed_fee=0.6,
                exit_reason="PARTIAL_TP_2",
                last_event_at=1_700_002_000.0,
            ),
        ]
        legs = build_trade_legs_from_journal(orders, exchange="bithumb")
        self.assertEqual(len(legs), 2)
        self.assertAlmostEqual(legs[0]["entry_fee_krw"], 0.4, places=4)
        self.assertAlmostEqual(legs[1]["entry_fee_krw"], 0.6, places=4)
        summary = summarize_trade_legs(legs)
        self.assertEqual(summary["confirmed_fill_count"], 2)

    def test_bilateral_fees_in_net_pnl(self):
        orders = [_buy_order(processed_fee=0.10), _sell_order(processed_fee=0.11)]
        legs = build_trade_legs_from_journal(orders, exchange="bithumb")
        self.assertEqual(len(legs), 1)
        leg = legs[0]
        gross = (110.0 * 1.0 - 0.11) - (100.0 * 1.0)
        net = gross - 0.10
        self.assertAlmostEqual(leg["gross_pnl_krw"], gross, places=2)
        self.assertAlmostEqual(leg["net_pnl_krw"], net, places=2)
        self.assertAlmostEqual(leg["total_fee_krw"], 0.21, places=2)

    def test_kst_midnight_date_split(self):
        ts_day1 = datetime_ts(2024, 1, 15, 23, 30)
        ts_day2 = datetime_ts(2024, 1, 16, 0, 30)
        self.assertEqual(kst_date_from_ts(ts_day1), "2024-01-15")
        self.assertEqual(kst_date_from_ts(ts_day2), "2024-01-16")

        orders = [
            _buy_order(),
            _sell_order(client_order_id="s1", last_event_at=ts_day1, updated_at=ts_day1),
            _buy_order(client_order_id="buy-2", position_id="pos-2"),
            _sell_order(
                client_order_id="s2",
                position_id="pos-2",
                last_event_at=ts_day2,
                updated_at=ts_day2,
            ),
        ]
        legs = build_trade_legs_from_journal(orders, exchange="bithumb")
        dates = {leg["kst_trading_date"] for leg in legs}
        self.assertEqual(dates, {"2024-01-15", "2024-01-16"})

    def test_exchange_isolation_in_report_builder(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = {
                "schema_version": 4,
                "exchange_scope": "upbit",
                "orders": [_buy_order(exchange="upbit"), _sell_order(exchange="upbit")],
            }
            with open(os.path.join(tmp, "order_journal.json"), "w", encoding="utf-8") as handle:
                json.dump(journal, handle)
            report = build_confirmed_fill_report(data_dir=tmp, exchange="bithumb")
            self.assertEqual(report["summary"]["confirmed_fill_count"], 0)

            report_up = build_confirmed_fill_report(data_dir=tmp, exchange="upbit")
            self.assertEqual(report_up["summary"]["confirmed_fill_count"], 1)

    def test_daily_stats_comparison_explanation(self):
        legs = build_trade_legs_from_journal([_buy_order(), _sell_order()], exchange="bithumb")
        kst_date = legs[0]["kst_trading_date"]
        comparison = compare_with_daily_stats(
            kst_date=kst_date,
            legs=legs,
            daily_stats={
                "date": kst_date,
                "realized_pnl_krw": 5.0,
                "total_trades": 1,
                "win_trades": 1,
            },
        )
        self.assertNotEqual(comparison["delta_pnl_krw"], 0.0)
        self.assertTrue(comparison["explanations"])
        self.assertIn("daily_stats", comparison["explanations"][0])


def datetime_ts(year: int, month: int, day: int, hour: int, minute: int) -> float:
    import datetime

    kst = datetime.timezone(datetime.timedelta(hours=9))
    dt = datetime.datetime(year, month, day, hour, minute, tzinfo=kst)
    return dt.timestamp()


if __name__ == "__main__":
    unittest.main()
