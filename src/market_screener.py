import logging
import math
import time
from typing import Any

import requests

from bithumb_api import BithumbAPI
from market_policy import get_excluded_markets
from strategy_engine import StrategyPolicy

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


def get_excluded_manual_holdings() -> set[str]:
    return get_excluded_markets()


class MarketScreener:
    """
    빗썸/업비트 실시간 거래대금 상위 + 급등 모멘텀 + BTC 대비 상대 강도(RS) 동적 탐색기 (Screener)
    - KRW 마켓 전체를 실시간 스캔하여 거래대금과 상승률이 우수한 유망 단타 종목 자동 선별
    - RISK_OFF 약세장 시 비트코인 대비 초과 상승(RS >= +1.5%) 및 거래대금(30억↑) 검증 종목만 엄선
    - 기보유 중인 코인은 매도/손절 완료 전까지 최우선으로 목록에 유지
    - USDT, USDC 등 무변동 스테이블코인은 자금 잠김 방지를 위해 원천 배제
    """

    def __init__(
        self,
        bithumb_api: BithumbAPI,
        min_trade_value_krw: float = 1_000_000_000.0,
        min_change_rate: float = 0.005,
        max_change_rate: float = 0.25,
        max_spread_pct: float = 0.0035,
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
        btc_regime: str = "NORMAL",
    ) -> list[dict[str, Any]]:
        held_set: set[str] = {m.upper() for m in (held_markets or [])}
        is_risk_off = (btc_regime or "NORMAL").upper() == "RISK_OFF"

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
            held_candidates: list[dict[str, Any]] = []
            excluded_manual = get_excluded_manual_holdings()

            min_trade_val = (
                StrategyPolicy.MIN_TRADE_VALUE_RISK_OFF
                if is_risk_off
                else self.min_trade_value_krw
            )

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

                if trade_price < StrategyPolicy.MIN_ASSET_PRICE_KRW:
                    continue

                if acc_price_24h < min_trade_val:
                    continue

                if not (self.min_change_rate <= change_rate <= self.max_change_rate):
                    continue

                if is_risk_off and relative_strength < StrategyPolicy.RS_MIN_RISK_OFF:
                    continue

                if 0.02 <= change_rate <= 0.08:
                    momentum_multiplier = 1.5
                elif change_rate < 0.02:
                    momentum_multiplier = 1.0
                else:
                    momentum_multiplier = 1.1

                rs_bonus = max(0.0, relative_strength * 50.0)
                score = ((change_rate * 100.0) * momentum_multiplier * math.log10(max(1.0, acc_price_24h))) + rs_bonus
                ticker_info["score"] = score
                ticker_info["is_held"] = False
                qualified_candidates.append(ticker_info)

            qualified_candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)

            top_candidates = qualified_candidates[: max(top_count * 4, 20)]
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
                    if len(screened_by_spread) >= top_count * 2:
                        break
                except Exception:
                    continue

            qualified_candidates = screened_by_spread

            if not is_risk_off and len(qualified_candidates) < top_count:
                min_fb_trade_val = min(self.min_trade_value_krw, 1_000_000_000.0)
                fallback_tickers = [
                    t for t in all_tickers
                    if t.get("market", "") not in held_set
                    and t.get("market", "") not in EXCLUDED_STABLE_MARKETS
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

            logger.info("========== [실시간 핫스팟 코인 스크리닝 결과] ==========")
            for rank, item in enumerate(final_selection, 1):
                held_tag = " [🔒보유중 포지션]" if item.get("is_held") else " [🔥신규 급등 포착]"
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
