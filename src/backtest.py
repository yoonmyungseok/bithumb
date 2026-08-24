import argparse
import logging
import sys
from typing import Any

from bithumb_api import BithumbAPI
from strategy_engine import entry_signal

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
    빗썸 2.0 과거 캔들 데이터 기반 퀀트 전략 백테스터
    - MTF 상위 추세 필터 + 5분봉 볼린저/RSI + ATR 변동성 손익비
    - 50% 분할 익절(+2.5%) + 본절가 방어선 + 가속 트레일링 스탑
    - 누적 수익률, MDD(최대 낙폭), 승률, 손익비 정밀 시뮬레이션
    """

    def __init__(self, initial_capital: float = 1_000_000.0, fee_rate: float = 0.0, slippage_rate: float = 0.0):
        self.initial_capital = initial_capital
        self.fee_rate = max(0.0, fee_rate)
        self.slippage_rate = max(0.0, slippage_rate)
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

        trade_logs = []
        equity_curve = [capital]

        for i in range(25, len(sorted_candles)):
            window = sorted_candles[: i + 1]
            c_candle = window[-1]
            cur_price = float(c_candle.get("trade_price", 0.0))
            high_price = float(c_candle.get("high_price", cur_price))
            low_price = float(c_candle.get("low_price", cur_price))

            window_desc = window[::-1]

            # 1. 포지션 보유 중인 경우: 익절/손절/트레일링 검사
            if in_position and entry_price > 0:
                peak_price = max(peak_price, high_price)
                pnl_rate = (cur_price - entry_price) / entry_price

                # A. 1차 50% 분할 익절 (+2.5%)
                if (high_price - entry_price) / entry_price >= 0.025 and not partial_tp_done:
                    partial_tp_done = True
                    sell_vol = position_vol * 0.5
                    exit_price = entry_price * 1.025 * (1.0 - self.slippage_rate)
                    realized_val = sell_vol * exit_price * (1.0 - self.fee_rate)
                    capital += realized_val
                    position_vol -= sell_vol
                    trade_logs.append({
                        "type": "PARTIAL_TP",
                        "price": exit_price,
                        "pnl_pct": 2.5,
                        "profit_krw": realized_val - sell_vol * entry_price,
                        "candle_idx": i,
                    })

                # B. 가속 트레일링 스탑
                if partial_tp_done or pnl_rate >= 0.02:
                    peak_pnl = (peak_price - entry_price) / entry_price
                    drop_rate = 0.005 if peak_pnl >= 0.10 else (0.008 if peak_pnl >= 0.05 else 0.012)
                    trail_stop_price = max(peak_price * (1.0 - drop_rate), entry_price * 1.002)

                    if low_price <= trail_stop_price:
                        exit_p = trail_stop_price * (1.0 - self.slippage_rate)
                        pnl_pct = ((exit_p - entry_price) / entry_price) * 100.0
                        proceeds = position_vol * exit_p * (1.0 - self.fee_rate)
                        profit_krw = proceeds - position_vol * entry_price
                        capital += proceeds
                        in_position = False
                        trade_logs.append({
                            "type": "TRAILING_STOP",
                            "price": exit_p,
                            "pnl_pct": pnl_pct,
                            "profit_krw": profit_krw,
                            "candle_idx": i,
                        })
                        position_vol = 0.0
                        continue

                # C. 손절 검사
                if low_price <= active_stop:
                    exit_p = active_stop * (1.0 - self.slippage_rate)
                    loss_pct = ((exit_p - entry_price) / entry_price) * 100.0
                    proceeds = position_vol * exit_p * (1.0 - self.fee_rate)
                    loss_krw = proceeds - position_vol * entry_price
                    capital += proceeds
                    in_position = False
                    trade_logs.append({
                        "type": "STOP_LOSS",
                        "price": exit_p,
                        "pnl_pct": loss_pct,
                        "profit_krw": loss_krw,
                        "candle_idx": i,
                    })
                    position_vol = 0.0
                    continue

            # 2. 미보유 상태: 퀀트 진입 신호 검사
            elif not in_position and capital >= 10_000:
                signal = entry_signal(window_desc)

                # 진입 조건: 골든크로스 + RSI 45~65 건전 구간 + 볼린저 %B 눌림목(0.3~0.7)
                if signal["allow_buy"]:
                    entry_price = cur_price * (1.0 + self.slippage_rate)
                    invest_amt = capital * 0.4  # 40% 비중
                    capital -= invest_amt
                    position_vol = invest_amt * (1.0 - self.fee_rate) / entry_price
                    in_position = True
                    partial_tp_done = False
                    peak_price = cur_price
                    active_target = signal["target_price"]
                    active_stop = signal["stop_loss"]

                    trade_logs.append({
                        "type": "BUY",
                        "price": cur_price,
                        "target": active_target,
                        "stop": active_stop,
                        "candle_idx": i,
                    })

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
        completed_trades = [t for t in trade_logs if t["type"] in ("PARTIAL_TP", "TRAILING_STOP", "STOP_LOSS")]
        win_trades = [t for t in completed_trades if t.get("profit_krw", 0) > 0]
        loss_trades = [t for t in completed_trades if t.get("profit_krw", 0) <= 0]
        win_rate = (len(win_trades) / len(completed_trades) * 100.0) if completed_trades else 0.0

        total_win_krw = sum(t.get("profit_krw", 0) for t in win_trades)
        total_loss_krw = abs(sum(t.get("profit_krw", 0) for t in loss_trades))
        profit_factor = (total_win_krw / total_loss_krw) if total_loss_krw > 0 else (99.9 if total_win_krw > 0 else 1.0)

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
            "fee_rate": self.fee_rate,
            "slippage_rate": self.slippage_rate,
        }

        self._print_report(result)
        return result

    def _print_report(self, r: dict[str, Any]):
        print("\n" + "=" * 60)
        print(f" 📊 [빗썸 퀀트 백테스팅 결과 리포트 - {r.get('market', '')}]")
        print("=" * 60)
        print(f"• 테스트 캔들 수: {r.get('candles_tested', 0)}개 캔들")
        print(f"• 초기 투자 자본: {r.get('initial_capital', 0):,.0f} KRW")
        print(f"• 최종 평가 자본: {r.get('final_capital', 0):,.0f} KRW")
        print(f"• <b>총 누적 수익률: {r.get('total_return_pct', 0.0):+.2f}%</b>")
        print(f"• 최대 낙폭(MDD): {r.get('max_drawdown_pct', 0.0):.2f}%")
        print(f"• 총 거래 횟수: {r.get('total_trades', 0)}회 (승리: {r.get('win_trades', 0)}회 / 패배: {r.get('loss_trades', 0)}회)")
        print(f"• 실시간 승률: {r.get('win_rate', 0.0):.1f}%")
        print(f"• 손익비(Profit Factor): {r.get('profit_factor', 0.0):.2f}")
        print(f"• 비용 가정: 수수료 {r.get('fee_rate', 0.0)*100:.3f}% / 편도 슬리피지 {r.get('slippage_rate', 0.0)*100:.3f}%")
        print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="빗썸 퀀트 백테스팅 시뮬레이터")
    parser.add_argument("--market", type=str, default="KRW-BTC", help="테스트할 마켓 (예: KRW-BTC, KRW-ENA)")
    parser.add_argument("--unit", type=int, default=5, help="분봉 단위 (1, 3, 5, 15, 60)")
    parser.add_argument("--count", type=int, default=200, help="캔들 개수 (최대 200)")
    parser.add_argument("--capital", type=float, default=1_000_000.0, help="초기 자본금")
    parser.add_argument("--fee-rate", type=float, default=0.0, help="편도 수수료율 (예: 0.0004)")
    parser.add_argument("--slippage-rate", type=float, default=0.0, help="편도 슬리피지율 (예: 0.001)")
    args = parser.parse_args()

    backtester = QuantBacktester(args.capital, args.fee_rate, args.slippage_rate)
    backtester.run_backtest(market=args.market, unit=args.unit, count=args.count)


if __name__ == "__main__":
    main()
