"""Idempotent fill reconciliation shared across runtime paths."""

from __future__ import annotations

import logging
import time
from typing import Any

from order_safety.journal import OrderJournal
from order_safety.types import OrderStatus

logger = logging.getLogger(__name__)

class OrderFillProcessor:
    """
    공통 체결 처리기 (Execution Reconciler / OrderFillProcessor)
    - “거래소의 주문 접수 ACK는 체결이 아니다. 실제 체결이 확인된 수량(fill_delta)만 포지션과 손익에 반영한다.”
    - Private WebSocket, REST reconcile, 실시간 리스크 엔진, 5분 루프 공통 단일 진입점
    """

    def __init__(
        self,
        order_journal: OrderJournal,
        risk_manager: Any = None,
        trade_memory: Any = None,
        trailing_tracker: Any = None,
        telegram: Any = None,
        send_fill_alerts: bool = False,
        cooldown_manager: Any = None,
    ):
        self.order_journal = order_journal
        self.risk_manager = risk_manager
        self.trade_memory = trade_memory
        self.trailing_tracker = trailing_tracker
        # 청산 쿨다운은 주문 접수 시점이 아닌 확인 체결 증가분에만 기록한다.
        self.cooldown_manager = cooldown_manager
        self.telegram = telegram
        self.send_fill_alerts = send_fill_alerts

    def process_order_fill(
        self,
        order_identifier: str,
        status: str,
        executed_volume: float,
        avg_price: float = 0.0,
        fee: float = 0.0,
        remaining_volume: float = 0.0,
        exchange_uuid: str | None = None,
        exchange_state: str | None = None,
        exit_reason: str | None = None,
        avg_buy_price: float | None = None,
        korean_name: str | None = None,
        timestamp_str: str | None = None,
        expected_price: float | None = None,
        chart_img: bytes | None = None,
    ) -> dict[str, Any]:
        """
        체결 내역을 멱등(Idempotent)하고 단조적(Monotonic)으로 반영하며 슬리피지를 추적하는 단일 체결 처리기 (P0-1, P0-3)
        """
        with self.order_journal._lock:
            order = None
            for o in self.order_journal.orders:
                if (
                    o.get("client_order_id") == order_identifier
                    or (exchange_uuid and o.get("exchange_uuid") == exchange_uuid)
                    or o.get("exchange_order_id") == order_identifier
                ):
                    order = o
                    break

            if not order:
                logger.debug("체결 처리 대상 주문 미발견: %s", order_identifier)
                return {"processed": False, "fill_delta": 0.0, "pnl_krw": 0.0, "status": status}

            client_order_id = order["client_order_id"]
            market = order["market"]
            side = str(order.get("side", "")).lower()
            is_buy = side in ("bid", "buy")
            position_id = order.get("position_id") or market
            stored_exit_reason = order.get("exit_reason") or exit_reason or "MANUAL_EXIT"

            prev_processed_vol = float(order.get("processed_executed_volume", 0.0) or 0.0)
            prev_processed_fee = float(order.get("processed_fee", 0.0) or 0.0)

            fill_delta = max(0.0, float(executed_volume) - prev_processed_vol)
            fee_delta = max(0.0, float(fee) - prev_processed_fee)

            pnl_krw = 0.0
            pnl_pct = 0.0
            effective_price = avg_price if avg_price > 0 else float(order.get("price", 0.0) or 0.0)
            now_str = timestamp_str or time.strftime("%Y-%m-%d %H:%M:%S")

            # 실시간 체결 슬리피지(Slippage Bps) 연산
            exp_p = expected_price or float(order.get("expected_price", 0.0) or order.get("price", 0.0) or 0.0)
            slippage_bps = 0.0
            if exp_p > 0 and effective_price > 0:
                if is_buy:
                    slippage_bps = ((effective_price - exp_p) / exp_p) * 10000.0
                else:
                    slippage_bps = ((exp_p - effective_price) / exp_p) * 10000.0

            if abs(slippage_bps) >= 30.0:
                logger.warning(
                    f"⚠️ [{market}] 슬리피지 편차 감지: {slippage_bps:+.1f} bps (체결단가: {effective_price:,.2f} vs 목표단가: {exp_p:,.2f})"
                )

            # 1. 체결 증가분(fill_delta > 0)에 대해서만 포지션 및 손익 반영
            if fill_delta > 0:
                if is_buy:
                    # 매수 첫 체결 시점에만 진입시간 생성 (P0-1)
                    if prev_processed_vol == 0.0 and self.trailing_tracker:
                        self.trailing_tracker.set_entry_time(market)
                    logger.info(
                        "🛒 [%s] 실제 매수 체결 확인: 증가분=%.6f, 체결단가=%.2f, 수수료=%.2f, 슬리피지=%+.1fbps",
                        market, fill_delta, effective_price, fee_delta, slippage_bps,
                    )

                    # 텔레그램 매수 체결 알림 발송 (체결 기반 실시간 알림)
                    entry_snapshot = dict(order.get("entry_strategy_snapshot") or {})
                    k_name = korean_name or entry_snapshot.get("korean_name") or order.get("korean_name") or market.replace("KRW-", "")
                    tp = entry_snapshot.get("target_price") or order.get("target_price")
                    sl = entry_snapshot.get("stop_loss") or order.get("stop_loss")
                    alpha = entry_snapshot.get("alpha_score")
                    raw_exchange = str(entry_snapshot.get("exchange") or order.get("exchange_name") or "").upper()
                    exchange_tag = "업비트" if "UPBIT" in raw_exchange else "빗썸"
                    entry_reason = entry_snapshot.get("entry_reason") or order.get("entry_reason") or ""
                    currency = market.replace("KRW-", "")
                    total_fill_krw = int(fill_delta * effective_price)

                    is_fully_filled = (status == OrderStatus.FILLED) or (remaining_volume <= 1e-8)
                    status_label = "체결 완료" if is_fully_filled else f"부분 체결 (누적 {prev_processed_vol + fill_delta:.6f})"

                    tp_str = f"• 목표가: {float(tp):,.2f} KRW | 손절가: {float(sl):,.2f} KRW\n" if tp and sl else ""
                    alpha_str = f"• AI 알파 스코어: <b>{alpha}점</b>\n" if alpha is not None else ""
                    reason_str = f"• 진입 사유: <i>{entry_reason}</i>\n" if entry_reason else ""
                    slip_str = f" ({slippage_bps:+.1f} bps)" if abs(slippage_bps) >= 0.1 else ""

                    caption = (
                        f"🛒 <b>[{exchange_tag} {k_name}({market}) 매수 {status_label}!]</b>\n"
                        f"• 실체결 단가: <b>{effective_price:,.2f} KRW</b>{slip_str}\n"
                        f"• 체결 수량: {fill_delta:.6f} {currency}\n"
                        f"• 체결 금액: <b>{total_fill_krw:,d} KRW</b>\n"
                        f"• 지불 수수료: {fee_delta:,.2f} KRW\n"
                        f"{tp_str}"
                        f"{alpha_str}"
                        f"{reason_str}"
                        f"• 주문 ID: <code>{order.get('exchange_uuid') or order.get('exchange_order_id') or exchange_uuid or client_order_id}</code>\n"
                        f"• 체결 일시: {now_str}"
                    )

                    if self.send_fill_alerts and self.telegram:
                        try:
                            if chart_img:
                                self.telegram.send_photo(chart_img, caption=caption)
                            else:
                                self.telegram.send_message(caption)
                        except Exception as te:
                            logger.warning("매수 체결 텔레그램 알림 발송 실패: %s", te)


                else:
                    # 매도 체결: 실제 체결 증가분만 실현 손익 계산 (P0-1, P0-3)
                    effective_avg_buy_price = avg_buy_price or float(order.get("avg_buy_price", 0.0) or 0.0)
                    if effective_price <= 0 or effective_avg_buy_price <= 0:
                        # 0원 체결/진입가는 손익·쿨다운·학습 데이터에 절대 기록하지 않는다.
                        self.order_journal.mark(
                            client_order_id,
                            OrderStatus.RECONCILIATION_PENDING,
                            reconciliation_reason="매도 체결가 또는 진입가가 0 이하",
                            last_event_at=time.time(),
                        )
                        logger.error("🛑 [%s] 0원 체결 데이터로 매도 손익 반영을 차단했습니다.", market)
                        return {"processed": False, "fill_delta": 0.0, "pnl_krw": 0.0, "status": OrderStatus.RECONCILIATION_PENDING}
                    proceeds = (effective_price * fill_delta) - fee_delta
                    cost_basis = effective_avg_buy_price * fill_delta
                    pnl_krw = proceeds - cost_basis
                    pnl_pct = ((effective_price - effective_avg_buy_price) / effective_avg_buy_price * 100.0) if effective_avg_buy_price > 0 else 0.0
                    # 실현 손익 기반 청산 사유 레이블 직관화
                    refined_exit_reason = stored_exit_reason
                    is_win = pnl_krw > 0
                    stored_upper = str(stored_exit_reason).upper()
                    if "TRAILING" in stored_upper or "트레일링" in str(stored_exit_reason):
                        if pnl_krw > 0:
                            refined_exit_reason = "트레일링 익절"
                        elif pnl_pct >= -0.5:
                            refined_exit_reason = "트레일링 본전방어"
                        else:
                            refined_exit_reason = "트레일링 방어매도"
                    elif "MOMENTUM_EARLY_EXIT" in stored_upper or "모멘텀" in str(stored_exit_reason):
                        if pnl_krw >= 0:
                            refined_exit_reason = "모멘텀 조기 본전탈출"
                        else:
                            refined_exit_reason = "모멘텀 소멸 방어탈출"
                    elif "TIME_STOP" in stored_upper or "타임스탑" in str(stored_exit_reason):
                        if pnl_krw > 0:
                            refined_exit_reason = "타임스탑 본전익절"
                        elif pnl_pct >= -0.5:
                            refined_exit_reason = "타임스탑 횡보청산"
                        else:
                            refined_exit_reason = "타임스탑 추세이탈청산"
                    elif "PARTIAL_TP" in stored_upper or "분할익절" in str(stored_exit_reason):
                        refined_exit_reason = "1차 분할익절"
                    elif "STOP_LOSS" in stored_upper or "HARD_STOP" in stored_upper or "손절" in str(stored_exit_reason):
                        refined_exit_reason = "손절 방어"
                    if self.risk_manager:
                        self.risk_manager.add_realized_trade(pnl_krw, is_win=is_win)

                    if self.cooldown_manager:
                        # 부분 체결도 실제 포지션 축소이므로 확인된 체결가만 쿨다운 기준으로 사용한다.
                        self.cooldown_manager.record_exit(
                            market, refined_exit_reason, exit_price=effective_price,
                        )

                    entry_order = self.order_journal.get_entry_order_for_exit(order)
                    entry_snapshot = dict((entry_order or {}).get("entry_strategy_snapshot") or {})
                    # 매도 주문이 구 position_id를 쓰더라도 진입 주문의 고유 ID로 포지션 관계를 보존한다.
                    position_id = (entry_order or {}).get("position_id") or position_id
                    if self.trade_memory:
                        self.trade_memory.record_completed_trade(
                            market=market,
                            side=refined_exit_reason,
                            entry_price=effective_avg_buy_price,
                            exit_price=effective_price,
                            filled_volume=fill_delta,
                            fee=fee_delta,
                            slippage=slippage_bps / 10000.0,
                            pnl_pct=pnl_pct,
                            pnl_krw=pnl_krw,
                            reason=refined_exit_reason,
                            timestamp=now_str,
                            position_id=position_id,
                            order_status=status,
                            exchange=(
                                entry_snapshot.get("exchange")
                                or (entry_order or {}).get("exchange")
                                or getattr(self.trade_memory, "exchange_scope", "")
                                or getattr(self.order_journal, "exchange_scope", "")
                                or "bithumb"
                            ),
                            btc_regime=entry_snapshot.get("entry_btc_regime", "UNKNOWN"),
                            alpha_score=entry_snapshot.get("alpha_score"),
                            indicators=entry_snapshot.get("indicators", {}),
                            entry_btc_regime=entry_snapshot.get("entry_btc_regime", "UNKNOWN"),
                            exit_btc_regime=entry_snapshot.get("exit_btc_regime", "UNKNOWN"),
                            entry_reason=entry_snapshot.get("entry_reason", ""),
                            entry_decision_at=entry_snapshot.get("entry_decision_at", ""),
                            target_price=entry_snapshot.get("target_price"),
                            stop_loss=entry_snapshot.get("stop_loss"),
                        )
                    logger.info(
                        "🎉 [%s] 실제 매도 체결 확인 (%s): 증가분=%.6f, 체결단가=%.2f, 손익=%+.0f원(%.2f%%), 슬리피지=%+.1fbps",
                        market, refined_exit_reason, fill_delta, effective_price, pnl_krw, pnl_pct, slippage_bps,
                    )

                    # [알림 최적화] 거래소 앱 자체 알림 활용을 위해 텔레그램 매도 체결 알림 비활성화
                    pass

            # 2. 완전 청산 상태 도달 시 트레일링 스탑 초기화
            if not is_buy and status in (OrderStatus.FILLED, OrderStatus.CANCELED) and remaining_volume == 0.0:
                if self.trailing_tracker:
                    self.trailing_tracker.clear(market)

            # 3. 저널 상태 영속 업데이트
            update_fields: dict[str, Any] = {
                "executed_volume": executed_volume,
                "processed_executed_volume": executed_volume,
                "remaining_volume": remaining_volume,
                "avg_price": effective_price,
                "fee": fee,
                "processed_fee": fee,
                "slippage_bps": round(slippage_bps, 1),
                "last_event_at": time.time(),
            }
            if exchange_uuid:
                update_fields["exchange_uuid"] = exchange_uuid
            self.order_journal.mark(client_order_id, status, **update_fields)

            return {
                "processed": (fill_delta > 0),
                "fill_delta": fill_delta,
                "fee_delta": fee_delta,
                "pnl_krw": pnl_krw,
                "pnl_pct": pnl_pct,
                "slippage_bps": round(slippage_bps, 1),
                "status": status,
                "order": order,
            }
