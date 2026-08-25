"""
Directional Tick Rounding and Boundaries Tests for Bithumb and Upbit
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bithumb_api import BithumbAPI
from upbit_api import UpbitAPI


class TickRoundingDirectionalTests(unittest.TestCase):
    def test_bithumb_tick_sizes(self):
        """빗썸 가격대별 공식 호가 단위 검증"""
        self.assertEqual(BithumbAPI.get_tick_size(2_500_000), 1000.0)
        self.assertEqual(BithumbAPI.get_tick_size(1_500_000), 500.0)
        self.assertEqual(BithumbAPI.get_tick_size(600_000), 100.0)
        self.assertEqual(BithumbAPI.get_tick_size(200_000), 50.0)
        self.assertEqual(BithumbAPI.get_tick_size(50_000), 10.0)
        self.assertEqual(BithumbAPI.get_tick_size(5_000), 1.0)
        self.assertEqual(BithumbAPI.get_tick_size(500), 0.1)
        self.assertEqual(BithumbAPI.get_tick_size(50), 0.01)
        self.assertEqual(BithumbAPI.get_tick_size(5), 0.001)
        self.assertEqual(BithumbAPI.get_tick_size(0.5), 0.0001)

    def test_bithumb_directional_rounding(self):
        """빗썸 매수(내림), 매도(올림), 반올림 동작 검증"""
        # 50원 틱 구간 (100,000 ~ 500,000)
        # 105,430원: 내림=105,400원, 올림=105,450원, 반올림=105,450원
        self.assertEqual(BithumbAPI.adjust_price_to_tick(105430.0, side="bid"), 105400.0)
        self.assertEqual(BithumbAPI.adjust_price_to_tick(105430.0, side="ask"), 105450.0)
        self.assertEqual(BithumbAPI.adjust_price_to_tick(105430.0, mode="round"), 105450.0)
        self.assertEqual(BithumbAPI.round_price_to_tick(105430.0), 105450.0)

        # 0.1원 틱 구간 (100 ~ 1,000)
        # 543.26원: 내림=543.2원, 올림=543.3원
        self.assertEqual(BithumbAPI.adjust_price_to_tick(543.26, side="bid"), 543.2)
        self.assertEqual(BithumbAPI.adjust_price_to_tick(543.26, side="ask"), 543.3)

        # 0.0001원 틱 구간 (< 1)
        # 0.12346원: 내림=0.1234원, 올림=0.1235원
        self.assertEqual(BithumbAPI.adjust_price_to_tick(0.12346, side="bid"), 0.1234)
        self.assertEqual(BithumbAPI.adjust_price_to_tick(0.12346, side="ask"), 0.1235)

    def test_upbit_tick_sizes(self):
        """업비트 가격대별 공식 호가 단위 검증"""
        self.assertEqual(UpbitAPI.get_tick_size(2_500_000), 1000.0)
        self.assertEqual(UpbitAPI.get_tick_size(1_500_000), 500.0)
        self.assertEqual(UpbitAPI.get_tick_size(600_000), 100.0)
        self.assertEqual(UpbitAPI.get_tick_size(200_000), 50.0)
        self.assertEqual(UpbitAPI.get_tick_size(50_000), 10.0)
        self.assertEqual(UpbitAPI.get_tick_size(500), 1.0)  # 100원 이상은 1원 단위
        self.assertEqual(UpbitAPI.get_tick_size(50), 0.1)
        self.assertEqual(UpbitAPI.get_tick_size(5), 0.01)
        self.assertEqual(UpbitAPI.get_tick_size(0.5), 0.001)
        self.assertEqual(UpbitAPI.get_tick_size(0.05), 0.0001)

    def test_upbit_directional_rounding(self):
        """업비트 매수(내림), 매도(올림), 반올림 동작 검증"""
        # 100원 이상 구간 (1원 단위)
        # 543.6원: 내림=543.0원, 올림=544.0원
        self.assertEqual(UpbitAPI.adjust_price_to_tick(543.6, side="bid"), 543.0)
        self.assertEqual(UpbitAPI.adjust_price_to_tick(543.6, side="ask"), 544.0)
        self.assertEqual(UpbitAPI.adjust_price_to_tick(543.6, mode="round"), 544.0)
        self.assertEqual(UpbitAPI.round_price_to_tick(543.6), 544.0)

        # 10원~100원 구간 (0.1원 단위)
        # 45.67원: 내림=45.6원, 올림=45.7원
        self.assertEqual(UpbitAPI.adjust_price_to_tick(45.67, side="bid"), 45.6)
        self.assertEqual(UpbitAPI.adjust_price_to_tick(45.67, side="ask"), 45.7)

    def test_create_order_directional_price_submission(self):
        """create_order 및 send_order 호출 시 주문 방향에 맞게 가격이 보정되는지 검증"""
        bithumb = BithumbAPI()
        bithumb._request = MagicMock(return_value={"order_id": "test-ord"})

        # 빗썸 매수 지정가 ➜ 내림 보정 확인 (543.26 -> 543.2)
        bithumb.create_order(market="KRW-XRP", side="bid", volume=10.0, price=543.26, ord_type="limit")
        sent_data = bithumb._request.call_args[1]["data"]
        self.assertEqual(sent_data["price"], "543.2")

        # 빗썸 매도 지정가 ➜ 올림 보정 확인 (543.21 -> 543.3)
        bithumb.create_order(market="KRW-XRP", side="ask", volume=10.0, price=543.21, ord_type="limit")
        sent_data = bithumb._request.call_args[1]["data"]
        self.assertEqual(sent_data["price"], "543.3")

        # 업비트 매수 지정가 ➜ 내림 보정 확인 (105,430 -> 105,400)
        upbit = UpbitAPI()
        upbit._request = MagicMock(return_value={"uuid": "test-ord"})
        upbit.create_order(market="KRW-ETH", side="bid", volume=0.1, price=105430.0, ord_type="limit")
        sent_data_upbit = upbit._request.call_args[1]["data"]
        self.assertEqual(sent_data_upbit["price"], "105400")


if __name__ == "__main__":
    unittest.main()
