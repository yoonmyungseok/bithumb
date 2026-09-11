"""Groq API 기반 주기적 거시 시장 분석 및 리스크 레짐 관리 서비스입니다.

- 15분 주기로 BTC 다중 타임프레임(1H, 4H), 공포/탐욕 지수, 거래소 동향을 종합 분석합니다.
- 초저지연 Groq Llama 모델을 활용하여 거시 레짐 및 위험도를 산출하고 영속 캐싱합니다.
- 거래소별 격리(bithumb/upbit)를 준수하며, 메인 루프를 방해하지 않도록 백그라운드 비동기 실행을 지원합니다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable

from groq_provider import GroqProvider
from state_store import load_json_with_backup_recovery, write_json_atomically
from strategy_engine import calculate_ema

logger = logging.getLogger(__name__)

# 분석 결과 검증용 필수 스키마
MARKET_INTELLIGENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["regime", "risk_score", "recommended_cash_ratio", "market_summary", "action_guideline"],
    "properties": {
        "regime": {"type": "string"},
        "risk_score": {"type": "integer"},
        "recommended_cash_ratio": {"type": "number"},
        "market_summary": {"type": "string"},
        "action_guideline": {"type": "string"},
    },
}

VALID_REGIMES = {"BULL_TREND", "NORMAL", "CAUTION_PULLBACK", "BEAR_REGIME", "CRASH"}


class MarketIntelligenceService:
    """주기적으로 시장을 종합 분석하여 최신 거시 레짐 및 위험도를 제공하는 서비스입니다."""

    _instances: dict[str, MarketIntelligenceService] = {}
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, exchange_scope: str = "bithumb", data_dir: str | None = None) -> MarketIntelligenceService:
        """거래소별 싱글톤 인스턴스를 반환합니다."""
        scope = exchange_scope.lower()
        with cls._instance_lock:
            if scope not in cls._instances:
                cls._instances[scope] = cls(exchange_scope=scope, data_dir=data_dir)
            return cls._instances[scope]

    def __init__(self, exchange_scope: str = "bithumb", data_dir: str | None = None) -> None:
        self.exchange_scope = exchange_scope.lower()
        base_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", self.exchange_scope)
        self.storage_path = os.path.join(base_dir, "market_intelligence.json")
        self.groq_provider = GroqProvider(exchange_scope=self.exchange_scope)

        self._lock = threading.RLock()
        self._cached_data: dict[str, Any] | None = None
        self._is_updating = False
        self._periodic_thread: threading.Thread | None = None
        self._stop_periodic = threading.Event()

        # 기존 영속 파일이 있으면 로드
        self._load_from_storage()

    def _load_from_storage(self) -> None:
        """디스크에 저장된 최신 분석 결과를 메모리에 적재합니다 (.bak 자동 복구 지원)."""
        try:
            data = load_json_with_backup_recovery(self.storage_path, default=None)
            if isinstance(data, dict) and "regime" in data:
                self._cached_data = data
                logger.info(
                    f"[{self.exchange_scope.upper()}] 기존 시장 분석 캐시 적재: "
                    f"{data.get('regime')} (위험도: {data.get('risk_score')}/100)"
                )
        except Exception as exc:
            logger.debug(f"시장 분석 캐시 파일 로드 예외 (무시): {exc}")

    def _save_to_storage(self, data: dict[str, Any]) -> None:
        """분석 결과를 파일에 원자적으로 안전하게 저장합니다."""
        try:
            write_json_atomically(self.storage_path, data)
        except Exception as exc:
            logger.warning(f"시장 분석 캐시 저장 예외: {exc}")

    def get_latest_intelligence(self, max_age_sec: float = 1200.0) -> dict[str, Any] | None:
        """최신 분석 결과를 반환합니다. 만료되었거나 없으면 None을 반환합니다."""
        with self._lock:
            if not self._cached_data:
                return None
            analyzed_at = float(self._cached_data.get("analyzed_at", 0.0))
            if time.time() - analyzed_at > max_age_sec:
                return None
            return dict(self._cached_data)

    def is_available(self) -> bool:
        """Groq API 사용 가능 여부를 반환합니다."""
        return self.groq_provider.is_available

    def update_intelligence(
        self,
        btc_candles_1h: list[dict[str, Any]] | None = None,
        btc_candles_4h: list[dict[str, Any]] | None = None,
        fng_index: dict[str, Any] | None = None,
        background: bool = True,
    ) -> dict[str, Any] | None:
        """시장 종합 분석을 수행하고 캐시를 갱신합니다."""
        if not self.is_available:
            return None

        if background:
            with self._lock:
                if self._is_updating:
                    return self._cached_data
                self._is_updating = True

            def _worker() -> None:
                try:
                    self._sync_update(btc_candles_1h, btc_candles_4h, fng_index)
                finally:
                    with self._lock:
                        self._is_updating = False

            t = threading.Thread(
                target=_worker,
                daemon=True,
                name=f"{self.exchange_scope}_GroqMarketIntelligenceWorker",
            )
            t.start()
            return self._cached_data

        return self._sync_update(btc_candles_1h, btc_candles_4h, fng_index)

    def _sync_update(
        self,
        btc_candles_1h: list[dict[str, Any]] | None = None,
        btc_candles_4h: list[dict[str, Any]] | None = None,
        fng_index: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """실제 동기 분석을 실행합니다."""
        if not btc_candles_1h or len(btc_candles_1h) < 10:
            logger.debug("시장 분석 생략: BTC 1H 캔들 데이터 부족")
            return None

        try:
            # 1. BTC 1시간 기술 지표 계산
            prices_1h = [float(c.get("trade_price", 0.0)) for c in btc_candles_1h]
            cur_btc = prices_1h[0]
            ema20_1h = calculate_ema(prices_1h, 20)
            ema50_1h = calculate_ema(prices_1h, 50)
            chg_1h = ((cur_btc - prices_1h[1]) / prices_1h[1] * 100.0) if len(prices_1h) > 1 else 0.0
            chg_24h = (
                ((cur_btc - prices_1h[min(24, len(prices_1h) - 1)]) / prices_1h[min(24, len(prices_1h) - 1)] * 100.0)
                if len(prices_1h) > 5
                else 0.0
            )

            # 2. BTC 4시간 기술 지표 계산
            btc_4h_desc = "4시간봉 데이터 없음"
            if btc_candles_4h and len(btc_candles_4h) >= 10:
                prices_4h = [float(c.get("trade_price", 0.0)) for c in btc_candles_4h]
                ema20_4h = calculate_ema(prices_4h, 20)
                ema60_4h = calculate_ema(prices_4h, 60)
                chg_4h = ((cur_btc - prices_4h[1]) / prices_4h[1] * 100.0) if len(prices_4h) > 1 else 0.0
                high_7d = max(prices_4h[:min(len(prices_4h), 42)])
                low_7d = min(prices_4h[:min(len(prices_4h), 42)])
                dd_7d = ((cur_btc - high_7d) / high_7d * 100.0) if high_7d > 0 else 0.0
                rebound_7d = ((cur_btc - low_7d) / low_7d * 100.0) if low_7d > 0 else 0.0
                trend_4h = "🟢 중기 정배열(상승)" if ema20_4h >= ema60_4h else "🔴 중기 역배열(하락)"
                btc_4h_desc = (
                    f"EMA20={ema20_4h:,.0f} | EMA60={ema60_4h:,.0f} ({trend_4h}) | 직전 4H {chg_4h:+.2f}% | "
                    f"최근 7일 고점 대비 {dd_7d:+.2f}%, 저점 대비 반등 {rebound_7d:+.2f}%"
                )

            # 3. 공포/탐욕 심리
            fng_desc = fng_index.get("desc", "50점 (중립)") if fng_index else "중립"

            # 4. Groq 프롬프트 구성
            prompt = f"""당신은 최고 수준의 크립토 퀀트 헤지펀드 거시 시장 분석가입니다.
