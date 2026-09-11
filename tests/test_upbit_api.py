import hashlib
import os
import sys
import threading
import unittest
import urllib.parse
from unittest.mock import MagicMock, patch

import warnings
import jwt

try:
    from jwt.warnings import InsecureKeyLengthWarning
    warnings.filterwarnings("ignore", category=InsecureKeyLengthWarning)
except ImportError:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from upbit_api import UpbitAPI, get_upbit_excluded_markets


class UpbitAPITests(unittest.TestCase):
    def setUp(self):
        # 클래스 공용 제한기/캐시가 다른 테스트 결과를 물려받지 않게 초기화한다.
        UpbitAPI.reset_shared_runtime_state_for_test()
        self.access_key = "test-access-key-12345"
        self.secret_key = "test-secret-key-67890"
        self.api = UpbitAPI(self.access_key, self.secret_key)

    def test_jwt_token_generation_without_params(self):
        token = self.api._generate_jwt_token()
        decoded = jwt.decode(token, self.secret_key, algorithms=["HS512"])
        self.assertEqual(decoded["access_key"], self.access_key)
        self.assertIn("nonce", decoded)
        self.assertNotIn("query_hash", decoded)

    def test_jwt_token_generation_with_params_sha512_hash(self):
        params = {
            "market": "KRW-BTC",
            "side": "bid",
            "volume": "0.01",
            "price": "100000000",
            "ord_type": "limit",
        }
        token = self.api._generate_jwt_token(params)
        decoded = jwt.decode(token, self.secret_key, algorithms=["HS512"])
        self.assertEqual(decoded["access_key"], self.access_key)
        self.assertEqual(decoded["query_hash_alg"], "SHA512")

        # 비인코딩 쿼리 스트링 SHA-512 해시 일치 검증
        expected_qs = urllib.parse.unquote(urllib.parse.urlencode(params, doseq=True)).encode("utf-8")
        expected_hash = hashlib.sha512(expected_qs).hexdigest()
        self.assertEqual(decoded["query_hash"], expected_hash)

    def test_round_price_to_tick_rules(self):
        # 2,000,000 이상 -> 1,000원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(2_500_400), 2_500_000.0)
        self.assertEqual(UpbitAPI.round_price_to_tick(2_500_600), 2_501_000.0)

        # 1,000,000 ~ 2,000,000 -> 500원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(1_234_200), 1_234_000.0)
        self.assertEqual(UpbitAPI.round_price_to_tick(1_234_300), 1_234_500.0)

        # 500,000 ~ 1,000,000 -> 100원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(750_140), 750_100.0)
        self.assertEqual(UpbitAPI.round_price_to_tick(750_160), 750_200.0)

        # 100,000 ~ 500,000 -> 50원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(234_120), 234_100.0)
        self.assertEqual(UpbitAPI.round_price_to_tick(234_130), 234_150.0)

        # 10,000 ~ 100,000 -> 10원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(55_432), 55_430.0)

        # 1,000 ~ 10,000 -> 1원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(3_456.7), 3457.0)

        # 100 ~ 1,000 -> 1원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(345.6), 346.0)

        # 10 ~ 100 -> 0.1원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(45.67), 45.7)

        # 1 ~ 10 -> 0.01원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(4.567), 4.57)

        # 0.1 ~ 1 -> 0.001원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(0.4567), 0.457)

        # < 0.1 -> 0.0001원 단위
        self.assertEqual(UpbitAPI.round_price_to_tick(0.04567), 0.0457)

    def test_round_volume(self):
        self.assertEqual(UpbitAPI.round_volume("KRW-BTC", 0.123456789), 0.12345679)
        self.assertEqual(UpbitAPI.round_volume("KRW-BTC", -1.0), 0.0)

    def test_rate_limit_header_parsing_blocks_only_exhausted_group(self):
        # Remaining-Req의 sec=0은 해당 그룹만 다음 초 경계까지 차단해야 한다.
        response = MagicMock()
        response.headers = {"Remaining-Req": "group=ticker; min=600; sec=0"}

        group, remaining = self.api._update_rate_limit_from_response(response, "ticker")

        self.assertEqual(group, "ticker")
        self.assertEqual(remaining, 0)
        self.assertIn("ticker", self.api._rate_limit_blocked_until)
        self.assertNotIn("orderbook", self.api._rate_limit_blocked_until)

    def test_rate_limit_candles_group_normalization_and_blocking(self):
        # /candles/와 /trades/ticks 엔드포인트는 업비트 규격인 복수형(candles, trades)으로 정규화된다.
        self.assertEqual(self.api._get_rate_limit_group("GET", "/candles/minutes/240"), "candles")
        self.assertEqual(self.api._get_rate_limit_group("GET", "/trades/ticks"), "trades")

        # 단수/복수형 어느 쪽으로 응답 헤더가 와도 candles 키로 통일 차단된다.
        response = MagicMock()
        response.headers = {"Remaining-Req": "group=candles; min=1799; sec=0"}
        group, remaining = self.api._update_rate_limit_from_response(response, "candle")

        self.assertEqual(group, "candles")
        self.assertEqual(remaining, 0)
        self.assertIn("candles", self.api._rate_limit_blocked_until)

        # 429 수동 차단도 candles로 정규화되어 기록된다.
        self.api._block_rate_limit_group("candle")
        self.assertIn("candles", self.api._rate_limit_blocked_until)

    def test_rate_limit_state_is_shared_between_instances(self):
        # 별도 인스턴스가 만든 429 차단도 같은 업비트 프로세스 전체에 적용되어야 한다.
        second_api = UpbitAPI(self.access_key, self.secret_key)
        response = MagicMock()
        response.headers = {"Remaining-Req": "group=candles; min=600; sec=0"}

        self.api._update_rate_limit_from_response(response, "candles")

        self.assertIs(self.api._rate_limit_blocked_until, second_api._rate_limit_blocked_until)
        self.assertIn("candles", second_api._rate_limit_blocked_until)


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

    def test_resolve_candle_cache_ttl_formula(self):
        self.assertEqual(UpbitAPI._resolve_candle_cache_ttl(5), 270.0)
        self.assertEqual(UpbitAPI._resolve_candle_cache_ttl(1), 30.0)
        self.assertEqual(UpbitAPI._resolve_candle_cache_ttl(60), 600.0)
        self.assertEqual(UpbitAPI._resolve_candle_cache_ttl(240), 1200.0)

    def test_resolve_candle_cache_ttl_env_override(self):
        with patch.dict(os.environ, {"UPBIT_CANDLE_CACHE_TTL_SHORT_SEC": "120"}, clear=False):
            self.assertEqual(UpbitAPI._resolve_candle_cache_ttl(5), 120.0)
        with patch.dict(os.environ, {"UPBIT_CANDLE_CACHE_TTL_LONG_SEC": "300"}, clear=False):
            self.assertEqual(UpbitAPI._resolve_candle_cache_ttl(60), 300.0)
        with patch.dict(os.environ, {"UPBIT_CANDLE_CACHE_TTL_4H_SEC": "900"}, clear=False):
            self.assertEqual(UpbitAPI._resolve_candle_cache_ttl(240), 900.0)

    def test_five_minute_candle_cache_reuses_within_ttl(self):
        self.api._valid_markets_cache = {"KRW-BTC"}
        fake_candles = [{"market": "KRW-BTC", "trade_price": 50000000.0}]
        with patch.object(self.api, "_request") as mock_request:
            mock_request.return_value = list(fake_candles)

            self.api.get_candles(unit=5, count=30, market="KRW-BTC")
            self.api.get_candles(unit=5, count=30, market="KRW-BTC")
            self.assertEqual(mock_request.call_count, 1)

            self.api.get_candles(unit=5, count=30, market="KRW-BTC", force_refresh=True)
            self.assertEqual(mock_request.call_count, 2)

    def test_get_candles_reuses_cache_and_force_refresh_bypasses(self):
        # 4시간봉(unit=240) 등 긴 주기의 캔들은 캐시되어 429를 방지한다.
        self.api._valid_markets_cache = {"KRW-BTC"}
        fake_candles = [{"market": "KRW-BTC", "trade_price": 50000000.0}]
        with patch.object(self.api, "_request") as mock_request:
            mock_request.return_value = list(fake_candles)

            # 첫 호출 -> _request 실행
            c1 = self.api.get_candles(unit=240, count=25, market="KRW-BTC")
            self.assertEqual(len(c1), 1)
            self.assertEqual(mock_request.call_count, 1)

            # 동일 인자 연속 호출 -> 캐시 재사용 (호출 횟수 증가 없음)
            c2 = self.api.get_candles(unit=240, count=25, market="KRW-BTC")
            self.assertEqual(len(c2), 1)
            self.assertEqual(mock_request.call_count, 1)

            # force_refresh=True 호출 -> 캐시 우회 및 갱신
            c3 = self.api.get_candles(unit=240, count=25, market="KRW-BTC", force_refresh=True)
            self.assertEqual(len(c3), 1)
            self.assertEqual(mock_request.call_count, 2)

    def test_long_candle_cache_is_shared_between_instances(self):
        # 사이클과 실시간 리스크가 별도 클라이언트를 만들어도 60분봉은 한 번만 조회해야 한다.
        second_api = UpbitAPI(self.access_key, self.secret_key)
        self.api._valid_markets_cache = {"KRW-BTC"}
        second_api._valid_markets_cache = {"KRW-BTC"}
        fake_candles = [{"market": "KRW-BTC", "trade_price": 50000000.0}]

        with patch.object(self.api, "_request", return_value=list(fake_candles)) as first_request:
            with patch.object(second_api, "_request") as second_request:
                self.assertEqual(
                    self.api.get_candles(unit=60, count=50, market="KRW-BTC"), fake_candles,
                )
                self.assertEqual(
                    second_api.get_candles(unit=60, count=50, market="KRW-BTC"), fake_candles,
                )

        self.assertEqual(first_request.call_count, 1)
        self.assertEqual(second_request.call_count, 0)
        self.assertGreaterEqual(UpbitAPI.get_shared_runtime_metrics()["candle_cache_hits"], 1)

    def test_concurrent_long_candle_misses_are_coalesced(self):
        # 동시에 시작한 60분봉 조회는 선행 요청 하나만 REST로 나가야 한다.
        second_api = UpbitAPI(self.access_key, self.secret_key)
        self.api._valid_markets_cache = {"KRW-BTC"}
        second_api._valid_markets_cache = {"KRW-BTC"}
        first_request_started = threading.Event()
        allow_first_request = threading.Event()
        fake_candles = [{"market": "KRW-BTC", "trade_price": 50000000.0}]
        first_result: list[list[dict[str, float | str]]] = []

        def first_request(*args, **kwargs):
            first_request_started.set()
            self.assertTrue(allow_first_request.wait(timeout=1.0))
            return list(fake_candles)

        def fetch_first() -> None:
            first_result.append(self.api.get_candles(unit=60, count=50, market="KRW-BTC"))

        with patch.object(self.api, "_request", side_effect=first_request) as first_mock:
            with patch.object(second_api, "_request") as second_mock:
                worker = threading.Thread(target=fetch_first)
                worker.start()
                self.assertTrue(first_request_started.wait(timeout=1.0))
                # 두 번째 호출은 선행 요청의 Event를 기다린 뒤 같은 캐시 값을 받아야 한다.
                release_timer = threading.Timer(0.02, allow_first_request.set)
                release_timer.start()
                second_result = second_api.get_candles(unit=60, count=50, market="KRW-BTC")
                worker.join(timeout=1.0)
                release_timer.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(first_result, [fake_candles])
        self.assertEqual(second_result, fake_candles)
        self.assertEqual(first_mock.call_count, 1)
        self.assertEqual(second_mock.call_count, 0)
        self.assertGreaterEqual(UpbitAPI.get_shared_runtime_metrics()["candle_coalesced_waits"], 1)


    @patch("requests.Session.get")
    def test_get_balances_normalization(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"currency": "KRW", "balance": "1500000.0", "locked": "50000.0", "avg_buy_price": "0"},
            {"currency": "BTC", "balance": "0.05", "locked": "0.0", "avg_buy_price": "90000000"},
        ]
        mock_get.return_value = mock_response

        balances = self.api.get_balances()
        self.assertIn("KRW", balances)
        self.assertEqual(balances["KRW"]["balance"], 1500000.0)
        self.assertEqual(balances["KRW"]["locked"], 50000.0)
        self.assertIn("BTC", balances)
        self.assertEqual(balances["BTC"]["balance"], 0.05)
        self.assertEqual(balances["BTC"]["avg_buy_price"], 90000000.0)

    @patch("requests.Session.post")
    def test_create_order_limit_with_identifier(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "uuid": "upbit-order-uuid-1",
            "side": "bid",
            "ord_type": "limit",
            "price": "100000000",
            "volume": "0.01",
            "identifier": "bot-order-12345",
        }
        mock_post.return_value = mock_response

        res = self.api.create_order(
            market="KRW-BTC",
            side="bid",
            volume=0.01,
            price=100000000.0,
            ord_type="limit",
            client_order_id="bot-order-12345",
        )
        self.assertEqual(res["uuid"], "upbit-order-uuid-1")
        self.assertEqual(res["order_id"], "upbit-order-uuid-1")

        # Verify POST payload sent to session.post
        call_kwargs = mock_post.call_args.kwargs
        data_sent = call_kwargs["json"]
        self.assertEqual(data_sent["identifier"], "bot-order-12345")
        self.assertEqual(data_sent["market"], "KRW-BTC")
        self.assertEqual(data_sent["side"], "bid")
        self.assertEqual(data_sent["ord_type"], "limit")

    def test_create_order_holo_is_rejected_immediately(self):
        with self.assertRaises(ValueError) as ctx:
            self.api.create_order(
                market="KRW-HOLO",
                side="bid",
                price=10000.0,
                ord_type="price",
            )
        self.assertIn("KRW-HOLO는 수동 매매 보호 종목", str(ctx.exception))

    @patch("requests.Session.delete")
    def test_cancel_order_with_identifier(self, mock_delete):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"uuid": "upbit-order-uuid-1", "state": "cancel"}
        mock_delete.return_value = mock_response

        res = self.api.cancel_order(client_order_id="bot-order-12345")
        self.assertEqual(res["state"], "cancel")
        call_kwargs = mock_delete.call_args.kwargs
        self.assertEqual(call_kwargs["params"]["identifier"], "bot-order-12345")


if __name__ == "__main__":
    unittest.main()
