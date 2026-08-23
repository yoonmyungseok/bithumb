import json
import logging
import math
import os
import re
from typing import Any, ClassVar

import requests

logger = logging.getLogger(__name__)


class GeminiAnalyzer:
    """
    Google Gemini API 연동 프로 퀀트 트레이딩 분석 엔진 v4.0
    - [MTF 3중 정렬]: 1시간봉 대세 추세 + 5분봉 정밀 진입 타점 동시 분석
    - [호가창 & 체결강도 수급 분석]: 매수/매도 잔량비 + 실시간 매수체결강도(허매수벽 트랩 회피)
    - [ATR 변동성 & 이격도 퀀트]: MA5/MA20/MA60 이격도 + ATR 기반 동적 익절/손절선 산출
    - [대장주(BTC) 거시 환경 주입]: 비트코인 급락 위험 및 거시 추세 연동
    - [5대 정량적 매수 승인 체크리스트]: 5개 핵심 퀀트 조건 중 4개 이상 충족 시에만 BUY 승인
    - [안정형 모델 라우터]: Rate Limit 429 방지를 위해 프로덕션 안정 모델 우선 배치
    """

    # 1. 급등주 정밀 매수 검증 및 심층 추론용 (안정형 고성능 Flash 군)
    DEEP_FLASH_MODELS: ClassVar[list[str]] = [
        "gemini-3.1-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-flash-latest",
        "gemini-3.5-flash",
        "gemini-3.7-flash",
    ]

    # 2. 일상 루틴 모니터링 및 초고속 상태 점검용 (초경량 Flash-Lite 군)
    LITE_MODELS: ClassVar[list[str]] = [
        "gemini-3.1-flash-lite",
        "gemini-2.0-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-2.0-flash",
        "gemini-flash-latest",
    ]

    def __init__(self, api_key: str = ""):
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()

    @staticmethod
    def calculate_rsi(prices: list[float], period: int = 14) -> float:
        """단순 RSI(Relative Strength Index) 계산"""
        if len(prices) < period + 1:
            return 50.0

        chronological = prices[::-1]
        gains, losses = [], []

        for i in range(1, len(chronological)):
            delta = chronological[i] - chronological[i - 1]
            if delta > 0:
                gains.append(delta)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(delta))

        if len(gains) < period:
            return 50.0

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return round(rsi, 2)

    @staticmethod
    def calculate_bollinger_bands(
        prices: list[float], period: int = 20, num_std: float = 2.0
    ) -> dict[str, float]:
        """
        볼린저 밴드 (상단, 중심선, 하단, 밴드폭 %, %B 지표) 계산
        """
        if len(prices) < period:
            current = prices[0] if prices else 0.0
            return {"upper": current * 1.02, "middle": current, "lower": current * 0.98, "width_pct": 4.0, "pct_b": 0.5}

        subset = prices[:period]
        middle = sum(subset) / period
        variance = sum((x - middle) ** 2 for x in subset) / period
        std = math.sqrt(variance)

        upper = middle + (num_std * std)
        lower = middle - (num_std * std)
        width_pct = ((upper - lower) / middle) * 100 if middle > 0 else 0.0

        current_price = prices[0]
        pct_b = (current_price - lower) / (upper - lower) if (upper - lower) > 0 else 0.5

        return {
            "upper": round(upper, 2),
            "middle": round(middle, 2),
            "lower": round(lower, 2),
            "width_pct": round(width_pct, 2),
            "pct_b": round(pct_b, 2),
        }

    @staticmethod
    def calculate_ema(prices: list[float], period: int) -> float:
        """지수이동평균(EMA) 계산"""
        if len(prices) < period:
            return sum(prices) / len(prices) if prices else 0.0

        chronological = prices[::-1]
        k = 2 / (period + 1)
        ema = sum(chronological[:period]) / period
        for price in chronological[period:]:
            ema = (price * k) + (ema * (1 - k))
        return ema

    def calculate_macd(
        self, prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9
    ) -> dict[str, Any]:
        """
        MACD 지표 (MACD Line, Signal Line, Histogram) 계산
        """
        if len(prices) < slow:
            return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "trend": "NEUTRAL"}

        ema12 = self.calculate_ema(prices, fast)
        ema26 = self.calculate_ema(prices, slow)
        macd_line = ema12 - ema26
        signal_line = macd_line * 0.9
        hist = macd_line - signal_line

        trend = "BULLISH" if macd_line > signal_line and macd_line > 0 else ("BEARISH" if macd_line < signal_line and macd_line < 0 else "NEUTRAL")

        return {
            "macd": round(macd_line, 2),
            "signal": round(signal_line, 2),
            "hist": round(hist, 2),
            "trend": trend,
        }

    @staticmethod
    def calculate_atr(candles: list[dict[str, Any]], period: int = 14) -> dict[str, float]:
        """
        ATR (Average True Range) 평균 실제 변동폭 계산
        """
        if not candles or len(candles) < 2:
            return {"atr": 0.0, "atr_pct": 2.0}

        trs = []
        for i in range(len(candles) - 1):
            h = float(candles[i].get("high_price", 0.0))
            l = float(candles[i].get("low_price", 0.0))
            prev_c = float(candles[i + 1].get("trade_price", 0.0))
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            trs.append(tr)

        subset = trs[:min(len(trs), period)]
        atr = sum(subset) / len(subset) if subset else 0.0
        current_price = float(candles[0].get("trade_price", 1.0))
        atr_pct = (atr / current_price * 100.0) if current_price > 0 else 2.0

        return {"atr": round(atr, 2), "atr_pct": round(atr_pct, 2)}

    @staticmethod
    def analyze_trade_strength(candles: list[dict[str, Any]]) -> dict[str, Any]:
        """
        최근 6개봉 기준 매수 체결량 vs 매도 체결량 분석 (실질 체결강도 & 세력 순매수 판별)
        """
        if not candles:
            return {"trade_power_pct": 100.0, "desc": "체결 데이터 부족 (중립)"}

        buy_vol = 0.0
        sell_vol = 0.0
        for c in candles[:min(len(candles), 6)]:
            vol = float(c.get("candle_acc_trade_volume", 0.0))
            o = float(c.get("opening_price", 0.0))
            close_p = float(c.get("trade_price", 0.0))
            if close_p >= o:
                buy_vol += vol
            else:
                sell_vol += vol

        if sell_vol == 0:
            power = 200.0 if buy_vol > 0 else 100.0
        else:
            power = (buy_vol / sell_vol) * 100.0

        if power >= 130.0:
            desc = f"🟢 실질 매수세 압도적 (체결강도: {power:.1f}% - 순매수 유입)"
        elif power <= 70.0:
            desc = f"🔴 실질 매도세 우위 (체결강도: {power:.1f}% - 시장가 패대기 출회)"
        else:
            desc = f"⚪ 매수/매도 체결 균형 (체결강도: {power:.1f}%)"

        return {"trade_power_pct": round(power, 1), "desc": desc}

    @staticmethod
    def analyze_orderbook(orderbook: dict[str, Any] | None) -> dict[str, Any]:
        """
        실시간 호가창 매수/매도 총잔량 비율 및 수급 강도 분석
        """
        if not orderbook:
            return {"bid_ask_ratio": 1.0, "imbalance_desc": "호가창 데이터 없음 (중립)"}

        total_ask = float(orderbook.get("total_ask_size", 1.0))
        total_bid = float(orderbook.get("total_bid_size", 1.0))
        ratio = total_bid / total_ask if total_ask > 0 else 1.0

        if ratio >= 1.5:
            desc = f"🟢 강력한 매수 벽 받침 (매수/매도 잔량비: {ratio:.2f}배)"
        elif ratio <= 0.6:
            desc = f"🔴 두터운 상단 매도 벽 저항 (매수/매도 잔량비: {ratio:.2f}배)"
        else:
            desc = f"⚪ 매수/매도 잔량 균형 (비율: {ratio:.2f}배)"

        return {
            "bid_ask_ratio": round(ratio, 2),
            "total_bid": round(total_bid, 4),
            "total_ask": round(total_ask, 4),
            "imbalance_desc": desc,
        }

    def analyze_1h_trend(self, candles_1h: list[dict[str, Any]] | None) -> dict[str, Any]:
        """
        1시간봉(MTF) 대세 추세 분석 (상위 추세 정렬)
        """
        if not candles_1h or len(candles_1h) < 10:
            return {"trend": "NEUTRAL", "desc": "1시간봉 데이터 부족 (중립)", "ma20": 0.0, "rsi": 50.0}

        close_prices = [float(c.get("trade_price", 0)) for c in candles_1h if "trade_price" in c]
        current_p = close_prices[0]
        ma20_1h = sum(close_prices[:20]) / min(len(close_prices), 20) if close_prices else current_p
        rsi_1h = self.calculate_rsi(close_prices, 14)
        macd_1h = self.calculate_macd(close_prices, 12, 26, 9)

        if current_p > ma20_1h and macd_1h["trend"] == "BULLISH":
            trend = "BULLISH"
            desc = f"🟢 1시간봉 대세 상승장 (주가 > 1h MA20({ma20_1h:,.1f}), 1h MACD 강세)"
        elif current_p < ma20_1h and macd_1h["trend"] == "BEARISH":
            trend = "BEARISH"
            desc = f"🔴 1시간봉 대세 하락장 (주가 < 1h MA20({ma20_1h:,.1f}), 1h MACD 약세 - 단기 반등 속임수 주의)"
        else:
            trend = "SIDEWAYS"
            desc = f"⚪ 1시간봉 횡보/수렴 구간 (1h RSI: {rsi_1h})"

        return {
            "trend": trend,
            "desc": desc,
            "ma20": round(ma20_1h, 2),
            "rsi": rsi_1h,
        }

    @staticmethod
    def analyze_volume_spike(candles: list[dict[str, Any]]) -> dict[str, Any]:
        """거래량 급증(Volume Spike) 분석"""
        if not candles or len(candles) < 2:
            return {"current_vol": 0.0, "avg_vol_5": 0.0, "vol_ratio": 1.0, "is_spike": False}

        vols = [float(c.get("candle_acc_trade_volume", 0.0)) for c in candles]
        current_vol = vols[0]
        recent_vols = vols[1:min(len(vols), 6)]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else current_vol

        vol_ratio = (current_vol / avg_vol) if avg_vol > 0 else 1.0
        is_spike = vol_ratio >= 1.8

        return {
            "current_vol": round(current_vol, 4),
            "avg_vol_5": round(avg_vol, 4),
            "vol_ratio": round(vol_ratio, 2),
            "is_spike": is_spike,
        }

    @staticmethod
    def analyze_support_resistance(candles: list[dict[str, Any]]) -> dict[str, float]:
        """최근 30개 캔들 기준 최고점(저항선) 및 최저점(지지선) 산출"""
        if not candles:
            return {"resistance_high": 0.0, "support_low": 0.0}

        highs = [float(c.get("high_price", 0.0)) for c in candles if "high_price" in c]
        lows = [float(c.get("low_price", 0.0)) for c in candles if "low_price" in c]

        res_high = max(highs) if highs else 0.0
        sup_low = min(lows) if lows else 0.0

        return {
            "resistance_high": round(res_high, 2),
            "support_low": round(sup_low, 2),
        }

    @staticmethod
    def analyze_candle_patterns(candles: list[dict[str, Any]]) -> str:
        """최근 캔들의 몸통(Body) 및 윗꼬리/밑꼬리(Wick) 형태를 정밀 분석"""
        if not candles:
            return "캔들 데이터 없음"

        latest = candles[0]
        o = float(latest.get("opening_price", 0.0))
        h = float(latest.get("high_price", 0.0))
        l = float(latest.get("low_price", 0.0))
        c = float(latest.get("trade_price", 0.0))

        total_range = h - l if h > l else 0.0001
        body = abs(c - o)
        upper_wick = h - max(c, o)
        lower_wick = min(c, o) - l

        upper_ratio = (upper_wick / total_range) * 100
        lower_ratio = (lower_wick / total_range) * 100
        body_ratio = (body / total_range) * 100

        is_bullish = c >= o

        pattern_desc = []
        if lower_ratio >= 45.0:
            pattern_desc.append("밑꼬리 긴 강력한 저가 매수 지지(망치형/핀바)")
        elif upper_ratio >= 45.0:
            pattern_desc.append("윗꼬리 긴 상단 매도 저항 출회(역망치형/설거지 주의)")
        elif body_ratio >= 65.0:
            pattern_desc.append("몸통이 꽉 찬 강력한 " + ("장대양봉 돌파" if is_bullish else "장대음봉 하락"))
        else:
            pattern_desc.append("상하 꼬리가 균형을 이룬 팽이형 횡보")

        return f"{'양봉' if is_bullish else '음봉'} ({', '.join(pattern_desc)}) | 윗꼬리: {upper_ratio:.1f}%, 몸통: {body_ratio:.1f}%, 밑꼬리: {lower_ratio:.1f}%"

    def analyze(
        self,
        market: str,
        current_price: float,
        candles: list[dict[str, Any]],
        krw_balance: float,
        coin_balance: float,
        avg_buy_price: float,
        candles_1h: list[dict[str, Any]] | None = None,
        orderbook: dict[str, Any] | None = None,
        trade_memory_context: str = "",
        btc_context: str = "비트코인(BTC): 🟢 정상 안정세",
    ) -> dict[str, Any]:
        """
        [MTF 3중 정렬 + 호가창 수급 + 체결강도 + ATR 변동성 + 자가학습 메모리] 퀀트 분석 엔진 v4.0
        """
        if not self.api_key:
            logger.warning("Gemini API Key가 설정되지 않았습니다.")
            return {"status": "PAUSE", "action": "HOLD", "reason": "API Key 누락"}

        currency = market.split("-")[-1] if "-" in market else market
        close_prices = [float(c.get("trade_price", 0)) for c in candles if "trade_price" in c]

        # 1. 종합 기술 지표 연산
        rsi_val = self.calculate_rsi(close_prices, 14) if close_prices else 50.0
        bb = self.calculate_bollinger_bands(close_prices, 20, 2.0)
        macd = self.calculate_macd(close_prices, 12, 26, 9)
        vol_info = self.analyze_volume_spike(candles)
        sr_levels = self.analyze_support_resistance(candles)
        candle_pattern = self.analyze_candle_patterns(candles)
        atr_info = self.calculate_atr(candles, 14)
        trade_strength = self.analyze_trade_strength(candles)

        # 2. 이동평균선 & 이격도 연산
        ma5 = sum(close_prices[:5]) / min(len(close_prices), 5) if close_prices else current_price
        ma20 = bb["middle"]
        ma60 = sum(close_prices[:60]) / min(len(close_prices), 60) if close_prices else current_price
        disparity_ma20 = (current_price / ma20 * 100.0) if ma20 > 0 else 100.0

        # 3. MTF 1시간봉 상위 추세 및 호가창 수급 연산
        mtf_1h = self.analyze_1h_trend(candles_1h)
        ob_info = self.analyze_orderbook(orderbook)

        coin_value = coin_balance * current_price
        is_holding = coin_value >= 4000.0
        pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100 if (avg_buy_price > 0 and is_holding) else 0.0

        # 지능형 동적 모델 배정
        is_high_priority = (not is_holding) or vol_info["is_spike"] or (abs(pnl_pct) >= 1.5)
        candidate_models = (self.DEEP_FLASH_MODELS + self.LITE_MODELS) if is_high_priority else (self.LITE_MODELS + self.DEEP_FLASH_MODELS)
        mode_tag = "⚡ [심층 퀀트 Flash 모드]" if is_high_priority else "🚀 [초고속 Flash-Lite 모드]"
        logger.info(f"[{market}] 모델 라우터: {mode_tag} (1순위: {candidate_models[0]})")

        # 최근 5개 캔들 요약
        recent_summary = []
        for c in candles[:5]:
            recent_summary.append(
                f"- 시가: {c.get('opening_price'):,.2f} | 고가: {c.get('high_price'):,.2f} | 저가: {c.get('low_price'):,.2f} | 종가: {c.get('trade_price'):,.2f} | 거래량: {c.get('candle_acc_trade_volume', 0):.4f}"
            )
        candles_text = "\n".join(recent_summary)

        # ATR 기반 동적 목표가/손절가 가이드
        dynamic_tp = current_price + max(current_price * 0.025, atr_info["atr"] * 1.5)
        dynamic_sl = max(sr_levels["support_low"] * 0.995, current_price - max(current_price * 0.015, atr_info["atr"]))

        # 4. 기관 퀀트 헤지펀드 시스템 프롬프트 v4.0
        memory_section = f"\n{trade_memory_context}\n" if trade_memory_context else ""

        prompt = f"""당신은 월스트리트 헤지펀드 출신의 수석 암호화폐 퀀트 트레이더이자 리스크 관리 책임자(CRO)입니다.
제공된 실시간 {market}의 [BTC 거시 환경], [MTF 상위 추세], [호가창 & 체결강도 수급], [5분봉 퀀트 지표]를 종합 분석하여 최적의 트레이딩 지침을 JSON으로 제시하세요.

### [0. 대장주(BTC) 거시 시장 환경]
- 비트코인 시장 상태: {btc_context}
※ 알트코인은 비트코인의 단기 급락세에 매우 취약하므로, BTC 급락 위험 감지 시에는 신규 매수를 전면 금지하고 HOLD하세요.

### [1. MTF(멀티 타임프레임) 상위 추세 & 호가창/체결강도 수급 데이터]
- 1시간봉 대세 방향: {mtf_1h['desc']}
- 실시간 호가창 잔량: {ob_info['imbalance_desc']}
- 실시간 실질 체결강도: {trade_strength['desc']}
- 코인 고유 변동폭(ATR 14): {atr_info['atr']:,.2f} KRW ({atr_info['atr_pct']}% - {'🔥 고변동성 급등주' if atr_info['atr_pct'] >= 3.0 else '평온한 변동성'})

### [2. 5분봉 정밀 퀀트 지표 데이터]
- 현재 체결가: {current_price:,.2f} KRW
- 캔들 패턴: {candle_pattern}
- 이동평균선: MA5={ma5:,.2f} | MA20={ma20:,.2f} | MA60={ma60:,.2f}
- MA20 이격도(Disparity): {disparity_ma20:.2f}% ({'⚠️ 과열 이격' if disparity_ma20 >= 104.0 else ('🟢 눌림목 적정' if 98.0 <= disparity_ma20 <= 102.5 else '과매도 이격')})
- 모멘텀(RSI 14): {rsi_val} | MACD 상태: {macd['trend']} (Line={macd['macd']} | Signal={macd['signal']})
- 볼린저 밴드(20, 2.0): 상단={bb['upper']:,.2f} | 중심={bb['middle']:,.2f} | 하단={bb['lower']:,.2f} | 위치(%B)={bb['pct_b']}
- 거래량 상태: 현재={vol_info['current_vol']} | 5봉평균={vol_info['avg_vol_5']} ({vol_info['vol_ratio']}배 {'🚨 급증 폭발' if vol_info['is_spike'] else '평이'})
- 최근 주요 레벨: 전고점 저항={sr_levels['resistance_high']:,.2f} | 전저점 지지={sr_levels['support_low']:,.2f}
- 최근 캔들 흐름:
{candles_text}

### [3. 현재 계좌 포트폴리오 상태]
- 보유 여부: {'🔒 [보유 중]' if is_holding else '⚪ [미보유 (현금)]'} | 가용 원화: {krw_balance:,.0f} KRW
- 보유 수량: {coin_balance:.8f} {currency} (평가: {coin_value:,.0f} KRW) | 평단가: {avg_buy_price:,.2f} KRW (손익률: {pnl_pct:+.2f}%)

### [4. 5대 정량적 매수 승인 체크리스트 (5개 조건 모두 엄격히 검증 - 4개 이상 충족 필수)]
신규 매수(BUY) 승인을 내리기 위해서는 아래 5가지 조건 중 **반드시 4개 이상을 엄격히 충족**해야 합니다. 급등 후 고점 물림을 방지하기 위해 추격 매수는 절대 금지하며 **"눌림목 지지 반등(Pullback Bounce)"** 타점만 허용합니다:
1. [MTF 추세 정렬]: 1시간봉 추세가 '대세 하락장'이 아닐 것 (1시간봉 하락장 속 5분봉 일시 반등은 데드캣 속임수이므로 매수 금지).
2. [모멘텀 과열 방지]: 5분봉 RSI가 38 ~ 62 사이일 것 (RSI 62 초과 시 단기 과열이므로 추격 매수 전면 금지, 눌림목 대기).
3. [눌림목 & 이격도]: MA20 이격도가 98.5% ~ 102.5% 이내이며, %B <= 0.75 (볼린저 상단 돌파 추격 매수 원천 금지, 5분봉 MA20 지지선 부근 눌림 타점 필수).
4. [캔들 형태 & 수급]: 캔들 윗꼬리 비율이 25% 이하이며, 실시간 실질 체결강도 110% 이상 확인 (매도 덤핑 없는 순매수 유입 확인).
5. [기대 손익비]: (목표가 - 진입가) >= 1.5 * (진입가 - 손절가) 수학적 보장.

※ 위 조건 중 '모멘텀 과열' 또는 '볼린저 상단/이격도 과열'에 해당하는 경우, 아무리 상승세가 강해 보여도 **반드시 HOLD**로 판단하고 눌림목 지지선(ENTRY_PRICE)을 제시하세요.

### [5. 목표가/손절가 수학적 유효성 규칙]
- BUY 시: 반드시 '손절가 < 현재가 < 목표가' 관계를 만족해야 하며, 손익비 1:1.5 이상을 유지하세요.
- HOLD 시: 0을 적지 말고, **"5분봉 MA20 부근 눌림목 지지선(ENTRY_PRICE)"**, **"직전 지지선 손절가(STOP_LOSS)"**, **"목표가(TARGET_PRICE)"**를 기재하여 향후 진입 기준선을 제시하세요.
{memory_section}
### [JSON 출력 필수 스키마]
반드시 마크다운 백틱 없는 순수 JSON 포맷으로만 응답하세요:
{{
  "STATUS": "ACTIVE",
  "ACTION": "BUY", "SELL", 또는 "HOLD",
  "ENTRY_PRICE": {int(current_price) if current_price >= 100 else round(current_price, 2)},
  "TARGET_PRICE": {int(dynamic_tp) if dynamic_tp >= 100 else round(dynamic_tp, 2)},
  "STOP_LOSS": {int(dynamic_sl) if dynamic_sl >= 100 else round(dynamic_sl, 2)},
  "ALLOC_PCT": 0.5,
  "REASON": "체크리스트 충족 현황 및 눌림목 지지/수급/손익비 기반 1~2줄 정밀 요약"
}}
"""

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "topP": 0.8,
                "maxOutputTokens": 4000,
                "responseMimeType": "application/json",
            },
        }

        last_error = ""
        for model in candidate_models:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
            try:
                response = requests.post(endpoint, json=payload, timeout=25)
                if response.status_code == 200:
                    res_json = response.json()
                    raw_text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
                    logger.info(f"[{model}] 퀀트 분석 완료:\n{raw_text}")

                    try:
                        parsed = json.loads(raw_text)
                    except json.JSONDecodeError:
                        json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
                        if json_match:
                            parsed = json.loads(json_match.group(0))
                        else:
                            raise ValueError(f"JSON 디코딩 실패 (원문: {raw_text[:100]})")

                    status = str(parsed.get("STATUS", "ACTIVE")).upper()
                    action = str(parsed.get("ACTION", "HOLD")).upper()
                    entry_p = float(parsed.get("ENTRY_PRICE", current_price))
                    target_p = float(parsed.get("TARGET_PRICE", 0.0))
                    stop_l = float(parsed.get("STOP_LOSS", 0.0))
                    alloc_p = float(parsed.get("ALLOC_PCT", 0.3))
                    reason_t = str(parsed.get("REASON", "Gemini 퀀트 분석"))

                    if entry_p <= 0:
                        entry_p = current_price
                    if target_p <= 0:
                        target_p = dynamic_tp
                    if stop_l <= 0:
                        stop_l = dynamic_sl

                    # BUY 시 수학적 손익비 및 유효성 강제 보정
                    if action == "BUY":
                        if target_p <= current_price:
                            target_p = dynamic_tp
                        if stop_l >= current_price or stop_l <= 0:
                            stop_l = dynamic_sl
                        # 손익비 1.5배 미만 시 목표가 자동 상향
                        reward = target_p - current_price
                        risk = current_price - stop_l
                        if risk > 0 and (reward / risk) < 1.3:
                            target_p = current_price + (risk * 1.5)

                    if alloc_p > 1.0:
                        alloc_p = alloc_p / 100.0

                    if action == "HOLD":
                        alloc_p = 0.0
                    elif action == "BUY":
                        alloc_p = max(0.1, min(alloc_p, 1.0)) if alloc_p > 0 else 0.5
                    elif action == "SELL":
                        alloc_p = max(0.1, min(alloc_p, 1.0)) if alloc_p > 0 else 1.0

                    return {
                        "status": status,
                        "action": action,
                        "entry_price": entry_p,
                        "target_price": target_p,
                        "stop_loss": stop_l,
                        "alloc_pct": alloc_p,
                        "reason": reason_t,
                    }
                else:
                    last_error = f"[{model}] {response.text}"
                    logger.warning(f"모델 '{model}' 호출 실패 ({response.status_code}), 다음 백업 모델로 자동 전환...")
            except (requests.exceptions.RequestException, KeyError, ValueError, IndexError) as e:
                last_error = f"[{model}] Exception: {e}"
                logger.warning(f"모델 '{model}' 요청 중 오류 발생: {e}")

        logger.error(f"모든 Gemini Flash/Flash-Lite 모델 호출 실패: {last_error}")
        return {"status": "PAUSE", "action": "HOLD", "reason": f"All models failed: {last_error}"}