제공된 비트코인(BTC) 다중 시간대 지표와 시장 심리를 분석하여, 실시간 거시 레짐(Regime)과 리스크 수준을 판정하세요.

[시장 데이터]
- 거래소: {self.exchange_scope.upper()}
- BTC 현재가: {cur_btc:,.0f} KRW
- 1시간 단기 추세: EMA20={ema20_1h:,.0f} | EMA50={ema50_1h:,.0f} ({'🟢 단기 정배열(상승세)' if ema20_1h >= ema50_1h else '🔴 단기 역배열(하락세)'})
- 최근 등락률: 1시간 {chg_1h:+.2f}% | 24시간 {chg_24h:+.2f}%
- 4시간 중기 추세: {btc_4h_desc}
- 공포/탐욕 심리 지수: {fng_desc}

[레짐 판정 기준]
- "BULL_TREND": 강력한 상승 추세 형성, 알트코인 모멘텀 매매 우호적
- "NORMAL": 안정적 박스권 및 완만한 상승, 통상적인 퀀트 분할 매매
- "CAUTION_PULLBACK": 단기 급등 후 과열에 따른 차익실현 눌림목 또는 조정 경계
- "BEAR_REGIME": 주요 이동평균선 역배열 지속 하락 추세, 방어적 매매 및 비중 축소
- "CRASH": 급락 패닉셀 진행 중, 신규 매수 전면 차단 필요

