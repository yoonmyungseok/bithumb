import logging
import os
import threading
import time
from typing import Any, Callable

from bithumb_api import BithumbAPI
from order_safety import OrderJournal, SafeOrderExecutor
from risk_manager import (
    DailyRiskManager,
    TrailingStopTracker,
    build_candidates_data,
    build_positions_data,
    calculate_total_equity,
    get_excluded_manual_holdings,
    get_fear_and_greed_index,
    get_held_markets,
    get_kst_now_str,
)
from strategy_engine import StrategyPolicy
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
        exchange_factory: Callable[[], Any],
        order_executor: SafeOrderExecutor,
        order_journal: OrderJournal,
        risk_manager: DailyRiskManager,
        trailing_tracker: TrailingStopTracker,
        trade_memory: TradeMemoryManager,
        telegram: TelegramAlert,
        get_is_paused: Callable[[], bool],
        set_is_paused: Callable[[bool], None],
        latest_strategies: dict[str, dict[str, Any]] | None = None,
        exchange_name: str = "빗썸",
        web_port: int = 7979,
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
        self.exchange_name = exchange_name
        self.web_port = web_port
        self.start_time = time.time()
        self._last_dashboard_fetch_ts: float = 0.0
        self._dashboard_cache_lock = threading.Lock()
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
            "position_win_rate": 0.0,
            "total_positions": 0,
            "kill_switch_active": False,
            "kill_switch_latched_date": "",
            "unknown_orders_count": 0,
            "fear_and_greed": "50점 (중립)",
            "btc_regime": "NORMAL",
            "btc_regime_desc": "🟢 정상장",
            "bot_state": "🟢 정상 가동 중",
            "positions": [],
            "candidates": [],
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
            exchange = self.get_exchange()
            fng = get_fear_and_greed_index()
            balances = exchange.get_balances()
            total_equity = calculate_total_equity(balances, exchange)
            krw_avail = balances.get("KRW", {}).get("balance", 0.0)
            daily_pnl_krw = total_equity - self.risk_manager.daily_start_equity
            daily_pnl_pct = (
                (daily_pnl_krw / self.risk_manager.daily_start_equity * 100)
                if self.risk_manager.daily_start_equity > 0
                else 0.0
            )
            held = get_held_markets(balances, exchange)
            held_str = ", ".join(held) if held else "없음 (100% 현금)"

            state_badge = "⏸️ 일시정지 중 (관망)" if self.get_is_paused() else "🟢 정상 가동 중"

            return (
                f"📊 <b>[{self.exchange_name} AI 퀀트 봇 실시간 종합 대시보드]</b>\n\n"
                f"• <b>봇 상태:</b> {state_badge}\n"
                f"• <b>총 평가 자산:</b> {total_equity:,.0f} KRW\n"
                f"• <b>가용 원화 잔고:</b> {krw_avail:,.0f} KRW\n"
                f"• <b>금일 자산 변동:</b> {daily_pnl_krw:+,.0f} KRW ({daily_pnl_pct:+.2f}%)\n"
                f"• <b>금일 확정 실현 손익:</b> {self.risk_manager.realized_pnl_krw:+,.0f} KRW (거래 {self.risk_manager.total_trades_today}회)\n"
                f"• <b>현재 보유 종목:</b> {held_str}\n"
                f"• <b>크립토 공포/탐욕 지수:</b> {fng['desc']}\n"
                f"• <b>웹소켓 스트리밍:</b> ⚡ 0.1초 실시간 체결 감시 가동 중\n"
                f"• <b>웹 대시보드:</b> <code>http://localhost:{self.web_port}</code>\n"
                f"• <b>조회 일시:</b> {now_str}"
            )
        except Exception as e:
            return f"❌ 상태 조회 실패: {e}"

    def get_balance_message(self) -> str:
        now_str = get_kst_now_str()
        try:
            exchange = self.get_exchange()
            balances = exchange.get_balances()
            lines = [f"💰 <b>[{self.exchange_name} 실시간 계좌 잔고 상세 내역]</b>\n"]
            krw = balances.get("KRW", {})
            lines.append(f"• <b>KRW (원화):</b> {krw.get('balance', 0.0):,.0f}원 (주문중: {krw.get('locked', 0.0):,.0f}원)")

            excluded = get_excluded_manual_holdings()
            for cur, info in balances.items():
                if cur in ("KRW", "P") or cur in excluded or f"KRW-{cur}" in excluded:
                    continue
                bal = info.get("balance", 0.0) + info.get("locked", 0.0)
                if bal > 0:
                    avg_p = info.get("avg_buy_price", 0.0)
                    try:
                        cur_p = exchange.get_current_price(f"KRW-{cur}")
                        val = bal * cur_p
                        pnl = ((cur_p - avg_p) / avg_p * 100) if avg_p > 0 else 0.0
                        k_name = exchange.get_korean_name(f"KRW-{cur}")
                        lines.append(f"• <b>{k_name}({cur}):</b> {bal:.6f}개 (평가: {val:,.0f}원 / 수익률: {pnl:+.2f}%)")
                    except Exception:
                        lines.append(f"• <b>{cur}:</b> {bal:.6f}개")
            lines.append(f"\n• 기준 일시: {now_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 잔고 조회 실패: {e}"

    def execute_panic_sell(self) -> str:
        now_str = get_kst_now_str()
        logger.warning(f"🚨 [{self.exchange_name} 긴급 매도 명령 수신!] 전 보유 종목 전량 시장가 매도 진행 (수동 격리 종목 제외)")
        self.set_is_paused(True)

        try:
            exchange = self.get_exchange()
            balances = exchange.get_balances()
            sold_list = []
            excluded = get_excluded_manual_holdings()

            for cur, info in balances.items():
                if cur in ("KRW", "P") or cur in excluded or f"KRW-{cur}" in excluded:
                    continue
                vol = info.get("balance", 0.0)
                if vol <= 0:
                    continue
                market = f"KRW-{cur}"
                try:
                    price = exchange.get_current_price(market)
                    if vol * price >= 4000.0:
                        self.cancel_bot_open_orders(market)
                        self.order_executor.submit(
                            exchange,
                            market=market,
                            side="ask",
                            volume=vol,
                            ord_type="market",
                            position_id=market,
                            exit_reason="PANIC_SELL",
                        )
                        k_name = exchange.get_korean_name(market)
                        sold_list.append(f"{k_name}({cur}) {vol:.4f}개")
                        self.trailing_tracker.clear(market)
                except Exception as e:
                    logger.error(f"긴급 매도 실패 ({cur}): {e}")

            sold_str = ", ".join(sold_list) if sold_list else "매도 대상 없음 (이미 현금 100%)"
            return (
                f"🚨 <b>[{self.exchange_name} 긴급 전량 매도 및 100% 현금화 완료]</b>\n\n"
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

    def restore_missing_position_strategies(self, held_markets: list[str]) -> int:
        """재시작 직후 보유 중이나 latest_strategies가 없는 종목을 주문 저널 스냅샷으로 복원"""
        restored = 0
        for market in held_markets:
            if market in self.latest_strategies:
                continue
            entry_snapshot = None
            if hasattr(self.order_journal, "get_last_entry_strategy"):
                try:
                    entry_snapshot = self.order_journal.get_last_entry_strategy(market)
                except Exception:
                    pass
            if not entry_snapshot and hasattr(self.order_journal, "orders"):
                try:
                    for o in reversed(self.order_journal.orders):
                        if o.get("market") == market and str(o.get("side", "")).lower() in ("bid", "buy", "매수") and o.get("status") in ("FILLED", "DONE"):
                            entry_snapshot = o.get("entry_strategy_snapshot")
                            if entry_snapshot:
                                break
                except Exception:
                    pass
            if not entry_snapshot:
                continue
            self.latest_strategies[market] = {
                "action": "HOLD",
                "target_price": float(entry_snapshot.get("target_price", 0.0) or 0.0),
                "stop_loss": float(entry_snapshot.get("stop_loss", 0.0) or 0.0),
                "reason": entry_snapshot.get("entry_reason", "기보유 포지션 퀀트 감시"),
                "alpha_score": int(entry_snapshot.get("alpha_score", 70) or 70),
                "indicators": entry_snapshot.get("indicators", {}),
                "allow_buy": False,
                "restored_from_order_journal": True,
            }
            restored += 1
            logger.info("[%s] 재시작 후 주문 저널의 진입 전략을 대시보드에 복원했습니다.", market)
        return restored

    def get_dashboard_data(self) -> dict[str, Any]:
        """웹 대시보드 프론트엔드 실시간 API 데이터 반환 (2.0초 캐싱으로 거래소 Rate Limit 및 지연 방지)"""
        now = time.time()
        if self._last_dashboard_fetch_ts > 0.0 and (now - self._last_dashboard_fetch_ts < 2.0) and self.latest_dashboard_data.get("total_equity", 0) > 0:
            return self.latest_dashboard_data

        if not self._dashboard_cache_lock.acquire(blocking=True, timeout=0.5):
            return self.latest_dashboard_data

        try:
            bithumb = self.get_exchange()
            fng = get_fear_and_greed_index()
            balances = bithumb.get_balances()
            total_equity = calculate_total_equity(balances, bithumb)
            krw_avail = balances.get("KRW", {}).get("balance", 0.0)

            # 장중 입출금 및 초기 입금 기준자산 실시간 보정
            self.risk_manager.adjust_for_current_equity(total_equity)

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

            # 1. 포지션 및 후보군 데이터
            self.restore_missing_position_strategies(get_held_markets(balances, bithumb))
            positions_data = build_positions_data(balances, bithumb, self.latest_strategies)
            candidates_data = build_candidates_data(balances, bithumb, self.latest_strategies)

            # 2. 최근 완료 거래 내역
            recent_trades_data = []
            try:
                raw_trades = self.trade_memory.get_recent_trades(limit=10)
                for t in raw_trades:
                    t_copy = dict(t)
                    m = t.get("market", "")
                    t_copy["korean_name"] = bithumb.get_korean_name(m) if m else ""
                    recent_trades_data.append(t_copy)
            except Exception as e:
                logger.debug(f"대시보드 최근 거래 로드 예외: {e}")

            # 3. 주문 저널
            recent_orders_data = []
            try:
                raw_orders = self.order_journal.get_recent_orders(limit=10)
                for o in raw_orders:
                    m = o.get("market", "")
                    recent_orders_data.append({
                        "client_order_id": o.get("client_order_id", ""),
                        "market": m,
                        "korean_name": bithumb.get_korean_name(m) if m else "",
                        "side": o.get("side", ""),
                        "status": o.get("status", ""),
                        "price": float(o.get("price", 0.0) or 0.0),
                        "avg_price": float(o.get("avg_price", 0.0) or 0.0),
                        "executed_volume": float(o.get("executed_volume", 0.0) or 0.0),
                        "volume": float(o.get("volume", 0.0) or 0.0),
                        "ord_type": o.get("ord_type", "market"),
                        "timestamp": o.get("updated_at") or o.get("created_at", ""),
                    })
            except Exception as e:
                logger.debug(f"대시보드 주문 저널 로드 예외: {e}")

            # 4. 포지션 단위 통합 통계 및 UNKNOWN 주문 수
            pos_stats = self.trade_memory.get_position_level_stats() if hasattr(self.trade_memory, "get_position_level_stats") else {}
            unknown_count = sum(1 for o in self.order_journal.orders if o.get("status") == "UNKNOWN")

            # 5. BTC 시장 레짐 정보
            btc_regime = "NORMAL"
            btc_regime_desc = f"🟢 정상장 (진입 {StrategyPolicy.ALPHA_BUY_THRESHOLD_NORMAL}점+)"
            btc_reason = "BTC 정상 안정세"
            btc_threshold = StrategyPolicy.ALPHA_BUY_THRESHOLD_NORMAL

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
                "position_win_rate": pos_stats.get("position_win_rate_pct", round(win_rate, 1)),
                "total_positions": pos_stats.get("total_positions", total_t),
                "kill_switch_active": self.risk_manager.kill_switch_active,
                "kill_switch_latched_date": getattr(self.risk_manager, "kill_switch_latched_date", ""),
                "unknown_orders_count": unknown_count,
                "fear_and_greed": fng.get("desc", str(fng)) if isinstance(fng, dict) else str(fng),
                "btc_regime": btc_regime,
                "btc_regime_desc": btc_regime_desc,
                "btc_regime_reason": btc_reason,
                "btc_regime_threshold": btc_threshold,
                "bot_state": state_badge,
                "positions": positions_data,
                "candidates": candidates_data,
                "recent_trades": recent_trades_data,
                "recent_orders": recent_orders_data,
            }
            self._last_dashboard_fetch_ts = time.time()
        except Exception as e:
            logger.debug(f"대시보드 데이터 갱신 예외: {e}")
        finally:
            self._dashboard_cache_lock.release()

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

    def get_diagnostics_data(self) -> dict[str, Any]:
        """실시간 원격 시스템 진단 텔레메트리 데이터 반환"""
        now = time.time()
        uptime_sec = int(now - self.start_time)
        hours, rem = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str = f"{hours}시간 {minutes}분 {seconds}초"

        # 최근 완료 거래 슬리피지 통계
        recent_trades = self.trade_memory.get_recent_trades(limit=20) if hasattr(self.trade_memory, "get_recent_trades") else []
        slippages = [abs(float(t.get("slippage", 0.0))) * 10000.0 for t in recent_trades if "slippage" in t]
        avg_slip_bps = round(sum(slippages) / len(slippages), 1) if slippages else 0.0

        excluded = get_excluded_manual_holdings()
        active_threads = threading.active_count()
        unknown_orders = sum(1 for o in self.order_journal.orders if o.get("status") == "UNKNOWN")

        return {
            "exchange": self.exchange_name,
            "pid": os.getpid(),
            "uptime_seconds": uptime_sec,
            "uptime_str": uptime_str,
            "active_threads": active_threads,
            "bot_paused": self.get_is_paused(),
            "kill_switch_active": self.risk_manager.kill_switch_active,
            "kill_switch_latched_date": getattr(self.risk_manager, "kill_switch_latched_date", ""),
            "consecutive_losses": self.risk_manager.consecutive_losses,
            "risk_scale_factor": self.risk_manager.get_risk_scale_factor(),
            "total_trades_today": self.risk_manager.total_trades_today,
            "realized_pnl_krw": self.risk_manager.realized_pnl_krw,
            "avg_slippage_bps": avg_slip_bps,
            "unknown_orders_count": unknown_orders,
            "excluded_holdings": sorted(list(excluded)),
            "web_port": self.web_port,
        }

    def get_diagnostics_message(self) -> str:
        """텔레그램 /diag, /health 회신용 상세 시스템 진단 메시지"""
        diag = self.get_diagnostics_data()
        now_str = get_kst_now_str()
        state_icon = "⏸️ 일시정지" if diag["bot_paused"] else "🟢 정상 가동"
        ks_icon = "🛑 활성화 (매수 차단)" if diag["kill_switch_active"] else "🟢 비활성 (안전)"

        excl_str = ", ".join(diag["excluded_holdings"]) if diag["excluded_holdings"] else "없음"

        return (
            f"🩺 <b>[{self.exchange_name} AI 트레이딩 시스템 정밀 진단 리포트]</b>\n\n"
            f"• <b>운영 상태:</b> {state_icon}\n"
            f"• <b>시스템 Uptime:</b> {diag['uptime_str']} (PID: {diag['pid']})\n"
            f"• <b>활성 스레드:</b> {diag['active_threads']}개 스레드\n"
            f"• <b>일일 킬스위치:</b> {ks_icon}\n"
            f"• <b>연속 손실 횟수:</b> {diag['consecutive_losses']}회 (자본 배율: {diag['risk_scale_factor']*100:.0f}%)\n"
            f"• <b>최근 평균 슬리피지:</b> {diag['avg_slippage_bps']:.1f} bps\n"
            f"• <b>미해결(UNKNOWN) 주문:</b> {diag['unknown_orders_count']}건\n"
            f"• <b>수동 격리 보호 종목:</b> {excl_str}\n"
            f"• <b>웹 대시보드 포트:</b> <code>http://localhost:{diag['web_port']}</code>\n"
            f"• <b>진단 일시:</b> {now_str}"
        )

    def get_trades_summary_message(self) -> str:
        """텔레그램 /trades 회신용 당일 매매 내역 및 슬리피지 요약 메시지"""
        now_str = get_kst_now_str()
        recent = self.trade_memory.get_recent_trades(limit=10) if hasattr(self.trade_memory, "get_recent_trades") else []
        if not recent:
            return f"📋 <b>[{self.exchange_name} 최근 매매 내역]</b>\n\n금일 완료된 청산 거래 내역이 없습니다.\n• 조회 일시: {now_str}"

        lines = [f"📋 <b>[{self.exchange_name} 최근 매매 및 체결 품질 내역]</b>\n"]
        for idx, t in enumerate(recent[:8], start=1):
            m = t.get("market", "")
            reason = t.get("reason", t.get("side", ""))
            pnl_krw = float(t.get("pnl_krw", 0.0))
            pnl_pct = float(t.get("pnl_pct", 0.0))
            slip = float(t.get("slippage", 0.0)) * 10000.0
            icon = "🟢" if pnl_krw > 0 else "🔴"
            lines.append(f"{idx}. {icon} <b>{m}</b> [{reason}]: {pnl_krw:+,.0f}원 ({pnl_pct:+.2f}%) | 슬리피지: {slip:+.1f}bps")

        lines.append(f"\n• <b>금일 누적 실현손익:</b> {self.risk_manager.realized_pnl_krw:+,.0f}원 (총 {self.risk_manager.total_trades_today}회)")
        lines.append(f"• <b>조회 일시:</b> {now_str}")
        return "\n".join(lines)

