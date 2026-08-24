import datetime
import json
import logging
import os
import time
from typing import Any

import requests

from bithumb_api import BithumbAPI
from order_safety import write_json_atomically

logger = logging.getLogger(__name__)


def get_kst_now() -> datetime.datetime:
    """한국 표준시(KST) 현재 datetime 반환"""
    kst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(kst)


def get_kst_now_str() -> str:
    """한국 표준시(KST) 포맷팅 문자열 반환"""
    return get_kst_now().strftime("%Y-%m-%d %H:%M:%S")


def get_fear_and_greed_index() -> dict[str, Any]:
    """글로벌 가상자산 크립토 공포 & 탐욕 지수 실시간 조회"""
    try:
        res = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        if res.status_code == 200:
            data = res.json().get("data", [{}])[0]
            val = int(data.get("value", 50))
            classification = data.get("value_classification", "Neutral")

            korean_map = {
                "Extreme Fear": "😱 극단적 공포 (투매/바닥권)",
                "Fear": "😨 공포 (보수적 접근)",
                "Neutral": "😐 중립 (균형)",
                "Greed": "🤑 탐욕 (상승 모멘텀)",
                "Extreme Greed": "🚀 극단적 탐욕 (과열/익절권)",
            }
            desc = korean_map.get(classification, classification)
            return {
                "value": val,
                "classification": classification,
                "desc": f"{val}점 ({desc})",
                "is_extreme_fear": val <= 25,
                "is_extreme_greed": val >= 75,
            }
    except (requests.exceptions.RequestException, KeyError, ValueError):
        pass
    return {
        "value": 50,
        "classification": "Neutral",
        "desc": "50점 (중립)",
        "is_extreme_fear": False,
        "is_extreme_greed": False,
    }


def get_excluded_manual_holdings() -> set[str]:
    """
    수동 매매 전용으로 봇의 자동 매매 및 자산 평가에서 완전히 격리할 종목/화폐 집합 반환
    예: EXCLUDED_MANUAL_HOLDINGS="KRW-HOLO,HOLO" -> {"KRW-HOLO", "HOLO"}
    """
    raw = os.getenv("EXCLUDED_MANUAL_HOLDINGS", "KRW-HOLO,HOLO").strip()
    if not raw:
        return set()
    items = set()
    for item in raw.split(","):
        s = item.strip().upper()
        if s:
            items.add(s)
            if s.startswith("KRW-"):
                items.add(s.replace("KRW-", ""))
            else:
                items.add(f"KRW-{s}")
    return items


def calculate_total_equity(balances: dict[str, dict[str, float]], bithumb: Any) -> float:
    """원화 잔고 및 보유 코인 평가금액을 합산하여 총 평가 자산 계산 (수동 격리 종목 완전 배제)"""
    krw_balance = balances.get("KRW", {}).get("balance", 0.0) + balances.get("KRW", {}).get("locked", 0.0)
    total_coin_val = 0.0
    excluded = get_excluded_manual_holdings()

    for cur, info in balances.items():
        if cur == "KRW" or cur in excluded or f"KRW-{cur}" in excluded:
            continue
        vol = info.get("balance", 0.0) + info.get("locked", 0.0)
        if vol > 0:
            try:
                price = bithumb.get_current_price(f"KRW-{cur}")
                total_coin_val += vol * price
            except (requests.exceptions.RequestException, KeyError, ValueError):
                logger.debug(f"{cur} 잔고 시세 조회 예외 무시")

    return krw_balance + total_coin_val


def get_held_markets(balances: dict[str, dict[str, float]], bithumb: Any, min_val_krw: float = 4000.0) -> list[str]:
    """현재 의미 있게 보유 중인(4,000원 이상) 마켓 코드 목록 반환 (수동 격리 종목 완전 배제)"""
    held = []
    excluded = get_excluded_manual_holdings()
    for cur, info in balances.items():
        if cur in ("KRW", "P") or cur in excluded or f"KRW-{cur}" in excluded:
            continue
        total_vol = info.get("balance", 0.0) + info.get("locked", 0.0)
        if total_vol > 0:
            market = f"KRW-{cur}"
            try:
                price = bithumb.get_current_price(market)
                if total_vol * price >= min_val_krw:
                    held.append(market)
            except (requests.exceptions.RequestException, KeyError, ValueError):
                logger.debug(f"{market} 보유 여부 확인 예외 무시")
    return held


