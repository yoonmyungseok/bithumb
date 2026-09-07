import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, ClassVar

import requests

from gemini_telemetry import GeminiTelemetry
from strategy_engine import (
    StrategyPolicy,
    calculate_atr as se_calculate_atr,
    calculate_bollinger_bands as se_calculate_bollinger_bands,
    calculate_composite_alpha_score as se_calculate_composite_alpha_score,
    calculate_ema as se_calculate_ema,
    calculate_macd as se_calculate_macd,
    calculate_macd_acceleration as se_calculate_macd_acceleration,
    calculate_rsi as se_calculate_rsi,
    calculate_vwap as se_calculate_vwap,
    get_alpha_buy_threshold,
    get_momentum_breakout_alpha_threshold,
    is_night_session,
)

logger = logging.getLogger(__name__)


class GeminiAnalyzer:
    """
    Google Gemini API 연동 프로 퀀트 트레이딩 분석 엔진 v5.0
    - [7대 복합 팩터 앙상블]: MTF 1H + VWAP + MACD 가속도 + RSI + 볼린저 + 수급/오더북 + 볼륨 스파이크
    - [MTF 3중 정렬]: 1시간봉 대세 추세 + 5분봉 정밀 진입 타점 동시 분석
    - [VWAP 기관 수급 & MACD 가속도]: 스마트 머니 평단가 지지 및 모멘텀 확장 구간 정밀 포착
    - [호가창 & 체결강도 수급 분석]: 매수/매도 잔량비 + 실시간 매수체결강도(허매수벽 트랩 회피)
    - [ATR 변동성 & 이격도 퀀트]: MA5/MA20/MA60 이격도 + ATR 기반 동적 익절/손절선 산출
    - [대장주(BTC) 거시 환경 주입]: 비트코인 급락 위험 및 거시 추세 연동
    - [안정형 모델 라우터 & 무중단 로컬 퀀트 폴백]: Rate Limit 429 시 100% 로컬 앙상블 자율 전환
    """

    # 1. Fallback 기본 안정 모델 (무료 티어 20 RPD 제한 회피 및 500 RPD 쿼터 최적화를 위해 실존하는 Flash-Lite 전용 구성)
    FALLBACK_MODELS: ClassVar[list[str]] = [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-2.5-flash-lite",
    ]
    # 하위 호환성을 위한 참조
    STABLE_MODELS = FALLBACK_MODELS

    # 브리핑 전용 Fallback 모델 (하루 1회 발송용 최신 Flash 모델 순차 구성, Pro 모델 완전 배제)
    BRIEFING_FALLBACK_MODELS: ClassVar[list[str]] = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]

    # 동적 감지된 모델 캐시 및 TTL (6시간)
    _CACHED_MODELS: ClassVar[list[str]] = []
    _CACHED_BRIEFING_MODELS: ClassVar[list[str]] = []
    _MODELS_CACHED_AT: ClassVar[float] = 0.0
    _BRIEFING_MODELS_CACHED_AT: ClassVar[float] = 0.0
    _MODELS_CACHE_TTL: ClassVar[float] = 21600.0

    # 모델별 쿨다운(429/타임아웃) 및 블랙리스트(404/지원종료) 만료 시점 캐시 (전역 공유)
    _MODEL_COOLDOWNS: ClassVar[dict[str, float]] = {}
    _MODEL_BLACKLIST: ClassVar[dict[str, float]] = {}
    _CLASS_LOCK: ClassVar[threading.RLock] = threading.RLock()

    # 전역 인스턴스 간 공유되는 통합 AI 결과 캐시 (인스턴스 재생성 시에도 캐시 보존)
    _MACRO_DIAG_CACHE: ClassVar[dict[str, Any]] = {}
    _LAST_MACRO_DIAG_TS: ClassVar[float] = 0.0
    _SCREENER_RANK_CACHE: ClassVar[dict[str, dict[str, Any]]] = {}
    _MARKET_AI_SCORE_CACHE: ClassVar[dict[str, dict[str, Any]]] = {}
    _HOLDING_EVAL_CACHE: ClassVar[dict[str, dict[str, Any]]] = {}
    _ANALYSIS_CACHE: ClassVar[dict[str, dict[str, Any]]] = {}

    @classmethod
    def clear_caches(cls) -> None:
        """단위 테스트 및 세션 재시작을 위한 AI 응답 캐시 초기화"""
        with cls._CLASS_LOCK:
            cls._CACHED_MODELS.clear()
            cls._CACHED_BRIEFING_MODELS.clear()
            cls._MODELS_CACHED_AT = 0.0
            cls._BRIEFING_MODELS_CACHED_AT = 0.0
            cls._MODEL_COOLDOWNS.clear()
            cls._MODEL_BLACKLIST.clear()
            cls._MACRO_DIAG_CACHE.clear()
            cls._LAST_MACRO_DIAG_TS = 0.0
            cls._SCREENER_RANK_CACHE.clear()
            cls._MARKET_AI_SCORE_CACHE.clear()
            cls._HOLDING_EVAL_CACHE.clear()
            cls._ANALYSIS_CACHE.clear()

    # 무료 티어 15 RPM(분당 15회) 준수를 위한 최소 호출 간격 제어
    _LAST_CALL_TS: ClassVar[float] = 0.0
    _MIN_CALL_INTERVAL_SEC: ClassVar[float] = 3.5  # 최소 3.5초 간격 유지 (최대 ~17 RPM 수준으로 억제)

    # 전 모델 쿨다운 시 경고 로그 중복 폭발 억제 (3분당 최대 1회 경고)
    _LAST_ALL_COOLDOWN_LOG_TS: ClassVar[float] = 0.0
    _ALL_COOLDOWN_LOG_INTERVAL_SEC: ClassVar[float] = 180.0

    @classmethod
    def _wait_for_rate_limit(cls) -> None:
        """
        무료 티어 분당 호출 한도(15 RPM)를 안전하게 준수하기 위해 연속 호출 간격을 스로틀링합니다.
        """
        with cls._CLASS_LOCK:
            now = time.time()
            elapsed = now - cls._LAST_CALL_TS
            if elapsed < cls._MIN_CALL_INTERVAL_SEC:
                sleep_needed = cls._MIN_CALL_INTERVAL_SEC - elapsed
                time.sleep(sleep_needed)
            cls._LAST_CALL_TS = time.time()

    @staticmethod
    def _record_http(
        model: str,
        context: str,
        endpoint: str = "generate_content",
        response: requests.Response | None = None,
        error_kind: str = "",
    ) -> None:
        """Google AI Studio와 동일하게 모든 HTTP 시도를 텔레메트리에 반영"""
        status_code = response.status_code if response is not None else None
        GeminiTelemetry.record_http_attempt(model, context, endpoint, status_code, error_kind)

    @classmethod
    def _model_priority_key(cls, name: str) -> tuple[int, float, int, int, str]:
        """
        Gemini 모델 우선순위 점수 산출 함수 (내림차순 정렬용)
        1순위(Tier 2): flash-lite 계열 (초고속, 최고 쿼터 효율)
        2순위(Tier 1): 일반 flash 계열
        3순위(Tier 0): 기타 계열
        세부 정렬: 최신 버전 번호(예: 3.8 > 3.7 > 3.5 > 3.1 > 2.5), latest 별칭, 정식 릴리스 우선
        """
        lower_name = name.lower()

        # 1. Tier 판별: flash-lite = 2, flash = 1, 기타 = 0
        if "flash-lite" in lower_name:
            tier = 2
        elif "flash" in lower_name:
            tier = 1
        else:
            tier = 0

        # 2. 버전 번호 파싱 (예: gemini-3.8-flash -> 3.8, gemini-3.1-flash -> 3.1)
        version_match = re.search(r"(\d+(?:\.\d+)?)", lower_name)
        version = float(version_match.group(1)) if version_match else 0.0

        # 'latest' 별칭 처리 (버전 명시 없는 latest는 안정적 최신으로 취급)
        is_latest = 1 if "latest" in lower_name else 0
        if is_latest and version == 0.0:
            version = 2.99

        # preview/experimental 모델 패널티 (안정 릴리스 우선)
        is_preview = 1 if ("preview" in lower_name or "exp" in lower_name) else 0
        stable_flag = 0 if is_preview else 1

        return (tier, version, stable_flag, is_latest, lower_name)

    @classmethod
    def _briefing_model_priority_key(cls, name: str) -> tuple[int, float, int, int, str]:
        """
        일일 브리핑용 Gemini 모델 우선순위 점수 산출 함수 (내림차순 정렬)
        1순위(Tier 2): 일반 flash 계열 (분석력/문장력 최우선, gemini-3.8-flash 우선)
        2순위(Tier 1): pro 계열
        3순위(Tier 0): flash-lite 계열 (폴백용)
        세부 정렬: 최신 버전 번호(예: 3.8 > 3.7 > 3.5 > 3.1 > 2.5), latest 별칭, 정식 릴리스 우선
        """
        lower_name = name.lower()

        # 1. Tier 판별: 일반 flash = 1, flash-lite = 0, pro 및 기타 = -1 (Pro 모델 완전 배제)
        if "pro" in lower_name:
            tier = -1
        elif "flash-lite" in lower_name:
            tier = 0
        elif "flash" in lower_name:
            tier = 1
        else:
            tier = -1

        # 2. 버전 번호 파싱 (예: gemini-3.8-flash -> 3.8, gemini-3.1-flash -> 3.1)
        version_match = re.search(r"(\d+(?:\.\d+)?)", lower_name)
        version = float(version_match.group(1)) if version_match else 0.0

        # 'latest' 별칭 처리 (버전 명시 없는 latest는 안정적 최신으로 취급)
        is_latest = 1 if "latest" in lower_name else 0
        if is_latest and version == 0.0:
            version = 2.99

        # preview/experimental 모델 패널티 (안정 릴리스 우선)
        is_preview = 1 if ("preview" in lower_name or "exp" in lower_name) else 0
        stable_flag = 0 if is_preview else 1

        return (tier, version, stable_flag, is_latest, lower_name)

    @classmethod
    def _set_model_cooldown(cls, model: str, duration_sec: float) -> None:
        """스레드 안전하게 모델별 쿨다운을 등록합니다."""
        with cls._CLASS_LOCK:
            cls._MODEL_COOLDOWNS[model] = time.time() + duration_sec

    @classmethod
    def _set_model_blacklist(cls, model: str, duration_sec: float) -> None:
        """스레드 안전하게 지원 종료 모델을 블랙리스트에 격리합니다."""
        with cls._CLASS_LOCK:
            cls._MODEL_BLACKLIST[model] = time.time() + duration_sec

    @classmethod
    def fetch_available_models(cls, api_key: str = "") -> list[str]:
        """
        Google Generative Language API(ListModels)로부터 실시간 지원 모델 목록을 조회하여
        flash-lite 최우선 및 최신 버전 순으로 자동 정렬합니다.
        (이미지·오디오·TTS 등 미디어 전용 모델은 엄격히 배제하고 순수 텍스트/추론 모델만 선별)
        """
        key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        if not key:
            return list(cls.FALLBACK_MODELS)

        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
        try:
            resp = requests.get(url, timeout=10)
            cls._record_http("", "trading_router", "list_models", resp)
            if resp.status_code == 200:
                data = resp.json()
                raw_models = data.get("models", [])
                valid_models = []
                for item in raw_models:
                    m_name = item.get("name", "").replace("models/", "").strip()
                    methods = item.get("supportedGenerationMethods", [])

                    # generateContent 지원 및 Flash-Lite 계열만 선별 (일반 Flash는 일일 20회 제한으로 배제)
                    if "generateContent" in methods and "flash-lite" in m_name.lower():
                        # 트레이딩 부적합 모델(이미지/오디오/음성/임베딩/특수 목적) 엄격 배제
                        bad_keywords = [
                            "embedding", "aqa", "imagen", "image", "audio",
                            "tts", "stt", "omni", "vision", "high-res"
                        ]
                        if any(bad in m_name.lower() for bad in bad_keywords):
                            continue
                        valid_models.append(m_name)

                if valid_models:
                    sorted_models = sorted(valid_models, key=cls._model_priority_key, reverse=True)
                    logger.info(
                        f"✨ [Gemini Dynamic Router] 지원 모델 {len(sorted_models)}개 자동 감지 및 정렬 완료 "
                        f"(1위: {sorted_models[0]}): {sorted_models[:5]}"
                    )
                    return sorted_models
                else:
                    logger.warning("Gemini ListModels 응답에 적합한 Flash-Lite 모델이 없어 기본 Fallback 목록을 사용합니다.")
            else:
                logger.warning(f"Gemini ListModels 조회 실패 (HTTP {resp.status_code}) ➜ 기본 Fallback 목록 사용")
        except Exception as e:
            cls._record_http("", "trading_router", "list_models", error_kind="exception")
            logger.warning(f"Gemini ListModels 조회 중 예외 발생: {e} ➜ 기본 Fallback 목록 사용")

        return list(cls.FALLBACK_MODELS)

    @classmethod
    def get_available_models(cls, api_key: str = "", force_refresh: bool = False) -> list[str]:
        """
        6시간 TTL 캐시를 적용하여 가용 모델 목록을 반환합니다. (영구/장기 블랙리스트 제외)
        """
        now = time.time()
        with cls._CLASS_LOCK:
            if force_refresh or not cls._CACHED_MODELS or (now - cls._MODELS_CACHED_AT > cls._MODELS_CACHE_TTL):
                models = cls.fetch_available_models(api_key)
                cls._CACHED_MODELS = models
                cls._MODELS_CACHED_AT = now

            # 블랙리스트 제외된 활성 모델만 반환
            active_models = [m for m in cls._CACHED_MODELS if cls._MODEL_BLACKLIST.get(m, 0.0) <= now]
            return active_models if active_models else list(cls.FALLBACK_MODELS)

    def get_candidate_models(self, limit: int = 2) -> list[str]:
        """
        현재 시점에 쿨다운이나 블랙리스트가 아니고, 모델별 일일 쿼터(500 RPD) 여유가 있는 최우선 순위 모델 목록을 최대 limit개 반환합니다.
        """
        now_ts = time.time()
        with self._CLASS_LOCK:
            all_models = self.get_available_models(self.api_key)
            usable = [m for m in all_models if self._MODEL_COOLDOWNS.get(m, 0.0) <= now_ts]
            # 모델별 일일 쿼터(500회)가 남아있는 모델을 최우선 배치
            quota_available = [m for m in usable if GeminiTelemetry.can_call_model(m, for_emergency_exit=False)]
            if quota_available:
                ordered = quota_available + [m for m in usable if m not in quota_available]
                return ordered[:limit]
            return usable[:limit]

    @classmethod
    def fetch_available_briefing_models(cls, api_key: str = "") -> list[str]:
        """
        브리핑용 최신 고성능 모델 목록을 API(ListModels)로부터 조회하여
        최신 flash 계열 우선(3.8-flash 최우선)으로 자동 정렬합니다.
        (이미지·오디오·임베딩 등 미디어/특수 목적 모델은 제외)
        """
        key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        if not key:
            return list(cls.BRIEFING_FALLBACK_MODELS)

        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
        try:
            resp = requests.get(url, timeout=10)
            cls._record_http("", "briefing_router", "list_models", resp)
            if resp.status_code == 200:
                data = resp.json()
                raw_models = data.get("models", [])
                valid_models = []
                for item in raw_models:
                    m_name = item.get("name", "").replace("models/", "").strip()
                    methods = item.get("supportedGenerationMethods", [])

                    # generateContent 지원 모델 선별
                    if "generateContent" in methods:
                        # 브리핑 부적합 모델(Pro 모델 완전 배제, 이미지/오디오/음성/임베딩/특수 목적 배제)
                        bad_keywords = [
                            "pro", "embedding", "aqa", "imagen", "image", "audio",
                            "tts", "stt", "omni", "vision", "high-res"
                        ]
                        if any(bad in m_name.lower() for bad in bad_keywords):
                            continue
                        # Flash 계열 모델만 허용
                        if "flash" not in m_name.lower():
                            continue
                        valid_models.append(m_name)

                if valid_models:
                    sorted_models = sorted(valid_models, key=cls._briefing_model_priority_key, reverse=True)
                    logger.info(
                        f"✨ [Gemini Briefing Router] 브리핑 지원 모델 {len(sorted_models)}개 자동 감지 및 정렬 완료 "
                        f"(1위: {sorted_models[0]}): {sorted_models[:5]}"
                    )
                    return sorted_models
                else:
                    logger.warning("Gemini ListModels 응답에 적합한 브리핑 모델이 없어 기본 Fallback 목록을 사용합니다.")
            else:
                logger.warning(f"Gemini ListModels 브리핑 모델 조회 실패 (HTTP {resp.status_code}) ➜ 기본 Fallback 목록 사용")
        except Exception as e:
            cls._record_http("", "briefing_router", "list_models", error_kind="exception")
            logger.warning(f"Gemini ListModels 브리핑 모델 조회 중 예외 발생: {e} ➜ 기본 Fallback 목록 사용")

        return list(cls.BRIEFING_FALLBACK_MODELS)

    @classmethod
    def get_available_briefing_models(cls, api_key: str = "", force_refresh: bool = False) -> list[str]:
        """
        6시간 TTL 캐시를 적용하여 브리핑용 가용 모델 목록을 반환합니다. (영구/장기 블랙리스트 제외)
        """
        now = time.time()
        with cls._CLASS_LOCK:
            if force_refresh or not cls._CACHED_BRIEFING_MODELS or (now - cls._BRIEFING_MODELS_CACHED_AT > cls._MODELS_CACHE_TTL):
                models = cls.fetch_available_briefing_models(api_key)
                cls._CACHED_BRIEFING_MODELS = models
                cls._BRIEFING_MODELS_CACHED_AT = now

            # 블랙리스트 제외된 활성 모델만 반환 (Pro 모델 엄격 배제)
            active_models = [
                m for m in cls._CACHED_BRIEFING_MODELS
                if cls._MODEL_BLACKLIST.get(m, 0.0) <= now and "pro" not in m.lower()
            ]
            return active_models if active_models else list(cls.BRIEFING_FALLBACK_MODELS)

    def get_briefing_candidate_models(self, limit: int = 5) -> list[str]:
        """
        현재 시점에 쿨다운이나 블랙리스트가 아닌 브리핑 최우선 순위 모델 목록을 최대 limit개 반환합니다.
        (Pro 모델 완전 제외, 3.8-flash 최우선 ➜ 3.7 ➜ 3.6 ➜ 3.5 ➜ 3-flash ➜ 3.5-flash-lite 순차 폴백)
        """
        now_ts = time.time()
        with self._CLASS_LOCK:
            all_models = self.get_available_briefing_models(self.api_key)
            usable = [
                m for m in all_models
                if self._MODEL_COOLDOWNS.get(m, 0.0) <= now_ts and "pro" not in m.lower()
            ]
            if usable:
                return usable[:limit]
            # 쿨다운 중이더라도 블랙리스트가 아닌 fallback 모델 반환
            fallback_usable = [
                m for m in self.BRIEFING_FALLBACK_MODELS
                if self._MODEL_BLACKLIST.get(m, 0.0) <= now_ts and "pro" not in m.lower()
            ]
            return fallback_usable[:limit] if fallback_usable else list(self.BRIEFING_FALLBACK_MODELS[:limit])

    @property
    def _analysis_cache(self) -> dict[str, dict[str, Any]]:
        return self.__class__._ANALYSIS_CACHE

    @property
    def _holding_eval_cache(self) -> dict[str, dict[str, Any]]:
        return self.__class__._HOLDING_EVAL_CACHE

    @property
    def _screener_rank_cache(self) -> dict[str, dict[str, Any]]:
        return self.__class__._SCREENER_RANK_CACHE

    @property
    def _macro_diag_cache(self) -> dict[str, Any]:
        return self.__class__._MACRO_DIAG_CACHE

    @_macro_diag_cache.setter
    def _macro_diag_cache(self, value: dict[str, Any]) -> None:
        self.__class__._MACRO_DIAG_CACHE = value

    @property
    def _last_macro_diag_ts(self) -> float:
        return self.__class__._LAST_MACRO_DIAG_TS

    @_last_macro_diag_ts.setter
    def _last_macro_diag_ts(self, value: float) -> None:
        self.__class__._LAST_MACRO_DIAG_TS = value

    def __init__(self, api_key: str = ""):
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        self._lock = threading.RLock()

    @staticmethod
    def calculate_rsi(prices: list[float], period: int = 14) -> float:
        return se_calculate_rsi(prices, period)

    @staticmethod
    def calculate_bollinger_bands(
        prices: list[float], period: int = 20, num_std: float = 2.0
    ) -> dict[str, float]:
        return se_calculate_bollinger_bands(prices, period, num_std)

    @staticmethod
    def calculate_ema(prices: list[float], period: int) -> float:
        return se_calculate_ema(prices, period)

    def calculate_macd(
        self, prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9
    ) -> dict[str, Any]:
        return se_calculate_macd(prices, fast, slow, signal)

    @staticmethod
    def calculate_macd_acceleration(
        prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9
    ) -> dict[str, Any]:
        return se_calculate_macd_acceleration(prices, fast, slow, signal)

    @staticmethod
    def calculate_vwap(candles: list[dict[str, Any]]) -> dict[str, Any]:
        return se_calculate_vwap(candles)

    @staticmethod
    def calculate_atr(candles: list[dict[str, Any]], period: int = 14) -> dict[str, float]:
        return se_calculate_atr(candles, period)

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
        실시간 호가창 매수/매도 총잔량 비율, 수급 강도 및 최우선 호가 스프레드(Gap) 분석
        """
        if not orderbook:
            return {"bid_ask_ratio": 1.0, "spread_pct": 0.0, "imbalance_desc": "호가창 데이터 없음 (중립)"}

        total_ask = float(orderbook.get("total_ask_size", 1.0))
        total_bid = float(orderbook.get("total_bid_size", 1.0))
        ratio = total_bid / total_ask if total_ask > 0 else 1.0

        # 최우선 호가 스프레드 계산 (매수/매도 1호가 갭)
        units = orderbook.get("orderbook_units", [])
        spread_pct = 0.0
        if units:
            top_ask = float(units[0].get("ask_price", 0.0))
            top_bid = float(units[0].get("bid_price", 0.0))
            spread_pct = ((top_ask - top_bid) / top_bid * 100.0) if top_bid > 0 else 0.0

        spread_desc = f" | 호가 갭(스프레드): {spread_pct:.2f}% ({'⚠️ 유동성 부족 갭 발생' if spread_pct > 0.5 else '🟢 촘촘한 유동성'})"

        if ratio >= 1.5:
            desc = f"🟢 강력한 매수 벽 받침 (매수/매도 잔량비: {ratio:.2f}배){spread_desc}"
        elif ratio <= 0.6:
            desc = f"🔴 두터운 상단 매도 벽 저항 (매수/매도 잔량비: {ratio:.2f}배){spread_desc}"
        else:
            desc = f"⚪ 매수/매도 잔량 균형 (비율: {ratio:.2f}배){spread_desc}"

        return {
            "bid_ask_ratio": round(ratio, 2),
            "spread_pct": round(spread_pct, 2),
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

    def _run_local_quant_engine(
        self,
        current_price: float,
        mtf_1h: dict[str, Any],
        disparity_ma20: float,
        rsi_val: float,
        bb: dict[str, float],
        vol_info: dict[str, Any],
        candle_pattern: str,
        trade_strength: dict[str, Any],
        ob_info: dict[str, Any],
        dynamic_tp: float,
        dynamic_sl: float,
        is_holding: bool,
        pnl_pct: float,
        vwap_info: dict[str, Any] | None = None,
        macd_acc: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        AI API 소진(429) 시 100% 자립 작동하는 7대 복합 팩터 앙상블 퀀트 엔진
        """
        alpha_score = 0
        reasons = []

        # 1. MTF 1시간봉 추세 (15점)
        if mtf_1h.get("trend") != "BEARISH":
            alpha_score += 15
            reasons.append("1H 추세 양호(+15)")
        else:
            reasons.append("1H 하락장(+0)")

        # 2. VWAP 지지/돌파 (15점)
        if vwap_info and vwap_info.get("is_above", False):
            alpha_score += 15
            reasons.append("VWAP 상향 지지(+15)")
        else:
            alpha_score += 5
            reasons.append("VWAP 보통(+5)")

        # 3. MACD 히스토그램 가속도 (15점)
        if macd_acc and macd_acc.get("is_accelerating", False):
            alpha_score += 15
            reasons.append(f"MACD 가속({macd_acc.get('momentum_state', '')})(+15)")
        else:
            alpha_score += 5
            reasons.append("MACD 중립(+5)")

        # 4. 5분봉 모멘텀 RSI 38 ~ 72 (15점)
        if 42.0 <= rsi_val <= 65.0:
            alpha_score += 15
            reasons.append(f"RSI 골든존({rsi_val})(+15)")
        elif 38.0 <= rsi_val <= 72.0:
            alpha_score += 10
            reasons.append(f"RSI 적정({rsi_val})(+10)")
        else:
            reasons.append(f"RSI 이탈({rsi_val})(+0)")

        # 5. MA20 지지/모멘텀 이격도 & 볼린저 위치 (15점)
        pct_b = bb.get("pct_b", 0.5)
        if 97.5 <= disparity_ma20 <= 103.5 and pct_b <= 0.88:
            alpha_score += 15
            reasons.append(f"이격/볼린저 적정(%B {pct_b:.2f})(+15)")
        else:
            reasons.append(f"이격도 과열/이탈({disparity_ma20:.1f}%)(+0)")

        # 6. 수급 체결강도 & 호가 스프레드 (15점)
        spread = ob_info.get("spread_pct", 0.0)
        t_power = trade_strength.get("trade_power_pct", 100.0)
        if spread <= 0.6 and t_power >= 110.0:
            alpha_score += 15
            reasons.append(f"수급 압도(체결강도 {t_power}%)(+15)")
        elif spread <= 0.6 and t_power >= 90.0:
            alpha_score += 10
            reasons.append(f"수급 양호(체결강도 {t_power}%)(+10)")
        else:
            reasons.append(f"수급 미달({t_power}%)(+0)")

        # 7. 거래량 / 볼륨 스파이크 (10점)
        if vol_info.get("is_spike", False):
            alpha_score += 10
            reasons.append("볼륨 폭발(+10)")
        else:
            alpha_score += 5
            reasons.append("거래량 평이(+5)")

        # 판정 (총 100점 만점 중 60점 이상 충족 시 적극 BUY)
        if not is_holding:
            if alpha_score >= 60:
                action = "BUY"
                alloc_pct = 0.5
                reason = f"⚡ [로컬 퀀트 앙상블 BUY] 알파 스코어 {alpha_score}/100점: {', '.join(reasons[:3])}"
            else:
                action = "HOLD"
                alloc_pct = 0.0
                reason = f"⚪ [로컬 퀀트 앙상블 HOLD] 알파 스코어 미달({alpha_score}/100점): {', '.join(reasons[:2])}"
        else:
            # 보유 중일 때
            if rsi_val >= 75.0 or disparity_ma20 >= 105.0:
                action = "SELL"
                alloc_pct = 0.5
                reason = f"🚨 [로컬 퀀트 알고리즘 SELL] 과열 감지(RSI {rsi_val}, 이격 {disparity_ma20:.1f}%)"
            else:
                action = "HOLD"
                alloc_pct = 0.0
                reason = f"🔒 [로컬 퀀트 알고리즘 포지션 유지] 수익률 {pnl_pct:+.2f}%"

        return {
            "status": "ACTIVE",
            "action": action,
            "entry_price": current_price,
            "target_price": dynamic_tp,
            "stop_loss": dynamic_sl,
            "alloc_pct": alloc_pct,
            "reason": reason,
            "alpha_score": alpha_score,
        }

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
        whale_context: str = "최근 5분간 고래 대량 체결 없음 (수급 평온)",
        rs_context: str = "",
        btc_regime: str = "NORMAL",
        is_night: bool | None = None,
        candidate_type: str = "CONFIRMED",
        entry_policy_mode: str = "STANDARD",
    ) -> dict[str, Any]:
        """
        [7대 팩터 앙상블 + VWAP + MACD 가속도 + MTF + 상대강도(RS) + 호가수급 + ATR 변동성] 퀀트 분석 엔진 v5.1
        """
        if not self.api_key:
            logger.warning("Gemini API Key가 설정되지 않았습니다.")
            return {"status": "PAUSE", "action": "HOLD", "reason": "API Key 누락"}

        # 0. 동일 5분봉 캔들 분석 캐시 검사 (재시작 및 5분 이내 중복 API 호출 낭비 원천 차단)
        time_slot = int(time.time() // 300)  # 5분 타임블록
        candle_ts = ""
        if candles:
            candle_ts = str(candles[0].get("candle_date_time_utc") or candles[0].get("timestamp") or "")
        # 같은 확정봉이라도 BTC 레짐·세션·후보 경로가 바뀌면 매수 기준이 달라지므로
        # 이전 AI 판단을 재사용하지 않는다.
        regime_upper = str(btc_regime or "NORMAL").upper()
        night_active = is_night if is_night is not None else is_night_session()
        normalized_candidate_type = str(candidate_type or "CONFIRMED").upper()
        normalized_policy_mode = str(entry_policy_mode or "STANDARD").upper()
        cache_key = (
            f"{market}:{candle_ts or time_slot}:{regime_upper}:"
            f"{int(night_active)}:{normalized_candidate_type}:{normalized_policy_mode}"
        )
        if hasattr(self, "_analysis_cache") and cache_key in self._analysis_cache:
            cached_entry = self._analysis_cache[cache_key]
            if (time.time() - float(cached_entry.get("cached_at", 0))) < 540.0:
                logger.info(f"⚡ [{market}] 동일 5분봉 AI 분석 캐시 재사용 (Gemini 중복 호출 생략, 쿼터 보존)")
                GeminiTelemetry.record_cache_hit(market)
                return dict(cached_entry["result"])

        currency = market.split("-")[-1] if "-" in market else market
        close_prices = [float(c.get("trade_price", 0)) for c in candles if "trade_price" in c]

        # 1. 종합 기술 지표 및 신규 알파 팩터 연산
        rsi_val = self.calculate_rsi(close_prices, 14) if close_prices else 50.0
        bb = self.calculate_bollinger_bands(close_prices, 20, 2.0)
        macd = self.calculate_macd(close_prices, 12, 26, 9)
        macd_acc = self.calculate_macd_acceleration(close_prices, 12, 26, 9)
        vwap_info = self.calculate_vwap(candles)
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

        # 최근 5개 캔들 요약
        recent_summary = []
        for c in candles[:5]:
            recent_summary.append(
                f"- 시가: {c.get('opening_price'):,.2f} | 고가: {c.get('high_price'):,.2f} | 저가: {c.get('low_price'):,.2f} | 종가: {c.get('trade_price'):,.2f} | 거래량: {c.get('candle_acc_trade_volume', 0):.4f}"
            )
        candles_text = "\n".join(recent_summary)

        # 4. 코인 고유 변동성(ATR %)에 따른 맞춤형 동적 손익비 가이드라인 (최소 손익비 1.5:1 보장)
        atr_pct = atr_info["atr_pct"]
        if atr_pct < 2.0:  # 저변동성 메이저
            tp_delta = max(current_price * 0.025, atr_info["atr"] * 1.8)
            sl_delta = max(current_price * 0.012, atr_info["atr"] * 1.0)
        elif atr_pct <= 4.0:  # 일반 알트코인
            tp_delta = max(current_price * 0.040, atr_info["atr"] * 2.0)
            sl_delta = max(current_price * 0.015, atr_info["atr"] * 1.1)
        else:  # 고변동성 급등주
            tp_delta = max(current_price * 0.060, atr_info["atr"] * 2.2)
            sl_delta = max(current_price * 0.020, atr_info["atr"] * 1.2)

        dynamic_tp = current_price + tp_delta
        support_sl = sr_levels["support_low"] * 0.992
        atr_sl = current_price - sl_delta
        dynamic_sl = min(support_sl, atr_sl, current_price * 0.985)

        # 5. 일일 쿼터 예산 가드 (당일 450회 초과 시 신규 매수 AI 차단 및 로컬 100% 전환)
        if hasattr(GeminiTelemetry, "can_make_api_call") and not GeminiTelemetry.can_make_api_call(for_emergency_exit=False):
            logger.info(f"[{market}] 🛑 일일 Gemini AI 쿼터 예산(450회) 도달 ➜ [로컬 퀀트 알고리즘 엔진]으로 안전 전환 (긴급 탈출 쿼터 보존)")
            GeminiTelemetry.record_local_fallback(market, "daily quota budget reached (450)")
            return self._run_local_quant_engine(
                current_price, mtf_1h, disparity_ma20, rsi_val, bb, vol_info, candle_pattern,
                trade_strength, ob_info, dynamic_tp, dynamic_sl, is_holding, pnl_pct,
                vwap_info=vwap_info, macd_acc=macd_acc
            )

        # 6. 동적 모델 라우터 및 쿨다운/블랙리스트 검사 (Flash-Lite 최우선 후보 3개 선정)
        candidate_models = self.get_candidate_models(limit=3)

        if not candidate_models:
            now_ts = time.time()
            with self._CLASS_LOCK:
                should_log_warning = (now_ts - self._LAST_ALL_COOLDOWN_LOG_TS) > self._ALL_COOLDOWN_LOG_INTERVAL_SEC
                if should_log_warning:
                    self.__class__._LAST_ALL_COOLDOWN_LOG_TS = now_ts

            if should_log_warning:
                logger.warning(f"[{market}] 모든 Gemini AI Lite 모델이 쿨다운/블랙리스트 상태입니다 ➜ [로컬 퀀트 알고리즘 엔진]으로 즉시 자동 전환")
            else:
                logger.debug(f"[{market}] Gemini AI Lite 모델 쿨다운 지속 중 ➜ [로컬 퀀트 알고리즘 엔진] 전환")

            GeminiTelemetry.record_local_fallback(market, "all models cooling down")
            return self._run_local_quant_engine(
                current_price, mtf_1h, disparity_ma20, rsi_val, bb, vol_info, candle_pattern,
                trade_strength, ob_info, dynamic_tp, dynamic_sl, is_holding, pnl_pct,
                vwap_info=vwap_info, macd_acc=macd_acc
            )

        # 6. 현재 StrategyPolicy를 AI 요청에도 그대로 주입해 정책 불일치를 방지한다.
        if normalized_candidate_type == "MOMENTUM_BREAKOUT":
            current_alpha_threshold = get_momentum_breakout_alpha_threshold(regime_upper, night_active)
            policy_details = (
                f"모멘텀 돌파 전용입니다. 최신 확정 5분봉이 직전 {StrategyPolicy.MOMENTUM_BREAKOUT_LOOKBACK_BARS}봉 고점을 돌파하고, "
                f"거래량이 최근 20봉 평균의 {StrategyPolicy.MOMENTUM_BREAKOUT_VOLUME_RATIO_MIN:.1f}배 이상, 양봉, "
                f"RSI {StrategyPolicy.MOMENTUM_BREAKOUT_RSI_MIN:.0f}~{StrategyPolicy.MOMENTUM_BREAKOUT_RSI_MAX:.0f}, "
                f"1시간 EMA20의 {StrategyPolicy.MOMENTUM_BREAKOUT_MTF_EMA20_RATIO:.3f}배 이상을 모두 충족해야 합니다. "
                f"초기 주문 비중은 최대 종목 비중의 {StrategyPolicy.MOMENTUM_BREAKOUT_ALLOC_RATIO * 100:.0f}%를 넘지 않습니다."
            )
        elif normalized_policy_mode == "RECOVERY_REBOUND":
            current_alpha_threshold = max(
                StrategyPolicy.RECOVERY_REBOUND_ALPHA_THRESHOLD,
                get_alpha_buy_threshold(regime_upper, night_active),
            )
            policy_details = (
                "급락 후 반등 전용 경로입니다. RISK_OFF에서만 사용하며, 확정 5분봉 반등과 핵심 하드 게이트, "
                f"BTC 대비 RS +{StrategyPolicy.RECOVERY_REBOUND_RS_MIN * 100:.1f}% 이상, 24시간 거래대금 "
                f"{StrategyPolicy.MIN_TRADE_VALUE_RISK_OFF / 100_000_000:.0f}억 원 이상, 1시간 EMA20의 "
                f"{StrategyPolicy.RECOVERY_REBOUND_MTF_EMA20_RATIO:.3f}배 이상을 모두 충족해야 합니다. "
                f"초기 주문 비중은 일반 슬롯의 {StrategyPolicy.RECOVERY_REBOUND_ALLOC_RATIO * 100:.0f}%입니다."
            )
        else:
            current_alpha_threshold = get_alpha_buy_threshold(regime_upper, night_active)
            policy_details = (
                "일반 확인형 경로입니다. 로컬 하드 게이트(확정봉·MTF·RSI·볼린저·MA 정렬·이격·윗꼬리·"
                "저점권 반등)를 모두 통과한 후보만 AI 분석 대상입니다."
            )

        if regime_upper == "RISK_OFF":
            if normalized_policy_mode == "RECOVERY_REBOUND":
                risk_off_instruction = (
                    f"반등 전용 경로이므로 BTC 대비 RS +{StrategyPolicy.RECOVERY_REBOUND_RS_MIN * 100:.1f}% 이상과 "
                    "독자 수급이 확인되지 않으면 HOLD를 반환하세요."
                )
            else:
                risk_off_instruction = (
                    f"약세장 스크리닝 기준인 BTC 대비 RS +{StrategyPolicy.RS_MIN_RISK_OFF * 100:.1f}% 이상을 충족하고, "
                    "비트코인과 동조하지 않는 독자 수급이 확인된 종목만 제한적으로 검토하세요."
                )
        else:
            risk_off_instruction = "BTC가 급락하거나 시장 데이터가 불확실하면 신규 매수를 제안하지 말고 HOLD를 반환하세요."

        # 7. 기관 퀀트 헤지펀드 시스템 프롬프트 v5.2
        memory_section = f"\n{trade_memory_context}\n" if trade_memory_context else ""
        rs_line = f"- 비트코인 대비 상대 강도(RS): {rs_context}\n" if rs_context else ""

        prompt = f"""당신은 월스트리트 헤지펀드 출신의 수석 암호화폐 퀀트 트레이더이자 리스크 관리 책임자(CRO)입니다.
제공된 실시간 {market}의 [BTC 거시 환경 & 상대강도], [MTF 상위 추세], [VWAP 기관 수급], [MACD 가속도], [호가창 & 고래 수급], [5분봉 퀀트 지표]를 종합 분석하여 최적의 트레이딩 지침을 JSON으로 제시하세요.

### [0. 대장주(BTC) 거시 시장 환경 & 코인 상대 강도(RS)]
- 비트코인 시장 상태: {btc_context}
{rs_line}※ 알트코인은 비트코인의 단기 급락세에 취약합니다. {risk_off_instruction}

### [0-1. 이번 요청의 실행 정책 — AI가 절대 우회할 수 없음]
- BTC 레짐: {regime_upper} | 심야 세션(00:00~07:00 KST): {'예' if night_active else '아니오'}
- 후보 유형: {normalized_candidate_type} | 정책 경로: {normalized_policy_mode}
- 현재 알파 승인 기준: {current_alpha_threshold}점 이상
- 경로 조건: {policy_details}
- AI의 판단은 주문 권한이 아닙니다. 로컬 하드 게이트, 주문 REST 대사 완료, WebSocket 정상, 종목별 쿨다운, 미해결 주문 부재, 리스크 한도, 호가 영향 검증 중 하나라도 불충족하면 시스템은 BUY를 제출하지 않습니다.
- 데이터가 누락·모순되거나 위 조건의 충족 여부를 확인할 수 없으면 추정하지 말고 반드시 HOLD를 반환하세요. ACK는 체결이 아니며 체결·포지션·손익 상태를 가정하지 마세요.

### [0-2. 증거 우선 의사결정 절차 — 결론보다 검증을 먼저 수행]
아래 순서를 생략하거나 임의의 외부 정보·추측으로 보완하지 마세요.
1. 제공된 수치만 사용해 7대 팩터 각각을 `충족`, `미충족`, `판정불가`로 분류하세요. 수치가 없거나 서로 모순되면 `판정불가`이며 해당 팩터는 0점입니다.
2. 각 충족 판정에는 이 요청에 포함된 수치 또는 상태를 하나씩 근거로 사용하세요. 단일 강한 신호가 다른 필수 팩터의 미충족을 상쇄할 수 없습니다.
3. 후보 유형과 정책 경로에 맞는 경로 조건이 하나라도 미충족이면 BUY가 아니라 HOLD입니다. `MOMENTUM_BREAKOUT`은 돌파 추격을 일반 눌림목 근거로, `RECOVERY_REBOUND`는 반등 기대를 일반 상승장 근거로 바꾸어 해석하지 마세요.
4. `ALPHA_SCORE`는 위 7대 팩터의 검증 결과만 반영한 0~100 정수입니다. BUY는 현재 알파 승인 기준 이상이고, 모든 필수 조건이 확인되며, 아래 손익비 규칙을 만족할 때만 선택하세요.
5. 결론을 내리기 전에 가장 강한 반대 근거 1개와 매수 가설의 무효화 조건 1개를 확인하세요. 둘 중 하나라도 신규 매수 위험을 높이면 HOLD를 선택하세요.

### [1. MTF 상위 추세 & VWAP 기관 수급 & 호가창 수급 데이터]
- 1시간봉 대세 방향: {mtf_1h['desc']}
- VWAP(거래량가중평균가): {vwap_info['vwap']:,.2f} KRW (현재가 대비 이격: {vwap_info['disparity_pct']:+.2f}%, {'🟢 VWAP 상단 지지' if vwap_info['is_above'] else '🔴 VWAP 하단 저항'})
- 실시간 호가창 잔량: {ob_info['imbalance_desc']}
- 실시간 실질 체결강도: {trade_strength['desc']}
- 실시간 고래(3,000만 원↑) 수급 흐름: {whale_context}
- 코인 고유 변동폭(ATR 14): {atr_info['atr']:,.2f} KRW ({atr_info['atr_pct']}% - {'🔥 고변동성 급등주' if atr_info['atr_pct'] >= 3.0 else '평온한 변동성'})

### [2. 5분봉 정밀 퀀트 지표 데이터]
- 현재 체결가: {current_price:,.2f} KRW
- 캔들 패턴: {candle_pattern}
- 이동평균선: MA5={ma5:,.2f} | MA20={ma20:,.2f} | MA60={ma60:,.2f}
- MA20 이격도(Disparity): {disparity_ma20:.2f}% ({'⚠️ 과열 이격' if disparity_ma20 >= 104.0 else ('🟢 눌림목 적정' if 98.0 <= disparity_ma20 <= 102.5 else '과매도 이격')})
- 모멘텀(RSI 14): {rsi_val}
- MACD 가속도: 상태={macd_acc['momentum_state']} | Slope={macd_acc['slope']} | Hist={macd_acc['hist']} ({'🟢 모멘텀 확장 가속' if macd_acc['is_accelerating'] else '모멘텀 둔화/하락'})
- 볼린저 밴드(20, 2.0): 상단={bb['upper']:,.2f} | 중심={bb['middle']:,.2f} | 하단={bb['lower']:,.2f} | 위치(%B)={bb['pct_b']}
- 거래량 상태: 현재={vol_info['current_vol']} | 5봉평균={vol_info['avg_vol_5']} ({vol_info['vol_ratio']}배 {'🚨 급증 폭발' if vol_info['is_spike'] else '평이'})
- 최근 주요 레벨: 전고점 저항={sr_levels['resistance_high']:,.2f} | 전저점 지지={sr_levels['support_low']:,.2f}
- 최근 캔들 흐름:
{candles_text}

### [3. 현재 계좌 포트폴리오 상태]
- 보유 여부: {'🔒 [보유 중]' if is_holding else '⚪ [미보유 (현금)]'} | 가용 원화: {krw_balance:,.0f} KRW
- 보유 수량: {coin_balance:.8f} {currency} (평가: {coin_value:,.0f} KRW) | 평단가: {avg_buy_price:,.2f} KRW (손익률: {pnl_pct:+.2f}%)

### [4. 7대 복합 팩터 앙상블 매수 승인 규칙]
신규 매수(BUY) 승인을 내리기 위해서는 아래 7대 팩터 종합 점수가 **이번 요청의 현재 알파 승인 기준({current_alpha_threshold}점) 이상**이어야 합니다:
1. [MTF 1H 추세] 1시간봉 대세 하락장이 아닐 것.
2. [VWAP 기관 수급] 현재가가 VWAP 상단에 안착 지지 또는 돌파할 것.
3. [MACD 가속도] 히스토그램 기울기가 양의 방향으로 가속 확장 중일 것.
4. [RSI 골든존] 5분봉 RSI가 38 ~ 72 사이일 것 (RSI 45~65 최적).
5. [볼린저 밴드 & 이격] MA20 이격도 97.5%~103.5% 및 %B <= 0.88.
6. [수급 & 호가창] 호가 갭 <= 0.35%, 체결강도 90% 이상 또는 고래 유입.
7. [기대 손익비] (목표가 - 진입가) >= 1.5 * (진입가 - 손절가) 수학적 보장.

※ [엄격한 상투 추격 매수 금지] 이미 최근 캔들이 급등하여 볼린저 밴드 상단(%B >= 0.88)에 도달했거나 거래량이 터진 뒤 윗꼬리가 달린 종목의 추격 매수(Chasing the Top)는 절대 금지(HOLD)합니다. 5분봉 MA20 또는 VWAP 지지선에서 안정적인 눌림목 안착이 확인되고 손익비가 1:1.5 이상 확보된 경우에만 BUY를 승인하세요.

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
  "ALPHA_SCORE": 0,
  "REASON": "1~2줄: 충족/미충족 핵심 팩터, 가장 강한 반대 근거, 진입 가설 무효화 조건을 제공된 수치로만 요약"
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
                self._wait_for_rate_limit()
                response = requests.post(endpoint, json=payload, timeout=25)
                self._record_http(model, market, response=response)
                if response.status_code == 200:
                    res_json = response.json()
                    raw_text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
                    logger.info(f"[{model}] Gemini 퀀트 분석 완료:\n{raw_text}")

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

                    if action == "BUY":
                        # 단기 과열 하드 가드레일 (Safe Guardrail: 상투 잡기 원천 방어)
                        overheat_reasons = []
                        if rsi_val > 65.0:
                            overheat_reasons.append(f"RSI과열({rsi_val:.1f}>65.0)")
                        pct_b_val = float(bb.get("pct_b", 0.5))
                        if pct_b_val > 0.88:
                            overheat_reasons.append(f"볼린저상단이탈(%B {pct_b_val:.2f}>0.88)")
                        if disparity_ma20 > 103.5:
                            overheat_reasons.append(f"MA20이격과열({disparity_ma20:.1f}%>103.5%)")
                        t_power_val = float(trade_strength.get("trade_power_pct", 100.0))
                        if t_power_val > 350.0:
                            overheat_reasons.append(f"체결강도비정상과열({t_power_val:.0f}%>350%)")

                        if overheat_reasons:
                            logger.warning(
                                f"🛡️ [{market}] AI 매수 신호에 과열 가드레일 작동 ➜ HOLD 강제 전환: {', '.join(overheat_reasons)}"
                            )
                            action = "HOLD"
                            alloc_p = 0.0
                            reason_t = f"[과열 가드레일 작동: HOLD 강제 전환 ({', '.join(overheat_reasons)})] {reason_t}"
                        else:
                            if target_p <= current_price:
                                target_p = dynamic_tp
                            if stop_l >= current_price or stop_l <= 0:
                                stop_l = dynamic_sl
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

                    alpha_sc = int(parsed.get("ALPHA_SCORE") or parsed.get("alpha_score", 0) or 0)
                    res = {
                        "status": status,
                        "action": action,
                        "entry_price": entry_p,
                        "target_price": target_p,
                        "stop_loss": stop_l,
                        "alloc_pct": alloc_p,
                        "reason": f"[{model}] {reason_t}",
                        "alpha_score": alpha_sc,
                    }
                    if hasattr(self, "_analysis_cache") and cache_key:
                        self._analysis_cache[cache_key] = {"cached_at": time.time(), "result": res}
                    return res
                elif response.status_code == 429:
                    self._set_model_cooldown(model, 120.0)  # 무료 티어 RPM 리셋을 고려하여 2분(120초) 쿨다운
                    last_error = f"[{model}] 429 Quota Exceeded (2분 쿨다운 등록)"
                    logger.warning(f"⚠️ 모델 '{model}' 429 Quota Exceeded 발생 ➜ 2분간 재호출 차단 쿨다운 등록")
                elif response.status_code in (404, 400):
                    # 모델 지원 종료(Deprecated) 또는 부존재 ➜ 24시간 블랙리스트 등록 (자기 치유)
                    self._set_model_blacklist(model, 86400.0)
                    last_error = f"[{model}] HTTP {response.status_code} Unsupported/Deprecated (24시간 블랙리스트 격리)"
                    logger.warning(f"⛔ 모델 '{model}' 지원 종료 또는 부존재 감지(HTTP {response.status_code}) ➜ 24시간 블랙리스트 격리")
                else:
                    last_error = f"[{model}] HTTP {response.status_code}: {response.text[:100]}"
                    logger.warning(f"모델 '{model}' 호출 실패 ({response.status_code})")
            except requests.exceptions.Timeout as e:
                # 일시적인 구글 서버 읽기 타임아웃 ➜ 3분 단기 쿨다운 후 차순위 모델 전환
                self._record_http(model, market, error_kind="timeout")
                self._set_model_cooldown(model, 180.0)
                last_error = f"[{model}] Timeout: {e}"
                logger.warning(f"⏳ 모델 '{model}' 응답 타임아웃 ➜ 3분 쿨다운 등록 후 차순위 모델 전환")
            except (requests.exceptions.RequestException, KeyError, ValueError, IndexError) as e:
                self._record_http(model, market, error_kind="exception")
                last_error = f"[{model}] Exception: {e}"
                logger.warning(f"모델 '{model}' 요청 예외: {e}")

        logger.warning(f"Gemini API 호출 제한({last_error}) ➜ [로컬 퀀트 알고리즘 엔진]으로 즉시 자동 전환합니다.")
        GeminiTelemetry.record_local_fallback(market, last_error or "api failure")
        local_res = self._run_local_quant_engine(
            current_price, mtf_1h, disparity_ma20, rsi_val, bb, vol_info, candle_pattern,
            trade_strength, ob_info, dynamic_tp, dynamic_sl, is_holding, pnl_pct,
            vwap_info=vwap_info, macd_acc=macd_acc,
        )
        if hasattr(self, "_analysis_cache") and cache_key:
            self._analysis_cache[cache_key] = {"cached_at": time.time(), "result": local_res}
        return local_res

    def _call_gemini_json(
        self,
        prompt: str,
        candidate_models: list[str] | None = None,
        timeout: float = 15.0,
        max_tokens: int = 2000,
    ) -> dict[str, Any] | list[Any] | None:
        """Gemini 모델을 순차 호출하여 JSON 응답을 디코딩하는 공통 통신 엔진"""
        if not self.api_key:
            return None

        models = candidate_models or self.get_candidate_models(limit=3)
        if not models:
            return None

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "topP": 0.8,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }

        for model in models:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
            try:
                self._wait_for_rate_limit()
                response = requests.post(endpoint, json=payload, timeout=timeout)
                self._record_http(model, "macro_or_batch", response=response)
                if response.status_code == 200:
                    res_json = response.json()
                    raw_text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
                    try:
                        parsed = json.loads(raw_text)
                    except json.JSONDecodeError:
                        json_match = re.search(r"(\{.*\}|\[.*\])", raw_text, re.DOTALL)
                        if json_match:
                            parsed = json.loads(json_match.group(0))
                        else:
                            continue
                    return parsed
                elif response.status_code == 429:
                    self._set_model_cooldown(model, 120.0)
                elif response.status_code in (404, 400):
                    self._set_model_blacklist(model, 86400.0)
            except requests.exceptions.Timeout:
                self._record_http(model, "macro_or_batch", error_kind="timeout")
                self._set_model_cooldown(model, 180.0)
            except Exception as e:
                self._record_http(model, "macro_or_batch", error_kind="exception")
                logger.debug(f"모델 '{model}' 호출 예외: {e}")
        return None

    def evaluate_holding_position(
        self,
        market: str,
        current_price: float,
        avg_buy_price: float,
        candles: list[dict[str, Any]],
        candles_1h: list[dict[str, Any]] | None = None,
        orderbook: dict[str, Any] | None = None,
        hold_duration_sec: float = 0.0,
        btc_context: str = "",
    ) -> dict[str, Any]:
        """
        [1순위] 기보유 포지션 동적 청산 및 러너(Runner) 추세 추종 평가
        - EMERGENCY_EXIT: 세력 덤핑/대량 매도/윗꼬리 거래량 폭증 감지 시 조기 전량 탈출
        - RUNNER_HOLD: 강력한 장대양봉 돌파 지속 시 기계적 익절 보류 및 목표가 상향
        - TIGHTEN_STOP: 단기 지지선 안착 시 손절가 상향(수익 보존)
        - HOLD: 정상 유지
        """
        fallback_res = {
            "action": "HOLD",
            "reason": "로컬 지표 감시 유지 (기본)",
            "adjusted_target_price": None,
            "adjusted_stop_loss": None,
            "confidence": 50,
        }
        if not self.api_key or not candles or avg_buy_price <= 0:
            return fallback_res

        # 손익률 상태에 따른 적응형 스마트 캐시 (횡보 시 900초, 급락/급등 시 60초)
        pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0 if avg_buy_price > 0 else 0.0
        adaptive_ttl = 60.0 if (pnl_pct <= -1.0 or pnl_pct >= 1.5) else 900.0

        cache_key = f"HOLDING:{market}"
        if hasattr(self, "_holding_eval_cache") and cache_key in self._holding_eval_cache:
            cached = self._holding_eval_cache[cache_key]
            if (time.time() - float(cached.get("cached_at", 0))) < adaptive_ttl:
                GeminiTelemetry.record_cache_hit(market)
                return dict(cached["result"])

        # 일일 비상 쿼터 가드 (490회 초과 시 긴급 탈출 AI 호출 중단 및 로컬 유지)
        if hasattr(GeminiTelemetry, "can_make_api_call") and not GeminiTelemetry.can_make_api_call(for_emergency_exit=True):
            logger.info(f"[{market}] 🛑 Gemini AI 일일 비상 쿼터(490회) 도달 ➜ 기보유 포지션 로컬 룰 유지")
            return fallback_res

        try:
            close_prices = [float(c.get("trade_price", 0)) for c in candles if "trade_price" in c]
            pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
            hold_min = hold_duration_sec / 60.0

            rsi_val = self.calculate_rsi(close_prices, 14) if close_prices else 50.0
            bb = self.calculate_bollinger_bands(close_prices, 20, 2.0)
            vwap_info = self.calculate_vwap(candles)
            trade_strength = self.analyze_trade_strength(candles)
            ob_info = self.analyze_orderbook(orderbook)
            vol_info = self.analyze_volume_spike(candles)
            candle_pattern = self.analyze_candle_patterns(candles)

            prompt = f"""당신은 암호화폐 퀀트 펀드의 수석 포지션 관리자(Risk Manager)입니다.
현재 보유 중인 포지션 [{market}]의 실시간 수급과 차트를 정밀 분석하여 다음 중 단 하나의 행동을 결정하세요:

1. "EMERGENCY_EXIT": 거래량 실린 윗꼬리 장대음봉, 대량 매도벽 출회, VWAP 이탈, 직전 저점 붕괴 등 돌이킬 수 없는 세력 덤핑/급락 징후 시 기계적 손절 전에 "즉시 시장가 전량 탈출" (※ 주의: 진입 초기 10분 이내의 단순 호가 흔들림, 손실률 -0.8% 이내의 미세 조정, 지지선 유지 상태에서는 절대 남발하지 말고 HOLD 또는 TIGHTEN_STOP 선택)
2. "RUNNER_HOLD": 거래량 급증 장대양봉 돌파, 신고가 랠리 강력 지속 시 "기계적 소액 익절을 보류하고 목표가를 상향하여 추세 최대 향유"
3. "TIGHTEN_STOP": 단기 지지선 구축 완료 및 안정적 수익권(또는 불안한 보합세 방어) 시 손절가를 평단가(본전) 또는 지지선으로 끌어올림
4. "HOLD": 정상적인 추세 유지/건강한 숨고르기 상태

### [판단 가이드라인]
- 손익률이 -0.8% 이내이거나 보유 시간이 짧은 경우, 단순 체결강도 저하나 일시적 매도벽만으로 EMERGENCY_EXIT을 선택하지 마십시오. 이는 정상적인 시장 노이즈일 수 있습니다.
- 진짜 덤핑(장대음봉 + 거래량 급증 + 지지선 이탈)이 명백한 경우에만 높은 확신(CONFIDENCE 80 이상)으로 EMERGENCY_EXIT을 결정하십시오.

### [현재 포지션 상태]
- 마켓: {market} | 보유 시간: {hold_min:.1f}분
- 평단가: {avg_buy_price:,.2f} KRW | 현재가: {current_price:,.2f} KRW
- 현재 손익률: {pnl_pct:+.2f}%
- 대장주(BTC) 시황: {btc_context or '정상 안정세'}

### [5분봉 실시간 퀀트 데이터]
- 체결강도: {trade_strength['desc']}
- VWAP: {vwap_info['vwap']:,.2f} KRW ({'🟢 VWAP 상단 지지' if vwap_info['is_above'] else '🔴 VWAP 하단 저항'})
- RSI(14): {rsi_val:.1f} | 볼린저 위치(%B): {bb['pct_b']:.2f}
- 거래량: {vol_info['vol_ratio']}배 ({'🚨 거래량 급증' if vol_info['is_spike'] else '평이'})
- 캔들 패턴: {candle_pattern}
- 호가창 잔량: {ob_info['imbalance_desc']} (매수/매도비: {ob_info['bid_ask_ratio']:.2f})

### [JSON 출력 필수 스키마]
반드시 마크다운 백틱 없이 순수 JSON으로만 응답하세요:
{{
  "ACTION": "HOLD" | "EMERGENCY_EXIT" | "RUNNER_HOLD" | "TIGHTEN_STOP",
  "REASON": "결정 사유 1줄 요약",
  "ADJUSTED_TARGET_PRICE": {round(current_price * 1.05, 2)},
  "ADJUSTED_STOP_LOSS": {round(avg_buy_price * 1.002, 2)},
  "CONFIDENCE": 85
}}
"""
            parsed = self._call_gemini_json(prompt, timeout=8.0)
            if isinstance(parsed, dict):
                act = str(parsed.get("ACTION", "HOLD")).upper()
                if act not in ("HOLD", "EMERGENCY_EXIT", "RUNNER_HOLD", "TIGHTEN_STOP"):
                    act = "HOLD"

                adj_tp = float(parsed.get("ADJUSTED_TARGET_PRICE") or 0.0)
                adj_sl = float(parsed.get("ADJUSTED_STOP_LOSS") or 0.0)
                conf = int(parsed.get("CONFIDENCE") or 70)
                reason_txt = str(parsed.get("REASON", "AI 보유 포지션 평가 완료"))

                res = {
                    "action": act,
                    "reason": f"[AI 포지션 관리] {reason_txt}",
                    "adjusted_target_price": adj_tp if adj_tp > current_price else None,
                    "adjusted_stop_loss": adj_sl if 0 < adj_sl < current_price else None,
                    "confidence": conf,
                }
                if hasattr(self, "_holding_eval_cache") and cache_key:
                    self._holding_eval_cache[cache_key] = {"cached_at": time.time(), "result": res}
                logger.info(f"🤖 [{market}] AI 포지션 진단: {act} ({reason_txt}) | 현재 손익: {pnl_pct:+.2f}%")
                return res
        except Exception as e:
            logger.warning(f"[{market}] evaluate_holding_position 예외: {e}")

        return fallback_res

    def rank_candidate_markets(
        self,
        candidates: list[dict[str, Any]],
        btc_regime: str = "NORMAL",
        btc_change_rate: float = 0.0,
    ) -> list[dict[str, Any]]:
        """
        [2순위] 1차 스크리닝 통과 종목들을 1회 배치(Batch) 프롬프트로 Gemini에게 전달하여
        세력 덤핑/설거지 여부를 여과하고 우선순위 랭킹(Tier 1, 2, 3)을 부여
        """
        if not self.api_key or not candidates or len(candidates) <= 1:
            return candidates

        # 900초(15분) 캐시 (정렬된 상위 종목 리스트 기준)
        cand_keys = ",".join(sorted(c.get("market", "") for c in candidates[:8]))
        cache_key = f"RANK:{cand_keys}"
        now_ts = time.time()
        if hasattr(self, "_screener_rank_cache") and cache_key in self._screener_rank_cache:
            cached = self._screener_rank_cache[cache_key]
            if (now_ts - float(cached.get("cached_at", 0))) < 900.0:
                GeminiTelemetry.record_cache_hit("RANK")
                return list(cached["result"])

        # 개별 종목 점수 캐시(_MARKET_AI_SCORE_CACHE) 활용: 이미 60% 이상 캐시되어 있고 미평가 신규 종목이 2개 이하이면 API 호출 생략 및 캐시 합성
        if hasattr(self, "_MARKET_AI_SCORE_CACHE"):
            valid_cached = {
                m: meta for m, meta in self._MARKET_AI_SCORE_CACHE.items()
                if (now_ts - float(meta.get("cached_at", 0))) < 900.0
            }
            top_candidates = candidates[:8]
            cached_count = sum(1 for c in top_candidates if c.get("market", "").upper() in valid_cached)
            uncached_markets = [c.get("market", "").upper() for c in top_candidates if c.get("market", "").upper() not in valid_cached]
            if len(top_candidates) >= 2 and cached_count > 0 and (cached_count / len(top_candidates)) >= 0.6 and len(uncached_markets) <= 2:
                # 60% 이상 유효 캐시 보유 ➜ API 호출 생략하고 캐시 점수 합성
                GeminiTelemetry.record_cache_hit("RANK")
                logger.info(f"⚡ [Gemini AI 랭킹] 개별 종목 캐시 재사용 (미평가 {len(uncached_markets)}개 ➜ API 호출 생략, 쿼터 보존)")
                tier_order = {"TIER_1": 1, "TIER_2": 2, "TIER_3": 3, "REJECT": 9}
                ranked_candidates = []
                for c in candidates:
                    m = c.get("market", "").upper()
                    meta = valid_cached.get(m, {"ai_rank": 50, "ai_tier": "TIER_2", "ai_score": 50.0, "ai_reason": "기본"})
                    c_copy = dict(c)
                    c_copy.update({
                        "ai_rank": meta.get("ai_rank", 50),
                        "ai_tier": meta.get("ai_tier", "TIER_2"),
                        "ai_score": meta.get("ai_score", 50.0),
                        "ai_reason": meta.get("ai_reason", "캐시 재사용"),
                    })
                    ranked_candidates.append(c_copy)
                ranked_candidates.sort(
                    key=lambda x: (
                        tier_order.get(x.get("ai_tier", "TIER_2"), 5),
                        x.get("ai_rank", 50),
                        -x.get("ai_score", 50),
                    )
                )
                self._screener_rank_cache[cache_key] = {"cached_at": now_ts, "result": ranked_candidates}
                return ranked_candidates

        try:
            cand_rows = []
            for i, c in enumerate(candidates[:10], 1):
                m = c.get("market", "")
                p = float(c.get("trade_price", 0.0))
                chg = float(c.get("change_rate", 0.0)) * 100.0
                tb_krw = float(c.get("acc_trade_price_24h", 0.0)) / 100_000_000.0
                rs = float(c.get("relative_strength", 0.0)) * 100.0
                c_type = c.get("candidate_type", "CONFIRMED")
                cand_rows.append(
                    f"{i}. {m} | 현재가: {p:,.2f}원 | 변동률: {chg:+.2f}% | RS: {rs:+.2f}% | 24h거래대금: {tb_krw:,.0f}억원 | 유형: {c_type}"
                )
            cand_summary = "\n".join(cand_rows)

            prompt = f"""당신은 월가 수석 암호화폐 퀀트 트레이더입니다.
아래는 실시간 1차 수량/거래대금/스프레드 필터를 통과한 후보 암호화폐 목록입니다.
비트코인 시장 상태: {btc_regime} (BTC 24h 변동: {btc_change_rate*100:+.2f}%)

각 종목의 [상대강도(RS)], [24h 거래대금 유동성], [상승률 및 모멘텀 건전성]을 종합 평가하여,
진짜 세력 수급 주도주와 가짜 펌핑/설거지 종목을 선별하고 최우선순위 랭킹을 지정하세요.

### [후보 종목 목록]
{cand_summary}

### [평가 가이드]
- TIER_1: 비트코인 대비 초과 수급(RS > 0), 풍부한 거래대금, 건강한 상승 초입 주도주 (최우선 매수 검토)
- TIER_2: 거래대금 충분한 안정적 추세 종목
- TIER_3: 상대강도 약하거나 변동성 큰 후발주
- REJECT: 무거래량 가짜 반등, 고점 피로도 극심, 설거지성 덤핑 의심 종목

### [JSON 출력 필수 스키마]
반드시 마크다운 백틱 없이 순수 JSON 배열로만 응답하세요:
[
  {{"market": "KRW-XXX", "rank": 1, "tier": "TIER_1", "score": 92, "reason": "거래대금 1위 및 BTC 대비 독자 랠리"}},
  ...
]
"""
            parsed = self._call_gemini_json(prompt, timeout=8.0)
            if isinstance(parsed, list) and len(parsed) > 0:
                rank_map = {}
                for item in parsed:
                    if isinstance(item, dict) and "market" in item:
                        m_code = str(item["market"]).upper()
                        r_info = {
                            "ai_rank": int(item.get("rank", 99)),
                            "ai_tier": str(item.get("tier", "TIER_2")).upper(),
                            "ai_score": float(item.get("score", 50)),
                            "ai_reason": str(item.get("reason", "")),
                        }
                        rank_map[m_code] = r_info
                        if hasattr(self, "_MARKET_AI_SCORE_CACHE"):
                            self._MARKET_AI_SCORE_CACHE[m_code] = dict(r_info, cached_at=now_ts)

                ranked_candidates = []
                for c in candidates:
                    m = c.get("market", "").upper()
                    meta = rank_map.get(m, {"ai_rank": 50, "ai_tier": "TIER_2", "ai_score": 50, "ai_reason": "기본"})
                    c_copy = dict(c)
                    c_copy.update(meta)
                    ranked_candidates.append(c_copy)

                # REJECT가 아닌 종목 우선 및 AI 랭킹 순 정렬
                tier_order = {"TIER_1": 1, "TIER_2": 2, "TIER_3": 3, "REJECT": 9}
                ranked_candidates.sort(
                    key=lambda x: (
                        tier_order.get(x.get("ai_tier", "TIER_2"), 5),
                        x.get("ai_rank", 50),
                        -x.get("ai_score", 50),
                    )
                )

                if hasattr(self, "_screener_rank_cache") and cache_key:
                    self._screener_rank_cache[cache_key] = {"cached_at": now_ts, "result": ranked_candidates}

                logger.info("✨ [Gemini AI 후보 종목 랭킹 완료]")
                for rk, item in enumerate(ranked_candidates[:5], 1):
                    logger.info(
                        f"  #{rk} {item.get('market')} [{item.get('ai_tier')}] 점수: {item.get('ai_score')}점 - {item.get('ai_reason')}"
                    )
                return ranked_candidates
        except Exception as e:
            logger.warning(f"rank_candidate_markets 예외 발생: {e} ➜ 기존 퀀트 순위 유지")

        return candidates

    def diagnose_macro_regime(
        self,
        btc_candles_1h: list[dict[str, Any]],
        btc_candles_4h: list[dict[str, Any]] | None = None,
        fng_index: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        [3순위] BTC 1시간봉/4시간봉 및 공포탐욕 지수를 종합 진단하여 매크로 레짐 및 권장 현금 비중 산출
        - 30분(1800초) 캐시 적용
        """
        now_ts = time.time()
        if (
            hasattr(self, "_macro_diag_cache")
            and self._macro_diag_cache
            and (now_ts - getattr(self, "_last_macro_diag_ts", 0.0) < 1800.0)
        ):
            GeminiTelemetry.record_cache_hit("MACRO")
            return dict(self._macro_diag_cache)

        fallback_diag = {
            "regime": "NORMAL",
            "risk_score": 40,
            "recommended_cash_ratio": 0.3,
            "summary": "BTC 정상 안정세 (로컬 규칙)",
            "action_guideline": "정상적인 퀀트 분할 매매 진행",
        }
        if not self.api_key or not btc_candles_1h or len(btc_candles_1h) < 10:
            return fallback_diag

        try:
            prices_1h = [float(c.get("trade_price", 0.0)) for c in btc_candles_1h]
            cur_btc = prices_1h[0]
            ema20 = self.calculate_ema(prices_1h, min(len(prices_1h), 20))
            ema50 = self.calculate_ema(prices_1h, min(len(prices_1h), 50))
            chg_1h = ((cur_btc - prices_1h[1]) / prices_1h[1] * 100.0) if len(prices_1h) > 1 else 0.0
            chg_24h = (
                ((cur_btc - prices_1h[min(24, len(prices_1h) - 1)]) / prices_1h[min(24, len(prices_1h) - 1)] * 100.0)
                if len(prices_1h) > 5
                else 0.0
            )

            fng_desc = fng_index.get("desc", "50점 (중립)") if fng_index else "중립"

            prompt = f"""당신은 매크로 크립토 헤지펀드 CIO입니다.
비트코인(BTC) 1시간봉 지표와 크립토 시장 심리를 진단하여 거시 레짐과 리스크 수준을 판정하세요:

- BTC 현재가: {cur_btc:,.0f} KRW
- 1시간 이평선: EMA20={ema20:,.0f} | EMA50={ema50:,.0f} ({'🟢 정배열(상승세)' if ema20 >= ema50 else '🔴 역배열(하락세)'})
- 최근 등락률: 1시간 {chg_1h:+.2f}% | 24시간 {chg_24h:+.2f}%
- 공포/탐욕 지수: {fng_desc}

### [레짐 분류 옵션]
- "BULL_TREND": 강력한 상승 추세, 알트코인 적극 매수
- "NORMAL": 안정적 박스권/완만한 상승, 표준 매매
- "CAUTION_PULLBACK": 단기 과열 후 눌림목/조정 경계
- "BEAR_REGIME": 지속적 하락 추세, 방어적 매매 및 비중 축소
- "CRASH": 급락/패닉 셀 진행 중, 신규 매수 전면 차단

### [JSON 출력 필수 스키마]
반드시 마크다운 백틱 없이 순수 JSON으로만 응답하세요:
{{
  "regime": "BULL_TREND" | "NORMAL" | "CAUTION_PULLBACK" | "BEAR_REGIME" | "CRASH",
  "risk_score": 35,
  "recommended_cash_ratio": 0.3,
  "summary": "거시 시장 진단 핵심 요약 1줄",
  "action_guideline": "봇 자금 운용 지침 1줄"
}}
"""
            parsed = self._call_gemini_json(prompt, timeout=8.0)
            if isinstance(parsed, dict) and "regime" in parsed:
                rg = str(parsed.get("regime", "NORMAL")).upper()
                if rg not in ("BULL_TREND", "NORMAL", "CAUTION_PULLBACK", "BEAR_REGIME", "CRASH"):
                    rg = "NORMAL"

                res = {
                    "regime": rg,
                    "risk_score": int(parsed.get("risk_score", 40)),
                    "recommended_cash_ratio": float(parsed.get("recommended_cash_ratio", 0.3)),
                    "summary": str(parsed.get("summary", "AI 매크로 진단 완료")),
                    "action_guideline": str(parsed.get("action_guideline", "정상 운용")),
                }
                self._macro_diag_cache = res
                self._last_macro_diag_ts = now_ts
                logger.info(
                    f"🌍 [Gemini 거시 시황 진단] 레짐: {rg} (위험도: {res['risk_score']}/100) ➜ {res['summary']}"
                )
                return res
        except Exception as e:
            logger.warning(f"diagnose_macro_regime 예외: {e}")

        return fallback_diag

    def generate_market_briefing(
        self,
        exchange_name: str,
        total_equity: float,
        daily_pnl_krw: float,
        daily_pnl_pct: float,
        held_positions_desc: str,
        macro_diag: dict[str, Any],
        fng_desc: str,
    ) -> str:
        """
        [3순위] 텔레그램 모닝/정기 브리핑용 Gemini AI 3줄 종합 해설 생성
        """
        default_comment = f"현재 {exchange_name} 계좌는 {held_positions_desc} 상태이며, 리스크 안전선 내에서 정상 운용 중입니다."
        if not self.api_key:
            return default_comment

        try:
            prompt = f"""당신은 {exchange_name} 전담 AI 퀀트 트레이딩 비서입니다.
아래의 실시간 봇 운용 및 시장 데이터를 바탕으로, 트레이더가 한눈에 시장 흐름과 계좌 상태를 파악할 수 있는 **친절하고 전문적인 3줄 시황 브리핑**을 작성하세요:

- 거래소: {exchange_name}
- 총 평가 자산: {total_equity:,.0f} KRW
- 금일 손익: {daily_pnl_krw:+,.0f} KRW ({daily_pnl_pct:+.2f}%)
- 보유 포지션: {held_positions_desc}
- BTC 거시 레짐: {macro_diag.get('regime', 'NORMAL')} (위험도: {macro_diag.get('risk_score', 40)}/100, 요약: {macro_diag.get('summary', '')})
- 공포/탐욕 심리: {fng_desc}

### [작성 규칙]
1. 불필요한 서론/인사말 생략.
2. 3개의 글머리 기호(•)로 작성.
   • [거시 시황]: BTC 추세와 시장 심리 핵심 요약
   • [계좌 진단]: 현재 포트폴리오 상태 및 손익 평가
   • [전략 제언]: 향후 몇 시간 동안의 안전 운용 지침
3. 반드시 한국어로 정중하고 명확하게 작성.
"""
            # 브리핑 전용: gemini-3.8-flash 및 최신 고성능 모델부터 순차 호출
            models = self.get_briefing_candidate_models(limit=5)
            if not models:
                return default_comment

            for model in models:
                endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
                # 3.7 등 하이브리드 추론 모델은 불필요한 Thinking에 토큰이 소진되어 출력이 잘리지 않도록 thinkingBudget=0 적용
                # 구형 모델과의 호환성을 위해 thinkingConfig를 조건부 적용하며, 400 발생 시 제거 후 1회 폴백 재시도
                supports_thinking_budget = any(k in model.lower() for k in ("3.7", "thinking", "2.5"))
                gen_config: dict[str, Any] = {
                    "temperature": 0.2,
                    "maxOutputTokens": 3000,
                }
                if supports_thinking_budget:
                    gen_config["thinkingConfig"] = {"thinkingBudget": 0}

                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": gen_config,
                }

                try:
                    self._wait_for_rate_limit()
                    resp = requests.post(endpoint, json=payload, timeout=12.0)
                    self._record_http(model, f"{exchange_name}_briefing", response=resp)
                    # 만약 thinkingConfig 미지원으로 HTTP 400 반환 시 thinkingConfig 제거 후 1회 재전송
                    if resp.status_code == 400 and "thinkingConfig" in gen_config:
                        gen_config_no_thinking = dict(gen_config)
                        gen_config_no_thinking.pop("thinkingConfig", None)
                        payload["generationConfig"] = gen_config_no_thinking
                        resp = requests.post(endpoint, json=payload, timeout=12.0)
                        self._record_http(model, f"{exchange_name}_briefing_retry", response=resp)

                    if resp.status_code == 200:
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            cand = candidates[0]
                            finish_reason = cand.get("finishReason", "")
                            if finish_reason == "MAX_TOKENS":
                                logger.warning(f"브리핑 모델 {model} 토큰 한도 도달(MAX_TOKENS)로 텍스트 잘림 감지 ➜ 차순위 모델 전환")
                                continue

                            parts = cand.get("content", {}).get("parts", [])
                            if parts and "text" in parts[0]:
                                text = parts[0]["text"].strip()
                                # 텍스트 완성도 검증: 3개 글머리 기호 중 최소 2개 이상 포함 및 최소 길이 확인
                                has_bullets = (
                                    text.count("•") >= 2
                                    or ("[거시" in text and "[전략" in text)
                                    or ("[거시" in text and "[계좌" in text)
                                )
                                if not has_bullets or len(text) < 40:
                                    logger.warning(f"브리핑 모델 {model} 불완전/미완성 텍스트 응답({len(text)}자) ➜ 차순위 모델 전환")
                                    continue

                                logger.info(f"✨ [{exchange_name}] 09:00 종합 시황 브리핑 생성 성공 (모델: {model})")
                                return text
                    elif resp.status_code in (429, 503):
                        logger.warning(f"브리핑 모델 {model} 일시 제한/오류 (HTTP {resp.status_code}) ➜ 차순위 최신 모델로 순차 전환")
                        self._set_model_cooldown(model, 300.0)
                    elif resp.status_code == 404:
                        logger.debug(f"브리핑 모델 {model} 미지원 (HTTP 404) ➜ 차순위 모델로 순차 전환")
                        self._set_model_blacklist(model, 86400.0)
                    else:
                        logger.debug(f"브리핑 모델 {model} 응답 실패: HTTP {resp.status_code}")
                except Exception as ex:
                    self._record_http(model, f"{exchange_name}_briefing", error_kind="exception")
                    logger.debug(f"브리핑 모델 {model} 호출 예외: {ex}")
                    continue
        except Exception as e:
            logger.debug(f"generate_market_briefing 예외: {e}")

        return default_comment
