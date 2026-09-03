import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from strategy_engine import StrategyPolicy, entry_signal
from market_screener import MarketScreener


class StrategyImprovementsTests(unittest.TestCase):
    """3가지 방어 필터 완화 및 모멘텀 포착력 강화 개선안 검증 테스트"""

    def test_momentum_leader_selected_as_breakout_type(self):
        """당일 변동률 3% 이상 및 상대강도 1.5% 이상인 주도주는 MOMENTUM_BREAKOUT으로 분류된다."""
        class LeaderAPI:
            def get_all_markets(self, is_details=True):
                return [{"market": "KRW-BTC"}, {"market": "KRW-LEADER"}, {"market": "KRW-NORMAL"}]

            def get_tickers(self, markets):
                return [
                    {"market": "KRW-BTC", "trade_price": "100000", "signed_change_rate": "0.0", "acc_trade_price_24h": "10000000000"},
                    # LEADER: 변동률 +5%, RS +5% (주도주 조건 충족)
                    {"market": "KRW-LEADER", "trade_price": "5000", "signed_change_rate": "0.05", "acc_trade_price_24h": "10000000000"},
                    # NORMAL: 변동률 +2%, RS +2% (일반 확인형)
                    {"market": "KRW-NORMAL", "trade_price": "2000", "signed_change_rate": "0.02", "acc_trade_price_24h": "5000000000"},
                ]

            def get_orderbook(self, market):
                return {"orderbook_units": [{"ask_price": 5001.0, "bid_price": 5000.0, "bid_size": 10000.0}]}

        screener = MarketScreener(
            LeaderAPI(),
            min_trade_value_krw=1,
            min_change_rate=0.005,
            enable_early_breakout=True,
        )
        markets = screener.scan_markets(top_count=2)

        leader = next(m for m in markets if m["market"] == "KRW-LEADER")
        self.assertEqual(leader["candidate_type"], "MOMENTUM_BREAKOUT")

        normal = next(m for m in markets if m["market"] == "KRW-NORMAL")
        self.assertEqual(normal["candidate_type"], "CONFIRMED")

    def test_risk_off_realistic_hard_gates_pass(self):
        """RISK_OFF 환경에서도 완화된 파라미터(RSI 60~62, 이격도 1.03 이하)를 정상 수용한다."""
        # RSI 60.0~62.0이 형성되는 가격열
        prices = [100.0 + i*0.12 if i % 2 == 0 else 100.0 + i*0.12 - 0.5 for i in range(25)][::-1]
        candles = []
        for p in prices:
            candles.append({
                "opening_price": p - 0.2,
                "trade_price": p,
                "high_price": p + 0.3,
                "low_price": p - 0.3,
                "candle_acc_trade_volume": 1000.0,
            })

        candles_1h = [{"trade_price": 105.0} for _ in range(30)]

        res = entry_signal(
            candles=candles,
            candles_1h=candles_1h,
            btc_regime="RISK_OFF",
            orderbook={"orderbook_units": [{"ask_price": candles[0]["trade_price"] + 0.1, "bid_price": candles[0]["trade_price"], "bid_size": 1000}]},
            market="KRW-TEST",
            exchange="bithumb",
            entry_type="CONFIRMED",
            is_night=False,
        )
        hard_gates = res.get("checklist_details", {}).get("hard_gates", {})
        # 기존 RISK_OFF RSI 상한(58.0)이었으면 차단되었겠지만, 신규 상한(65.0)이므로 rsi_guard 통과해야 함
        rsi_val = hard_gates.get("rsi_guard", {}).get("value", 0.0)
        self.assertGreater(rsi_val, 58.0, f"RSI({rsi_val})가 기존 한도(58.0)를 초과해야 함")
        self.assertTrue(hard_gates.get("rsi_guard", {}).get("pass", False), f"RSI({rsi_val}) 하드게이트가 정상 통과되어야 함")
        self.assertTrue(hard_gates.get("disparity_guard", {}).get("pass", False), "이격도 하드게이트가 정상 통과되어야 함")

    def test_rebound_confirmed_flexibility(self):
        """양봉 전환된 상태에서 0.1% 미세 갭하락이어도 양봉이면 rebound_confirmed가 허용된다."""
        candles = [
            {"opening_price": 995.0, "trade_price": 1000.0, "high_price": 1002.0, "low_price": 994.0, "candle_acc_trade_volume": 100.0},
            {"opening_price": 990.0, "trade_price": 1001.0, "high_price": 1003.0, "low_price": 989.0, "candle_acc_trade_volume": 100.0},
        ]
        for i in range(2, 30):
            candles.append({"opening_price": 990.0, "trade_price": 990.0, "high_price": 995.0, "low_price": 985.0, "candle_acc_trade_volume": 100.0})

        candles_1h = [{"trade_price": 1000.0} for _ in range(30)]

        res = entry_signal(
            candles=candles,
            candles_1h=candles_1h,
            btc_regime="NORMAL",
            orderbook={"orderbook_units": [{"ask_price": 1001.0, "bid_price": 1000.0, "bid_size": 1000}]},
            market="KRW-TEST",
            exchange="bithumb",
            entry_type="CONFIRMED",
            is_night=False,
        )
        rebound_conf = res.get("checklist_details", {}).get("hard_gates", {}).get("rebound_confirmation", {})
        self.assertTrue(rebound_conf.get("pass", False), "0.2% 미세 차이 양봉은 반등 확인을 통과해야 함")

    def test_bearish_candle_never_confirmed(self):
        """음봉(현재가 < 시가)인 하락 캔들은 절대로 rebound_confirmed를 통과할 수 없다 (fail-closed 안전망)."""
        candles = [
            {"opening_price": 1005.0, "trade_price": 1000.0, "high_price": 1006.0, "low_price": 998.0, "candle_acc_trade_volume": 100.0},
            {"opening_price": 990.0, "trade_price": 999.0, "high_price": 1000.0, "low_price": 989.0, "candle_acc_trade_volume": 100.0},
        ]
        for i in range(2, 30):
            candles.append({"opening_price": 990.0, "trade_price": 990.0, "high_price": 995.0, "low_price": 985.0, "candle_acc_trade_volume": 100.0})

        candles_1h = [{"trade_price": 1000.0} for _ in range(30)]

        res = entry_signal(
            candles=candles,
            candles_1h=candles_1h,
            btc_regime="NORMAL",
            orderbook={"orderbook_units": [{"ask_price": 1001.0, "bid_price": 1000.0, "bid_size": 1000}]},
            market="KRW-TEST",
            exchange="bithumb",
            entry_type="CONFIRMED",
            is_night=False,
        )
        rebound_conf = res.get("checklist_details", {}).get("hard_gates", {}).get("rebound_confirmation", {})
        self.assertFalse(rebound_conf.get("pass", True), "음봉은 무조건 반등 확인 차단되어야 함")


if __name__ == "__main__":
    unittest.main()
