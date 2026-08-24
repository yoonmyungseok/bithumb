import logging
import math
from typing import Any

import requests

from bithumb_api import BithumbAPI

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Exchange payloads can contain null/empty/non-finite numeric fields."""
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


EXCLUDED_STABLE_MARKETS: set[str] = {
    "KRW-USDT",
    "KRW-USDC",
    "KRW-DAI",
    "KRW-TUSD",
    "KRW-FDUSD",
    "KRW-USDE",
}


class MarketScreener:
    """
    빗썸 실시간 거래대금 상위 + 급등 모멘텀 종목 동적 탐색기 (Screener)
    - KRW 마켓 전체를 실시간 스캔하여 거래대금과 상승률이 우수한 유망 단타 종목 자동 선별
    - 기보유 중인 코인은 매도/손절 완료 전까지 최우선으로 목록에 유지
    - USDT, USDC 등 무변동 스테이블코인은 자금 잠김 방지를 위해 원천 배제
    """

    def __init__(
        self,
        bithumb_api: BithumbAPI,
        min_trade_value_krw: float = 1_000_000_000.0,  # 최소 24시간 거래대금 10억 원
        min_change_rate: float = 0.005,                # 최소 당일 상승률 +0.5% (완만한 우상향 메이저 포함)
        max_change_rate: float = 0.25,                 # 최대 당일 상승률 +25.0% (급등 모멘텀 포함)
        max_spread_pct: float = 0.005,                 # 최대 호가 스프레드 0.50% (유동성 확보)
    ):
        self.api = bithumb_api
        self.min_trade_value_krw = min_trade_value_krw
        self.min_change_rate = min_change_rate
        self.max_change_rate = max_change_rate
        self.max_spread_pct = max_spread_pct

    def scan_markets(
        self,
        top_count: int = 3,
        held_markets: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        거래대금 및 모멘텀 기반 상위 종목 선별
        - held_markets: 현재 계좌에 보유 중인 마켓 리스트 (최우선 포함)
        """
        held_set: set[str] = {m.upper() for m in (held_markets or [])}

        try:
            # 1. 전체 마켓 목록 조회 (KRW 페어만 필터링)
            all_markets_data = self.api.get_all_markets()
            krw_markets = [
                m["market"]
                for m in all_markets_data
                if m.get("market", "").startswith("KRW-")
            ]

            if not krw_markets:
                logger.warning("KRW 마켓 목록을 가져오지 못했습니다. 기본값 반환")
                return [{"market": m, "reason": "기본 마켓"} for m in (held_markets or ["KRW-BTC"])]

            # 2. 전체 KRW 마켓의 시세/거래대금 일괄 조회 (청크 분할)
            chunk_size = 50
            all_tickers: list[dict[str, Any]] = []
            for i in range(0, len(krw_markets), chunk_size):
                chunk = krw_markets[i : i + chunk_size]
                tickers_chunk = self.api.get_tickers(chunk)
                all_tickers.extend(tickers_chunk)

            logger.info(f"빗썸 KRW 마켓 {len(all_tickers)}개 종목 시세 스캔 완료")

            # 3. 퀀트 필터링 및 상승 초기 가중치 모멘텀 스코어링
            qualified_candidates: list[dict[str, Any]] = []
            held_candidates: list[dict[str, Any]] = []

            for t in all_tickers:
                market = t.get("market", "")
                trade_price = _safe_float(t.get("trade_price"))
                change_rate = _safe_float(t.get("signed_change_rate", t.get("change_rate")))
                acc_price_24h = _safe_float(t.get("acc_trade_price_24h", t.get("acc_trade_value_24h", 0.0)))

                if not market or trade_price <= 0:
                    logger.debug("유효하지 않은 티커 레코드 제외: market=%r trade_price=%r", market, t.get("trade_price"))
                    continue

                if market in EXCLUDED_STABLE_MARKETS:
                    continue

                ticker_info = {
                    "market": market,
                    "trade_price": trade_price,
                    "change_rate": change_rate,
                    "acc_trade_price_24h": acc_price_24h,
                }

                # 기보유 코인은 필터 조건과 상관없이 무조건 유지
                if market in held_set:
                    ticker_info["is_held"] = True
                    held_candidates.append(ticker_info)
                    continue

                # 거래대금 필터 (최소 기준 또는 기본 유동성)
                if acc_price_24h < self.min_trade_value_krw:
                    continue

                # 상승률 필터 (적정 모멘텀 구간: +1.5% ~ +15.0%)
                if not (self.min_change_rate <= change_rate <= self.max_change_rate):
                    continue

                # 상승 초기(+2% ~ +8%) 가중치: 초고점 물림을 방지하고 상승 시동 거는 종목 우대
                if 0.02 <= change_rate <= 0.08:
                    momentum_multiplier = 1.5
                elif change_rate < 0.02:
                    momentum_multiplier = 1.0
                else:
                    # +8% ~ +15% 구간은 상승 피로감 고려하여 기본 가중치
                    momentum_multiplier = 1.1

                # 모멘텀 스코어: (상승률 %) * 가중치 * log10(거래대금)
                score = (change_rate * 100.0) * momentum_multiplier * math.log10(max(1.0, acc_price_24h))
                ticker_info["score"] = score
                ticker_info["is_held"] = False
                qualified_candidates.append(ticker_info)

            # 모멘텀 스코어 높은 순으로 정렬
            qualified_candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)

            # 유동성 스프레드 및 호가 깊이 필터 적용 (상위 후보군 대상, Fail-Closed 정책)
            screened_by_spread: list[dict[str, Any]] = []
            for cand in qualified_candidates[: top_count * 2]:
                m = cand["market"]
                try:
                    ob = self.api.get_orderbook(m)
                    units = ob.get("orderbook_units", []) if ob else []
                    if not units:
                        logger.warning("호가창 데이터 누락으로 제외 (Fail-Closed): %s", m)
                        continue

                    top_ask = float(units[0].get("ask_price", 0.0))
                    top_bid = float(units[0].get("bid_price", 0.0))
                    if top_bid <= 0 or top_ask <= 0:
                        logger.warning("비정상 호가 가격으로 제외: %s", m)
                        continue

                    spread = (top_ask - top_bid) / top_bid
                    if spread > self.max_spread_pct:
                        logger.debug("호가 스프레드 과다로 제외: %s (spread=%.3f%% > %.3f%%)", m, spread * 100, self.max_spread_pct * 100)
                        continue

                    # 상위 5호가 매수 잔량 깊이(Depth) 검증 (최소 2천만 원)
                    top5_bid_krw = sum(float(u.get("bid_price", 0.0)) * float(u.get("bid_size", 0.0)) for u in units[:5])
                    if top5_bid_krw < 20_000_000.0:
                        logger.debug("상위 5호가 매수 잔량 부족으로 제외: %s (잔량=%.0f만 원 < 2000만 원)", m, top5_bid_krw / 10_000)
                        continue

                    screened_by_spread.append(cand)
                except Exception as exc:
                    logger.warning("호가 검증 오류로 후보 제외 (Fail-Closed): %s (%s)", m, exc)
                    continue

            qualified_candidates = screened_by_spread

            # 만약 조건에 맞는 급등주가 부족하면, 단순히 거래대금 상위 종목 중 스프레드 검증된 종목으로 채움
            if len(qualified_candidates) < top_count:
                fallback_tickers = [
                    t for t in all_tickers
                    if t.get("market", "") not in held_set
                    and t.get("market", "") not in EXCLUDED_STABLE_MARKETS
                    and _safe_float(t.get("trade_price")) > 0
                    and _safe_float(t.get("acc_trade_price_24h", t.get("acc_trade_value_24h", 0.0))) >= 1_000_000_000.0
                ]
                fallback_tickers.sort(
                    key=lambda x: _safe_float(x.get("acc_trade_price_24h", x.get("acc_trade_value_24h", 0.0))), reverse=True
                )
                for ft in fallback_tickers:
                    if len(qualified_candidates) >= top_count:
                        break
                    m = ft.get("market", "")
                    if any(c["market"] == m for c in qualified_candidates):
                        continue

                    # Fallback 종목도 호가 스프레드/깊이 검증 (Fail-Closed)
                    try:
                        ob = self.api.get_orderbook(m)
                        units = ob.get("orderbook_units", []) if ob else []
                        if not units:
                            continue
                        top_ask = float(units[0].get("ask_price", 0.0))
                        top_bid = float(units[0].get("bid_price", 0.0))
                        if top_bid <= 0 or top_ask <= 0:
                            continue
                        if (top_ask - top_bid) / top_bid > self.max_spread_pct:
                            continue
                        top5_bid_krw = sum(float(u.get("bid_price", 0.0)) * float(u.get("bid_size", 0.0)) for u in units[:5])
                        if top5_bid_krw < 20_000_000.0:
                            continue
                    except Exception:
                        continue

                    qualified_candidates.append({
                        "market": m,
                        "trade_price": _safe_float(ft.get("trade_price")),
                        "change_rate": _safe_float(ft.get("signed_change_rate", ft.get("change_rate"))),
                        "acc_trade_price_24h": _safe_float(ft.get("acc_trade_price_24h", ft.get("acc_trade_value_24h", 0.0))),
                        "is_held": False,
                    })

            # 4. 기보유 코인 + 스크리닝 상위 코인 조합
            final_selection: list[dict[str, Any]] = []
            selected_markets_set: set[str] = set()

            # 1순위: 기보유 코인 (목표가/손절가 관리 지속)
            for hc in held_candidates:
                final_selection.append(hc)
                selected_markets_set.add(hc["market"])

            # 2순위: 신규 급등주 추가 (남은 슬롯만큼)
            for qc in qualified_candidates:
                if len(final_selection) >= (len(held_candidates) + top_count):
                    break
                if qc["market"] not in selected_markets_set:
                    final_selection.append(qc)
                    selected_markets_set.add(qc["market"])

            # 로깅
            logger.info("========== [실시간 핫스팟 코인 스크리닝 결과] ==========")
            for rank, item in enumerate(final_selection, 1):
                held_tag = " [🔒보유중 포지션]" if item.get("is_held") else " [🔥신규 급등 포착]"
                trade_b_krw = item.get("acc_trade_price_24h", 0.0) / 100_000_000.0
                logger.info(
                    f"#{rank} {item['market']}{held_tag} | 현재가: {item['trade_price']:,.2f}원 | 24h변동: {item['change_rate']*100:+.2f}% | 24h거래대금: {trade_b_krw:,.0f}억 원"
                )

            return final_selection

        except (requests.exceptions.RequestException, KeyError, ValueError, IndexError) as e:
            logger.error(f"마켓 스크리닝 중 오류 발생: {e}")
            fallback = [{"market": m} for m in (held_markets or ["KRW-BTC"])]
            return fallback
