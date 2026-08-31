import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from bithumb_api import BithumbAPI
from market_screener import MarketScreener
from risk_manager import TrailingStopTracker
from strategy_engine import StrategyPolicy, entry_signal, is_major_market
from upbit_api import UpbitAPI


class TestOperationalImprovements(unittest.TestCase):
    def setUp(self):
        self.mock_exchange = MagicMock()
        self.mock_exchange.get_korean_name = lambda m: m.split('-')[-1]

    def test_major_market_classification(self):
        self.assertTrue(is_major_market('KRW-BTC'))
        self.assertTrue(is_major_market('BTC'))
        self.assertTrue(is_major_market('KRW-ETH'))
        self.assertTrue(is_major_market('ETH'))
        self.assertFalse(is_major_market('KRW-XRP'))
        self.assertFalse(is_major_market('KRW-SOL'))
        self.assertFalse(is_major_market('KRW-GRVT'))

    def test_major_vs_alt_trailing_and_tp(self):
        tracker = TrailingStopTracker(data_dir=os.path.join(os.getcwd(), 'data', 'test_risk'))
        tracker.clear('KRW-ETH')
        tracker.clear('KRW-GRVT')

        # 1. 메이저 코인 (KRW-ETH): +1.8% 도달 시 1차 분할 익절 발생
        res, peak, stop, peak_pct, real_pct = tracker.check_position(
            market='KRW-ETH',
            current_price=3465000.0,
            avg_buy_price=3400000.0,
        )
        self.assertEqual(res, 'PARTIAL_TP_1')

        # 2. 일반 알트코인 (KRW-GRVT): +2.5%에서는 1차 분할 익절 미발생 (알트는 +3.5% 기준)
        res_alt, _, _, _, _ = tracker.check_position(
            market='KRW-GRVT',
            current_price=251.125,
            avg_buy_price=245.0,
        )
        self.assertEqual(res_alt, 'NONE')

        # 알트코인은 +3.5% 도달 시 1차 분할 익절 발생
        res_alt_35, _, _, _, _ = tracker.check_position(
            market='KRW-GRVT',
            current_price=254.0,
            avg_buy_price=245.0,
        )
        self.assertEqual(res_alt_35, 'PARTIAL_TP_1')

    def test_major_coin_dynamic_target_in_entry_signal(self):
        candles = [{'trade_price': 3400000.0, 'high_price': 3410000.0, 'low_price': 3390000.0, 'opening_price': 3400000.0, 'candle_acc_trade_volume': 100.0} for _ in range(30)]
        res_eth = entry_signal(candles, market='KRW-ETH')
        res_alt = entry_signal(candles, market='KRW-GRVT')
        self.assertIn('target_price', res_eth)
        self.assertIn('target_price', res_alt)
        eth_tgt_pct = (res_eth['target_price'] - 3400000.0) / 3400000.0
        alt_tgt_pct = (res_alt['target_price'] - 3400000.0) / 3400000.0
        self.assertLess(eth_tgt_pct, alt_tgt_pct)

    def test_upbit_batch_orderbooks_query(self):
        upbit = UpbitAPI(access_key='dummy', secret_key='dummy')
        upbit._valid_markets_cache = {'KRW-BTC', 'KRW-ETH'}
        with patch.object(upbit, '_request') as mock_req:
            mock_req.return_value = [
                {'market': 'KRW-BTC', 'orderbook_units': [{'ask_price': 100, 'bid_price': 99}]},
                {'market': 'KRW-ETH', 'orderbook_units': [{'ask_price': 300, 'bid_price': 298}]},
            ]
            result = upbit.get_orderbooks(['KRW-BTC', 'KRW-ETH'])
            self.assertEqual(len(result), 2)
            mock_req.assert_called_once_with('GET', '/orderbook', params={'markets': 'KRW-BTC,KRW-ETH'})

    def test_market_screener_uses_batch_orderbooks(self):
        mock_api = MagicMock()
        mock_api.get_all_markets.return_value = [
            {'market': 'KRW-GRVT'},
            {'market': 'KRW-DOS'},
        ]
        mock_api.get_tickers.return_value = [
            {'market': 'KRW-GRVT', 'trade_price': 245.0, 'signed_change_rate': 0.08, 'acc_trade_price_24h': 5000000000.0},
            {'market': 'KRW-DOS', 'trade_price': 380.0, 'signed_change_rate': 0.05, 'acc_trade_price_24h': 4000000000.0},
        ]
        mock_api.get_orderbooks.return_value = [
            {
                'market': 'KRW-GRVT',
                'orderbook_units': [
                    {'ask_price': 245.0, 'bid_price': 244.0, 'bid_size': 100000.0},
                    {'ask_price': 246.0, 'bid_price': 243.0, 'bid_size': 100000.0},
                ],
            },
            {
                'market': 'KRW-DOS',
                'orderbook_units': [
                    {'ask_price': 380.0, 'bid_price': 379.0, 'bid_size': 100000.0},
                    {'ask_price': 381.0, 'bid_price': 378.0, 'bid_size': 100000.0},
                ],
            },
        ]
        screener = MarketScreener(bithumb_api=mock_api, max_spread_pct=0.015, min_trade_value_krw=1000000000.0)
        results = screener.scan_markets(top_count=2)
        mock_api.get_orderbooks.assert_called()
        self.assertGreaterEqual(len(results), 1)

if __name__ == '__main__':
    unittest.main()