[출력 요구사항 (JSON 필수)]
반드시 마크다운 백틱 없이 순수 JSON 객체로만 응답하세요:
{{
  "regime": "BULL_TREND" | "NORMAL" | "CAUTION_PULLBACK" | "BEAR_REGIME" | "CRASH",
  "risk_score": 45,
  "recommended_cash_ratio": 0.3,
  "market_summary": "반드시 한국어로 현재 거시 시장의 핵심 상황을 명료하게 1줄 요약",
  "action_guideline": "반드시 한국어로 트레이딩 봇 자금 운용 지침을 1줄 요약"
}}
"""
            result = self.groq_provider.complete_json(
                prompt,
                schema=MARKET_INTELLIGENCE_SCHEMA,
                timeout=7.0,
            )

            if result.success and isinstance(result.value, dict):
                parsed = result.value
                regime = str(parsed.get("regime", "NORMAL")).upper()
                if regime not in VALID_REGIMES:
                    regime = "NORMAL"

                intelligence = {
                    "regime": regime,
                    "risk_score": int(parsed.get("risk_score", 50)),
                    "recommended_cash_ratio": float(parsed.get("recommended_cash_ratio", 0.3)),
                    "market_summary": str(parsed.get("market_summary", "거시 시장 분석 완료")),
                    "action_guideline": str(parsed.get("action_guideline", "정상 운용")),
                    "analyzed_at": time.time(),
                    "provider": "groq",
                    "model": result.model,
                    "latency_sec": round(result.latency_sec, 3),
                }

                with self._lock:
                    self._cached_data = intelligence

                self._save_to_storage(intelligence)
                logger.info(
                    f"⚡ [Groq 시장 분석 완료 - {self.exchange_scope.upper()}] 레짐: {regime} "
                    f"(위험도: {intelligence['risk_score']}/100, 지연: {intelligence['latency_sec']}s) ➜ {intelligence['market_summary']}"
                )
                return intelligence

        except Exception as exc:
            logger.warning(f"Groq 시장 분석 수행 중 예외 발생: {exc}")

        return None

    def start_periodic_updater(
        self,
        interval_sec: float = 900.0,
        data_fetcher: Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]] | None = None,
    ) -> None:
        """15분 주기로 시장 분석을 자동 수행하는 백그라운드 스레드를 시작합니다."""
        if not self.is_available:
            logger.info(f"[{self.exchange_scope.upper()}] Groq API 키가 없어 정기 시장 분석 스레드를 시작하지 않습니다.")
            return

        with self._lock:
            if self._periodic_thread and self._periodic_thread.is_alive():
                return

            self._stop_periodic.clear()

            def _loop() -> None:
                logger.info(f"🚀 [{self.exchange_scope.upper()}] Groq 주기적 시장 분석 스레드 가동 (주기: {int(interval_sec)}초)")
                while not self._stop_periodic.is_set():
                    try:
                        if data_fetcher:
                            c1h, c4h, fng = data_fetcher()
                            self.update_intelligence(c1h, c4h, fng, background=False)
                    except Exception as exc:
                        logger.debug(f"정기 시장 분석 루프 예외: {exc}")

                    # 주기 대기
                    self._stop_periodic.wait(interval_sec)

            self._periodic_thread = threading.Thread(
                target=_loop,
                daemon=True,
                name=f"{self.exchange_scope}_GroqPeriodicIntelligence",
            )
            self._periodic_thread.start()

    def stop_periodic_updater(self) -> None:
        """정기 시장 분석 스레드를 정지합니다."""
        self._stop_periodic.set()

