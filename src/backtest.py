import argparse
import logging
import math
import sys
from typing import Any

from bithumb_api import BithumbAPI
from order_safety import calculate_risk_position_size
from strategy_engine import calculate_chandelier_exit, entry_signal

# 윈도우 cp949 인코딩 표준화
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, AttributeError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("Backtester")


class QuantBacktester:
    """
    빗썸 2.0 과거 캔들 데이터 기반 비편향(Unbiased) 퀀트 백테스터
    - Next-Bar Open 체결 모델링 (신호 발생 익봉 시가 진입으로 룩어헤드 편향 제거)
    - Pessimistic Intra-Candle Matching (동일 봉 High/Low 충돌 시 손절 우선 판정)
    - 1% Fixed Risk Volatility Position Sizing (ATR 기반 동적 사이징)
    - 손절 후 재진입 쿨다운 (9개 캔들 = 45분) 시뮬레이션
    - 정밀 성과 리포트 (기대수익률, 손익비, MDD, 최대 연속 손실)
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        fee_rate: float = 0.0004,
        slippage_rate: float = 0.001,
        risk_fraction: float = 0.01,
    ):
        self.initial_capital = initial_capital
        self.fee_rate = max(0.0, fee_rate)
        self.slippage_rate = max(0.0, slippage_rate)
        self.risk_fraction = risk_fraction
        self.bithumb = BithumbAPI()

    def run_backtest(
        self,
        market: str = "KRW-BTC",
        unit: int = 5,
        count: int = 200,
    ) -> dict[str, Any]:
        logger.info(f"🧪 [{market}] 과거 {count}개 {unit}분봉 캔들 백테스팅 시작 (초기 자본: {self.initial_capital:,.0f}원)")

        candles = self.bithumb.get_candles(unit=unit, count=count, market=market)
        if not candles or len(candles) < 30:
            logger.warning(f"{market} 캔들 데이터 부족 ({len(candles)}개)")
            return {}

        # 시간순(과거 ➜ 현재) 정렬
        sorted_candles = candles[::-1]

        capital = self.initial_capital
        peak_capital = capital
        max_drawdown = 0.0

        in_position = False
        entry_price = 0.0
        position_vol = 0.0
        partial_tp_done = False
        peak_price = 0.0
        active_target = 0.0
        active_stop = 0.0
        bars_held = 0
        cooldown_until_idx = 0

        pending_entry = False
        pending_signal: dict[str, Any] = {}

        trade_logs = []
        equity_curve = [capital]

        for i in range(25, len(sorted_candles)):
            window = sorted_candles[: i + 1]
            c_candle = window[-1]
            open_price = float(c_candle.get("opening_price", c_candle.get("trade_price", 0.0)))
            cur_price = float(c_candle.get("trade_price", 0.0))
            high_price = float(c_candle.get("high_price", cur_price))
            low_price = float(c_candle.get("low_price", cur_price))

            window_desc = window[::-1]

            # 0. 익봉 시가 진입 처리 (Next-Bar Open Execution)
            if pending_entry and not in_position:
                pending_entry = False
                if i >= cooldown_until_idx:
                    entry_price = open_price * (1.0 + self.slippage_rate)
                    active_target = pending_signal["target_price"]
                    active_stop = pending_signal["stop_loss"]

                    # 1% 고정 리스크 포지션 사이징
                    invest_amt = calculate_risk_position_size(
                        total_equity=capital,
                        entry_price=entry_price,
                        stop_loss=active_stop,
                        risk_fraction=self.risk_fraction,
                        fee_rate=self.fee_rate,
                        slippage_rate=self.slippage_rate,
                        max_position_pct=0.35,
                    )
                    if invest_amt >= 5000.0 and capital >= invest_amt:
                        capital -= invest_amt
                        position_vol = invest_amt * (1.0 - self.fee_rate) / entry_price
                        in_position = True
                        partial_tp_done = False
                        peak_price = entry_price
                        bars_held = 0
                        trade_logs.append({
                            "type": "BUY",
                            "price": entry_price,
                            "target": active_target,
                            "stop": active_stop,
                            "candle_idx": i,
                        })

            # 1. 포지션 보유 중인 경우: 보수적(Pessimistic) 청산 검사
            if in_position and entry_price > 0:
                bars_held += 1
                peak_price = max(peak_price, high_price)

                # A. 손절선 검사 (Pessimistic Rule: 손절을 1순위로 평가하여 동일봉 낙관 왜곡 차단)
                hit_stop = low_price <= active_stop
                hit_tp = (high_price >= active_target) and not partial_tp_done

                # 동일 캔들에서 Stop과 TP가 동시에 닿은 경우 -> 보수적으로 Stop Loss 우선 처리
                if hit_stop:
                    exit_p = min(open_price, active_stop) * (1.0 - self.slippage_rate)
                    loss_pct = ((exit_p - entry_price) / entry_price) * 100.0
                    proceeds = position_vol * exit_p * (1.0 - self.fee_rate)
                    loss_krw = proceeds - (position_vol * entry_price)
                    capital += proceeds
                    in_position = False
                    cooldown_until_idx = i + 9  # 45분(9개 5분봉) 쿨다운
                    trade_logs.append({
                        "type": "STOP_LOSS",
                        "price": exit_p,
                        "pnl_pct": loss_pct,
                        "profit_krw": loss_krw,
                        "candle_idx": i,
                    })
                    position_vol = 0.0
                    continue

                # B. 1차 50% 분할 익절 (ATR 2.0x 목표가 도달 시)
                if hit_tp:
                    partial_tp_done = True
                    sell_vol = position_vol * 0.5
                    exit_price = active_target * (1.0 - self.slippage_rate)
                    realized_val = sell_vol * exit_price * (1.0 - self.fee_rate)
                    capital += realized_val
                    position_vol -= sell_vol
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
                    trade_logs.append({
                        "type": "PARTIAL_TP",
                        "price": exit_price,
                        "pnl_pct": pnl_pct,
                        "profit_krw": realized_val - (sell_vol * entry_price),
                        "candle_idx": i,
                    })
                    # 본절가 방어선 가동 (수수료 보전)
                    active_stop = max(active_stop, entry_price * (1.0 + (2.0 * self.fee_rate) + 0.002))

                # C. 샹들리에 트레일링 스탑
                if partial_tp_done or (cur_price >= entry_price * 1.02):
                    ch_stop = calculate_chandelier_exit(window_desc, period=14, multiplier=1.5)
                    trail_stop_price = max(ch_stop, entry_price * 1.002)
                    if low_price <= trail_stop_price:
                        exit_p = trail_stop_price * (1.0 - self.slippage_rate)
                        pnl_pct = ((exit_p - entry_price) / entry_price) * 100.0
                        proceeds = position_vol * exit_p * (1.0 - self.fee_rate)
                        profit_krw = proceeds - (position_vol * entry_price)
                        capital += proceeds
                        in_position = False
                        cooldown_until_idx = i + 3  # 15분 쿨다운
                        trade_logs.append({
                            "type": "TRAILING_STOP",
                            "price": exit_p,
                            "pnl_pct": pnl_pct,
                            "profit_krw": profit_krw,
                            "candle_idx": i,
                        })
                        position_vol = 0.0
                        continue

                # D. 동적 타임스탑 (16개 봉 = 80분 경과 시 청산)
                if bars_held >= 16:
                    exit_p = cur_price * (1.0 - self.slippage_rate)
                    pnl_pct = ((exit_p - entry_price) / entry_price) * 100.0
                    proceeds = position_vol * exit_p * (1.0 - self.fee_rate)
                    profit_krw = proceeds - (position_vol * entry_price)
                    capital += proceeds
                    in_position = False
                    cooldown_until_idx = i + 3
                    trade_logs.append({
                        "type": "TIME_STOP",
                        "price": exit_p,
                        "pnl_pct": pnl_pct,
                        "profit_krw": profit_krw,
                        "candle_idx": i,
                    })
                    position_vol = 0.0
                    continue

            # 2. 미보유 상태: 퀀트 진입 신호 검사
            elif not in_position and capital >= 10_000 and i >= cooldown_until_idx:
                signal = entry_signal(window_desc)
                if signal["allow_buy"]:
                    # 익봉 시가에 진입하기 위해 예약
                    pending_entry = True
                    pending_signal = signal

            # MDD 추적
            cur_equity = capital + (position_vol * cur_price if in_position else 0.0)
            peak_capital = max(peak_capital, cur_equity)
            dd = (peak_capital - cur_equity) / peak_capital * 100.0 if peak_capital > 0 else 0.0
            max_drawdown = max(max_drawdown, dd)
            equity_curve.append(cur_equity)

        # 미청산 포지션 잔여 가치 합산
        if in_position:
            last_price = float(sorted_candles[-1].get("trade_price", entry_price))
            capital += position_vol * last_price * (1.0 - self.slippage_rate) * (1.0 - self.fee_rate)

        total_return_pct = ((capital - self.initial_capital) / self.initial_capital) * 100.0
        completed_trades = [t for t in trade_logs if t["type"] in ("PARTIAL_TP", "TRAILING_STOP", "STOP_LOSS", "TIME_STOP")]
        win_trades = [t for t in completed_trades if t.get("profit_krw", 0) > 0]
        loss_trades = [t for t in completed_trades if t.get("profit_krw", 0) <= 0]
        win_rate = (len(win_trades) / len(completed_trades) * 100.0) if completed_trades else 0.0

        total_win_krw = sum(t.get("profit_krw", 0) for t in win_trades)
        total_loss_krw = abs(sum(t.get("profit_krw", 0) for t in loss_trades))
        profit_factor = (total_win_krw / total_loss_krw) if total_loss_krw > 0 else (99.9 if total_win_krw > 0 else 1.0)

        # 최대 연속 손실 계산
        max_consecutive_losses = 0
        current_losses = 0
        for t in completed_trades:
            if t.get("profit_krw", 0) <= 0:
                current_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, current_losses)
            else:
                current_losses = 0

        # 1회 거래당 기대수익률 (Expectancy)
        avg_win_pct = (sum(t.get("pnl_pct", 0) for t in win_trades) / len(win_trades)) if win_trades else 0.0
        avg_loss_pct = (abs(sum(t.get("pnl_pct", 0) for t in loss_trades)) / len(loss_trades)) if loss_trades else 0.0
        expectancy_pct = ((win_rate / 100.0) * avg_win_pct) - (((100.0 - win_rate) / 100.0) * avg_loss_pct)

        result = {
            "market": market,
            "candles_tested": len(sorted_candles),
            "initial_capital": self.initial_capital,
            "final_capital": capital,
            "total_return_pct": total_return_pct,
            "max_drawdown_pct": max_drawdown,
            "total_trades": len(completed_trades),
            "win_trades": len(win_trades),
            "loss_trades": len(loss_trades),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "max_consecutive_losses": max_consecutive_losses,
            "expectancy_pct": expectancy_pct,
            "fee_rate": self.fee_rate,
            "slippage_rate": self.slippage_rate,
        }

        self._print_report(result)
        return result

    def _print_report(self, r: dict[str, Any]):
        print("\n" + "=" * 65)
        print(f" 📊 [빗썸 비편향 퀀트 백테스팅 리포트 - {r.get('market', '')}]")
        print("=" * 65)
        print(f"• 테스트 캔들 수: {r.get('candles_tested', 0)}개 캔들")
        print(f"• 초기 투자 자본: {r.get('initial_capital', 0):,.0f} KRW")
        print(f"• 최종 평가 자본: {r.get('final_capital', 0):,.0f} KRW")
        print(f"• <b>총 누적 수익률: {r.get('total_return_pct', 0.0):+.2f}%</b>")
        print(f"• 최대 낙폭(MDD): {r.get('max_drawdown_pct', 0.0):.2f}%")
        print(f"• 총 거래 횟수: {r.get('total_trades', 0)}회 (승리: {r.get('win_trades', 0)}회 / 패배: {r.get('loss_trades', 0)}회)")
        print(f"• 실시간 승률: {r.get('win_rate', 0.0):.1f}%")
        print(f"• 손익비(Profit Factor): {r.get('profit_factor', 0.0):.2f}")
        print(f"• 거래당 기대수익률(Expectancy): {r.get('expectancy_pct', 0.0):+.2f}%")
        print(f"• 최대 연속 손실 횟수: {r.get('max_consecutive_losses', 0)}회")
        print(f"• 비용 가정: 수수료 {r.get('fee_rate', 0.0)*100:.3f}% / 편도 슬리피지 {r.get('slippage_rate', 0.0)*100:.3f}%")
        print(f"• 체결 모델: Next-Bar Open + Pessimistic SL-First Rule + 45분 쿨다운")
        print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="빗썸 퀀트 백테스팅 시뮬레이터")
    parser.add_argument("--market", type=str, default="KRW-BTC", help="테스트할 마켓 (예: KRW-BTC, KRW-ENA)")
    parser.add_argument("--unit", type=int, default=5, help="분봉 단위 (1, 3, 5, 15, 60)")
    parser.add_argument("--count", type=int, default=200, help="캔들 개수 (최대 200)")
    parser.add_argument("--capital", type=float, default=1_000_000.0, help="초기 자본금")
    parser.add_argument("--fee-rate", type=float, default=0.0004, help="편도 수수료율 (기본: 0.04%%)")
    parser.add_argument("--slippage-rate", type=float, default=0.001, help="편도 슬리피지율 (기본: 0.10%%)")
    args = parser.parse_args()

    backtester = QuantBacktester(args.capital, args.fee_rate, args.slippage_rate)
    backtester.run_backtest(market=args.market, unit=args.unit, count=args.count)


if __name__ == "__main__":
    main()
