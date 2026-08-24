import datetime
import logging
import threading
import time
from typing import Any

from bithumb_api import BithumbAPI
from order_safety import CooldownManager, OrderJournal, SafeOrderExecutor
from risk_manager import DailyRiskManager, TrailingStopTracker, get_kst_now_str
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager

logger = logging.getLogger(__name__)


class RealtimeRiskEngine:
    """
    0.1초 실시간 웹소켓 체결가 수신 시 즉시 트레일링 스탑, 50% 분할 익절, 칼손절 감시 및 자동 청산 엔진
    """

    def __init__(
        self,
        exchange_factory,
        order_executor: SafeOrderExecutor,
        order_journal: OrderJournal,
        risk_manager: DailyRiskManager,
        cooldown_manager: CooldownManager,
        trade_memory: TradeMemoryManager,
        trailing_tracker: TrailingStopTracker,
        telegram: TelegramAlert,
        min_order_krw: float = 5000.0,
        latest_strategies: dict[str, dict[str, Any]] | None = None,
    ):
        self.get_exchange = exchange_factory
        self.order_executor = order_executor
        self.order_journal = order_journal
        self.risk_manager = risk_manager
        self.cooldown_manager = cooldown_manager
        self.trade_memory = trade_memory
        self.trailing_tracker = trailing_tracker
        self.telegram = telegram
        self.min_order_krw = min_order_krw
        self.latest_strategies = latest_strategies if latest_strategies is not None else {}
        self._lock = threading.Lock()
        self._last_trigger: dict[str, float] = {}

    def cancel_bot_open_orders(self, market: str | None = None) -> int:
        """봇이 발행한 미체결 주문만 선별 취소하여 외부/수동 주문 보호"""
        canceled = 0
        try:
            bithumb = self.get_exchange()
            open_orders = bithumb.get_open_orders(market=market)
            for order in open_orders:
                o_id = order.get("uuid") or order.get("order_id", "")
                if o_id and self.order_journal.is_managed_order(o_id):
                    bithumb.cancel_order(o_id)
                    self.order_journal.mark_by_uuid(o_id, "CANCELED")
                    canceled += 1
        except Exception as e:
            logger.warning(f"미체결 주문 취소 중 오류 ({market}): {e}")
        return canceled

    def clean_stale_orders(self, max_age_seconds: int = 180) -> int:
        """3분(180초) 이상 미체결 봇 주문만 자동 취소 및 예수금 즉시 회수"""
        canceled_count = 0
        try:
            bithumb = self.get_exchange()
            open_orders = bithumb.get_open_orders()
            now_ts = time.time()
            for order in open_orders:
                order_uuid = order.get("uuid") or order.get("order_id", "")
                if not order_uuid or not self.order_journal.is_managed_order(order_uuid):
                    continue

                market = order.get("market", "")
                side = "매수" if order.get("side") == "bid" else "매도"
                created_at_str = order.get("created_at", "")

                try:
                    dt = datetime.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    order_age = now_ts - dt.timestamp()
                except (ValueError, TypeError):
                    order_age = 1000

                if order_age >= max_age_seconds:
                    logger.info(f"🧹 [장기 미체결 봇 주문 청소] {market} {side} 주문 (경과: {order_age/60:.1f}분) 취소 진행")
                    bithumb.cancel_order(order_uuid)
                    self.order_journal.mark_by_uuid(order_uuid, "CANCELED")
                    canceled_count += 1

        except Exception as e:
            logger.warning(f"미체결 주문 청소 중 오류: {e}")

        return canceled_count

    def requote_pending_orders(self) -> int:
        """[스마트 최우선 호가 재정정 (Smart Re-quoter)] 봇 발행 지정가 매수 주문만 선별 재정정"""
        requoted_count = 0
        try:
            bithumb = self.get_exchange()
            open_orders = bithumb.get_open_orders()
            now_ts = time.time()
            for order in open_orders:
                if order.get("side") != "bid":
                    continue

                order_uuid = order.get("uuid") or order.get("order_id", "")
                if not order_uuid or not self.order_journal.is_managed_order(order_uuid):
                    continue

                market = order.get("market", "")
                order_price = float(order.get("price", 0.0))
                order_vol = float(order.get("volume", 0.0))
                created_at_str = order.get("created_at", "")

                try:
                    dt = datetime.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    order_age = now_ts - dt.timestamp()
                except (ValueError, TypeError):
                    order_age = 0

                if 60 <= order_age <= 300:
                    current_price = bithumb.get_current_price(market)
                    if current_price > order_price and current_price <= order_price * 1.008:
                        new_price = bithumb.round_price_to_tick(current_price)
                        logger.info(
                            f"🎛️ [스마트 호가 재정정] {market} 봇 지정가({order_price:,.2f}원) ➜ 최신 체결가({new_price:,.2f}원)로 자동 정정"
                        )
                        bithumb.cancel_order(order_uuid)
                        self.order_journal.mark_by_uuid(order_uuid, "CANCELED")
                        self.order_executor.submit(
                            bithumb,
                            market=market,
                            side="bid",
                            price=new_price,
                            volume=order_vol,
                            ord_type="limit",
                        )
                        requoted_count += 1
        except Exception as e:
            logger.warning(f"스마트 호가 재정정 중 오류: {e}")

        return requoted_count

    def on_price_tick(self, market: str, current_price: float) -> None:
        """0.1초 실시간 웹소켓 체결가 수신 시 즉시 트레일링 스탑 / 손절 감시 및 자동 청산"""
        if current_price <= 0 or not market.startswith("KRW-"):
            return

        now_ts = time.time()
        with self._lock:
            if now_ts - self._last_trigger.get(market, 0.0) < 5.0:
                return

        try:
            bithumb = self.get_exchange()
            currency = market.split("-")[-1]
            balances = bithumb.get_balances()
            coin_info = balances.get(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})
            coin_available = float(coin_info.get("balance", 0.0))
            avg_buy_price = float(coin_info.get("avg_buy_price", 0.0))
            coin_value = coin_available * current_price

            if coin_value < self.min_order_krw or avg_buy_price <= 0:
                return

            korean_name = bithumb.get_korean_name(market)
            strat = self.latest_strategies.get(market, {})
            stop_loss = float(strat.get("STOP_LOSS", 0.0))
            # 전략상 손절가가 0이면 평단가 대비 -3.0% 비상 하한선 기본 적용
            effective_stop_loss = stop_loss if stop_loss > 0 else (avg_buy_price * 0.97)
            now_str = get_kst_now_str()

            # 1. 실시간 손절 검사
            if effective_stop_loss > 0 and current_price <= effective_stop_loss:
                with self._lock:
                    if now_ts - self._last_trigger.get(market, 0.0) < 5.0:
                        return
                    self._last_trigger[market] = now_ts

                logger.warning(
                    f"⚡ [실시간 웹소켓 손절 발동] {korean_name}({market}) 현재가({current_price:,.2f}원) <= 손절가({effective_stop_loss:,.2f}원). 즉시 시장가 매도!"
                )
                self.trailing_tracker.clear(market)
                self.cancel_bot_open_orders(market)

                self.order_executor.submit(
                    bithumb,
                    market=market,
                    side="ask",
                    volume=coin_available,
                    ord_type="market",
                )
                pnl_krw = (current_price - avg_buy_price) * coin_available
                loss_pct = ((current_price - avg_buy_price) / avg_buy_price * 100)
                self.risk_manager.add_realized_trade(pnl_krw, is_win=False)
                self.cooldown_manager.record_exit(market, "STOP_LOSS")
                self.trade_memory.record_completed_trade(
                    market=market,
                    side="STOP_LOSS",
                    entry_price=avg_buy_price,
                    exit_price=current_price,
                    pnl_pct=loss_pct,
                    pnl_krw=pnl_krw,
                    reason=f"0.1초 실시간 웹소켓 즉각 손절 실행 (손절선 {effective_stop_loss:,.2f}원 터치)",
                    timestamp=now_str,
                )
                self.telegram.send_message(
                    f"🚨 <b>[실시간 초저지연 손절 매도 실행]</b>\n"
                    f"• 종목: {korean_name}({market})\n"
                    f"• 체결가: {current_price:,.2f} KRW (손절가: {effective_stop_loss:,.2f} KRW)\n"
                    f"• 손실: {pnl_krw:,.0f} KRW ({loss_pct:.2f}%)\n"
                    f"• 사유: 0.1초 실시간 급락 방어선 즉시 청산\n"
                    f"• 일시: {now_str}"
                )
                return

            # 2. 실시간 50% 분할 익절 & 가속 트레일링 스탑 검사
            action_type, peak_p, _trigger_p, peak_profit_pct, realized_profit_pct = (
                self.trailing_tracker.check_position(market, current_price, avg_buy_price)
            )

            if action_type == "PARTIAL_TP":
                sell_vol = coin_available * 0.5
                sell_val = sell_vol * current_price
                if sell_val >= self.min_order_krw:
                    with self._lock:
                        if now_ts - self._last_trigger.get(market, 0.0) < 5.0:
                            return
                        self._last_trigger[market] = now_ts

                    logger.info(
                        f"⚡ [실시간 1차 50% 분할익절] {korean_name}({market}) 현재가 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 즉시 50% 시장가 익절!"
                    )
                    self.cancel_bot_open_orders(market)

                    self.order_executor.submit(
                        bithumb,
                        market=market,
                        side="ask",
                        volume=sell_vol,
                        ord_type="market",
                    )
                    pnl_krw = (current_price - avg_buy_price) * sell_vol
                    self.risk_manager.add_realized_trade(pnl_krw, is_win=True)
                    self.trade_memory.record_completed_trade(
                        market=market,
                        side="PARTIAL_TP",
                        entry_price=avg_buy_price,
                        exit_price=current_price,
                        pnl_pct=realized_profit_pct,
                        pnl_krw=pnl_krw,
                        reason="0.1초 실시간 1차 +2.5% 도달 50% 분할 익절",
                        timestamp=now_str,
                    )
                    self.telegram.send_message(
                        f"🎉 <b>[실시간 1차 50% 분할익절 체결]</b>\n"
                        f"• 종목: {korean_name}({market})\n"
                        f"• 체결가: {current_price:,.2f} KRW (+{realized_profit_pct:.2f}%)\n"
                        f"• 실현수익: +{pnl_krw:,.0f} KRW 💰\n"
                        f"• 남은 50%: 무한 트레일링 러너 자동 전환\n"
                        f"• 일시: {now_str}"
                    )

            elif action_type == "TRAILING_STOP":
                with self._lock:
                    if now_ts - self._last_trigger.get(market, 0.0) < 5.0:
                        return
                    self._last_trigger[market] = now_ts

                logger.info(
                    f"⚡ [실시간 트레일링 스탑 최고점 익절] {korean_name}({market}) 최고 {peak_p:,.2f}원(+{peak_profit_pct:.2f}%) ➜ 현재 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 즉시 전량 시장가 익절!"
                )
                self.cancel_bot_open_orders(market)

                self.order_executor.submit(
                    bithumb,
                    market=market,
                    side="ask",
                    volume=coin_available,
                    ord_type="market",
                )
                pnl_krw = (current_price - avg_buy_price) * coin_available
                self.risk_manager.add_realized_trade(pnl_krw, is_win=True)
                self.cooldown_manager.record_exit(market, "TRAILING_STOP")
                self.trade_memory.record_completed_trade(
                    market=market,
                    side="TRAILING_STOP",
                    entry_price=avg_buy_price,
                    exit_price=current_price,
                    pnl_pct=realized_profit_pct,
                    pnl_krw=pnl_krw,
                    reason="0.1초 실시간 가속 트레일링 스탑 최고점 익절",
                    timestamp=now_str,
                )
                self.trailing_tracker.clear(market)
                self.telegram.send_message(
                    f"🎯 <b>[실시간 트레일링 스탑 최고점 익절 완료]</b>\n"
                    f"• 종목: {korean_name}({market})\n"
                    f"• 최고가: {peak_p:,.2f} KRW (+{peak_profit_pct:.2f}%)\n"
                    f"• 익절 체결가: {current_price:,.2f} KRW (+{realized_profit_pct:.2f}%)\n"
                    f"• 확정 실현수익: +{pnl_krw:,.0f} KRW 🚀\n"
                    f"• 일시: {now_str}"
                )
        except Exception as e:
            logger.debug(f"실시간 시세 콜백 처리 예외 ({market}): {e}")
