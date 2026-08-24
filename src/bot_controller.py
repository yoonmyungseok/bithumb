import logging
from typing import Any, Callable

from bithumb_api import BithumbAPI
from order_safety import OrderJournal, SafeOrderExecutor
from risk_manager import (
    DailyRiskManager,
    TrailingStopTracker,
    build_positions_data,
    calculate_total_equity,
    get_fear_and_greed_index,
    get_held_markets,
    get_kst_now_str,
)
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager

logger = logging.getLogger(__name__)


class BotController:
    """
    텔레그램 양방향 명령어(/status, /balance, /panic, /pause, /resume) 처리 및
    웹 대시보드 API 데이터 캐시 공급자
    """

    def __init__(
        self,
        exchange_factory: Callable[[], BithumbAPI],
        order_executor: SafeOrderExecutor,
        order_journal: OrderJournal,
        risk_manager: DailyRiskManager,
        trailing_tracker: TrailingStopTracker,
        trade_memory: TradeMemoryManager,
        telegram: TelegramAlert,
        get_is_paused: Callable[[], bool],
        set_is_paused: Callable[[bool], None],
        latest_strategies: dict[str, dict[str, Any]] | None = None,
    ):
        self.get_exchange = exchange_factory
        self.order_executor = order_executor
        self.order_journal = order_journal
        self.risk_manager = risk_manager
        self.trailing_tracker = trailing_tracker
        self.trade_memory = trade_memory
        self.telegram = telegram
        self.get_is_paused = get_is_paused
        self.set_is_paused = set_is_paused
        self.latest_strategies = latest_strategies if latest_strategies is not None else {}
        self.latest_dashboard_data: dict[str, Any] = {
            "total_equity": 0,
            "krw_available": 0,
            "daily_start_equity": 0,
            "daily_pnl_krw": 0,
            "daily_pnl_pct": 0.0,
            "realized_pnl_krw": 0,
            "total_trades": 0,
            "win_trades": 0,
            "win_rate": 0.0,
            "fear_and_greed": "50점 (중립)",
            "bot_state": "🟢 정상 가동 중",
            "positions": [],
            "recent_trades": [],
            "recent_orders": [],
        }

    def cancel_bot_open_orders(self, market: str | None = None) -> int:
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

    def get_status_message(self) -> str:
        now_str = get_kst_now_str()
        try:
            bithumb = self.get_exchange()
            fng = get_fear_and_greed_index()
            balances = bithumb.get_balances()
            total_equity = calculate_total_equity(balances, bithumb)
            krw_avail = balances.get("KRW", {}).get("balance", 0.0)
            daily_pnl_krw = total_equity - self.risk_manager.daily_start_equity
            daily_pnl_pct = (
                (daily_pnl_krw / self.risk_manager.daily_start_equity * 100)
                if self.risk_manager.daily_start_equity > 0
                else 0.0
            )
            held = get_held_markets(balances, bithumb)
            held_str = ", ".join(held) if held else "없음 (100% 현금)"

            state_badge = "⏸️ 일시정지 중 (관망)" if self.get_is_paused() else "🟢 정상 가동 중"

            return (
                f"📊 <b>[빗썸 AI 퀀트 봇 실시간 종합 대시보드]</b>\n\n"
                f"• <b>봇 상태:</b> {state_badge}\n"
                f"• <b>총 평가 자산:</b> {total_equity:,.0f} KRW\n"
                f"• <b>가용 원화 잔고:</b> {krw_avail:,.0f} KRW\n"
                f"• <b>금일 자산 변동:</b> {daily_pnl_krw:+,.0f} KRW ({daily_pnl_pct:+.2f}%)\n"
                f"• <b>금일 확정 실현 손익:</b> {self.risk_manager.realized_pnl_krw:+,.0f} KRW (거래 {self.risk_manager.total_trades_today}회)\n"
                f"• <b>현재 보유 종목:</b> {held_str}\n"
                f"• <b>크립토 공포/탐욕 지수:</b> {fng['desc']}\n"
                f"• <b>웹소켓 스트리밍:</b> ⚡ 0.1초 실시간 체결 감시 가동 중\n"
                f"• <b>웹 대시보드:</b> <code>http://localhost:7979</code>\n"
                f"• <b>조회 일시:</b> {now_str}"
            )
        except Exception as e:
            return f"❌ 상태 조회 실패: {e}"

    def get_balance_message(self) -> str:
        now_str = get_kst_now_str()
        try:
            bithumb = self.get_exchange()
            balances = bithumb.get_balances()
            lines = ["💰 <b>[실시간 계좌 잔고 상세 내역]</b>\n"]
            krw = balances.get("KRW", {})
            lines.append(f"• <b>KRW (원화):</b> {krw.get('balance', 0.0):,.0f}원 (주문중: {krw.get('locked', 0.0):,.0f}원)")

            for cur, info in balances.items():
                if cur in ("KRW", "P"):
                    continue
                bal = info.get("balance", 0.0) + info.get("locked", 0.0)
                if bal > 0:
                    avg_p = info.get("avg_buy_price", 0.0)
                    try:
                        cur_p = bithumb.get_current_price(f"KRW-{cur}")
                        val = bal * cur_p
                        pnl = ((cur_p - avg_p) / avg_p * 100) if avg_p > 0 else 0.0
                        k_name = bithumb.get_korean_name(f"KRW-{cur}")
                        lines.append(f"• <b>{k_name}({cur}):</b> {bal:.6f}개 (평가: {val:,.0f}원 / 수익률: {pnl:+.2f}%)")
                    except Exception:
                        lines.append(f"• <b>{cur}:</b> {bal:.6f}개")
            lines.append(f"\n• 기준 일시: {now_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 잔고 조회 실패: {e}"

    def execute_panic_sell(self) -> str:
        now_str = get_kst_now_str()
        logger.warning("🚨 [긴급 매도 명령 수신!] 전 보유 종목 전량 시장가 매도 진행")
        self.set_is_paused(True)

        try:
            bithumb = self.get_exchange()
            balances = bithumb.get_balances()
            sold_list = []

            for cur, info in balances.items():
                if cur in ("KRW", "P"):
                    continue
                vol = info.get("balance", 0.0)
                if vol <= 0:
                    continue
                market = f"KRW-{cur}"
                try:
                    price = bithumb.get_current_price(market)
                    if vol * price >= 4000.0:
                        self.cancel_bot_open_orders(market)
                        self.order_executor.submit(
                            bithumb,
                            market=market,
                            side="ask",
                            volume=vol,
                            ord_type="market",
                        )
                        k_name = bithumb.get_korean_name(market)
                        sold_list.append(f"{k_name}({cur}) {vol:.4f}개")
                        self.trailing_tracker.clear(market)
                except Exception as e:
                    logger.error(f"긴급 매도 실패 ({cur}): {e}")

            sold_str = ", ".join(sold_list) if sold_list else "매도 대상 없음 (이미 현금 100%)"
            return (
                f"🚨 <b>[긴급 전량 매도 및 100% 현금화 완료]</b>\n\n"
                f"• <b>매도 내역:</b> {sold_str}\n"
                f"• <b>봇 상태:</b> ⏸️ 자동매매 일시정지됨\n"
                f"• <b>일시:</b> {now_str}\n\n"
                f"💡 <i>매매를 다시 시작하려면 <code>/resume</code> 또는 [재개] 버튼을 누르세요.</i>"
            )
        except Exception as e:
            return f"❌ 긴급 매도 실패: {e}"

    def pause_bot(self) -> str:
        self.set_is_paused(True)
        now_str = get_kst_now_str()
        logger.info("⏸️ [원격 제어] 봇 일시정지 (신규 매수 중단)")
        return (
            f"⏸️ <b>[자동매매 일시정지 완료]</b>\n\n"
            f"• 신규 매수가 중단되고 관망 모드로 전환되었습니다.\n"
            f"• 기존 보유 중인 코인의 손절/트레일링 익절은 정상 가동됩니다.\n"
            f"• <b>일시:</b> {now_str}"
        )

    def resume_bot(self) -> str:
        self.set_is_paused(False)
        now_str = get_kst_now_str()
        logger.info("▶️ [원격 제어] 봇 자동매매 정상 재개")
        return (
            f"▶️ <b>[자동매매 정상 재개 완료]</b>\n\n"
            f"• 5분 주기 AI 퀀트 분석 및 신규 급등주 매매가 재개되었습니다.\n"
            f"• <b>일시:</b> {now_str}"
        )

    def get_dashboard_data(self) -> dict[str, Any]:
        """웹 대시보드 프론트엔드 실시간 API 데이터 반환"""
        try:
            bithumb = self.get_exchange()
            fng = get_fear_and_greed_index()
            balances = bithumb.get_balances()
            total_equity = calculate_total_equity(balances, bithumb)
            krw_avail = balances.get("KRW", {}).get("balance", 0.0)

            daily_pnl_krw = total_equity - self.risk_manager.daily_start_equity
            daily_pnl_pct = (
                (daily_pnl_krw / self.risk_manager.daily_start_equity * 100)
                if self.risk_manager.daily_start_equity > 0
                else 0.0
            )

            total_t = self.risk_manager.total_trades_today
            win_t = self.risk_manager.win_trades_today
            win_rate = (win_t / total_t * 100) if total_t > 0 else 0.0

            state_badge = "⏸️ 일시정지 중" if self.get_is_paused() else "🟢 정상 가동 중"

            # 1. 포지션 데이터
            positions_data = build_positions_data(balances, bithumb, self.latest_strategies)

            # 2. 최근 완료 거래 내역
            recent_trades_data = []
            try:
                recent_trades_data = self.trade_memory.get_recent_trades(limit=10)
            except Exception as e:
                logger.debug(f"대시보드 최근 거래 로드 예외: {e}")

            # 3. 주문 저널
            recent_orders_data = []
            try:
                raw_orders = self.order_journal.get_recent_orders(limit=10)
                for o in raw_orders:
                    recent_orders_data.append({
                        "client_order_id": o.get("client_order_id", ""),
                        "market": o.get("market", ""),
                        "side": o.get("side", ""),
                        "status": o.get("status", ""),
                        "price": float(o.get("price", 0.0) or 0.0),
                        "volume": float(o.get("volume", 0.0) or 0.0),
                        "ord_type": o.get("ord_type", "market"),
                        "timestamp": o.get("updated_at") or o.get("created_at", ""),
                    })
            except Exception as e:
                logger.debug(f"대시보드 주문 저널 로드 예외: {e}")

            self.latest_dashboard_data = {
                "total_equity": int(total_equity),
                "krw_available": int(krw_avail),
                "daily_start_equity": int(self.risk_manager.daily_start_equity),
                "daily_pnl_krw": int(daily_pnl_krw),
                "daily_pnl_pct": round(daily_pnl_pct, 2),
                "realized_pnl_krw": int(self.risk_manager.realized_pnl_krw),
                "total_trades": total_t,
                "win_trades": win_t,
                "win_rate": round(win_rate, 1),
                "fear_and_greed": fng["desc"],
                "bot_state": state_badge,
                "positions": positions_data,
                "recent_trades": recent_trades_data,
                "recent_orders": recent_orders_data,
            }
        except Exception as e:
            logger.debug(f"대시보드 데이터 갱신 예외: {e}")

        return self.latest_dashboard_data

    def handle_web_action(self, action: str) -> str:
        """웹 대시보드 원격 버튼 액션 핸들러"""
        if action == "panic":
            return self.execute_panic_sell()
        elif action == "pause":
            return self.pause_bot()
        elif action == "resume":
            return self.resume_bot()
        return "알 수 없는 작업"
