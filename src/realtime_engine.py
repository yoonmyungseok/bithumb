import datetime
import logging
import threading
import time
from typing import Any

from bithumb_api import BithumbAPI
from strategy_engine import StrategyPolicy
from order_safety import CooldownManager, OrderFillProcessor, OrderJournal, OrderStatus, SafeOrderExecutor
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
        self.fill_processor = OrderFillProcessor(
            order_journal=self.order_journal,
            risk_manager=self.risk_manager,
            trade_memory=self.trade_memory,
            trailing_tracker=self.trailing_tracker,
            # 실시간 청산도 확인 체결 뒤에만 쿨다운을 기록한다.
            cooldown_manager=self.cooldown_manager,
            telegram=self.telegram,
        )
        self._lock = threading.Lock()
        self._last_trigger: dict[str, float] = {}
        self._sl_hit_count: dict[str, int] = {}
        self._cached_balances: dict[str, Any] = {}
        self._last_balance_ts: float = 0.0
        self._balance_lock = threading.Lock()

    def _get_cached_balances(self, ttl: float = 1.5) -> dict[str, Any]:
        """REST API Rate Limit 방어를 위해 1.5초간 계좌 잔고를 캐싱"""
        now_ts = time.time()
        with self._balance_lock:
            if self._cached_balances and (now_ts - self._last_balance_ts < ttl):
                return self._cached_balances
            try:
                bithumb = self.get_exchange()
                fresh = bithumb.get_balances()
                if fresh:
                    self._cached_balances = fresh
                    self._last_balance_ts = now_ts
                return self._cached_balances
            except Exception as e:
                logger.debug(f"실시간 잔고 조회 예외: {e}")
                return self._cached_balances

    def _invalidate_balance_cache(self) -> None:
        """주문 체결 직후 잔고 캐시 즉시 무효화"""
        with self._balance_lock:
            self._last_balance_ts = 0.0

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
                    current_price = float(bithumb.get_current_price(market))
                    if current_price > order_price and current_price <= order_price * 1.008:
                        if hasattr(bithumb, "adjust_price_to_tick") and callable(getattr(bithumb, "adjust_price_to_tick")):
                            try:
                                adj = bithumb.adjust_price_to_tick(current_price, side="bid")
                                new_price = float(adj)
                            except (TypeError, ValueError):
                                new_price = float(bithumb.round_price_to_tick(current_price)) if hasattr(bithumb, "round_price_to_tick") else current_price
                        elif hasattr(bithumb, "round_price_to_tick") and callable(getattr(bithumb, "round_price_to_tick")):
                            new_price = float(bithumb.round_price_to_tick(current_price))
                        else:
                            new_price = current_price
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
                        # 재호가도 제출 직전 최신가 확인 경계를 통과하게 한다.
                        expected_price=new_price,
                    )
                        requoted_count += 1
        except Exception as e:
            logger.warning(f"스마트 호가 재정정 중 오류: {e}")

        return requoted_count

    def _confirm_and_record_exit(
        self,
        exchange: Any,
        market: str,
        korean_name: str,
        side_label: str,
        order_res: dict[str, Any],
        avg_buy_price: float,
        fallback_price: float,
        fallback_vol: float,
        exit_reason: str,
        now_str: str,
    ) -> dict[str, Any]:
        """주문 접수 후 거래소 체결 조회를 통해 실제 체결가/수수료 기반으로만 손익 확정 (가상 체결/손익 완전 제거, P0-1)"""
        order_uuid = order_res.get("uuid") or order_res.get("order_id") if isinstance(order_res, dict) else None
        client_order_id = order_res.get("client_order_id") if isinstance(order_res, dict) else None
        if not client_order_id and order_uuid:
            ord_obj = self.order_journal.get_order_by_uuid(order_uuid)
            client_order_id = ord_obj.get("client_order_id") if ord_obj else None

        identifier = client_order_id or order_uuid
        if not identifier:
            return {"confirmed": False, "status": OrderStatus.UNKNOWN, "filled_delta": 0.0, "pnl_krw": 0.0, "message": "주문 식별자 부재"}

        exec_price = 0.0
        exec_vol = 0.0
        rem_vol = 0.0
        paid_fee = 0.0
        raw_state = "wait"

        # 1. 거래소 체결 내역 조회 시도
        if order_uuid:
            for attempt in range(2):
                try:
                    time.sleep(0.05 * (attempt + 1))
                    remote = exchange.get_order(order_uuid)
                    if isinstance(remote, dict):
                        raw_state = str(remote.get("state") or remote.get("status") or "").lower()
                        remote_exec_vol = float(remote.get("executed_volume", 0.0) or 0.0)
                        remote_fee = float(remote.get("paid_fee", 0.0) or 0.0)
                        rem_vol = float(remote.get("remaining_volume", 0.0) or 0.0)
                        trades = remote.get("trades", [])
                        if trades:
                            total_funds = sum(float(t.get("price", 0.0)) * float(t.get("volume", 0.0)) for t in trades)
                            total_v = sum(float(t.get("volume", 0.0)) for t in trades)
                            if total_v > 0:
                                exec_price = total_funds / total_v
                                exec_vol = total_v
                        elif remote_exec_vol > 0 and float(remote.get("price", 0.0) or 0.0) > 0:
                            exec_price = float(remote.get("price", 0.0))
                            exec_vol = remote_exec_vol
                        if remote_fee > 0:
                            paid_fee = remote_fee

                        if exec_vol > 0:
                            break
                except Exception as e:
                    logger.debug(f"[{market}] 청산 주문 즉시 조회 예외: {e}")

        # 2. 미체결 또는 조회 실패 시: 절대로 가상 손익을 기록하지 않음 (P0-1)
        if exec_vol <= 0:
            logger.info("⏳ [%s] 주문 접수 완료 (ACK/OPEN). 실제 체결 대기 중 (가상 손익 미생성)", market)
            return {
                "confirmed": False,
                "status": OrderStatus.OPEN if raw_state in ("wait", "watch") else OrderStatus.ACKNOWLEDGED,
                "filled_delta": 0.0,
                "pnl_krw": 0.0,
                "message": "체결 확인 대기",
            }

        # 3. 실제 체결 확인된 경우에만 공통 체결 처리기 호출
        order_status = OrderStatus.FILLED if rem_vol == 0.0 or raw_state == "done" else OrderStatus.PARTIALLY_FILLED
        fill_res = self.fill_processor.process_order_fill(
            order_identifier=identifier,
            status=order_status,
            executed_volume=exec_vol,
            avg_price=exec_price,
            fee=paid_fee,
            remaining_volume=rem_vol,
            exchange_uuid=order_uuid,
            exchange_state=raw_state,
            exit_reason=exit_reason,
            avg_buy_price=avg_buy_price,
            korean_name=korean_name,
            timestamp_str=now_str,
            expected_price=fallback_price,
        )

        pnl_krw = float(fill_res.get("pnl_krw", 0.0) or 0.0)
        pnl_pct = float(fill_res.get("pnl_pct", 0.0) or 0.0)

        return {
            "exec_price": exec_price,
            "exec_vol": exec_vol,
            "pnl_krw": pnl_krw,
            "pnl_pct": pnl_pct,
            "paid_fee": paid_fee,
        }

    def on_price_tick(self, market: str, current_price: float) -> None:
        """0.1초 실시간 웹소켓 체결가 수신 시 즉시 트레일링 스탑 / 손절 감시 및 자동 청산 (P0-2 동시성 락 적용)"""
        if current_price <= 0 or not market.startswith("KRW-"):
            return

        now_ts = time.time()
        with self._lock:
            if now_ts - self._last_trigger.get(market, 0.0) < 5.0:
                return

        # P0-2: 이미 진행 중인 청산 주문이 있거나 다른 스레드에서 청산 중인 경우 중복 청산 차단
        if self.order_journal.has_active_exit_order(market) or self.trailing_tracker.is_exiting(market):
            return

        try:
            bithumb = self.get_exchange()
            currency = market.split("-")[-1]
            balances = self._get_cached_balances(ttl=1.5)
            coin_info = balances.get(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})
            coin_available = float(coin_info.get("balance", 0.0))
            avg_buy_price = float(coin_info.get("avg_buy_price", 0.0))
            coin_value = coin_available * current_price

            if coin_value < self.min_order_krw or avg_buy_price <= 0:
                return

            korean_name = bithumb.get_korean_name(market)
            strat = self.latest_strategies.get(market, {})
            raw_stop_loss = float(strat.get("STOP_LOSS", 0.0))
            # 진입 직후 1틱(-0.3%) 털림 방지: 손절선은 평단가 대비 기본 -1.5% 이하로 안전 마진 보장
            base_stop_loss = avg_buy_price * (1.0 - StrategyPolicy.STOP_LOSS_PCT)
            effective_stop_loss = raw_stop_loss if raw_stop_loss > 0 else base_stop_loss
            effective_stop_loss = min(effective_stop_loss, base_stop_loss)

            # 🛡️ [수익 보존 브레이크이븐]: 1차 분할 익절(+2.5%) 완료 시 손절선을 '평단가 + 0.3%'로 자동 락인
            if self.trailing_tracker.is_breakeven_active(market):
                breakeven_sl = avg_buy_price * (1.0 + StrategyPolicy.BREAKEVEN_STOP_PCT)
                effective_stop_loss = max(effective_stop_loss, breakeven_sl)
            now_str = get_kst_now_str()

            # 0. 단일 종목 절대 손실 하드 스탑 (Hard-Stop Guard: -4.5% 도달 시 틱 카운트 지연 없이 즉각 청산)
            hard_stop_price = avg_buy_price * 0.955
            is_hard_stop = current_price <= hard_stop_price

            # 1. 실시간 손절 검사 (단일 틱 휩소 방지: 2회 연속 하회 또는 급락 -2.5% 이하, 또는 하드스탑 -4.5% 시 즉시 실행)
            if (effective_stop_loss > 0 and current_price <= effective_stop_loss) or is_hard_stop:
                with self._lock:
                    self._sl_hit_count[market] = self._sl_hit_count.get(market, 0) + 1
                    is_severe_drop = current_price <= (avg_buy_price * 0.975)
                    if not is_hard_stop and not is_severe_drop and self._sl_hit_count[market] < 2:
                        logger.debug(f"⚠️ [{market}] 1차 손절선 터치 ({current_price:,.2f}원 <= {effective_stop_loss:,.2f}원) - 휩소 확인 중")
                        return

                    if now_ts - self._last_trigger.get(market, 0.0) < 5.0:
                        return
                    self._last_trigger[market] = now_ts
                    self._sl_hit_count[market] = 0

                # P0-2: 원자적 청산 락 획득
                if not self.trailing_tracker.acquire_exit_lock(market):
                    return

                try:
                    stop_type = "절대 하드스탑(Hard-Stop)" if is_hard_stop else "손절"
                    logger.warning(
                        f"⚡ [실시간 웹소켓 {stop_type} 발동] {korean_name}({market}) 현재가({current_price:,.2f}원) <= 기준선({hard_stop_price if is_hard_stop else effective_stop_loss:,.2f}원). 즉시 시장가 매도!"
                    )
                    self.trailing_tracker.clear(market)
                    self.cancel_bot_open_orders(market)

                    ex_name = "bithumb" if hasattr(bithumb, "get_candlestick") else "upbit"
                    order_res = self.order_executor.submit(
                        bithumb,
                        market=market,
                        side="ask",
                        volume=coin_available,
                        ord_type="market",
                        position_id=market,
                        expected_price=current_price,
                        exit_reason=f"0.1초 실시간 웹소켓 {stop_type} 실행",
                        avg_buy_price=avg_buy_price,
                        exchange_name=ex_name,
                    )
                    self._invalidate_balance_cache()
                    self.cooldown_manager.record_exit(market, "STOP_LOSS", exit_price=current_price)

                    res_data = self._confirm_and_record_exit(
                        exchange=bithumb,
                        market=market,
                        korean_name=korean_name,
                        side_label="STOP_LOSS",
                        order_res=order_res,
                        avg_buy_price=avg_buy_price,
                        fallback_price=current_price,
                        fallback_vol=coin_available,
                        exit_reason=f"0.1초 실시간 웹소켓 손절 실행 (손절선 {effective_stop_loss:,.2f}원 터치)",
                        now_str=now_str,
                    )

                    # [알림 최적화] 거래소 앱 자체 알림 활용을 위해 텔레그램 실시간 손절 알림 비활성화
                    pass
                finally:
                    self.trailing_tracker.release_exit_lock(market)
                return
            else:
                # 손절선 위로 복귀 시 틱 카운터 리셋
                with self._lock:
                    if market in self._sl_hit_count:
                        self._sl_hit_count[market] = 0

            # 2. 실시간 3단계 분할 익절 & 가속 트레일링 스탑 검사
            action_type, peak_p, _trigger_p, peak_profit_pct, realized_profit_pct = (
                self.trailing_tracker.check_position(market, current_price, avg_buy_price)
            )

            if action_type in ("PARTIAL_TP", "PARTIAL_TP_1", "PARTIAL_TP_2"):
                is_stage2 = (action_type == "PARTIAL_TP_2")
                # 1차: 보유의 30% 매도 | 2차: 잔여의 42.85% (원금 대비 30%) 매도 ➔ 최종 40% 잔여 러너 유지
                sell_ratio = (30.0 / 70.0) if is_stage2 else StrategyPolicy.PARTIAL_TP_1_RATIO
                sell_vol = coin_available * sell_ratio
                sell_val = sell_vol * current_price
                if sell_val >= self.min_order_krw:
                    with self._lock:
                        if now_ts - self._last_trigger.get(market, 0.0) < 5.0:
                            return
                        self._last_trigger[market] = now_ts

                    if not self.trailing_tracker.acquire_exit_lock(market):
                        return

                    try:
                        stage_label = (
                            f"2차 +{StrategyPolicy.PARTIAL_TP_2_PCT*100:.1f}%(30%)"
                            if is_stage2
                            else f"1차 +{StrategyPolicy.PARTIAL_TP_1_PCT*100:.1f}%(30%)"
                        )
                        logger.info(
                            f"⚡ [실시간 {stage_label} 분할익절] {korean_name}({market}) 현재가 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 시장가 분할 익절!"
                        )
                        self.cancel_bot_open_orders(market)

                        ex_name = "bithumb" if hasattr(bithumb, "get_candlestick") else "upbit"
                        order_res = self.order_executor.submit(
                            bithumb,
                            market=market,
                            side="ask",
                            volume=sell_vol,
                            ord_type="market",
                            position_id=market,
                            expected_price=current_price,
                            exit_reason=f"0.1초 실시간 {stage_label} 도달 분할 익절",
                            avg_buy_price=avg_buy_price,
                            exchange_name=ex_name,
                        )
                        self._invalidate_balance_cache()

                        res_data = self._confirm_and_record_exit(
                            exchange=bithumb,
                            market=market,
                            korean_name=korean_name,
                            side_label=f"PARTIAL_TP_{2 if is_stage2 else 1}",
                            order_res=order_res,
                            avg_buy_price=avg_buy_price,
                            fallback_price=current_price,
                            fallback_vol=sell_vol,
                            exit_reason=f"0.1초 실시간 {stage_label} 도달 분할 익절",
                            now_str=now_str,
                        )

                        be_note = "🛡️ 본전 보장(Break-Even +0.3%) 스탑 활성화" if not is_stage2 else "🚀 잔여 40% 대세 트레일링 러너 추종"
                        # [알림 최적화] 거래소 앱 자체 알림 활용을 위해 텔레그램 실시간 분할익절 알림 비활성화
                        pass
                    finally:
                        self.trailing_tracker.release_exit_lock(market)

            elif action_type == "TRAILING_STOP":
                with self._lock:
                    if now_ts - self._last_trigger.get(market, 0.0) < 5.0:
                        return
                    self._last_trigger[market] = now_ts

                if not self.trailing_tracker.acquire_exit_lock(market):
                    return

                try:
                    logger.info(
                        f"⚡ [실시간 트레일링 스탑 최고점 익절] {korean_name}({market}) 최고 {peak_p:,.2f}원(+{peak_profit_pct:.2f}%) ➜ 현재 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 즉시 전량 시장가 익절!"
                    )
                    self.cancel_bot_open_orders(market)

                    ex_name = "bithumb" if hasattr(bithumb, "get_candlestick") else "upbit"
                    order_res = self.order_executor.submit(
                        bithumb,
                        market=market,
                        side="ask",
                        volume=coin_available,
                        ord_type="market",
                        position_id=market,
                        expected_price=current_price,
                        exit_reason="0.1초 실시간 최고점 대비 트레일링 스탑 익절",
                        avg_buy_price=avg_buy_price,
                        exchange_name=ex_name,
                    )
                    self._invalidate_balance_cache()
                    self.cooldown_manager.record_exit(market, "TRAILING_STOP", exit_price=current_price)

                    res_data = self._confirm_and_record_exit(
                        exchange=bithumb,
                        market=market,
                        korean_name=korean_name,
                        side_label="TRAILING_STOP",
                        order_res=order_res,
                        avg_buy_price=avg_buy_price,
                        fallback_price=current_price,
                        fallback_vol=coin_available,
                        exit_reason="0.1초 실시간 최고점 대비 트레일링 스탑 익절",
                        now_str=now_str,
                    )

                    pnl_krw = res_data['pnl_krw']
                    pnl_pct = res_data['pnl_pct']

                    if pnl_krw > 0:
                        header = "🏆 <b>[실시간 트레일링 스탑 전량 익절 완료]</b>"
                        pnl_line = f"• 확정 수익: +{pnl_krw:,.0f} KRW 💰"
                    elif pnl_pct >= -0.5:
                        header = "🛡️ <b>[실시간 트레일링 스탑 본전 방어 완료]</b>"
                        pnl_line = f"• 실현 손익: {pnl_krw:+,.0f} KRW (수수료/슬리피지 본전 방어)"
                    else:
                        header = "🛑 <b>[실시간 트레일링 스탑 방어 매도 완료]</b>"
                        pnl_line = f"• 실현 손익: {pnl_krw:+,.0f} KRW (고점 꺾임 후 비상 탈출)"

                    # [알림 최적화] 거래소 앱 자체 알림 활용을 위해 텔레그램 실시간 트레일링 익절 알림 비활성화
                    pass
                finally:
                    self.trailing_tracker.release_exit_lock(market)
        except Exception as e:
            logger.debug(f"실시간 시세 콜백 처리 예외 ({market}): {e}")
