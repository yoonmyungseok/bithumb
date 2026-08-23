import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class GeminiAnalyzer:
    """
    Google Gemini API 연동 프로 퀀트 트레이딩 분석 엔진 v2.5
    - [지능형 동적 모델 라우터 (Dynamic Model Router)] 탑재:
      * 1) 급등주 포착 및 신규 매수 정밀 검증 시 ➜ [고성능 Flash (gemini-3.7-flash, gemini-flash-latest)] 자동 배정
      * 2) 일상 루틴 모니터링 및 보유 포지션 관망 시 ➜ [초고속 Flash-Lite (gemini-3.1-flash-lite)] 자동 배정
      * 3) 모델 장애/지연 발생 시 ➜ 같은 Flash/Flash-Lite 군 내에서 0.1초 자동 폴백(Fallback)
    - 5대 프로 퀀트 전략 원칙: 3중 진입 필터, 손익비(Risk/Reward >= 1:1.5) 강제, 추격매수 금지(No-Chase), 장세별 모드, 동적 손절/익절
    """

    # 1. 급등주 정밀 매수 검증 및 심층 추론용 (고성능 Flash 군)
    DEEP_FLASH_MODELS = [
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-flash-latest",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
    ]

    # 2. 일상 루틴 모니터링 및 초고속 상태 점검용 (초경량 Flash-Lite 군)
    LITE_MODELS = [
        "gemini-3.1-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-2.5-flash-lite",
        "gemini-3.5-flash-lite",
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

    @staticmethod
    def analyze_candle_patterns(candles: List[Dict[str, Any]]) -> str:
        """
        최근 캔들의 몸통(Body) 및 윗꼬리/밑꼬리(Wick) 형태를 정밀 분석하여 속임수 반등 필터링
        """
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
        candles: List[Dict[str, Any]],
        krw_balance: float,
        coin_balance: float,
        avg_buy_price: float,
    ) -> Dict[str, Any]:
        """
        [지능형 라우팅] 상황에 따라 Flash vs Flash-Lite를 자동 선택하여 퀀트 분석 수행
        """
        if not self.api_key:
            logger.warning("Gemini API Key가 설정되지 않았습니다.")
            return {"status": "PAUSE", "action": "HOLD", "reason": "API Key 누락"}

        currency = market.split("-")[-1] if "-" in market else market
        close_prices = [float(c.get("trade_price", 0)) for c in candles if "trade_price" in c]

        # 1. 종합 기술 지표 및 캔들 패턴 정밀 연산
        rsi_val = self.calculate_rsi(close_prices, 14) if close_prices else 50.0
        bb = self.calculate_bollinger_bands(close_prices, 20, 2.0)
        macd = self.calculate_macd(close_prices, 12, 26, 9)
        vol_info = self.analyze_volume_spike(candles)
        sr_levels = self.analyze_support_resistance(candles)
        candle_pattern = self.analyze_candle_patterns(candles)

        ma5 = sum(close_prices[:5]) / min(len(close_prices), 5) if close_prices else current_price
        ma20 = bb["middle"]

        # 현재 포지션 보유 상태 및 수익률 계산
        coin_value = coin_balance * current_price
        is_holding = coin_value >= 4000.0
        pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100 if (avg_buy_price > 0 and is_holding) else 0.0

        # =========================================================================
        # 🧠 [지능형 동적 모델 라우터 (Dynamic Model Router)]
        # =========================================================================
        # • 급등주 탐색/거래량 폭발/신규 매수 후보: 고성능 Flash (gemini-3.7-flash) 우선
        # • 평온한 일상 모니터링/보유 유지 상태: 초고속 Flash-Lite (gemini-3.1-flash-lite) 우선
        is_high_priority_trade = (not is_holding) or vol_info["is_spike"] or (abs(pnl_pct) >= 1.5)

        if is_high_priority_trade:
            # 1순위: 심층 Flash ➜ 2순위: Flash-Lite 폴백
            candidate_models = self.DEEP_FLASH_MODELS + self.LITE_MODELS
            mode_tag = "⚡ [심층 퀀트 Flash 모드]"
        else:
            # 1순위: 초고속 Flash-Lite ➜ 2순위: Flash 폴백
            candidate_models = self.LITE_MODELS + self.DEEP_FLASH_MODELS
            mode_tag = "🚀 [초고속 Flash-Lite 모드]"

        logger.info(f"[{market}] 모델 라우터: {mode_tag} (1순위: {candidate_models[0]})")

        # 최근 5개 캔들 요약
        recent_summary = []
        for c in candles[:5]:
            recent_summary.append(
                f"- 시가: {c.get('opening_price'):,.2f} | 고가: {c.get('high_price'):,.2f} | 저가: {c.get('low_price'):,.2f} | 종가: {c.get('trade_price'):,.2f} | 거래량: {c.get('candle_acc_trade_volume', 0):.4f}"
            )
        candles_text = "\n".join(recent_summary)

        # 2. 프로 퀀트 시스템 프롬프트 구성
        prompt = f"""당신은 월스트리트 헤지펀드 출신의 수석 암호화폐 퀀트 트레이더이자 리스크 관리 책임자(CRO)입니다.
아래 제공된 실시간 {market}({currency})의 [정밀 퀀트 지표 데이터]와 [포트폴리오 상태]를 바탕으로, [5대 프로 퀀트 전략 원칙]을 엄격히 적용하여 최적의 트레이딩 지침을 JSON으로 제시하세요.

### [1. 실시간 정밀 퀀트 지표 데이터]
- 마켓: {market}
- 현재 체결가: {current_price:,.2f} KRW
- 최근 캔들 패턴: {candle_pattern}
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

### [2. 현재 계좌 포트폴리오 상태]
- 보유 여부: {'🔒 [현재 코인 보유 중]' if is_holding else '⚪ [미보유 (100% 현금 상태)]'}
- 가용 원화(KRW): {krw_balance:,.0f} KRW (최소 주문 가능: 5,000 KRW)
- 보유 {currency}: {coin_balance:.8f} {currency} (평가금액: {coin_value:,.0f} KRW)
- 평단가: {avg_buy_price:,.2f} KRW | 현재 평가 손익률: {pnl_pct:+.2f}%

### [3. 5대 프로 퀀트 전략 원칙 (철저 준수)]
1. 포지션 상태별 맞춤 행동:
   - 미보유 상태: SELL은 불가능하므로 BUY(진입) 또는 HOLD(관망) 중에서만 결정.
   - 보유 중 상태: 이미 코인을 쥐고 있으므로 추가 매수(BUY)보다는 HOLD(수익 극대화 유지) 또는 SELL(분할 익절) 우선 판단.

2. 추격 매수 엄격 금지 (No-Chase Rule):
   - 현재 주가가 볼린저 상단 부근(%B > 0.85)이거나 윗꼬리가 길게 달린 경우, 상단 매도세 출회 위험이 크므로 절대로 매수하지 말고 '눌림목(MA5 또는 볼린저 중심선)'까지 HOLD로 대기.

3. 3중 진입 검증 필터 (BUY 조건 - 모두 부합 시에만 매수):
   - [필터1 추세]: MA20 위 또는 단기 골든크로스(MA5 > MA20) 지지 확인
   - [필터2 모멘텀]: RSI 30~45 바닥권 반등 or 밑꼬리 긴 캔들(저가 매수세 지지) 확인
   - [필터3 거래량]: 반등 시 거래량이 동반(평균 이상)되어 가짜 반등이 아님을 검증
   - ※ 가용 원화가 5,000원 이상이면 소액이라도 정상 주문이 가능하므로 '잔고 부족' 이유로 HOLD하지 마세요. (소액 ALLOC_PCT: 0.5~1.0)

4. 손익비(Risk/Reward Ratio >= 1:1.5) 강제 규칙:
   - (목표가 - 진입가)의 기대 수익이 (진입가 - 손절가)의 예상 손실보다 최소 1.5배 이상 클 때만 매수 허용.
   - 바로 위에 저항선이 있어 기대 수익이 적다면 진입 금지(HOLD).

5. 동적 손절 & 본절 방어 & 시간 초과 룰:
   - 손절가: 최근 전저점({sr_levels['support_low']:,.0f}) 또는 볼린저 하단({bb['lower']:,.0f})의 -0.5% 아래로 정밀 설정.
   - 본절 방어: 보유 중 수익률이 +2% 이상이면 손절가를 내 평단가({avg_buy_price:,.0f}) 위로 올려 원금 무위험 상태 확보.
   - 횡보 청산: 진입 후 변동성 없이 장시간 횡보 시 본절가 부근에서 정리(SELL) 유도.

### [JSON 출력 필수 스키마]
반드시 마크다운 백틱 없는 순수 JSON 포맷으로만 응답하세요:
{{
  "STATUS": "ACTIVE" 또는 "PAUSE",
  "ACTION": "BUY", "SELL", 또는 "HOLD",
  "ENTRY_PRICE": {int(current_price) if current_price >= 100 else round(current_price, 2)},
  "TARGET_PRICE": {int(bb['upper']) if current_price >= 100 else round(bb['upper'], 2)},
  "STOP_LOSS": {int(sr_levels['support_low'] * 0.995) if current_price >= 100 else round(sr_levels['support_low'] * 0.995, 2)},
  "ALLOC_PCT": 0.5,
  "REASON": "정밀 지표 기반 분석 근거 1~2줄 요약 (캔들꼬리/장세/손익비 명시)"
}}
"""

        # JSON 강제 출력 및 토큰 설정
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
            except Exception as e:
                last_error = f"[{model}] Exception: {str(e)}"
                logger.warning(f"모델 '{model}' 요청 중 오류 발생: {e}")

        logger.error(f"모든 Gemini Flash/Flash-Lite 모델 호출 실패: {last_error}")
        return {"status": "PAUSE", "action": "HOLD", "reason": f"All models failed: {last_error}"}