def build_positions_data(
    balances: dict[str, dict[str, float]],
    bithumb: Any,
    strategies: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """웹 대시보드 표시용 보유 코인 포지션 목록 생성 (수동 격리 종목 완전 배제)"""
    positions = []
    strategies = strategies or {}
    excluded = get_excluded_manual_holdings()
    for cur, info in balances.items():
        if cur in ("KRW", "P") or cur in excluded or f"KRW-{cur}" in excluded:
            continue
        vol = info.get("balance", 0.0) + info.get("locked", 0.0)
        if vol <= 0:
            continue
        market = f"KRW-{cur}"
        try:
            price = bithumb.get_current_price(market)
            if price <= 0:
                continue
            val = vol * price
            if val < 1000.0:  # 1천원 미만 자투리/에어드랍 먼지 제외
                continue
            avg_price = info.get("avg_buy_price", 0.0)
            pnl_pct = ((price - avg_price) / avg_price * 100) if avg_price > 0 else 0.0
            strat = strategies.get(market, {})
            positions.append({
                "market": market,
                "korean_name": bithumb.get_korean_name(market),
                "current_price": price,
                "balance": f"{vol:.6f}".rstrip("0").rstrip("."),
                "value": int(val),
                "pnl_pct": pnl_pct,
                "action": strat.get("ACTION", "HOLD"),
                "target_price": strat.get("TARGET_PRICE", 0),
                "stop_loss": strat.get("STOP_LOSS", 0),
                "reason": strat.get("REASON", "보유 중 (AI 실시간 관망 및 모니터링)"),
            })
        except (requests.exceptions.RequestException, KeyError, ValueError):
            continue
    return positions


class TrailingStopTracker:
    """
    [50% 분할 익절 + 50% 가속 트레일링 러너] 관리자 (영구 저장 연동)
    """

    def __init__(self, start_profit_pct: float = 0.02, trailing_drop_pct: float = 0.012, data_dir: str | None = None):
        self.start_profit_pct = start_profit_pct
        self.trailing_drop_pct = trailing_drop_pct
        self.peaks: dict[str, float] = {}
        self.partial_tp_done: dict[str, bool] = {}
        self.entry_times: dict[str, float] = {}
        self.data_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.state_file = os.path.join(self.data_dir, "position_state.json")
        self._load_state()

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.peaks = data.get("peaks", {})
                    self.partial_tp_done = data.get("partial_tp_done", {})
                    self.entry_times = data.get("entry_times", {})
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.warning(f"포지션 상태 파일 로드 실패: {e}")

    def _save_state(self):
        try:
            write_json_atomically(self.state_file, {
                "peaks": self.peaks,
                "partial_tp_done": self.partial_tp_done,
                "entry_times": self.entry_times,
            })
        except OSError as e:
            logger.warning(f"포지션 상태 파일 저장 실패: {e}")

    def set_entry_time(self, market: str, ts: float | None = None):
        self.entry_times[market] = ts or time.time()
        self._save_state()

    def get_entry_time(self, market: str) -> float:
        if market not in self.entry_times:
            self.entry_times[market] = time.time()
            self._save_state()
        return self.entry_times[market]

    def check_position(
        self, market: str, current_price: float, avg_buy_price: float
    ) -> tuple[str, float, float, float, float]:
        if avg_buy_price <= 0:
            return "NONE", 0.0, 0.0, 0.0, 0.0

        current_profit_rate = (current_price - avg_buy_price) / avg_buy_price
        current_profit_pct = current_profit_rate * 100.0

        # 1. [1차 50% 분할 익절 체크 (+2.5% 이상 도달 시)]
        if current_profit_rate >= 0.025 and not self.partial_tp_done.get(market, False):
            self.partial_tp_done[market] = True
            self.peaks[market] = max(self.peaks.get(market, avg_buy_price), current_price)
            self._save_state()
            return "PARTIAL_TP", current_price, current_price, current_profit_pct, current_profit_pct

        # 2. [수익률 단계별 가속 트레일링 스탑 (Ratchet Tightening)]
        if current_profit_rate >= self.start_profit_pct or self.partial_tp_done.get(market, False):
            previous_peak = self.peaks.get(market, avg_buy_price)
            current_peak = max(previous_peak, current_price)
            self.peaks[market] = current_peak
            self._save_state()

            peak_profit_pct = ((current_peak - avg_buy_price) / avg_buy_price) * 100.0

            if peak_profit_pct >= 10.0:
                active_drop_pct = 0.005  # +10% 이상: 0.5% 초밀착
            elif peak_profit_pct >= 5.0:
                active_drop_pct = 0.008  # +5% 이상: 0.8% 밀착
            else:
                active_drop_pct = self.trailing_drop_pct  # 기본 1.2%

            trailing_stop_price = current_peak * (1.0 - active_drop_pct)

            # 수수료(0.1%) 차감 후 최소 +0.2% 순수익 안전 보장
            min_guaranteed_profit = avg_buy_price * 1.002
            trailing_stop_price = max(trailing_stop_price, min_guaranteed_profit)

            logger.info(
                f"🎯 [{market}] 가속 트레일링 추적 중: 최고점 {current_peak:,.2f}원 (+{peak_profit_pct:.2f}% | 드롭폭 {active_drop_pct*100:.1f}%) ➜ 익절기준선 {trailing_stop_price:,.2f}원"
            )

            if current_price <= trailing_stop_price:
                realized_profit_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                self.clear(market)
                return "TRAILING_STOP", current_peak, trailing_stop_price, peak_profit_pct, realized_profit_pct

        return "NONE", self.peaks.get(market, current_price), 0.0, 0.0, current_profit_pct

    def clear(self, market: str):
        self.peaks.pop(market, None)
        self.partial_tp_done.pop(market, None)
        self.entry_times.pop(market, None)
        self._save_state()

    def reconcile_markets(self, held_markets: list[str]) -> int:
        """Drop stale trailing state after a restart; exchange balances are authoritative."""
        stale_markets = (set(self.peaks) | set(self.partial_tp_done) | set(self.entry_times)) - set(held_markets)
        for market in stale_markets:
            self.peaks.pop(market, None)
            self.partial_tp_done.pop(market, None)
            self.entry_times.pop(market, None)
        if stale_markets:
            self._save_state()
        return len(stale_markets)


class DailyRiskManager:
    """
    일일 손익 추적, 킬 스위치(Kill-Switch), 장중 입출금 보정 및 연속 손절 30분 쿨다운 관리자 (영구 저장 연동)
    """

    def __init__(self, max_loss_pct: float = 0.05, data_dir: str | None = None):
        self.max_loss_pct = max_loss_pct
        self.data_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.stats_file = os.path.join(self.data_dir, "daily_stats.json")

        self.current_date_str = ""
        self.daily_start_equity = 0.0
        self.last_known_equity = 0.0
        self.realized_pnl_krw = 0.0
        self.kill_switch_active = False
        self.total_trades_today = 0
        self.win_trades_today = 0
        self.consecutive_losses = 0
        self.cooldown_until_ts = 0.0
        self.daily_history: list[dict[str, Any]] = []
        self._load_state()

    def _load_state(self):
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.current_date_str = data.get("date", "")
                    self.daily_start_equity = float(data.get("start_equity", 0.0))
                    self.last_known_equity = self.daily_start_equity
                    self.realized_pnl_krw = float(data.get("realized_pnl_krw", 0.0))
                    self.total_trades_today = int(data.get("total_trades", 0))
                    self.win_trades_today = int(data.get("win_trades", 0))
                    self.consecutive_losses = int(data.get("consecutive_losses", 0))
                    self.cooldown_until_ts = float(data.get("cooldown_until_ts", 0.0))
                    self.daily_history = data.get("history", [])
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.warning(f"일일 통계 로드 실패: {e}")

    def _save_state(self):
        try:
            write_json_atomically(self.stats_file, {
                "date": self.current_date_str,
                "start_equity": self.daily_start_equity,
                "realized_pnl_krw": self.realized_pnl_krw,
                "total_trades": self.total_trades_today,
                "win_trades": self.win_trades_today,
                "consecutive_losses": self.consecutive_losses,
                "cooldown_until_ts": self.cooldown_until_ts,
                "history": self.daily_history,
            })
        except OSError as e:
            logger.warning(f"일일 통계 저장 실패: {e}")

    def add_realized_trade(self, pnl_krw: float, is_win: bool):
        self.realized_pnl_krw += pnl_krw
        self.total_trades_today += 1
        if is_win:
            self.win_trades_today += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            if self.consecutive_losses >= 2:
                self.cooldown_until_ts = time.time() + 1800.0  # 30분 쿨다운
                logger.warning("🛑 [연속 2회 손절 발생] 30분간 뇌동매매 방지 신규 매수 쿨다운 가동!")

        self._save_state()

    def is_cooling_down(self) -> tuple[bool, int]:
        now_ts = time.time()
        if now_ts < self.cooldown_until_ts:
            remain_minutes = max(1, int((self.cooldown_until_ts - now_ts) / 60))
            return True, remain_minutes
        return False, 0

    def update_daily_equity(self, current_total_equity: float, now_kst: datetime.datetime) -> tuple[bool, float]:
        date_key = now_kst.strftime("%Y-%m-%d")

        if date_key != self.current_date_str or self.daily_start_equity <= 0:
            if date_key != self.current_date_str and self.current_date_str:
                self.daily_history.append({
                    "date": self.current_date_str,
                    "total_trades": self.total_trades_today,
                    "win_trades": self.win_trades_today,
                    "realized_pnl_krw": self.realized_pnl_krw,
                })
                self.realized_pnl_krw = 0.0
                self.total_trades_today = 0
                self.win_trades_today = 0

            self.current_date_str = date_key
            self.daily_start_equity = current_total_equity
            self.last_known_equity = current_total_equity
            self.kill_switch_active = False
            self._save_state()
            logger.info(f"📅 [일일 손익 기준일 갱신: {date_key}] 시작 총 자산: {self.daily_start_equity:,.0f}원")

        # 💵 당일 장중 외부 자금 입출금 자동 감지 및 기준자산 자동 보정 (Cashflow Adjustment)
        elif self.last_known_equity > 0:
            equity_jump = current_total_equity - self.last_known_equity
            if abs(equity_jump) >= 10000.0:
                old_start = self.daily_start_equity
                self.daily_start_equity += equity_jump
                self._save_state()
                logger.info(
                    f"💵 [장중 외부 자금 입출금 감지] 변동액: {equity_jump:+,.0f}원 ➜ 당일 시작 기준자산 보정: {old_start:,.0f}원 -> {self.daily_start_equity:,.0f}원"
                )

        self.last_known_equity = current_total_equity

        daily_pnl_pct = (
            (current_total_equity - self.daily_start_equity) / self.daily_start_equity
            if self.daily_start_equity > 0
            else 0.0
        )

        if daily_pnl_pct <= -self.max_loss_pct:
            if not self.kill_switch_active:
                self.kill_switch_active = True
                logger.warning(
                    f"🛑 [일일 킬 스위치 발동!] 당일 손실률({daily_pnl_pct*100:.2f}%)이 한도(-{self.max_loss_pct*100:.1f}%)를 초과했습니다."
                )
        else:
            self.kill_switch_active = False

        return self.kill_switch_active, daily_pnl_pct
