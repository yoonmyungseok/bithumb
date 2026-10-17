"""
수동 포지션 스마트 모니터링 알림 (MANUAL_POSITION_OBSERVER)
봇의 매매 격리 원칙을 엄격히 준수하면서, 수동 보유 종목의 급락(-8%) 및
주요 지지선(일봉 EMA20) 이탈 시 참고용 시황 알림을 사용자에게 제공하는 관측 모드.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from position_guard import get_manual_held_markets
from strategy_engine import calculate_ema


class ManualPositionObserver:
    """수동 보유 종목의 변동성을 안전하게 모니터링하고 텔레그램 알림을 발송하는 관측기."""

    def __init__(
        self,
        exchange: Any,
        telegram: Any = None,
        logger: logging.Logger | None = None,
        *,
        enabled: bool = True,
        drop_threshold_pct: float = 0.08,  # 고점 대비 -8.0%
        cooldown_sec: float = 7200.0,       # 동일 종목 2시간 쿨다운 (중복 알림 방지)
    ):
        self.exchange = exchange
        self.telegram = telegram
        self.logger = logger or logging.getLogger("manual_position_observer")
        self.enabled = enabled
        self.drop_threshold_pct = abs(drop_threshold_pct)
        self.cooldown_sec = max(300.0, float(cooldown_sec))
        self._alert_history: dict[str, float] = {}

    def observe_manual_positions(
        self,
        balances: dict[str, Any],
        order_journal: Any = None,
        trailing_tracker: Any = None,
    ) -> list[dict[str, Any]]:
        """
        수동 매수 종목들을 관측하여 고점 대비 급락 또는 지지선 이탈 시 텔레그램 알림을 발송한다.
        봇의 자동 매매(주문 제출/취소/청산)는 절대 간섭하지 않는다.
        """
        if not self.enabled:
            return []

        manual_markets = get_manual_held_markets(balances, order_journal, trailing_tracker)
        if not manual_markets:
            return []

        now = time.time()
        triggered_events: list[dict[str, Any]] = []

        for market in manual_markets:
            # 쿨다운 검사: 최근 알림 후 쿨다운 시간이 지나지 않았으면 스킵
            last_alert = self._alert_history.get(market, 0.0)
            if now - last_alert < self.cooldown_sec:
                continue

            try:
                # 1. 일봉 캔들 조회 (최근 30일)
                candles_daily = self.exchange.get_candles(unit="days", count=30, market=market)
                if not candles_daily or len(candles_daily) < 5:
                    continue

                cur_price = float(candles_daily[0].get("trade_price", 0.0) or 0.0)
                if cur_price <= 0:
                    continue

                # 2. 24시간/최근 2일 고가 대비 낙폭 산정
                recent_high = max(
                    float(c.get("high_price", 0.0) or 0.0)
                    for c in candles_daily[:min(len(candles_daily), 2)]
                )
                drop_from_high = (cur_price - recent_high) / recent_high if recent_high > 0 else 0.0

                # 3. 주요 지지선 (일봉 EMA20) 산정
                closes = [float(c.get("trade_price", 0.0) or 0.0) for c in candles_daily]
                ema20_daily = calculate_ema(closes, min(len(closes), 20))
                is_below_ema20 = (cur_price < ema20_daily) if ema20_daily > 0 else False

                reasons: list[str] = []
                if drop_from_high <= -self.drop_threshold_pct:
                    reasons.append(f"최근 고점({recent_high:,.2f}원) 대비 {drop_from_high*100:.1f}% 급락 감지")
                if is_below_ema20 and len(candles_daily) >= 20:
                    reasons.append(f"주요 지지선(일봉 EMA20 {ema20_daily:,.2f}원) 하향 이탈")

                if not reasons:
                    continue

                # 알림 트리거 성립
                event_data = {
                    "market": market,
                    "current_price": cur_price,
                    "recent_high": recent_high,
                    "drop_from_high_pct": round(drop_from_high * 100.0, 2),
                    "ema20_daily": round(ema20_daily, 2),
                    "is_below_ema20": is_below_ema20,
                    "reasons": reasons,
                }
                triggered_events.append(event_data)
                self._alert_history[market] = now

                # 텔레그램 메시지 구성
                self._send_observation_alert(event_data)

            except Exception as exc:
                self.logger.debug("[%s] 수동 포지션 모니터링 조회 예외: %s", market, exc)

        return triggered_events

    def _send_observation_alert(self, event: dict[str, Any]) -> None:
        """관측 결과를 텔레그램으로 전송한다."""
        market = event["market"]
        cur_p = event["current_price"]
        high_p = event["recent_high"]
        drop_pct = event["drop_from_high_pct"]
        ema20 = event["ema20_daily"]
        is_broken = event["is_below_ema20"]
        reasons_text = "\n".join(f"  • {r}" for r in event["reasons"])

        support_status = f"{ema20:,.2f}원 (하향 이탈 ⚠️)" if is_broken else f"{ema20:,.2f}원 (지지 유지 ✅)"

        msg = (
            f"🔔 [수동 포지션 스마트 모니터링 알림]\n\n"
            f"• 종목: {market}\n"
            f"• 현재가: {cur_p:,.2f}원\n"
            f"• 24H 고점 대비: {drop_pct:+.2f}% (고점 {high_p:,.2f}원)\n"
            f"• 지지선(일봉 EMA20): {support_status}\n\n"
            f"📋 감지 사유:\n{reasons_text}\n\n"
            f"──────────────────\n"
            f"ℹ️ 본 알림은 참고용 시황 관측이며, 봇의 수동 자산 완전 격리 원칙에 따라 자동 매수/매도 등 매매 간섭은 일절 발생하지 않습니다."
        )

        self.logger.info("📢 [%s 수동 포지션 관측 알림] %s", market, ", ".join(event["reasons"]))

        if self.telegram:
            try:
                if hasattr(self.telegram, "send_debounced_message"):
                    self.telegram.send_debounced_message(
                        key=f"manual_obs_{market}",
                        message=msg,
                        debounce_seconds=int(self.cooldown_sec),
                    )
                elif hasattr(self.telegram, "send_message"):
                    self.telegram.send_message(msg)
            except Exception as exc:
                self.logger.warning("[%s] 수동 포지션 텔레그램 발송 실패: %s", market, exc)
