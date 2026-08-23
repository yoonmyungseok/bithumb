import logging
import math
from typing import Any, Dict, List, Optional, Set

from bithumb_api import BithumbAPI

logger = logging.getLogger(__name__)


class MarketScreener:
    """
    빗썸 실시간 거래대금 상위 + 급등 모멘텀 종목 동적 탐색기 (Screener)
    - KRW 마켓 전체를 실시간 스캔하여 거래대금과 상승률이 우수한 유망 단타 종목 자동 선별
    - 기보유 중인 코인은 매도/손절 완료 전까지 최우선으로 목록에 유지
    """

    def __init__(
        self,
        bithumb_api: BithumbAPI,
        min_trade_value_krw: float = 5_000_000_000.0,  # 최소 24시간 거래대금 50억 원
        min_change_rate: float = 0.01,                 # 최소 당일 상승률 +1.0%
        max_change_rate: float = 0.25,                 # 최대 당일 상승률 +25.0% (초고점 설거지 방지)
    ):
        self.api = bithumb_api
        self.min_trade_value_krw = min_trade_value_krw
        self.min_change_rate = min_change_rate
        self.max_change_rate = max_change_rate

    def scan_markets(
        self,
        top_count: int = 3,
        held_markets: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        거래대금 및 모멘텀 기반 상위 종목 선별
        - held_markets: 현재 계좌에 보유 중인 마켓 리스트 (최우선 포함)
        """
        held_set: Set[str] = set(m.upper() for m in (held_markets or []))

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
            all_tickers: List[Dict[str, Any]] = []
            for i in range(0, len(krw_markets), chunk_size):
                chunk = krw_markets[i : i + chunk_size]
                tickers_chunk = self.api.get_tickers(chunk)
                all_tickers.extend(tickers_chunk)

            logger.info(f"빗썸 KRW 마켓 {len(all_tickers)}개 종목 시세 스캔 완료")

            # 3. 퀀트 필터링 및 모멘텀 스코어링
            qualified_candidates: List[Dict[str, Any]] = []
            held_candidates: List[Dict[str, Any]] = []

            for t in all_tickers:
                market = t.get("market", "")
                trade_price = float(t.get("trade_price", 0.0))
                change_rate = float(t.get("signed_change_rate", t.get("change_rate", 0.0)))
                acc_price_24h = float(t.get("acc_trade_price_24h", 0.0))

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

                # 상승률 필터 (적정 모멘텀 구간: +1% ~ +25%)
                if not (self.min_change_rate <= change_rate <= self.max_change_rate):
                    continue

                # 모멘텀 스코어: (상승률 %) * log10(거래대금)
                score = (change_rate * 100.0) * math.log10(max(1.0, acc_price_24h))
                ticker_info["score"] = score
                ticker_info["is_held"] = False
                qualified_candidates.append(ticker_info)

            # 모멘텀 스코어 높은 순으로 정렬
            qualified_candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)

            # 만약 조건에 맞는 급등주가 부족하면, 단순히 거래대금 상위 종목으로 채움
            if len(qualified_candidates) < top_count:
                fallback_tickers = [
                    t for t in all_tickers
                    if t.get("market", "") not in held_set and float(t.get("acc_trade_price_24h", 0.0)) >= 1_000_000_000.0
                ]
                fallback_tickers.sort(
                    key=lambda x: float(x.get("acc_trade_price_24h", 0.0)), reverse=True
                )
                for ft in fallback_tickers:
                    if len(qualified_candidates) >= top_count:
                        break
                    m = ft.get("market", "")
                    if not any(c["market"] == m for c in qualified_candidates):
                        qualified_candidates.append({
                            "market": m,
                            "trade_price": float(ft.get("trade_price", 0.0)),
                            "change_rate": float(ft.get("signed_change_rate", ft.get("change_rate", 0.0))),
                            "acc_trade_price_24h": float(ft.get("acc_trade_price_24h", 0.0)),
                            "is_held": False,
                        })

            # 4. 기보유 코인 + 스크리닝 상위 코인 조합
            final_selection: List[Dict[str, Any]] = []
            selected_markets_set: Set[str] = set()

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

        except Exception as e:
            logger.error(f"마켓 스크리닝 중 오류 발생: {e}")
            fallback = [{"market": m} for m in (held_markets or ["KRW-BTC"])]
            return fallback
