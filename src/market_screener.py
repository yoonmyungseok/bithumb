import logging
import math
import time
from typing import Any

import requests

from bithumb_api import BithumbAPI
from market_policy import get_excluded_markets
from strategy_engine import StrategyPolicy, is_night_session

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

# 대형 메이저 코인은 단타 급등주 발굴 풀에서 제외하고 시장 레짐 및 상대강도(RS) 지표로 전담 활용
EXCLUDED_MAJOR_SCALPING_MARKETS: set[str] = {
    "KRW-BTC",
    "KRW-ETH",
    "KRW-SOL",
    "KRW-XRP",
}



def get_excluded_manual_holdings() -> set[str]:
    return get_excluded_markets()


class MarketScreener:
    """
    빗썸/업비트 실시간 거래대금 상위 + 급등 모멘텀 + BTC 대비 상대 강도(RS) 동적 탐색기 (Screener)
    - KRW 마켓 전체를 실시간 스캔하여 거래대금과 상승률이 우수한 유망 단타 종목 자동 선별
    - RISK_OFF 약세장 시 BTC 대비 초과 상승(RS >= +0.8%)과 거래대금을 확인해 공격형 후보를 선별
    - 기보유 중인 코인은 매도/손절 완료 전까지 최우선으로 목록에 유지
    - USDT, USDC 등 무변동 스테이블코인은 자금 잠김 방지를 위해 원천 배제
    """

    def __init__(
        self,
        bithumb_api: BithumbAPI,
        min_trade_value_krw: float = 1_000_000_000.0,
        min_change_rate: float = 0.005,
        max_change_rate: float = 0.12,
        max_spread_pct: float = 0.0035,
        enable_early_breakout: bool = False,
        early_breakout_min_change_rate: float = 0.003,
        early_breakout_max_candidates: int = 2,
    ):
        self.api = bithumb_api
        self.min_trade_value_krw = min_trade_value_krw
        self.min_change_rate = min_change_rate
        self.max_change_rate = max_change_rate
        self.max_spread_pct = max_spread_pct
        # 모멘텀 돌파는 확인형 후보와 분리해 소수 슬롯에서만 평가한다.
        self.enable_early_breakout = enable_early_breakout
        self.early_breakout_min_change_rate = max(0.0, early_breakout_min_change_rate)
        self.early_breakout_max_candidates = max(0, early_breakout_max_candidates)

    def scan_markets(
        self,
        top_count: int = 3,
        held_markets: list[str] | None = None,
        btc_regime: str = "NORMAL",
        analyzer: Any | None = None,
    ) -> list[dict[str, Any]]:
        held_set: set[str] = {m.upper() for m in (held_markets or [])}
        is_risk_off = (btc_regime or "NORMAL").upper() == "RISK_OFF"
        is_bull_trend = (btc_regime or "NORMAL").upper() == "BULL_TREND"

        try:
            try:
                all_markets_data = self.api.get_all_markets(is_details=True)
            except TypeError:
                all_markets_data = self.api.get_all_markets()

            krw_markets = []
            for m in all_markets_data:
                m_code = m.get("market", "")
                if not m_code.startswith("KRW-"):
                    continue
                m_event = m.get("market_event") or {}
                if isinstance(m_event, dict) and m_event.get("warning") is True:
                    continue
                if m.get("market_warning") in ("CAUTION", "WARNING"):
                    continue
                krw_markets.append(m_code)

            if not krw_markets:
                return [{"market": m, "reason": "기본 마켓"} for m in (held_markets or ["KRW-BTC"])]

            chunk_size = 50
            all_tickers: list[dict[str, Any]] = []
            for i in range(0, len(krw_markets), chunk_size):
                chunk = krw_markets[i : i + chunk_size]
                tickers_chunk = self.api.get_tickers(chunk)
                all_tickers.extend(tickers_chunk)

            ex_name = "업비트" if "upbit" in str(type(self.api)).lower() else "빗썸"
            logger.info(f"{ex_name} KRW 마켓 {len(all_tickers)}개 종목 시세 스캔 완료 (레짐: {btc_regime})")

            btc_ticker = next((t for t in all_tickers if t.get("market") == "KRW-BTC"), None)
            btc_change_rate = _safe_float(btc_ticker.get("signed_change_rate", btc_ticker.get("change_rate", 0.0))) if btc_ticker else 0.0

            qualified_candidates: list[dict[str, Any]] = []
            early_breakout_candidates: list[dict[str, Any]] = []
            held_candidates: list[dict[str, Any]] = []
            excluded_manual = get_excluded_manual_holdings()

            min_trade_val = (
                StrategyPolicy.MIN_TRADE_VALUE_RISK_OFF
                if is_risk_off
                else self.min_trade_value_krw
            )
            if is_night_session():
                min_trade_val = min_trade_val * StrategyPolicy.NIGHT_TRADE_VALUE_MULTIPLIER

            for t in all_tickers:
                market = t.get("market", "")
                trade_price = _safe_float(t.get("trade_price"))
                change_rate = _safe_float(t.get("signed_change_rate", t.get("change_rate")))
                acc_price_24h = _safe_float(t.get("acc_trade_price_24h", t.get("acc_trade_value_24h", 0.0)))
                relative_strength = change_rate - btc_change_rate

                if not market or trade_price <= 0:
                    continue

                if market in EXCLUDED_STABLE_MARKETS or market in excluded_manual or market.replace("KRW-", "") in excluded_manual:
                    continue

                ticker_info = {
                    "market": market,
                    "trade_price": trade_price,
                    "change_rate": change_rate,
                    "acc_trade_price_24h": acc_price_24h,
                    "relative_strength": relative_strength,
                }

                if market in held_set:
                    ticker_info["is_held"] = True
                    held_candidates.append(ticker_info)
                    continue

                # 대형 메이저 코인(BTC/ETH/SOL/XRP) 처리:
                # - BULL_TREND(대세 상승장)일 때는 시장 주도 대형 메이저(BTC/ETH/SOL)를 단타 풀에 허용하여 추세 랠리 직접 참여
                # - 그 외 레짐에서는 자금 잠김 방지를 위해 단타 급등주 발굴 풀에서 배제
                if market in EXCLUDED_MAJOR_SCALPING_MARKETS:
                    if not (is_bull_trend and market in ("KRW-BTC", "KRW-ETH", "KRW-SOL")):
                        continue


                if trade_price < StrategyPolicy.MIN_ASSET_PRICE_KRW:
                    continue

                if acc_price_24h < min_trade_val:
                    continue

                if is_risk_off and relative_strength < StrategyPolicy.RS_MIN_RISK_OFF:
                    continue

                # 확인형 후보는 기존 상승률 조건을 그대로 사용한다.
                if self.min_change_rate <= change_rate <= self.max_change_rate:
                    # [모멘텀 주도주 돌파] 모멘텀 돌파가 활성화되어 있고, 당일 상승 탄력이 강하면서(+3% 이상)
                    # 비트코인 대비 독자 랠리(상대강도 RS >= +1.5%)를 펼치는 주도주는
                    # 눌림목 잣대(%B/저점거리)로 거르지 않고 고점 돌파 로직(MOMENTUM_BREAKOUT)으로 진입하도록 분류한다.
                    is_momentum_leader = (
                        self.enable_early_breakout
                        and change_rate >= 0.030
                        and relative_strength >= 0.015
                    )

                    # 상승 초입(+1.5% ~ +6.0%) 종목에 최고 가중치를 부여하고, 이미 많이 오른(+8% 초과) 종목은 감점
                    if 0.015 <= change_rate <= 0.060:
                        momentum_multiplier = 2.0   # 상승 초입 골든존 최고 가중치
                    elif 0.005 <= change_rate < 0.015:
                        momentum_multiplier = 1.3   # 바닥 탈출 초기 구간
                    elif 0.060 < change_rate <= 0.090:
                        momentum_multiplier = 1.0   # 진행 중인 상승세
                    else:
                        momentum_multiplier = 0.5   # +9% 이상 급등 과열 종목 (고점 피로도 감점)

                    rs_bonus = max(0.0, relative_strength * 60.0)
                    # 거래대금의 로그 스케일과 초입 모멘텀 가중치를 결합
                    effective_rate = min(change_rate, 0.08)  # 지나치게 높은 상승률이 점수를 과도하게 왜곡하지 않도록 상한 8% 캡 적용
                    score = ((effective_rate * 100.0) * momentum_multiplier * math.log10(max(1.0, acc_price_24h))) + rs_bonus
                    ticker_info["score"] = score
                    ticker_info["candidate_type"] = "MOMENTUM_BREAKOUT" if is_momentum_leader else "CONFIRMED"
                    ticker_info["is_held"] = False
                    qualified_candidates.append(ticker_info)
                    continue

                # 초기 돌파 후보는 당일 상승률이 확인형 기준에 도달하기 전 구간만 별도로 감시한다.
                if (
                    self.enable_early_breakout
                    and self.early_breakout_min_change_rate <= change_rate < self.min_change_rate
                    and relative_strength >= StrategyPolicy.MOMENTUM_BREAKOUT_RS_MIN
                ):
                    # 상대강도와 거래대금은 이미 위에서 검증했으므로, 초입 변동과 유동성을 함께 점수화한다.
                    early_score = (change_rate * 100.0 * math.log10(max(1.0, acc_price_24h))) + max(0.0, relative_strength * 80.0)
                    ticker_info["score"] = early_score
                    ticker_info["candidate_type"] = "MOMENTUM_BREAKOUT"
                    ticker_info["is_held"] = False
                    early_breakout_candidates.append(ticker_info)

            qualified_candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)
            early_breakout_candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)

            top_candidates = qualified_candidates[: max(top_count * 4, 20)]
            early_scan_limit = max(4, self.early_breakout_max_candidates * 3)
            top_candidates.extend(early_breakout_candidates[:early_scan_limit])
            markets_to_check = [c["market"] for c in top_candidates if "market" in c]
            ob_map: dict[str, dict[str, Any]] = {}

            if hasattr(self.api, "get_orderbooks") and markets_to_check:
                try:
                    raw_obs = self.api.get_orderbooks(markets_to_check)
                    for ob in (raw_obs or []):
                        if isinstance(ob, dict) and "market" in ob:
                            ob_map[ob["market"]] = ob
                except Exception as exc:
                    logger.debug("호가창 일괄 조회 폴백: %s", exc)

            screened_by_spread: list[dict[str, Any]] = []
            for cand in top_candidates:
                m = cand["market"]
                try:
                    ob = ob_map.get(m)
                    if not ob:
                        time.sleep(0.05)
                        ob = self.api.get_orderbook(m)
                    units = ob.get("orderbook_units", []) if ob else []
                    if not units:
                        continue

                    top_ask = float(units[0].get("ask_price", 0.0))
                    top_bid = float(units[0].get("bid_price", 0.0))
                    if top_bid <= 0 or top_ask <= 0:
                        continue

                    spread = (top_ask - top_bid) / top_bid
                    if spread > self.max_spread_pct:
                        continue

                    top5_bid_krw = sum(float(u.get("bid_price", 0.0)) * float(u.get("bid_size", 0.0)) for u in units[:5])
                    if top5_bid_krw < 20_000_000.0:
                        continue

                    screened_by_spread.append(cand)
                    # 확인형과 초기 돌파형을 모두 검사해 한 유형이 다른 유형의 검사를 막지 않게 한다.
                    if len(screened_by_spread) >= (top_count * 2 + early_scan_limit):
                        break
                except Exception:
                    continue

            # 초기 돌파(변동률 < min_change_rate)는 별도 소수 슬롯으로 격리 관리하고,
            # 주도주 모멘텀(변동률 >= min_change_rate)은 일반 확인형과 함께 종합 순위 풀에서 경쟁한다.
            screened_early_breakouts = [
                c for c in screened_by_spread
                if c.get("candidate_type") == "MOMENTUM_BREAKOUT" and c.get("change_rate", 0.0) < self.min_change_rate
            ]
            qualified_candidates = [c for c in screened_by_spread if c not in screened_early_breakouts]

            # [2순위] 1차 스프레드 통과 후보들에 대해 Gemini AI 1회 배치 랭킹 적용
            active_analyzer = analyzer or getattr(self, "analyzer", None)
            if active_analyzer is not None and hasattr(active_analyzer, "rank_candidate_markets"):
                candidates_to_rank = [c for c in qualified_candidates if not c.get("is_held")]
                if len(candidates_to_rank) >= 2:
                    try:
                        ranked = active_analyzer.rank_candidate_markets(
                            candidates_to_rank,
                            btc_regime=btc_regime,
                            btc_change_rate=btc_change_rate,
                        )
                        if ranked:
                            qualified_candidates = ranked
                    except Exception as exc:
                        logger.debug("AI 스크리너 랭킹 예외 폴백: %s", exc)

            if not is_risk_off and len(qualified_candidates) < top_count:
                min_fb_trade_val = min(self.min_trade_value_krw, 1_000_000_000.0)
                fallback_tickers = [
                    t for t in all_tickers
                    if t.get("market", "") not in held_set
                    and t.get("market", "") not in EXCLUDED_STABLE_MARKETS
                    and t.get("market", "") not in EXCLUDED_MAJOR_SCALPING_MARKETS
                    and t.get("market", "") not in excluded_manual
                    and t.get("market", "").replace("KRW-", "") not in excluded_manual
                    and _safe_float(t.get("trade_price")) >= StrategyPolicy.MIN_ASSET_PRICE_KRW
                    and _safe_float(t.get("acc_trade_price_24h", t.get("acc_trade_value_24h", 0.0))) >= min_fb_trade_val
                ]

                fallback_tickers.sort(
                    key=lambda x: _safe_float(x.get("acc_trade_price_24h", x.get("acc_trade_value_24h", 0.0))), reverse=True
                )
                fb_candidates_to_fetch = [
                    ft.get("market", "") for ft in fallback_tickers[:10]
                    if ft.get("market", "") and not any(c["market"] == ft.get("market", "") for c in qualified_candidates)
                ]
                fb_ob_map: dict[str, dict[str, Any]] = {}
                if hasattr(self.api, "get_orderbooks") and fb_candidates_to_fetch:
                    try:
                        raw_fb_obs = self.api.get_orderbooks(fb_candidates_to_fetch)
                        for ob in (raw_fb_obs or []):
                            if isinstance(ob, dict) and "market" in ob:
                                fb_ob_map[ob["market"]] = ob
                    except Exception:
                        pass

                for ft in fallback_tickers:
                    if len(qualified_candidates) >= top_count:
                        break
                    m = ft.get("market", "")
                    if any(c["market"] == m for c in qualified_candidates):
                        continue

                    try:
                        ob = fb_ob_map.get(m)
                        if not ob:
                            time.sleep(0.05)
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
                        "relative_strength": _safe_float(ft.get("signed_change_rate", ft.get("change_rate"))) - btc_change_rate,
                        "candidate_type": "CONFIRMED_FALLBACK",
                        "is_held": False,
                    })

            final_selection: list[dict[str, Any]] = []
            selected_markets_set: set[str] = set()

            for hc in held_candidates:
                final_selection.append(hc)
                selected_markets_set.add(hc["market"])

            for qc in qualified_candidates:
                if len(final_selection) >= (len(held_candidates) + top_count):
                    break
                if qc["market"] not in selected_markets_set:
                    final_selection.append(qc)
                    selected_markets_set.add(qc["market"])

            # 초기 돌파형은 확인형 후보와 별도의 소수 슬롯만 사용한다.
            for ec in screened_early_breakouts[:self.early_breakout_max_candidates]:
                if ec["market"] not in selected_markets_set:
                    final_selection.append(ec)
                    selected_markets_set.add(ec["market"])

            logger.info("========== [실시간 핫스팟 코인 스크리닝 결과] ==========")
            for rank, item in enumerate(final_selection, 1):
                if item.get("is_held"):
                    held_tag = " [🔒보유중 포지션]"
                elif item.get("candidate_type") == "MOMENTUM_BREAKOUT":
                    held_tag = " [⚡모멘텀 돌파 후보]"
                else:
                    held_tag = " [🔥신규 급등 포착]"
                trade_b_krw = item.get("acc_trade_price_24h", 0.0) / 100_000_000.0
                rs_info = f" | RS: {item.get('relative_strength', 0.0)*100:+.2f}%" if "relative_strength" in item else ""
                logger.info(
                    f"#{rank} {item['market']}{held_tag} | 현재가: {item['trade_price']:,.2f}원 | 24h변동: {item['change_rate']*100:+.2f}%{rs_info} | 24h거래대금: {trade_b_krw:,.0f}억 원"
                )

            return final_selection

        except (requests.exceptions.RequestException, KeyError, ValueError, IndexError) as e:
            logger.error(f"마켓 스크리닝 중 오류 발생: {e}")
            fallback = [{"market": m} for m in (held_markets or ["KRW-BTC"])]
            return fallback
