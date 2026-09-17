import email.utils
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bithumb_api import BithumbAPI


class BithumbAPITests(unittest.TestCase):
    def setUp(self):
        self.api = BithumbAPI("test-access-key-12345", "test-secret-key-67890")

    def test_get_tickers_reuses_short_lived_cache_and_force_refresh_bypasses_it(self):
        # 시장 스캔으로 받은 시세는 짧은 시간 동안 개별 현재가 조회에 재사용한다.
        self.api._valid_markets_cache = {"KRW-BTC"}
        with patch.object(self.api, "_request") as mock_request:
            mock_request.return_value = [{"market": "KRW-BTC", "trade_price": 100.0}]

            self.assertEqual(self.api.get_current_price("KRW-BTC"), 100.0)
            self.assertEqual(self.api.get_current_price("KRW-BTC"), 100.0)
            self.assertEqual(mock_request.call_count, 1)

            self.assertEqual(self.api.get_current_price("KRW-BTC", force_refresh=True), 100.0)
            self.assertEqual(mock_request.call_count, 2)

    def test_server_time_offset_applied_in_jwt_token(self):
        # 15초 오프셋을 설정했을 때 JWT 토큰 timestamp에 반영되는지 확인
        import jwt as pyjwt
        import time

        BithumbAPI.set_server_time_offset_for_test(15.0)
        try:
            t0 = time.time()
            token = self.api._generate_jwt_token()
            payload = pyjwt.decode(token, options={"verify_signature": False})

            expected_ts_min = int((t0 + 15.0 - 0.5) * 1000)
            expected_ts_max = int((time.time() + 15.0 + 0.5) * 1000)
            self.assertGreaterEqual(payload["timestamp"], expected_ts_min)
            self.assertLessEqual(payload["timestamp"], expected_ts_max)
        finally:
            BithumbAPI.set_server_time_offset_for_test(0.0)

    def test_sync_server_time_from_response_date_header(self):
        import time

        # 서버 시각이 로컬보다 10초 앞서는 임의의 Date 헤더
        server_epoch = time.time() + 10.0
        date_str = email.utils.formatdate(server_epoch, usegmt=True)

        offset = BithumbAPI._sync_server_time_from_headers({"Date": date_str})
        self.assertAlmostEqual(offset, 10.0, delta=1.5)
        self.assertAlmostEqual(BithumbAPI.get_server_time_offset(), 10.0, delta=1.5)
        BithumbAPI.set_server_time_offset_for_test(0.0)

    def test_get_request_recovers_on_expired_jwt(self):
        from unittest.mock import MagicMock
        import requests

        # 1차 응답: 401 expired_jwt, 2차 응답: 200 OK
        resp_401 = MagicMock(spec=requests.Response)
        resp_401.status_code = 401
        resp_401.text = '{"error":{"name":"expired_jwt","message":"Jwt가 만료되었습니다."}}'
        resp_401.headers = {"Date": email.utils.formatdate(time.time() + 2.0, usegmt=True)}

        resp_200 = MagicMock(spec=requests.Response)
        resp_200.status_code = 200
        resp_200.headers = {"Date": email.utils.formatdate(time.time() + 2.0, usegmt=True)}
        resp_200.json.return_value = [{"currency": "KRW", "balance": "50000.0", "locked": "0.0", "avg_buy_price": "0.0"}]

        with patch.object(self.api.session, "get", side_effect=[resp_401, resp_200]) as mock_get:
            balances = self.api.get_balances()
            self.assertEqual(mock_get.call_count, 2)
            self.assertIn("KRW", balances)
            self.assertEqual(balances["KRW"]["balance"], 50000.0)

    def test_post_request_does_not_retry_on_expired_jwt_to_prevent_duplicate_orders(self):
        from unittest.mock import MagicMock
        import requests

        resp_401 = MagicMock(spec=requests.Response)
        resp_401.status_code = 401
        resp_401.text = '{"error":{"name":"expired_jwt","message":"Jwt가 만료되었습니다."}}'
        resp_401.headers = {"Date": email.utils.formatdate(time.time() + 2.0, usegmt=True)}
        resp_401.raise_for_status.side_effect = requests.exceptions.HTTPError("401 Client Error")

        with patch.object(self.api.session, "post", return_value=resp_401) as mock_post:
            with self.assertRaises(requests.exceptions.HTTPError):
                self.api._request("POST", "/orders", data={"market": "KRW-BTC", "side": "bid", "volume": "0.01"})
            # 중복 발주 방지를 위해 1회만 호출되고 자동 재시도하지 않아야 함
            self.assertEqual(mock_post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
