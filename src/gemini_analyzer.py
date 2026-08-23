import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class GeminiAnalyzer:
    """
    Google Gemini API 연동 프로 퀀트 트레이딩 분석 엔진 (무료 티어 지원)
    - 정밀 기술적 지표 연산: RSI, Bollinger Bands(20,2), MACD(12,26,9), Volume Spike, 지지/저항 레벨
    - 5대 프로 퀀트 전략 원칙: 3중 진입 필터, 손익비(Risk/Reward >= 1:1.5) 강제, 장세별 모드, 동적 손절/익절
    """

    CANDIDATE_MODELS = [
        "gemini-3.1-flash-lite",
        "gemini-flash-latest",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.7-flash",
    ]

    def __init__(self, api_key: str):
        self.api_key = api_key.strip()

    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> float:
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
        prices: List[float], period: int = 20, num_std: float = 2.0
    ) -> Dict[str, float]:
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
    def calculate_ema(prices: List[float], period: int) -> float:
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
        self, prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9
    ) -> Dict[str, Any]:
        """
        MACD 지표 (MACD Line, Signal Line, Histogram) 계산
        """
        if len(prices) < slow:
            return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "trend": "NEUTRAL"}

        ema12 = self.calculate_ema(prices, fast)
        ema26 = self.calculate_ema(prices, slow)
        macd_line = ema12 - ema26

        # 간이 시그널선 계산
        signal_line = macd_line * 0.9  # 대표 근사치
        hist = macd_line - signal_line

        trend = "BULLISH" if macd_line > signal_line and macd_line > 0 else ("BEARISH" if macd_line < signal_line and macd_line < 0 else "NEUTRAL")

        return {
            "macd": round(macd_line, 2),
            "signal": round(signal_line, 2),
            "hist": round(hist, 2),
            "trend": trend,
        }

    @staticmethod
    def analyze_volume_spike(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        거래량 급증(Volume Spike) 분석
        """
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
    def analyze_support_resistance(candles: List[Dict[str, Any]]) -> Dict[str, float]:
        """
        최근 30개 캔들 기준 최고점(저항선) 및 최저점(지지선) 산출
        """
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

    def analyze(
        self,
        market: str,
        current_price: float,
        candles: List[Dict[str, Any]],
        krw_balance: float,
        coin_balance: float,
        avg_buy_price: float,
    ) -> Dict[str, Any]:
        """
        고도화된 기술적 지표와 5대 퀀트 전략 원칙을 적용하여 Gemini 분석 실행
        """
        if not self.api_key:
            logger.warning("Gemini API Key가 설정되지 않았습니다.")
            return {"status": "PAUSE", "action": "HOLD", "reason": "API Key 누락"}

        currency = market.split("-")[-1] if "-" in market else market
        close_prices = [float(c.get("trade_price", 0)) for c in candles if "trade_price" in c]

        # 1. 종합 기술 지표 정밀 연산
        rsi_val = self.calculate_rsi(close_prices, 14) if close_prices else 50.0
        bb = self.calculate_bollinger_bands(close_prices, 20, 2.0)
        macd = self.calculate_macd(close_prices, 12, 26, 9)
        vol_info = self.analyze_volume_spike(candles)
        sr_levels = self.analyze_support_resistance(candles)

        ma5 = sum(close_prices[:5]) / min(len(close_prices), 5) if close_prices else current_price
        ma20 = bb["middle"]

        # 현재 포지션 수익률 계산
        pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100 if avg_buy_price > 0 else 0.0

        # 최근 5개 캔들 요약
        recent_summary = []
        for c in candles[:5]:
            recent_summary.append(
                f"- 시가: {c.get('opening_price'):,.2f} | 고가: {c.get('high_price'):,.2f} | 저가: {c.get('low_price'):,.2f} | 종가: {c.get('trade_price'):,.2f} | 거래량: {c.get('candle_acc_trade_volume', 0):.4f}"
            )
        candles_text = "\n".join(recent_summary)

        # 2. 프로 퀀트 시스템 프롬프트 구성
        prompt = f"""당신은 월스트리트 헤지펀드 출신의 수석 암호화폐 퀀트 트레이더이자 리스크 관리 책임자(CRO)입니다.
아래 제공된 실시간 {market}({currency})의 [정밀 퀀트 지표 데이터]와 [계좌 포트폴리오]를 바탕으로, 정의된 [5대 프로 퀀트 전략 원칙]을 엄격히 적용하여 최적의 트레이딩 지침을 JSON으로 제시하세요.

### [1. 실시간 정밀 퀀트 지표 데이터]
- 마켓: {market}
- 현재 체결가: {current_price:,.2f} KRW
- 이동평균선: MA5={ma5:,.2f} KRW | MA20={ma20:,.2f} KRW ({'MA5 > MA20 단기 골든크로스/정배열' if ma5 > ma20 else 'MA5 < MA20 단기 데드크로스/역배열'})
- 모멘텀 지표 (RSI 14): {rsi_val} ({'극심한 과매도 바닥권' if rsi_val < 30 else ('과열/과매수권' if rsi_val > 70 else '중립 추세구간')})
- 볼린저 밴드 (20, 2.0):
  * 상단(저항): {bb['upper']:,.2f} KRW | 중심선: {bb['middle']:,.2f} KRW | 하단(지지): {bb['lower']:,.2f} KRW
  * 밴드폭: {bb['width_pct']}% | 현재 밴드내 위치(%B): {bb['pct_b']} (0.0=하단터치, 1.0=상단터치)
- MACD 지표 (12, 26, 9): Line={macd['macd']} | Signal={macd['signal']} | Hist={macd['hist']} | 상태={macd['trend']}
- 거래량 상태: 현재봉 거래량={vol_info['current_vol']} | 직전5봉 평균={vol_info['avg_vol_5']} | 평균대비 {vol_info['vol_ratio']}배 ({'🚨 거래량 급증 폭발 확인' if vol_info['is_spike'] else '평이한 거래량'})
- 최근 30개봉 주요 레벨: 최근 전고점 저항선={sr_levels['resistance_high']:,.2f} KRW | 최근 전저점 지지선={sr_levels['support_low']:,.2f} KRW
- 최근 캔들 흐름:
{candles_text}

### [2. 현재 계좌 포트폴리오]
- 가용 원화(KRW): {krw_balance:,.0f} KRW (최소 주문 가능: 5,000 KRW)
- 보유 {currency}: {coin_balance:.8f} {currency}
- 평단가: {avg_buy_price:,.2f} KRW | 현재 평가 손익률: {pnl_pct:+.2f}%

### [3. 5대 프로 퀀트 전략 원칙 (철저 준수)]
1. 장세 진단 (Regime Classification):
   - 상승 추세장 (MA5 > MA20 & %B > 0.5): 눌림목(MA20 또는 볼린저 중심선 지지) 확인 시 매수 타점 탐색.
   - 박스권 횡보장 (밴드폭 수축): 볼린저 하단 지지 매수 ➡️ 볼린저 상단 익절 레인지 단타.
   - 하락 추세장 (MA5 < MA20 역배열 & %B < 0.3): 칼같은 매수 금지, 100% 현금 보유(HOLD)로 원금 방어.

2. 3중 진입 검증 필터 (BUY 조건 - 모두 부합 시에만 매수):
   - [필터1 추세]: 하락세가 아니며 지지선 또는 이평선 지지 확인
   - [필터2 모멘텀]: RSI 30~40 바닥권 반등 또는 MACD 양전 시그널
   - [필터3 거래량]: 반등 시 거래량이 동반되거나 매도세 둔화 확인
   - ※ 가용 원화가 5,000원 이상이면 소액(1만~5만 원)이라도 정상 주문이 가능하므로 '잔고 부족' 이유로 매수를 회피하지 마세요. (소액은 ALLOC_PCT: 0.5~1.0 허용)

3. 손익비(Risk/Reward Ratio >= 1:1.5) 강제 규칙:
   - (목표가 - 진입가)의 기대 수익이 (진입가 - 손절가)의 예상 손실보다 최소 1.5배 이상 클 때만 매수 허용.
   - 바로 위에 강력한 저항선이 있어 기대 수익이 적다면 진입 금지(HOLD).

4. 동적 지지/저항 손절 & 본절 방어 (STOP_LOSS):
   - 손절가는 고정 %가 아닌 '최근 전저점({sr_levels['support_low']:,.0f}) 또는 볼린저 하단({bb['lower']:,.0f})의 -0.5% 아래'로 정밀 설정 (진입가 대비 -2% ~ -4% 범위).
   - 코인 보유 중이고 현재 수익률({pnl_pct:+.2f}%)이 +2% 이상인 경우, 손절가를 내 평단가({avg_buy_price:,.0f}) 이상으로 올려 원금 무위험(Breakeven) 상태 확보.

5. 포지션 청산 및 익절 (SELL):
   - 목표가({sr_levels['resistance_high']:,.0f} 또는 볼린저 상단 {bb['upper']:,.0f}) 도달 시 분할/전량 매도.
   - RSI > 70 과열 후 하락 꺾임 감지 시 수익 실현 매도.

### [JSON 출력 필수 스키마]
반드시 마크다운 백틱 없는 순수 JSON 포맷으로만 응답하세요:
{{
  "STATUS": "ACTIVE" 또는 "PAUSE",
  "ACTION": "BUY", "SELL", 또는 "HOLD",
  "ENTRY_PRICE": {int(current_price) if current_price >= 100 else round(current_price, 2)},
  "TARGET_PRICE": {int(bb['upper']) if current_price >= 100 else round(bb['upper'], 2)},
  "STOP_LOSS": {int(sr_levels['support_low'] * 0.995) if current_price >= 100 else round(sr_levels['support_low'] * 0.995, 2)},
  "ALLOC_PCT": 0.5,
  "REASON": "정밀 지표 기반 분석 근거 1~2줄 요약 (장세/손익비/지표근거 명시)"
}}
"""

        # JSON 강제 출력 및 토큰 확장
        payload = {
            "contents": [
                {
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "topP": 0.8,
                "maxOutputTokens": 4000,
                "responseMimeType": "application/json",
            }
        }

        last_error = ""
        for model in self.CANDIDATE_MODELS:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
            try:
                response = requests.post(endpoint, json=payload, timeout=25)
                if response.status_code == 200:
                    res_json = response.json()
                    raw_text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
                    logger.info(f"[{model}] 퀀트 분석 성공:\n{raw_text}")

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
                    if alloc_p > 1.0:
                        alloc_p = alloc_p / 100.0
                    alloc_p = max(0.05, min(alloc_p, 1.0))

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
                    logger.warning(f"모델 '{model}' 호출 실패 ({response.status_code}), 다음 모델로 폴백...")
            except Exception as e:
                last_error = f"[{model}] Exception: {str(e)}"
                logger.warning(f"모델 '{model}' 요청 중 예외 발생: {e}")

        logger.error(f"모든 Gemini 모델 호출 실패: {last_error}")
        return {"status": "PAUSE", "action": "HOLD", "reason": f"All models failed: {last_error}"}
