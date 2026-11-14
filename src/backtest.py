import argparse
import logging
import sys
from typing import Any
from bithumb_api import BithumbAPI
from order_safety import calculate_partial_take_profit_volume, calculate_risk_position_size
from strategy_engine import StrategyPolicy, calculate_chandelier_exit, classify_btc_regime, entry_signal, get_time_stop_bars_5m, select_completed_candles

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


def synthesize_1h_candles(sorted_5m_candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group chronological 5m candles into 1h candles (returned newest-first)."""
    candles_1h = []
    chunk_size = 12  # 12 * 5m = 60m
    for i in range(0, len(sorted_5m_candles), chunk_size):
        chunk = sorted_5m_candles[i : i + chunk_size]
        # 마지막 미완성 묶음은 진행 중 1시간봉이므로 MTF 입력에서 제외한다.
        if len(chunk) != chunk_size:
            continue
        h_open = float(chunk[0].get("opening_price", chunk[0].get("trade_price", 0.0)))
        h_close = float(chunk[-1].get("trade_price", 0.0))
        h_high = max(float(c.get("high_price", c.get("trade_price", 0.0))) for c in chunk)
        h_low = min(float(c.get("low_price", c.get("trade_price", 0.0))) for c in chunk)
        h_vol = sum(float(c.get("candle_acc_trade_volume", 0.0)) for c in chunk)
        candles_1h.append({
            "opening_price": h_open,
            "high_price": h_high,
            "low_price": h_low,
            "trade_price": h_close,
            "candle_acc_trade_volume": h_vol,
        })
    return candles_1h[::-1]  # Return newest-first for strategy_engine


class QuantBacktester:
    """
    빗썸 2.0 과거 캔들 데이터 기반 비편향(Unbiased) 퀀트 백테스터 v2.0
    - Next-Bar Open 체결 모델링 & 시가 갭 보호(Gap Filter)
    - Pessimistic Intra-Candle Matching (동일 봉 High/Low 충돌 시 손절 우선)
    - 샹들리에 스탑: 직전 확정 봉까지의 최고가/ATR로만 산출 (캔들 내 룩어헤드 제거)
    - MTF(1H) 합성 캔들 및 BTC 레짐 연동 시뮬레이션
    - 1% Fixed Risk Volatility Position Sizing
    - 손절 후 재진입 쿨다운 (StrategyPolicy.COOLDOWN_STOP_LOSS_SEC) 시뮬레이션
    - Paging 기반 대규모 캔들(수백~수천 개) 백테스트 지원
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        fee_rate: float = 0.0004,
        slippage_rate: float = 0.001,
        risk_fraction: float = 0.01,
        bithumb_api: Any = None,
    ):
        self.initial_capital = initial_capital
        self.fee_rate = max(0.0, fee_rate)
        self.slippage_rate = max(0.0, slippage_rate)
        self.risk_fraction = risk_fraction
        self.bithumb = bithumb_api if bithumb_api is not None else BithumbAPI()

    def fetch_candles_paged(self, market: str, unit: int = 5, total_count: int = 200) -> list[dict[str, Any]]:
        """Fetch historical candles with pagination (Bithumb returns newest-first)."""
        all_candles: list[dict[str, Any]] = []
        to_param: str | None = None
        remaining = total_count

        while remaining > 0:
            fetch_n = min(remaining, 200)
            chunk = self.bithumb.get_candles(unit=unit, count=fetch_n, market=market, to=to_param)
            if not chunk:
                break
            all_candles.extend(chunk)
            remaining -= len(chunk)
            if len(chunk) < fetch_n:
                break

            # Use oldest candle timestamp in chunk as next 'to' parameter
            oldest = chunk[-1]
            oldest_ts = oldest.get("candle_date_time_kst") or oldest.get("candle_date_time_utc")
            if oldest_ts:
                to_param = oldest_ts.replace("T", " ")
            else:
                break

        return all_candles

    def run_backtest(
        self,
        market: str = "KRW-BTC",
        unit: int = 5,
        count: int = 200,
        candles: list[dict[str, Any]] | None = None,
        btc_candles: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        logger.info(f"🧪 [{market}] 과거 {count}개 {unit}분봉 캔들 백테스팅 시작 (초기 자본: {self.initial_capital:,.0f}원)")

        if candles is None:
            candles = self.fetch_candles_paged(market=market, unit=unit, total_count=count)
        if not candles or len(candles) < 30:
            logger.warning(f"{market} 캔들 데이터 부족 ({len(candles) if candles else 0}개)")
            return {}

        # 시간순(과거 ➜ 현재) 정렬
        sorted_candles = candles[::-1]
        sorted_btc = btc_candles[::-1] if btc_candles else None

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
        round_trip_positions: list[dict[str, Any]] = []
        current_pos_info: dict[str, Any] = {}
        equity_curve = [capital]

        for i in range(25, len(sorted_candles)):
            window = sorted_candles[: i + 1]
            c_candle = window[-1]
            open_price = float(c_candle.get("opening_price", c_candle.get("trade_price", 0.0)))
            cur_price = float(c_candle.get("trade_price", 0.0))
            high_price = float(c_candle.get("high_price", cur_price))
            low_price = float(c_candle.get("low_price", cur_price))

            window_desc = window[::-1]

            # 과거 시점의 BTC 레짐 동적 계산
            curr_btc_regime = "NORMAL"
            if sorted_btc and i < len(sorted_btc):
                btc_window = sorted_btc[: i + 1]
                btc_window_desc = btc_window[::-1]
                btc_regime_res = classify_btc_regime(btc_window_desc)
                curr_btc_regime = btc_regime_res.get("regime", "NORMAL")

            # 0. 익봉 시가 진입 처리 (Next-Bar Open Execution & Gap Protection)
            if pending_entry and not in_position:
                pending_entry = False
                if i >= cooldown_until_idx:
                    # 직전 종가 및 ATR 기준 갭 검증
                    prior_close = float(window_desc[1].get("trade_price", open_price))
                    prior_atr = pending_signal.get("atr", prior_close * 0.015)
                    gap_pct = abs(open_price - prior_close) / prior_close if prior_close > 0 else 0.0
                    max_allowable_gap = 1.5 * (prior_atr / prior_close) if prior_close > 0 else 0.03

                    # 시가 갭이 너무 크면 진입 취소 (Gap Protection)
                    if gap_pct <= max_allowable_gap:
                        entry_price = open_price * (1.0 + self.slippage_rate)

                        # 실제 체결가 기준으로 StrategyPolicy ATR 동적 목표가/손절가 재계산
                        current_atr = prior_atr
                        target_offset = max(entry_price * StrategyPolicy.MIN_TARGET_PCT, current_atr * StrategyPolicy.ATR_TARGET_MULTIPLIER)
                        stop_offset = max(entry_price * StrategyPolicy.MIN_STOP_PCT, current_atr * StrategyPolicy.ATR_STOP_MULTIPLIER)
                        active_target = entry_price + target_offset
                        active_stop = entry_price - stop_offset

                        # 1% 고정 리스크 포지션 사이징
                        trade_budget = calculate_risk_position_size(
                            total_equity=capital,
                            entry_price=entry_price,
                            stop_loss=active_stop,
                            risk_fraction=self.risk_fraction,
                            fee_rate=self.fee_rate,
                            slippage_rate=self.slippage_rate,
                            max_position_pct=0.35,
                            min_order_krw=StrategyPolicy.MIN_ORDER_KRW,
                        )

                        if trade_budget >= StrategyPolicy.MIN_ORDER_KRW:
                            position_vol = (trade_budget * (1.0 - self.fee_rate)) / entry_price
                            capital -= trade_budget
                            in_position = True
                            partial_tp_done = False
                            peak_price = entry_price
                            bars_held = 0
                            current_pos_info = {
                                "entry_idx": i,
                                "entry_price": entry_price,
                                "trade_budget": trade_budget,
                                "pnl_krw": 0.0,
                                "btc_regime": curr_btc_regime,
                                "events": ["BUY"],
                            }
                            trade_logs.append({
                                "type": "BUY",
                                "price": entry_price,
                                "target": active_target,
                                "stop": active_stop,
                                "candle_idx": i,
                                "btc_regime": curr_btc_regime,
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
                    cooldown_until_idx = i + int(StrategyPolicy.COOLDOWN_STOP_LOSS_SEC / 300.0)  # 30분 쿨다운
                    if current_pos_info:
                        current_pos_info["pnl_krw"] += loss_krw
                        current_pos_info["events"].append("STOP_LOSS")
                        current_pos_info["bars_held"] = bars_held
                        round_trip_positions.append(dict(current_pos_info))
                        current_pos_info = {}
                    trade_logs.append({
                        "type": "STOP_LOSS",
                        "price": exit_p,
                        "pnl_pct": loss_pct,
                        "profit_krw": loss_krw,
                        "candle_idx": i,
                        "bars_held": bars_held,
                        "btc_regime": curr_btc_regime,
                    })
                    position_vol = 0.0
                    continue

                # B. 1차 분할 익절 (ATR 목표가 도달 시)
                if hit_tp:
                    partial_tp_done = True
                    sell_vol = position_vol * StrategyPolicy.PARTIAL_TP_1_RATIO
                    exit_price = active_target * (1.0 - self.slippage_rate)
                    realized_val = sell_vol * exit_price * (1.0 - self.fee_rate)
                    realized_profit = realized_val - (sell_vol * entry_price)
                    capital += realized_val
                    position_vol -= sell_vol
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
                    if current_pos_info:
                        current_pos_info["pnl_krw"] += realized_profit
                        current_pos_info["events"].append("PARTIAL_TP")
                    trade_logs.append({
                        "type": "PARTIAL_TP",
                        "price": exit_price,
                        "pnl_pct": pnl_pct,
                        "profit_krw": realized_profit,
                        "candle_idx": i,
                        "bars_held": bars_held,
                        "btc_regime": curr_btc_regime,
                    })
                    # 본절가 방어선 가동 (수수료 보전)
                    active_stop = max(active_stop, entry_price * (1.0 + (2.0 * self.fee_rate) + StrategyPolicy.MIN_PROFIT_BUFFER_PCT))

                # C. 샹들리에 트레일링 스탑 (직전 확정 봉 window_desc[1:] 기준으로만 산출 -> 인트라바 미래 고가 참조 차단)
                if partial_tp_done or (cur_price >= entry_price * (1.0 + StrategyPolicy.TRAILING_START_PCT)):
                    ch_stop = calculate_chandelier_exit(window_desc[1:], period=14, multiplier=StrategyPolicy.ATR_STOP_MULTIPLIER)
                    trail_stop_price = max(ch_stop, entry_price * (1.0 + StrategyPolicy.MIN_PROFIT_BUFFER_PCT))
                    if low_price <= trail_stop_price:
                        exit_p = trail_stop_price * (1.0 - self.slippage_rate)
                        pnl_pct = ((exit_p - entry_price) / entry_price) * 100.0
                        proceeds = position_vol * exit_p * (1.0 - self.fee_rate)
                        profit_krw = proceeds - (position_vol * entry_price)
                        capital += proceeds
                        in_position = False
                        cooldown_until_idx = i + int(StrategyPolicy.COOLDOWN_TP_SEC / 300.0)  # 5분 쿨다운
                        if current_pos_info:
                            current_pos_info["pnl_krw"] += profit_krw
                            current_pos_info["events"].append("TRAILING_STOP")
                            current_pos_info["bars_held"] = bars_held
                            round_trip_positions.append(dict(current_pos_info))
                            current_pos_info = {}
                        trade_logs.append({
                            "type": "TRAILING_STOP",
                            "price": exit_p,
                            "pnl_pct": pnl_pct,
                            "profit_krw": profit_krw,
                            "candle_idx": i,
                            "bars_held": bars_held,
                            "btc_regime": curr_btc_regime,
                        })
                        position_vol = 0.0
                        continue

                # D. 동적 본전 보장 타임스탑 (레짐별 profit/max_hold 봉 수 — StrategyPolicy SSOT)
                be_pct = StrategyPolicy.TIME_STOP_BREAKEVEN_MIN_PNL_PCT * 100.0
                cur_unrealized_pct = ((cur_price - entry_price) / entry_price) * 100.0
                profit_stop_bars, max_hold_bars = get_time_stop_bars_5m(curr_btc_regime)
                should_time_stop = (
                    (bars_held >= profit_stop_bars and cur_unrealized_pct >= be_pct)
                    or (bars_held >= max_hold_bars)
                )
                if should_time_stop:
                    exit_p = cur_price * (1.0 - self.slippage_rate)
                    pnl_pct = ((exit_p - entry_price) / entry_price) * 100.0
                    proceeds = position_vol * exit_p * (1.0 - self.fee_rate)
                    profit_krw = proceeds - (position_vol * entry_price)
                    capital += proceeds
                    in_position = False
                    cooldown_until_idx = i + int(StrategyPolicy.COOLDOWN_TIME_STOP_SEC / 300.0)
                    if current_pos_info:
                        current_pos_info["pnl_krw"] += profit_krw
                        current_pos_info["events"].append("TIME_STOP")
                        current_pos_info["bars_held"] = bars_held
                        round_trip_positions.append(dict(current_pos_info))
                        current_pos_info = {}
                    trade_logs.append({
                        "type": "TIME_STOP",
                        "price": exit_p,
                        "pnl_pct": pnl_pct,
                        "profit_krw": profit_krw,
                        "candle_idx": i,
                    })
                    position_vol = 0.0
                    continue

            # 2. 미보유 상태: 퀀트 진입 신호 검사 (동적 BTC 레짐 및 합성 1시간봉 MTF 결합)
            elif not in_position and capital >= 10_000 and i >= cooldown_until_idx:
                candles_1h_synth = synthesize_1h_candles(sorted_candles[: i + 1])
                completed_window = select_completed_candles(window_desc, minimum_count=25)
                if not completed_window:
                    continue
                signal = entry_signal(
                    completed_window,
                    candles_1h=candles_1h_synth,
                    btc_regime=curr_btc_regime,
                    market=market,
                    exchange="backtest",
                )
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
            leftover_val = position_vol * last_price * (1.0 - self.slippage_rate) * (1.0 - self.fee_rate)
            leftover_profit = leftover_val - (position_vol * entry_price)
            capital += leftover_val
            if current_pos_info:
                current_pos_info["pnl_krw"] += leftover_profit
                current_pos_info["events"].append("UNREALIZED_CLOSE")
                round_trip_positions.append(dict(current_pos_info))

        total_return_pct = ((capital - self.initial_capital) / self.initial_capital) * 100.0
        completed_trades = [t for t in trade_logs if t["type"] in ("PARTIAL_TP", "TRAILING_STOP", "STOP_LOSS", "TIME_STOP")]
        win_trades = [t for t in completed_trades if t.get("profit_krw", 0) > 0]
        loss_trades = [t for t in completed_trades if t.get("profit_krw", 0) <= 0]
        win_rate = (len(win_trades) / len(completed_trades) * 100.0) if completed_trades else 0.0

        pos_win_trades = [p for p in round_trip_positions if p.get("pnl_krw", 0) > 0]
        position_win_rate = (len(pos_win_trades) / len(round_trip_positions) * 100.0) if round_trip_positions else win_rate

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

        # 레짐별 (NORMAL / RISK_OFF) 성과 분리 집계 (과제 D)
        regime_breakdown: dict[str, Any] = {}
        for rg in ("NORMAL", "RISK_OFF", "CRASH"):
            rg_trades = [t for t in completed_trades if t.get("btc_regime") == rg]
            if rg_trades:
                rg_win = [t for t in rg_trades if t.get("profit_krw", 0) > 0]
                rg_loss = [t for t in rg_trades if t.get("profit_krw", 0) <= 0]
                rg_win_rate = (len(rg_win) / len(rg_trades) * 100.0)
                rg_win_krw = sum(t.get("profit_krw", 0) for t in rg_win)
                rg_loss_krw = abs(sum(t.get("profit_krw", 0) for t in rg_loss))
                rg_pf = (rg_win_krw / rg_loss_krw) if rg_loss_krw > 0 else (99.9 if rg_win_krw > 0 else 1.0)
                rg_avg_win = (sum(t.get("pnl_pct", 0) for t in rg_win) / len(rg_win)) if rg_win else 0.0
                rg_avg_loss = (abs(sum(t.get("pnl_pct", 0) for t in rg_loss)) / len(rg_loss)) if rg_loss else 0.0
                rg_exp = ((rg_win_rate / 100.0) * rg_avg_win) - (((100.0 - rg_win_rate) / 100.0) * rg_avg_loss)
                rg_avg_bars = sum(t.get("bars_held", 0) for t in rg_trades) / len(rg_trades)
                regime_breakdown[rg] = {
                    "trades_count": len(rg_trades),
                    "win_rate": round(rg_win_rate, 1),
                    "profit_factor": round(rg_pf, 2),
                    "expectancy_pct": round(rg_exp, 2),
                    "total_pnl_krw": round(rg_win_krw - rg_loss_krw, 0),
                    "avg_bars_held": round(rg_avg_bars, 1),
                }

        # 1회 거래당 종합 기대수익률 (Expectancy)
        avg_win_pct = (sum(t.get("pnl_pct", 0) for t in win_trades) / len(win_trades)) if win_trades else 0.0
        avg_loss_pct = (abs(sum(t.get("pnl_pct", 0) for t in loss_trades)) / len(loss_trades)) if loss_trades else 0.0
        expectancy_pct = ((win_rate / 100.0) * avg_win_pct) - (((100.0 - win_rate) / 100.0) * avg_loss_pct)

        result = {
            "market": market,
            "candles_tested": len(sorted_candles),
            "initial_capital": self.initial_capital,
            "final_capital": capital,
            "total_return_pct": round(total_return_pct, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "total_trades": len(completed_trades),
            "win_trades": len(win_trades),
            "loss_trades": len(loss_trades),
            "win_rate": round(win_rate, 1),
            "round_trip_count": len(round_trip_positions),
            "position_win_rate": round(position_win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "max_consecutive_losses": max_consecutive_losses,
            "expectancy_pct": round(expectancy_pct, 2),
            "fee_rate": self.fee_rate,
            "slippage_rate": self.slippage_rate,
            "timestop_bars": StrategyPolicy.TIME_STOP_BARS_5M,
            "regime_breakdown": regime_breakdown,
            "completed_trades": completed_trades,
            "equity_curve": equity_curve,
        }

        self._print_report(result)
        return result

    def run_walk_forward_backtest(
        self,
        market: str = "KRW-BTC",
        unit: int = 5,
        candles: list[dict[str, Any]] | None = None,
        btc_candles: list[dict[str, Any]] | None = None,
        num_windows: int = 4,
        train_ratio: float = 0.7,
    ) -> dict[str, Any]:
        """
        Walk-Forward 시계열 롤링 전진 검증 (과최적화 방지 및 전략 견고성 입증)
        """
        if candles is None:
            candles = self.fetch_candles_paged(market=market, unit=unit, total_count=400)
        if not candles or len(candles) < 60:
            logger.warning("Walk-Forward 검증에 필요한 최소 캔들 수(60개) 부족")
            return {}

        sorted_candles = candles[::-1]
        sorted_btc = btc_candles[::-1] if btc_candles else None
        total_len = len(sorted_candles)
        window_size = total_len // num_windows

        if window_size < 30:
            window_size = total_len
            num_windows = 1

        windows_results = []
        out_returns = []
        out_win_rates = []

        for w_idx in range(num_windows):
            start_idx = w_idx * (window_size // 2) if num_windows > 1 else 0
            end_idx = min(start_idx + window_size, total_len)
            sub_candles = sorted_candles[start_idx:end_idx]
            sub_btc = sorted_btc[start_idx:end_idx] if sorted_btc else None

            if len(sub_candles) < 30:
                continue

            split_pt = int(len(sub_candles) * train_ratio)
            train_candles = sub_candles[:split_pt][::-1]
            test_candles = sub_candles[split_pt:][::-1]
            train_btc = sub_btc[:split_pt][::-1] if sub_btc else None
            test_btc = sub_btc[split_pt:][::-1] if sub_btc else None

            # In-Sample (Train) 백테스트
            in_sample_res = self.run_backtest(
                market=market,
                unit=unit,
                candles=train_candles,
                btc_candles=train_btc,
            )

            # Out-of-Sample (Test) 전진 검증
            out_sample_res = self.run_backtest(
                market=market,
                unit=unit,
                candles=test_candles,
                btc_candles=test_btc,
            )

            out_ret = out_sample_res.get("total_return_pct", 0.0)
            out_wr = out_sample_res.get("win_rate", 0.0)
            out_returns.append(out_ret)
            out_win_rates.append(out_wr)

            windows_results.append({
                "window_index": w_idx + 1,
                "in_sample": in_sample_res,
                "out_of_sample": out_sample_res,
            })

        mean_out_return = sum(out_returns) / len(out_returns) if out_returns else 0.0
        mean_out_win_rate = sum(out_win_rates) / len(out_win_rates) if out_win_rates else 0.0
        robustness_score = round(max(0.0, min(100.0, 50.0 + (mean_out_return * 5.0) + (mean_out_win_rate * 0.5))), 1)

        summary = {
            "market": market,
            "num_windows": len(windows_results),
            "mean_out_of_sample_return_pct": round(mean_out_return, 2),
            "mean_out_of_sample_win_rate": round(mean_out_win_rate, 1),
            "robustness_score": robustness_score,
            "windows": windows_results,
        }

        print("\n" + "=" * 65)
        print(f" 🔄 [Walk-Forward 시계열 전진 검증 리포트 - {market}]")
        print("=" * 65)
        print(f"• 분할 검증 윈도우 수: {len(windows_results)}개 구간")
        print(f"• Out-of-Sample 평균 수익률: {mean_out_return:+.2f}%")
        print(f"• Out-of-Sample 평균 승률: {mean_out_win_rate:.1f}%")
        print(f"• 전략 견고성 지수 (Robustness Score): {robustness_score} / 100 점")
        print("=" * 65 + "\n")

        return summary

    def run_monte_carlo_simulation(
        self,
        completed_trades: list[dict[str, Any]],
        num_simulations: int = 1000,
        initial_capital: float | None = None,
    ) -> dict[str, Any]:
        """
        1,000회 부트스트랩 리샘플링 몬테카를로 시뮬레이션 (MDD VaR 95% 및 파산 위험도 산출)
        """
        import random
        base_capital = initial_capital or self.initial_capital
        profits = [float(t.get("profit_krw", 0.0)) for t in completed_trades]

        if not profits:
            return {
                "simulations": num_simulations,
                "trades_count": 0,
                "mdd_var_95_pct": 0.0,
                "mdd_var_99_pct": 0.0,
                "worst_mdd_pct": 0.0,
                "ruin_probability_pct": 0.0,
            }

        simulated_mdds = []
        simulated_returns = []
        ruin_count = 0

        for _ in range(num_simulations):
            # 복원 무작위 리샘플링
            sampled_profits = [random.choice(profits) for _ in range(len(profits))]
            cap = base_capital
            peak = cap
            max_dd = 0.0

            for p in sampled_profits:
                cap += p
                peak = max(peak, cap)
                dd = (peak - cap) / peak * 100.0 if peak > 0 else 0.0
                max_dd = max(max_dd, dd)

            ret = ((cap - base_capital) / base_capital) * 100.0
            simulated_mdds.append(max_dd)
            simulated_returns.append(ret)

            if max_dd >= 50.0 or cap <= (base_capital * 0.5):
                ruin_count += 1

        simulated_mdds.sort()
        simulated_returns.sort()

        idx_95 = int(num_simulations * 0.95)
        idx_99 = int(num_simulations * 0.99)

        mdd_var_95 = round(simulated_mdds[min(idx_95, num_simulations - 1)], 2)
        mdd_var_99 = round(simulated_mdds[min(idx_99, num_simulations - 1)], 2)
        worst_mdd = round(simulated_mdds[-1], 2)
        ruin_prob = round((ruin_count / num_simulations) * 100.0, 2)

        res = {
            "simulations": num_simulations,
            "trades_count": len(profits),
            "mdd_var_95_pct": mdd_var_95,
            "mdd_var_99_pct": mdd_var_99,
            "worst_mdd_pct": worst_mdd,
            "ruin_probability_pct": ruin_prob,
            "median_return_pct": round(simulated_returns[num_simulations // 2], 2),
        }

        print("\n" + "=" * 65)
        print(" 🎲 [몬테카를로 1,000회 리샘플링 스트레스 테스트 리포트]")
        print("=" * 65)
        print(f"• 시뮬레이션 반복 횟수: {num_simulations:,}회")
        print(f"• 표본 거래 수: {len(profits)}건")
        print(f"• 95% 신뢰수준 최대 낙폭 (MDD VaR 95%): {mdd_var_95:.2f}%")
        print(f"• 99% 신뢰수준 최대 낙폭 (MDD VaR 99%): {mdd_var_99:.2f}%")
        print(f"• 최악의 시나리오 낙폭 (Worst MDD): {worst_mdd:.2f}%")
        print(f"• 계좌 반토막(파산) 위험률 (Risk of Ruin): {ruin_prob:.2f}%")
        print("=" * 65 + "\n")

        return res

    def run_sensitivity_analysis(
        self,
        market: str = "KRW-BTC",
        unit: int = 5,
        candles: list[dict[str, Any]] | None = None,
        btc_candles: list[dict[str, Any]] | None = None,
        risk_fractions: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """
        리스크 비율 및 파라미터 민감도 분석 (Parameter Sensitivity Grid)
        """
        if risk_fractions is None:
            risk_fractions = [0.005, 0.010, 0.015, 0.020]

        results = []
        orig_risk = self.risk_fraction

        for rf in risk_fractions:
            self.risk_fraction = rf
            res = self.run_backtest(
                market=market,
                unit=unit,
                candles=candles,
                btc_candles=btc_candles,
            )
            results.append({
                "risk_fraction_pct": round(rf * 100.0, 2),
                "total_return_pct": res.get("total_return_pct", 0.0),
                "max_drawdown_pct": res.get("max_drawdown_pct", 0.0),
                "win_rate": res.get("win_rate", 0.0),
                "profit_factor": res.get("profit_factor", 0.0),
            })

        self.risk_fraction = orig_risk
        return results

    def _summarize_completed_trades(
        self,
        completed_trades: list[dict[str, Any]],
        round_trip_positions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        win_trades = [t for t in completed_trades if t.get("profit_krw", 0) > 0]
        loss_trades = [t for t in completed_trades if t.get("profit_krw", 0) <= 0]
        win_rate = (len(win_trades) / len(completed_trades) * 100.0) if completed_trades else 0.0
        pos_win_trades = [p for p in round_trip_positions if p.get("pnl_krw", 0) > 0]
        position_win_rate = (len(pos_win_trades) / len(round_trip_positions) * 100.0) if round_trip_positions else win_rate
        total_win_krw = sum(t.get("profit_krw", 0) for t in win_trades)
        total_loss_krw = abs(sum(t.get("profit_krw", 0) for t in loss_trades))
        profit_factor = (total_win_krw / total_loss_krw) if total_loss_krw > 0 else (99.9 if total_win_krw > 0 else 1.0)
        avg_win_pct = (sum(t.get("pnl_pct", 0) for t in win_trades) / len(win_trades)) if win_trades else 0.0
        avg_loss_pct = (abs(sum(t.get("pnl_pct", 0) for t in loss_trades)) / len(loss_trades)) if loss_trades else 0.0
        expectancy_pct = ((win_rate / 100.0) * avg_win_pct) - (((100.0 - win_rate) / 100.0) * avg_loss_pct)
        return {
            "total_trades": len(completed_trades),
            "win_trades": len(win_trades),
            "loss_trades": len(loss_trades),
            "win_rate": round(win_rate, 1),
            "round_trip_count": len(round_trip_positions),
            "position_win_rate": round(position_win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "expectancy_pct": round(expectancy_pct, 2),
        }

    def run_portfolio_backtest(
        self,
        market_candles: dict[str, list[dict[str, Any]]],
        unit: int = 5,
        btc_candles: list[dict[str, Any]] | None = None,
        max_positions: int = 3,
        eval_start_idx: int = 25,
    ) -> dict[str, Any]:
        """다중 마켓 공유 자본·동시 포지션 한도·BTC 레짐을 반영한 포트폴리오 백테스트."""
        valid_markets = {m: c for m, c in market_candles.items() if c}
        if not valid_markets:
            return {}
        common_len = min(len(c) for c in valid_markets.values())
        start_idx = max(25, eval_start_idx)
        if common_len <= start_idx:
            return {}

        sorted_map = {m: c[::-1][-common_len:] for m, c in valid_markets.items()}
        sorted_btc = btc_candles[::-1][-common_len:] if btc_candles and len(btc_candles) >= common_len else None

        cash = self.initial_capital
        peak_capital = cash
        max_drawdown = 0.0
        max_concurrent = 0

        positions: dict[str, dict[str, Any]] = {}
        cooldown_until: dict[str, int] = {}
        pending_entries: list[dict[str, Any]] = []
        trade_logs: list[dict[str, Any]] = []
        round_trip_positions: list[dict[str, Any]] = []
        equity_curve = [cash]

        for i in range(start_idx, common_len):
            curr_btc_regime = "NORMAL"
            if sorted_btc is not None:
                btc_desc = sorted_btc[: i + 1][::-1]
                curr_btc_regime = classify_btc_regime(btc_desc).get("regime", "NORMAL")

            # 1. 보유 포지션 청산 검사 (손절 -> 분할/전량 익절 -> 샹들리에 -> 타임스탑)
            for m in list(positions.keys()):
                pos = positions[m]
                c_candle = sorted_map[m][i]
                open_price = float(c_candle.get("opening_price", c_candle.get("trade_price", 0.0)))
                cur_price = float(c_candle.get("trade_price", 0.0))
                high_price = float(c_candle.get("high_price", cur_price))
                low_price = float(c_candle.get("low_price", cur_price))
                window_desc = sorted_map[m][: i + 1][::-1]

                pos["bars_held"] += 1
                pos["peak_price"] = max(pos["peak_price"], high_price)

                hit_stop = low_price <= pos["active_stop"]
                hit_tp = (high_price >= pos["active_target"]) and not pos["partial_tp_done"]

                if hit_stop:
                    exit_p = min(open_price, pos["active_stop"]) * (1.0 - self.slippage_rate)
                    proceeds = pos["vol"] * exit_p * (1.0 - self.fee_rate)
                    loss_krw = proceeds - (pos["vol"] * pos["entry_price"])
                    loss_pct = ((exit_p - pos["entry_price"]) / pos["entry_price"]) * 100.0
                    cash += proceeds
                    cooldown_until[m] = i + int(StrategyPolicy.COOLDOWN_STOP_LOSS_SEC / 300.0)
                    pos["rt"]["pnl_krw"] += loss_krw
                    pos["rt"]["events"].append("STOP_LOSS")
                    pos["rt"]["bars_held"] = pos["bars_held"]
                    round_trip_positions.append(pos["rt"])
                    trade_logs.append({
                        "market": m, "type": "STOP_LOSS", "price": exit_p,
                        "pnl_pct": loss_pct, "profit_krw": loss_krw,
                        "candle_idx": i, "bars_held": pos["bars_held"], "btc_regime": curr_btc_regime,
                    })
                    positions.pop(m)
                    continue

                if hit_tp:
                    pos["partial_tp_done"] = True
                    sell_vol, is_full_exit = calculate_partial_take_profit_volume(
                        pos["vol"],
                        partial_ratio=StrategyPolicy.PARTIAL_TP_1_RATIO,
                        current_price=pos["active_target"],
                        min_order_krw=StrategyPolicy.MIN_ORDER_KRW,
                    )
                    exit_price = pos["active_target"] * (1.0 - self.slippage_rate)
                    realized_val = sell_vol * exit_price * (1.0 - self.fee_rate)
                    realized_profit = realized_val - (sell_vol * pos["entry_price"])
                    cash += realized_val
                    pos["vol"] -= sell_vol
                    pnl_pct = ((exit_price - pos["entry_price"]) / pos["entry_price"]) * 100.0
                    pos["rt"]["pnl_krw"] += realized_profit
                    pos["rt"]["events"].append("PARTIAL_TP")
                    trade_logs.append({
                        "market": m, "type": "PARTIAL_TP", "price": exit_price,
                        "pnl_pct": pnl_pct, "profit_krw": realized_profit,
                        "candle_idx": i, "bars_held": pos["bars_held"], "btc_regime": curr_btc_regime,
                    })
                    if is_full_exit or pos["vol"] <= 1e-12:
                        pos["rt"]["bars_held"] = pos["bars_held"]
                        round_trip_positions.append(pos["rt"])
                        cooldown_until[m] = i + int(StrategyPolicy.COOLDOWN_TP_SEC / 300.0)
                        positions.pop(m)
                        continue
                    pos["active_stop"] = max(
                        pos["active_stop"],
                        pos["entry_price"] * (1.0 + (2.0 * self.fee_rate) + StrategyPolicy.MIN_PROFIT_BUFFER_PCT),
                    )

                if pos["partial_tp_done"] or (cur_price >= pos["entry_price"] * (1.0 + StrategyPolicy.TRAILING_START_PCT)):
                    ch_stop = calculate_chandelier_exit(window_desc[1:], period=14, multiplier=StrategyPolicy.ATR_STOP_MULTIPLIER)
                    trail_stop_price = max(ch_stop, pos["entry_price"] * (1.0 + StrategyPolicy.MIN_PROFIT_BUFFER_PCT))
                    if low_price <= trail_stop_price:
                        exit_p = trail_stop_price * (1.0 - self.slippage_rate)
                        proceeds = pos["vol"] * exit_p * (1.0 - self.fee_rate)
                        profit_krw = proceeds - (pos["vol"] * pos["entry_price"])
                        pnl_pct = ((exit_p - pos["entry_price"]) / pos["entry_price"]) * 100.0
                        cash += proceeds
                        cooldown_until[m] = i + int(StrategyPolicy.COOLDOWN_TP_SEC / 300.0)
                        pos["rt"]["pnl_krw"] += profit_krw
                        pos["rt"]["events"].append("TRAILING_STOP")
                        pos["rt"]["bars_held"] = pos["bars_held"]
                        round_trip_positions.append(pos["rt"])
                        trade_logs.append({
                            "market": m, "type": "TRAILING_STOP", "price": exit_p,
                            "pnl_pct": pnl_pct, "profit_krw": profit_krw,
                            "candle_idx": i, "bars_held": pos["bars_held"], "btc_regime": curr_btc_regime,
                        })
                        positions.pop(m)
                        continue

                be_pct = StrategyPolicy.TIME_STOP_BREAKEVEN_MIN_PNL_PCT * 100.0
                cur_unrealized_pct = ((cur_price - pos["entry_price"]) / pos["entry_price"]) * 100.0
                profit_stop_bars, max_hold_bars = get_time_stop_bars_5m(curr_btc_regime)
                if (pos["bars_held"] >= profit_stop_bars and cur_unrealized_pct >= be_pct) or (pos["bars_held"] >= max_hold_bars):
                    exit_p = cur_price * (1.0 - self.slippage_rate)
                    proceeds = pos["vol"] * exit_p * (1.0 - self.fee_rate)
                    profit_krw = proceeds - (pos["vol"] * pos["entry_price"])
                    pnl_pct = ((exit_p - pos["entry_price"]) / pos["entry_price"]) * 100.0
                    cash += proceeds
                    cooldown_until[m] = i + int(StrategyPolicy.COOLDOWN_TIME_STOP_SEC / 300.0)
                    pos["rt"]["pnl_krw"] += profit_krw
                    pos["rt"]["events"].append("TIME_STOP")
                    pos["rt"]["bars_held"] = pos["bars_held"]
                    round_trip_positions.append(pos["rt"])
                    trade_logs.append({
                        "market": m, "type": "TIME_STOP", "price": exit_p,
                        "pnl_pct": pnl_pct, "profit_krw": profit_krw,
                        "candle_idx": i, "bars_held": pos["bars_held"], "btc_regime": curr_btc_regime,
                    })
                    positions.pop(m)

            # 2. 예약된 익봉 시가 진입 처리 (공유 현금 + 최대 포지션 수 제한)
            if pending_entries:
                for cand in sorted(pending_entries, key=lambda x: x.get("score", 0.0), reverse=True):
                    if len(positions) >= max_positions or cash < StrategyPolicy.MIN_ORDER_KRW:
                        break
                    m = cand["market"]
                    if m in positions or i < cooldown_until.get(m, 0):
                        continue
                    c_candle = sorted_map[m][i]
                    open_price = float(c_candle.get("opening_price", c_candle.get("trade_price", 0.0)))
                    prior_close = float(sorted_map[m][i - 1].get("trade_price", open_price))
                    prior_atr = float(cand.get("atr", prior_close * 0.015))
                    gap_pct = abs(open_price - prior_close) / prior_close if prior_close > 0 else 0.0
                    max_allowable_gap = 1.5 * (prior_atr / prior_close) if prior_close > 0 else 0.03
                    if gap_pct > max_allowable_gap:
                        continue

                    entry_price = open_price * (1.0 + self.slippage_rate)
                    target_offset = max(entry_price * StrategyPolicy.MIN_TARGET_PCT, prior_atr * StrategyPolicy.ATR_TARGET_MULTIPLIER)
                    stop_offset = max(entry_price * StrategyPolicy.MIN_STOP_PCT, prior_atr * StrategyPolicy.ATR_STOP_MULTIPLIER)
                    active_target = entry_price + target_offset
                    active_stop = entry_price - stop_offset

                    total_equity = cash + sum(
                        p["vol"] * float(sorted_map[pm][i].get("trade_price", p["entry_price"]))
                        for pm, p in positions.items()
                    )
                    trade_budget = min(
                        cash,
                        calculate_risk_position_size(
                            total_equity=total_equity,
                            entry_price=entry_price,
                            stop_loss=active_stop,
                            risk_fraction=self.risk_fraction,
                            fee_rate=self.fee_rate,
                            slippage_rate=self.slippage_rate,
                            max_position_pct=0.35,
                            min_order_krw=StrategyPolicy.MIN_ORDER_KRW,
                        ),
                    )
                    if trade_budget >= StrategyPolicy.MIN_ORDER_KRW:
                        vol = (trade_budget * (1.0 - self.fee_rate)) / entry_price
                        cash -= trade_budget
                        positions[m] = {
                            "entry_price": entry_price,
                            "vol": vol,
                            "partial_tp_done": False,
                            "peak_price": entry_price,
                            "active_target": active_target,
                            "active_stop": active_stop,
                            "bars_held": 0,
                            "rt": {
                                "market": m, "entry_idx": i, "entry_price": entry_price,
                                "trade_budget": trade_budget, "pnl_krw": 0.0,
                                "btc_regime": curr_btc_regime, "events": ["BUY"],
                            },
                        }
                        trade_logs.append({
                            "market": m, "type": "BUY", "price": entry_price,
                            "target": active_target, "stop": active_stop,
                            "candle_idx": i, "btc_regime": curr_btc_regime,
                        })
                pending_entries = []

            # 3. 미보유 마켓 신규 진입 신호 평가
            if len(positions) < max_positions and cash >= StrategyPolicy.MIN_ORDER_KRW:
                next_candidates = []
                for m, s_candles in sorted_map.items():
                    if m in positions or i < cooldown_until.get(m, 0):
                        continue
                    window_desc = s_candles[: i + 1][::-1]
                    completed_window = select_completed_candles(window_desc, minimum_count=25)
                    if not completed_window:
                        continue
                    candles_1h_synth = synthesize_1h_candles(s_candles[: i + 1])
                    sig = entry_signal(
                        completed_window,
                        candles_1h=candles_1h_synth,
                        btc_regime=curr_btc_regime,
                        market=m,
                        exchange="backtest",
                    )
                    if sig.get("allow_buy"):
                        next_candidates.append({
                            "market": m,
                            "atr": sig.get("atr", 0.0),
                            "score": float(sig.get("score", 0.0) or 0.0),
                        })
                pending_entries = next_candidates

            # 4. 포트폴리오 자산 및 MDD 갱신
            max_concurrent = max(max_concurrent, len(positions))
            cur_equity = cash + sum(
                p["vol"] * float(sorted_map[pm][i].get("trade_price", p["entry_price"]))
                for pm, p in positions.items()
            )
            peak_capital = max(peak_capital, cur_equity)
            dd = (peak_capital - cur_equity) / peak_capital * 100.0 if peak_capital > 0 else 0.0
            max_drawdown = max(max_drawdown, dd)
            equity_curve.append(cur_equity)

        # 잔여 미청산 포지션 마감 평가
        for m, pos in list(positions.items()):
            last_price = float(sorted_map[m][-1].get("trade_price", pos["entry_price"]))
            leftover_val = pos["vol"] * last_price * (1.0 - self.slippage_rate) * (1.0 - self.fee_rate)
            leftover_profit = leftover_val - (pos["vol"] * pos["entry_price"])
            cash += leftover_val
            pos["rt"]["pnl_krw"] += leftover_profit
            pos["rt"]["events"].append("UNREALIZED_CLOSE")
            round_trip_positions.append(pos["rt"])

        completed_trades = [t for t in trade_logs if t["type"] in ("PARTIAL_TP", "TRAILING_STOP", "STOP_LOSS", "TIME_STOP")]
        summary_stats = self._summarize_completed_trades(completed_trades, round_trip_positions)
        total_return_pct = ((cash - self.initial_capital) / self.initial_capital) * 100.0

        market_breakdown: dict[str, Any] = {}
        for m in valid_markets:
            m_trades = [t for t in completed_trades if t.get("market") == m]
            m_rts = [p for p in round_trip_positions if p.get("market") == m]
            m_stats = self._summarize_completed_trades(m_trades, m_rts)
            m_stats["total_pnl_krw"] = round(sum(t.get("profit_krw", 0.0) for t in m_trades), 0)
            market_breakdown[m] = m_stats

        return {
            "markets": list(valid_markets.keys()),
            "candles_tested": common_len - start_idx,
            "initial_capital": self.initial_capital,
            "final_capital": cash,
            "total_return_pct": round(total_return_pct, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "max_concurrent_positions": max_concurrent,
            "market_breakdown": market_breakdown,
            "completed_trades": completed_trades,
            "equity_curve": equity_curve,
            **summary_stats,
        }

    def run_portfolio_walk_forward_backtest(
        self,
        market_candles: dict[str, list[dict[str, Any]]],
        unit: int = 5,
        btc_candles: list[dict[str, Any]] | None = None,
        num_windows: int = 3,
        train_ratio: float = 0.7,
        max_positions: int = 3,
        warmup_bars: int = 25,
    ) -> dict[str, Any]:
        """다중 마켓 포트폴리오 기반 Walk-Forward 시계열 전진 검증."""
        valid_markets = {m: c for m, c in market_candles.items() if c}
        if not valid_markets:
            return {}
        common_len = min(len(c) for c in valid_markets.values())
        if common_len < 60:
            logger.warning("포트폴리오 Walk-Forward 검증에 필요한 최소 캔들 수(60개) 부족")
            return {}

        sorted_map = {m: c[::-1][-common_len:] for m, c in valid_markets.items()}
        sorted_btc = btc_candles[::-1][-common_len:] if btc_candles and len(btc_candles) >= common_len else None

        window_size = common_len // max(1, num_windows)
        if window_size < 45:
            window_size = common_len
            num_windows = 1

        step = max(1, window_size // 2) if num_windows > 1 else common_len
        windows_results: list[dict[str, Any]] = []
        out_returns: list[float] = []
        out_win_rates: list[float] = []
        out_mdds: list[float] = []

        for w_idx in range(num_windows):
            start_idx = w_idx * step if num_windows > 1 else 0
            end_idx = min(start_idx + window_size, common_len)
            sub_len = end_idx - start_idx
            if sub_len < 45:
                continue

            split_pt = max(30, min(sub_len - 10, int(sub_len * train_ratio)))
            train_map = {m: sc[start_idx : start_idx + split_pt][::-1] for m, sc in sorted_map.items()}
            train_btc = sorted_btc[start_idx : start_idx + split_pt][::-1] if sorted_btc else None

            # Out-of-Sample 구간 앞에 warmup_bars를 붙여 지표 계산 워밍업 손실을 제거한다.
            oos_slice_start = max(start_idx, start_idx + split_pt - warmup_bars)
            actual_warmup = (start_idx + split_pt) - oos_slice_start
            test_map = {m: sc[oos_slice_start:end_idx][::-1] for m, sc in sorted_map.items()}
            test_btc = sorted_btc[oos_slice_start:end_idx][::-1] if sorted_btc else None

            in_sample_res = self.run_portfolio_backtest(
                market_candles=train_map,
                unit=unit,
                btc_candles=train_btc,
                max_positions=max_positions,
                eval_start_idx=warmup_bars,
            )
            out_sample_res = self.run_portfolio_backtest(
                market_candles=test_map,
                unit=unit,
                btc_candles=test_btc,
                max_positions=max_positions,
                eval_start_idx=max(warmup_bars, actual_warmup),
            )
            if not out_sample_res:
                continue

            out_returns.append(out_sample_res.get("total_return_pct", 0.0))
            out_win_rates.append(out_sample_res.get("win_rate", 0.0))
            out_mdds.append(out_sample_res.get("max_drawdown_pct", 0.0))
            windows_results.append({
                "window_index": w_idx + 1,
                "in_sample": in_sample_res,
                "out_of_sample": out_sample_res,
            })

        mean_out_return = sum(out_returns) / len(out_returns) if out_returns else 0.0
        mean_out_win_rate = sum(out_win_rates) / len(out_win_rates) if out_win_rates else 0.0
        mean_out_mdd = sum(out_mdds) / len(out_mdds) if out_mdds else 0.0
        robustness_score = round(
            max(0.0, min(100.0, 50.0 + (mean_out_return * 5.0) + (mean_out_win_rate * 0.5) - (mean_out_mdd * 1.5))),
            1,
        )

        return {
            "markets": list(valid_markets.keys()),
            "num_windows": len(windows_results),
            "max_positions": max_positions,
            "mean_out_of_sample_return_pct": round(mean_out_return, 2),
            "mean_out_of_sample_win_rate": round(mean_out_win_rate, 1),
            "mean_out_of_sample_mdd_pct": round(mean_out_mdd, 2),
            "robustness_score": robustness_score,
            "windows": windows_results,
        }

    def _print_report(self, r: dict[str, Any]):
        print("\n" + "=" * 65)
        print(f" 📊 [빗썸 비편향 퀀트 백테스팅 리포트 v2.0 - {r.get('market', '')}]")
        print("=" * 65)
        print(f"• 테스트 캔들 수: {r.get('candles_tested', 0)}개 캔들")
        print(f"• 초기 투자 자본: {r.get('initial_capital', 0):,.0f} KRW")
        print(f"• 최종 평가 자본: {r.get('final_capital', 0):,.0f} KRW")
        print(f"• 총 누적 수익률: {r.get('total_return_pct', 0.0):+.2f}%")
        print(f"• 최대 낙폭(MDD): {r.get('max_drawdown_pct', 0.0):.2f}%")
        print(f"• 총 거래 횟수: {r.get('total_trades', 0)}회 (승리: {r.get('win_trades', 0)}회 / 패배: {r.get('loss_trades', 0)}회)")
        print(f"• 실시간 승률: {r.get('win_rate', 0.0):.1f}%")
        print(f"• 손익비(Profit Factor): {r.get('profit_factor', 0.0):.2f}")
        print(f"• 비용 가정: 수수료 {r.get('fee_rate', 0.0)*100:.3f}% / 편도 슬리피지 {r.get('slippage_rate', 0.0)*100:.3f}%")
        print(f"• 체결 모델: Next-Bar Open + Gap Filter + 확정봉 Chandelier Stop + 타임스탑 {r.get('timestop_bars', 12)}봉")

        # 레짐별 성과 출력 (과제 D)
        regime_bd = r.get("regime_breakdown", {})
        if regime_bd:
            print("-----------------------------------------------------------------")
            print(" [레짐별 성과 분리 분석 (Market Regime Breakdown)]")
            for rg_name, stats in regime_bd.items():
                print(
                    f"  • {rg_name:<10}: 거래 {stats['trades_count']}회 | 승률 {stats['win_rate']:.1f}% | "
                    f"PF {stats['profit_factor']:.2f} | 기대수익 {stats['expectancy_pct']:+.2f}% | "
                    f"손익 {stats['total_pnl_krw']:+,.0f}원 | 평균보유 {stats['avg_bars_held']}봉"
                )
        print("=" * 65 + "\n")

    @staticmethod
    def save_report_json(result: dict[str, Any], filepath: str) -> None:
        """백테스팅 결과를 JSON 파일로 저장 (P3-2)"""
        import json
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    @staticmethod
    def save_report_markdown(result: dict[str, Any], filepath: str) -> None:
        """백테스팅 결과를 Markdown 문서로 저장 (P3-2)"""
        md_content = f"""# 📊 퀀트 백테스팅 결과 리포트: {result.get('market', '')}

| 지표 항목 | 백테스트 결과 | 비고 |
| :--- | :--- | :--- |
| **테스트 마켓** | `{result.get('market', '')}` | 5분봉 기준 |
| **테스트 캔들 수** | {result.get('candles_tested', 0):,} 개 | |
| **초기 투자 자본** | {result.get('initial_capital', 0):,.0f} KRW | |
| **최종 평가 자본** | {result.get('final_capital', 0):,.0f} KRW | |
| **총 누적 수익률** | **{result.get('total_return_pct', 0.0):+.2f}%** | Next-Bar Open 체결 |
| **최대 낙폭 (MDD)** | {result.get('max_drawdown_pct', 0.0):.2f}% | |
| **총 매매 횟수** | {result.get('total_trades', 0)} 회 | 승리 {result.get('win_trades', 0)} / 패배 {result.get('loss_trades', 0)} |
| **승률 (Win Rate)** | {result.get('win_rate', 0.0):.1f}% | |
| **손익비 (Profit Factor)** | {result.get('profit_factor', 0.0):.2f} | |
| **기대수익률 (Expectancy)** | {result.get('expectancy_pct', 0.0):+.2f}% / 회 | |
| **수수료 및 슬리피지** | 편도 {result.get('fee_rate', 0.0)*100:.2f}% / 슬리피지 {result.get('slippage_rate', 0.0)*100:.2f}% | 보수적 마찰비용 반영 |
"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)


def main():
    parser = argparse.ArgumentParser(description="빗썸 퀀트 백테스팅 시뮬레이터 v2.0")
    parser.add_argument("--market", type=str, default="KRW-BTC", help="테스트할 마켓 (예: KRW-BTC, KRW-ENA)")
    parser.add_argument("--unit", type=int, default=5, help="분봉 단위 (1, 3, 5, 15, 60)")
    parser.add_argument("--count", type=int, default=200, help="캔들 개수 (최대 2000)")
    parser.add_argument("--capital", type=float, default=1_000_000.0, help="초기 자본금")
    parser.add_argument("--fee-rate", type=float, default=0.0004, help="편도 수수료율 (기본: 0.04%%)")
    parser.add_argument("--slippage-rate", type=float, default=0.001, help="편도 슬리피지율 (기본: 0.10%%)")
    parser.add_argument("--export-json", type=str, default="", help="JSON 리포트 저장 경로")
    parser.add_argument("--export-md", type=str, default="", help="Markdown 리포트 저장 경로")
    args = parser.parse_args()

    backtester = QuantBacktester(args.capital, args.fee_rate, args.slippage_rate)
    res = backtester.run_backtest(market=args.market, unit=args.unit, count=args.count)

    if res:
        if args.export_json:
            QuantBacktester.save_report_json(res, args.export_json)
        if args.export_md:
            QuantBacktester.save_report_markdown(res, args.export_md)


if __name__ == "__main__":
    main()
