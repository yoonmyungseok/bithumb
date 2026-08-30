"""Presentation-only formatting for bot-controller query results."""

from __future__ import annotations

from typing import Any


class ControllerPresenter:
    """Keeps transport-ready Korean text outside the trading controller."""

    @staticmethod
    def diagnostics_message(diag: dict[str, Any], now_str: str) -> str:
        state_icon = "⏸️ 일시정지" if diag["bot_paused"] else "🟢 정상 가동"
        kill_switch_icon = "🛑 활성화 (매수 차단)" if diag["kill_switch_active"] else "🟢 비활성 (안전)"
        excluded = ", ".join(diag["excluded_holdings"]) if diag["excluded_holdings"] else "없음"
        return (
            f"🩺 <b>[{diag['exchange']} AI 트레이딩 시스템 정밀 진단 리포트]</b>\n\n"
            f"• <b>운영 상태:</b> {state_icon}\n"
            f"• <b>시스템 Uptime:</b> {diag['uptime_str']} (PID: {diag['pid']})\n"
            f"• <b>활성 스레드:</b> {diag['active_threads']}개 스레드\n"
            f"• <b>일일 킬스위치:</b> {kill_switch_icon}\n"
            f"• <b>연속 손실 횟수:</b> {diag['consecutive_losses']}회 (자본 배율: {diag['risk_scale_factor']*100:.0f}%)\n"
            f"• <b>최근 평균 슬리피지:</b> {diag['avg_slippage_bps']:.1f} bps\n"
            f"• <b>미해결(UNKNOWN) 주문:</b> {diag['unknown_orders_count']}건\n"
            f"• <b>수동 격리 보호 종목:</b> {excluded}\n"
            f"• <b>웹 대시보드 포트:</b> <code>http://localhost:{diag['web_port']}</code>\n"
            f"• <b>진단 일시:</b> {now_str}"
        )

    @staticmethod
    def trades_summary(exchange_name: str, trades: list[dict[str, Any]], realized_pnl: float, total_trades: int, now_str: str) -> str:
        if not trades:
            return f"📋 <b>[{exchange_name} 최근 매매 내역]</b>\n\n금일 완료된 청산 거래 내역이 없습니다.\n• 조회 일시: {now_str}"
        lines = [f"📋 <b>[{exchange_name} 최근 매매 및 체결 품질 내역]</b>\n"]
        for index, trade in enumerate(trades[:8], start=1):
            pnl_krw = float(trade.get("pnl_krw", 0.0))
            pnl_pct = float(trade.get("pnl_pct", 0.0))
            slippage_bps = float(trade.get("slippage", 0.0)) * 10000.0
            icon = "🟢" if pnl_krw > 0 else "🔴"
            lines.append(f"{index}. {icon} <b>{trade.get('market', '')}</b> [{trade.get('reason', trade.get('side', ''))}]: {pnl_krw:+,.0f}원 ({pnl_pct:+.2f}%) | 슬리피지: {slippage_bps:+.1f}bps")
        lines.append(f"\n• <b>금일 누적 실현손익:</b> {realized_pnl:+,.0f}원 (총 {total_trades}회)")
        lines.append(f"• <b>조회 일시:</b> {now_str}")
        return "\n".join(lines)
