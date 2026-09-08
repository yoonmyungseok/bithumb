import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy_engine import StrategyPolicy, entry_signal


class TestMicroAssetPrice(unittest.TestCase):
    def test_min_asset_price_setting(self):
        self.assertEqual(StrategyPolicy.MIN_ASSET_PRICE_KRW, 0.0001)

    def test_entry_signal_allows_sub_10_krw_and_sub_1_krw(self):
        # 1) 0원 이하(<=0)는 비정상 가격으로 차단
        mock_zero = [{'trade_price': 0.0, 'candle_acc_trade_volume': 1000} for _ in range(30)]
        sig_zero = entry_signal(mock_zero)
        self.assertFalse(sig_zero['allow_buy'])
        self.assertIn('0.0001', sig_zero['reason'])

        # 2) 0.8524원 자산은 '저가 차단'에 걸리지 않아야 함
        mock_sub1 = [{'trade_price': 0.8524, 'candle_acc_trade_volume': 1000} for _ in range(30)]
        sig_sub1 = entry_signal(mock_sub1)
        self.assertNotIn('0.0001', sig_sub1.get('reason', ''))

        # 3) 3.5원 자산도 저가 차단에 걸리지 않아야 함
        mock_sub10 = [{'trade_price': 3.50, 'candle_acc_trade_volume': 1000} for _ in range(30)]
        sig_sub10 = entry_signal(mock_sub10)
        self.assertNotIn('0.0001', sig_sub10.get('reason', ''))

    def test_target_price_and_stop_loss_precision(self):
        current_price = 0.9143
        mock_candles = []
        for index in range(30):
            p = current_price + ((index % 4) - 1) * 0.005
            mock_candles.append({
                'trade_price': p,
                'high_price': p + 0.01,
                'low_price': p - 0.01,
                'candle_acc_trade_volume': 10000000
            })
        sig = entry_signal(mock_candles)
        if sig.get('target_price') is not None:
            # 1원 미만은 소수점 4자리까지 표현되어야 함
            self.assertEqual(sig['target_price'], round(sig['target_price'], 4))
        if sig.get('stop_loss') is not None:
            self.assertEqual(sig['stop_loss'], round(sig['stop_loss'], 4))


if __name__ == '__main__':
    unittest.main()
