import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import StrategyPolicy, is_rs_leader, entry_signal
from market_screener import MarketScreener
from runtime_config import COMMON_CONFIG_SCHEMA


class TestLeaderBreakoutAndAiDirectBlock(unittest.TestCase):
    def test_ai_direct_entry_default_disabled(self):
        """AI 단독 진입이 기본적으로 비활성화(False)되어 로컬 퀀트 불허 시 AI가 단독 진입하지 못하는지 검증"""
        self.assertFalse(StrategyPolicy.ENABLE_AI_DIRECT_ENTRY)
        self.assertFalse(COMMON_CONFIG_SCHEMA["ENABLE_AI_DIRECT_ENTRY"].default)

    def test_ssot_constants_and_breakout_pct_b_cap(self):
        """기본 SSOT 상수의 일관성 검증"""
        self.assertEqual(StrategyPolicy.PCT_B_MAX, 0.70)
        self.assertEqual(StrategyPolicy.PCT_B_MAX_RISK_OFF, 0.65)
        self.assertEqual(StrategyPolicy.RSI_MIN_NORMAL, 42.0)

    def test_screener_weights_leaders_properly(self):
        """스크리너에서 상대강도 주도주 및 +6%~+15% 강세 종목의 점수가 정상 우대되는지 검증"""
        class FakeAPI:
            def get_all_markets(self, is_details=True):
                return [{"market": "KRW-LEADER"}, {"market": "KRW-SLUGGISH"}, {"market": "KRW-BTC"}]

            def get_orderbook(self, market):
                return {
                    "orderbook_units": [
                        {"ask_price": 1001.0, "bid_price": 1000.0, "bid_size": 100000.0}
                    ]
                }

        screener = MarketScreener(bithumb_api=FakeAPI(), max_change_rate=0.15)
        tickers = [
            # 주도주 (+10%, 거래대금 50억, RS +9%)
            {
                "market": "KRW-LEADER",
                "trade_price": 1000.0,
                "signed_change_rate": 0.10,
                "acc_trade_price_24h": 5_000_000_000.0,
            },
            # 횡보 종목 (+0.6%, 거래대금 50억, RS -0.4%)
            {
                "market": "KRW-SLUGGISH",
                "trade_price": 1000.0,
                "signed_change_rate": 0.006,
                "acc_trade_price_24h": 5_000_000_000.0,
            },
            # 비트코인 (+1%)
            {
                "market": "KRW-BTC",
                "trade_price": 100_000_000.0,
                "signed_change_rate": 0.01,
                "acc_trade_price_24h": 50_000_000_000.0,
            },
        ]
        res = screener.scan_markets(top_count=10, ticker_seed=tickers)
        leader = next((c for c in res if c["market"] == "KRW-LEADER"), None)
        sluggish = next((c for c in res if c["market"] == "KRW-SLUGGISH"), None)

        self.assertIsNotNone(leader)
        self.assertIsNotNone(sluggish)
        self.assertGreater(leader["score"], sluggish["score"] * 2.0)


if __name__ == "__main__":
    unittest.main()
