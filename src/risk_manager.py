import datetime
import logging
import os
import threading
import time
from typing import Any

import requests

from bithumb_api import BithumbAPI
from db_manager import get_db_manager
from market_policy import get_excluded_markets
from state_store import load_json_with_backup_recovery, write_json_atomically
from strategy_engine import StrategyPolicy, is_major_market

logger = logging.getLogger(__name__)


def get_kst_now() -> datetime.datetime:
    """한국 표준시(KST) 현재 datetime 반환"""
    kst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(kst)


def get_kst_now_str() -> str:
    """한국 표준시(KST) 포맷팅 문자열 반환"""
    return get_kst_now().strftime("%Y-%m-%d %H:%M:%S")


_FNG_CACHE: dict[str, Any] = {}
_FNG_CACHE_TS: float = 0.0


def get_fear_and_greed_index(ttl_seconds: float = 600.0) -> dict[str, Any]:
    """글로벌 가상자산 크립토 공포 & 탐욕 지수 실시간 조회 (10분 캐싱으로 대시보드 지연 및 Rate Limit 방지)"""
    global _FNG_CACHE, _FNG_CACHE_TS
    now = time.time()
    if _FNG_CACHE and (now - _FNG_CACHE_TS < ttl_seconds):
        return _FNG_CACHE

    try:
        res = requests.get("https://api.alternative.me/fng/?limit=1", timeout=3)
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
            result = {
                "value": val,
                "classification": classification,
                "desc": f"{val}점 ({desc})",
                "is_extreme_fear": val <= 25,
                "is_extreme_greed": val >= 75,
            }
            _FNG_CACHE = result
            _FNG_CACHE_TS = now
            return result
    except (requests.exceptions.RequestException, KeyError, ValueError):
        pass

    if _FNG_CACHE:
        return _FNG_CACHE

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
    - KRW-HOLO, HOLO는 어떠한 경우에도 자동매매/자산평가 대상에서 엄격히 제외
    """
    return get_excluded_markets()


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
                if price > 0 and (total_vol * price) >= min_val_krw:
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
            action = strat.get("action") or strat.get("ACTION") or "HOLD"
            target_price = float(strat.get("target_price") or strat.get("TARGET_PRICE") or 0.0)
            stop_loss = float(strat.get("stop_loss") or strat.get("STOP_LOSS") or 0.0)
            if target_price <= 0.0 and price > 0.0:
                target_price = price * (1.0 + StrategyPolicy.PARTIAL_TP_1_PCT)
            if stop_loss <= 0.0 and price > 0.0:
                stop_loss = price * (1.0 - StrategyPolicy.STOP_LOSS_PCT)

            reason = strat.get("reason") or strat.get("REASON") or "보유 중 (AI 실시간 관망 및 모니터링)"
            alpha_score = int(strat.get("alpha_score", 0) or 0)
            factor_breakdown = strat.get("factor_breakdown", {})
            target_pct = float(strat.get("target_pct") or (((target_price - price) / price * 100) if price > 0 and target_price > 0 else 0.0))
            stop_pct = float(strat.get("stop_pct") or (((stop_loss - price) / price * 100) if price > 0 and stop_loss > 0 else 0.0))
            rr_denom = max(1e-6, price - stop_loss)
            rr_ratio = float(strat.get("risk_reward_ratio") or (((target_price - price) / rr_denom) if price > stop_loss > 0 and target_price > price else 0.0))
            if rr_ratio <= 0.0 and abs(stop_pct) > 0.0:
                rr_ratio = target_pct / abs(stop_pct)

            positions.append({
                "market": market,
                "korean_name": bithumb.get_korean_name(market),
                "current_price": price,
                "avg_buy_price": avg_price,
                "balance": f"{vol:.6f}".rstrip("0").rstrip("."),
                "value": int(val),
                "pnl_pct": pnl_pct,
                "action": action,
                "target_price": target_price,
                "stop_loss": stop_loss,
                "reason": reason,
                "alpha_score": alpha_score,
                "factor_breakdown": factor_breakdown,
                "target_pct": round(target_pct, 2),
                "stop_pct": round(stop_pct, 2),
                "risk_reward_ratio": round(rr_ratio, 2),
            })
        except (requests.exceptions.RequestException, KeyError, ValueError):
            continue
    return positions


def build_candidates_data(
    balances: dict[str, dict[str, float]],
    exchange_api: Any,
    strategies: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """웹 대시보드 표시용 신규 스캔 종목 AI 진입 전략 후보군 (미보유 종목)"""
    candidates = []
    strategies = strategies or {}
    excluded = get_excluded_manual_holdings()

    # 현재 보유 중인 마켓 목록 식별
    held_markets = set()
    for cur, info in balances.items():
        if cur in ("KRW", "P") or cur in excluded or f"KRW-{cur}" in excluded:
            continue
        vol = info.get("balance", 0.0) + info.get("locked", 0.0)
        if vol > 0:
            held_markets.add(f"KRW-{cur}")

    for market, strat in strategies.items():
        if not market or not isinstance(strat, dict):
            continue
        if market in held_markets or market.replace("KRW-", "") in excluded or market in excluded:
            continue

        try:
            curr_price = float(strat.get("current_price", 0.0) or 0.0)
            if curr_price <= 0:
                curr_price = float(exchange_api.get_current_price(market) or 0.0)

            korean_name = strat.get("korean_name") or exchange_api.get_korean_name(market)
            action = strat.get("action") or strat.get("ACTION") or "HOLD"
            entry_price = float(strat.get("entry_price", curr_price) or curr_price)
            target_price = float(strat.get("target_price") or strat.get("TARGET_PRICE") or 0.0)
            stop_loss = float(strat.get("stop_loss") or strat.get("STOP_LOSS") or 0.0)
            if target_price <= 0.0 and curr_price > 0.0:
                target_price = curr_price * (1.0 + StrategyPolicy.PARTIAL_TP_1_PCT)
            if stop_loss <= 0.0 and curr_price > 0.0:
                stop_loss = curr_price * (1.0 - StrategyPolicy.STOP_LOSS_PCT)

            alloc_pct = float(strat.get("alloc_pct", 0.0) or 0.0)
            reason = strat.get("reason") or strat.get("REASON") or "스캔 분석 완료"
            alpha_score = int(strat.get("alpha_score", 0) or 0)
            allow_buy = bool(strat.get("allow_buy", False))
            factor_breakdown = strat.get("factor_breakdown", {})
            updated_at = strat.get("updated_at", "")

            target_pct = float(strat.get("target_pct") or (((target_price - curr_price) / curr_price * 100) if curr_price > 0 and target_price > 0 else 0.0))
            stop_pct = float(strat.get("stop_pct") or (((stop_loss - curr_price) / curr_price * 100) if curr_price > 0 and stop_loss > 0 else 0.0))
            rr_denom = max(1e-6, curr_price - stop_loss)
            rr_ratio = float(strat.get("risk_reward_ratio") or (((target_price - curr_price) / rr_denom) if curr_price > stop_loss > 0 and target_price > curr_price else 0.0))
            if rr_ratio <= 0.0 and abs(stop_pct) > 0.0:
                rr_ratio = target_pct / abs(stop_pct)

            candidates.append({
                "market": market,
                "korean_name": korean_name,
                "current_price": curr_price,
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss": stop_loss,
                "action": action,
                "alloc_pct": alloc_pct,
                "reason": reason,
                "alpha_score": alpha_score,
                "allow_buy": allow_buy,
                "factor_breakdown": factor_breakdown,
                "target_pct": round(target_pct, 2),
                "stop_pct": round(stop_pct, 2),
                "risk_reward_ratio": round(rr_ratio, 2),
                "updated_at": updated_at,
            })
        except (requests.exceptions.RequestException, KeyError, ValueError, TypeError) as e:
            logger.debug(f"후보 종목 {market} 대시보드 데이터 생성 예외: {e}")
            continue

    # 1. 매수 허용(allow_buy) 여부 우선, 2. 알파 점수 높은 순 정렬
    candidates.sort(key=lambda x: (1 if x.get("allow_buy") else 0, x.get("alpha_score", 0)), reverse=True)
    return candidates


class TrailingStopTracker:
    """
    [3단계 다단계 분할 익절 + 40% 잔여 러너 가속 트레일링] 관리자 (영구 저장 연동, RLock 스레드 안전성 보장)
    """

    def __init__(self, start_profit_pct: float = 0.030, trailing_drop_pct: float = 0.020, data_dir: str | None = None):
        self._lock = threading.RLock()
        self.start_profit_pct = start_profit_pct
        self.trailing_drop_pct = trailing_drop_pct
        self.macro_defensive_mode = False
        self.peaks: dict[str, float] = {}
        self.partial_tp_done: dict[str, Any] = {}
        self.entry_times: dict[str, float] = {}
        self._exiting_markets: set[str] = set()
        self._last_log_ts: dict[str, float] = {}
        self._last_logged_peak: dict[str, float] = {}
        self.data_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.state_file = os.path.join(self.data_dir, "position_state.json")
        self.exchange = "upbit" if "upbit" in self.data_dir.lower() else "bithumb"
        self.db = get_db_manager(os.path.join(os.path.dirname(self.data_dir) if "upbit" in self.data_dir.lower() else self.data_dir, "trading.db"))
        self._load_state()

    def set_macro_defensive_mode(self, enabled: bool) -> None:
        """거시 BTC 급락 또는 위기 감지 시 전 포지션 트레일링 타이트닝 비상 방어 모드 설정"""
        with self._lock:
            if self.macro_defensive_mode != enabled:
                self.macro_defensive_mode = enabled
                if enabled:
                    logger.warning("🚨 [거시 BTC 급락 감지] ➜ 전 포지션 [비상 방어 모드] 가동 (익절 시작 +0.8% 초밀착 타이트닝)")
                else:
                    logger.info("🟢 [거시 시장 안정 복귀] ➜ 전 포지션 정상 트레일링 모드 복귀")

    def acquire_exit_lock(self, market: str) -> bool:
        """동일 종목의 동시 다발적 청산 주문 제출을 방지하기 위한 원자적 락 (P0-2)"""
        m_upper = market.upper()
        with self._lock:
            if m_upper in self._exiting_markets:
                return False
            self._exiting_markets.add(m_upper)
            return True

    def release_exit_lock(self, market: str) -> None:
        """청산 락 해제"""
        m_upper = market.upper()
        with self._lock:
            self._exiting_markets.discard(m_upper)

    def is_exiting(self, market: str) -> bool:
        """현재 청산 진행 중 여부 확인"""
        with self._lock:
            return market.upper() in self._exiting_markets

    def _load_state(self):
        with self._lock:
            data = load_json_with_backup_recovery(self.state_file, default={})
            if isinstance(data, dict):
                self.peaks = data.get("peaks", {})
                self.partial_tp_done = data.get("partial_tp_done", {})
                self.entry_times = data.get("entry_times", {})

    def _save_state(self):
        with self._lock:
            payload = {
                "peaks": self.peaks,
                "partial_tp_done": self.partial_tp_done,
                "entry_times": self.entry_times,
            }
            try:
                write_json_atomically(self.state_file, payload)
            except OSError as e:
                logger.warning(f"포지션 상태 파일 저장 실패: {e}")
            try:
                self.db.save_position_state(self.exchange, payload)
            except Exception as e:
                logger.debug(f"SQLite 포지션 저장 예외: {e}")

    def set_entry_time(self, market: str, ts: float | None = None):
        with self._lock:
            self.entry_times[market] = ts or time.time()
            self._save_state()

    def get_entry_time(self, market: str) -> float:
        with self._lock:
            return float(self.entry_times.get(market, 0.0))

    def check_position(
        self, market: str, current_price: float, avg_buy_price: float
    ) -> tuple[str, float, float, float, float]:
        if avg_buy_price <= 0:
            return "NONE", 0.0, 0.0, 0.0, 0.0

        current_profit_rate = (current_price - avg_buy_price) / avg_buy_price
        current_profit_pct = current_profit_rate * 100.0

        with self._lock:
            is_major = is_major_market(market)
            tp_1_target = StrategyPolicy.MAJOR_PARTIAL_TP_1_PCT if is_major else StrategyPolicy.PARTIAL_TP_1_PCT
            tp_2_target = StrategyPolicy.MAJOR_PARTIAL_TP_2_PCT if is_major else StrategyPolicy.PARTIAL_TP_2_PCT

            # 1. [3단계 다단계 분할 익절: 1차(30%) / 2차(30%) / 3차 잔여(40%) 가속 트레일링]
            raw_stage = self.partial_tp_done.get(market, 0)
            cur_stage = 1 if raw_stage is True else int(raw_stage or 0)

            # 1-A. 1차 30% 분할 익절 (메이저 +1.5% / 알트 +2.5% 도달 시)
            if current_profit_rate >= tp_1_target and cur_stage < 1:
                self.partial_tp_done[market] = 1
                self.peaks[market] = max(self.peaks.get(market, avg_buy_price), current_price)
                self._save_state()
                return "PARTIAL_TP_1", current_price, current_price, current_profit_pct, current_profit_pct

            # 1-B. 2차 30% 추가 분할 익절 (메이저 +3.0% / 알트 +5.0% 도달 시)
            if current_profit_rate >= tp_2_target and cur_stage < 2:
                self.partial_tp_done[market] = 2
                self.peaks[market] = max(self.peaks.get(market, avg_buy_price), current_price)
                self._save_state()
                return "PARTIAL_TP_2", current_price, current_price, current_profit_pct, current_profit_pct

            # 2. [수익률 단계별 가속 트레일링 스탑 (Ratchet Tightening)]
            # 비상 방어 모드 가동 시: +0.8%부터 즉시 0.4% 초밀착 트레일링 가동
            if self.macro_defensive_mode:
                effective_start_pct = 0.008
                base_drop_pct = 0.004
            elif is_major:
                effective_start_pct = StrategyPolicy.MAJOR_TRAILING_START_PCT  # +1.2%
                base_drop_pct = StrategyPolicy.MAJOR_TRAILING_DROP_PCT        # 0.8%
            else:
                effective_start_pct = self.start_profit_pct                    # +2.0%
                base_drop_pct = self.trailing_drop_pct                        # 1.2%

            has_peak_trailing = (market in self.peaks and self.peaks[market] >= avg_buy_price * (1.0 + effective_start_pct))

            if current_profit_rate >= effective_start_pct or (cur_stage >= 1) or has_peak_trailing:
                previous_peak = self.peaks.get(market, avg_buy_price)
                current_peak = max(previous_peak, current_price)
                self.peaks[market] = current_peak
                self._save_state()

                peak_profit_pct = ((current_peak - avg_buy_price) / avg_buy_price) * 100.0

                if self.macro_defensive_mode:
                    active_drop_pct = 0.004  # 비상 방어 모드: 0.4% 극초밀착
                elif is_major:
                    active_drop_pct = min(base_drop_pct, 0.010)  # 메이저: 1.0% 밀착
                elif peak_profit_pct >= 20.0:
                    active_drop_pct = 0.012  # +20% 이상 폭등 구간: 1.2% 고점 추적
                elif peak_profit_pct >= 10.0:
                    active_drop_pct = 0.015  # +10% 이상 대세 상승: 1.5% 추적
                elif peak_profit_pct >= 5.0:
                    active_drop_pct = 0.018  # +5% 이상: 1.8% 추적
                else:
                    active_drop_pct = base_drop_pct  # 기본 2.0% 버퍼 (노이즈 방어)

                trailing_stop_price = current_peak * (1.0 - active_drop_pct)

                # 수수료 및 슬리피지 차감 후 최소 안전 마진 확보 (메이저 +0.3%, 알트 +0.5%)
                min_buffer = 1.003 if is_major else 1.005
                min_guaranteed_profit = avg_buy_price * min_buffer
                trailing_stop_price = max(trailing_stop_price, min_guaranteed_profit)

                now_ts = time.time()
                is_new_peak = current_peak > (self._last_logged_peak.get(market, 0.0) + 1e-6)
                is_time_to_log = (now_ts - self._last_log_ts.get(market, 0.0)) >= 15.0

                if is_new_peak or is_time_to_log:
                    mode_tag = " [🚨비상방어]" if self.macro_defensive_mode else ""
                    logger.info(
                        f"🎯 [{market}]{mode_tag} 가속 트레일링 추적 중: 최고점 {current_peak:,.2f}원 (+{peak_profit_pct:.2f}% | 드롭폭 {active_drop_pct*100:.1f}%) ➜ 익절기준선 {trailing_stop_price:,.2f}원"
                    )
                    self._last_log_ts[market] = now_ts
                    self._last_logged_peak[market] = current_peak

                if current_price <= trailing_stop_price:
                    realized_profit_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                    self.clear(market)
                    return "TRAILING_STOP", current_peak, trailing_stop_price, peak_profit_pct, realized_profit_pct

            return "NONE", self.peaks.get(market, current_price), 0.0, 0.0, current_profit_pct

    def get_tp_stage(self, market: str) -> int:
        """현재 포지션의 분할 익절 단계 반환 (0=미실행, 1=1차 완료, 2=2차 완료)"""
        with self._lock:
            raw = self.partial_tp_done.get(market, 0)
            return 1 if raw is True else int(raw or 0)

    def is_breakeven_active(self, market: str) -> bool:
        """1차 분할 익절 완료 후 본전 보장(Break-Even) 스탑 가동 여부"""
        return self.get_tp_stage(market) >= 1

    def clear(self, market: str):
        with self._lock:
            self.peaks.pop(market, None)
            self.partial_tp_done.pop(market, None)
            self.entry_times.pop(market, None)
            self._last_log_ts.pop(market, None)
            self._last_logged_peak.pop(market, None)
            self._save_state()

    def reconcile_markets(self, held_markets: list[str]) -> int:
        """Drop stale trailing state after a restart; exchange balances are authoritative."""
        with self._lock:
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
    일일 손익 추적, 킬 스위치(Kill-Switch Latch) 및 연속 손절 30분 쿨다운 관리자 (영구 저장 연동, RLock 스레드 안전성 보장, P1-1)
    """

    def __init__(self, max_loss_pct: float = 0.05, data_dir: str | None = None):
        self._lock = threading.RLock()
        self.max_loss_pct = max_loss_pct
        self.data_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.stats_file = os.path.join(self.data_dir, "daily_stats.json")
        self.exchange = "upbit" if "upbit" in self.data_dir.lower() else "bithumb"
        self.db = get_db_manager(os.path.join(os.path.dirname(self.data_dir) if "upbit" in self.data_dir.lower() else self.data_dir, "trading.db"))

        self.current_date_str = ""
        self.daily_start_equity = 0.0
        self.last_known_equity = 0.0
        self.realized_pnl_krw = 0.0
        self.kill_switch_active = False
        self.kill_switch_latched_date = ""
        self.total_trades_today = 0
        self.win_trades_today = 0
        self.consecutive_losses = 0
        self.cooldown_until_ts = 0.0
        self.daily_history: list[dict[str, Any]] = []
        self._load_state()

    def _load_state(self):
        with self._lock:
            data = load_json_with_backup_recovery(self.stats_file, default={})
            if isinstance(data, dict):
                self.current_date_str = data.get("date", "")
                self.daily_start_equity = float(data.get("start_equity", 0.0))
                self.last_known_equity = self.daily_start_equity
                self.realized_pnl_krw = float(data.get("realized_pnl_krw", 0.0))
                self.total_trades_today = int(data.get("total_trades", 0))
                self.win_trades_today = int(data.get("win_trades", 0))
                self.consecutive_losses = int(data.get("consecutive_losses", 0))
                self.cooldown_until_ts = float(data.get("cooldown_until_ts", 0.0))
                self.kill_switch_latched_date = data.get("kill_switch_latched_date", "")
                self.daily_history = data.get("history", [])

                # 오늘 날짜와 일치하는 킬스위치 Latch 복원
                now_kst_date = get_kst_now().strftime("%Y-%m-%d")
                if self.kill_switch_latched_date == now_kst_date:
                    self.kill_switch_active = True
                    logger.warning(f"🛑 [일일 킬 스위치 복원] {now_kst_date} 킬스위치 Latch가 활성 상태입니다.")

    def _save_state(self):
        with self._lock:
            payload = {
                "date": self.current_date_str,
                "start_equity": self.daily_start_equity,
                "realized_pnl_krw": self.realized_pnl_krw,
                "total_trades": self.total_trades_today,
                "win_trades": self.win_trades_today,
                "consecutive_losses": self.consecutive_losses,
                "cooldown_until_ts": self.cooldown_until_ts,
                "kill_switch_latched_date": self.kill_switch_latched_date,
                "kill_switch_active": self.kill_switch_active,
                "history": self.daily_history,
            }
            try:
                write_json_atomically(self.stats_file, payload)
            except OSError as e:
                logger.warning(f"일일 통계 저장 실패: {e}")
            if self.current_date_str:
                try:
                    self.db.save_daily_stats(self.exchange, self.current_date_str, payload)
                except Exception as e:
                    logger.debug(f"SQLite 일일 통계 저장 예외: {e}")

    def add_realized_trade(self, pnl_krw: float, is_win: bool):
        with self._lock:
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
        with self._lock:
            now_ts = time.time()
            if now_ts < self.cooldown_until_ts:
                remain_minutes = max(1, int((self.cooldown_until_ts - now_ts) / 60))
                return True, remain_minutes
            return False, 0

    def get_risk_scale_factor(self) -> float:
        """연속 손실 횟수 기반 동적 자본 디스케일링 배율 반환 (0회: 1.0, 1회: 0.8, 2회 이상: 0.5)"""
        with self._lock:
            if self.consecutive_losses == 0:
                return 1.0
            elif self.consecutive_losses == 1:
                return 0.8
            else:
                return 0.5

    def register_cashflow(self, amount_krw: float, reason: str = "명시적 입출금") -> None:
        """명시적으로 확인된 외부 자금 입출금 시 시작 기준자산 수동/원장 보정 (P1-1)"""
        with self._lock:
            old_start = self.daily_start_equity
            self.daily_start_equity += amount_krw
            self._save_state()
            logger.info(
                f"💵 [명시적 자금 입출금 반영: {reason}] 변동액: {amount_krw:+,.0f}원 ➜ 당일 시작 기준자산 보정: {old_start:,.0f}원 -> {self.daily_start_equity:,.0f}원"
            )

    def manual_reset_kill_switch(self) -> None:
        """관리자 명시적 승인에 의한 당일 킬스위치 수동 해제"""
        with self._lock:
            self.kill_switch_active = False
            self.kill_switch_latched_date = ""
            self._save_state()
            logger.info("🔓 [관리자 수동 명령] 일일 킬 스위치가 수동 해제되었습니다.")

    def adjust_for_current_equity(self, current_total_equity: float) -> None:
        """초기 빈 계좌 입금 감지 (평가손익을 입출금으로 오인하지 않음)"""
        with self._lock:
            if current_total_equity <= 0:
                return
            if self.daily_start_equity < 5000.0 and current_total_equity >= 5000.0:
                old_start = self.daily_start_equity
                self.daily_start_equity = current_total_equity
                self.last_known_equity = current_total_equity
                self._save_state()
                logger.info(
                    f"💵 [초기 입금 감지 및 기준자산 리셋] 기존 잔고 {old_start:,.0f}원 ➜ 실투자 기준자산으로 재설정: {self.daily_start_equity:,.0f}원"
                )

    def update_daily_equity(self, current_total_equity: float, now_kst: datetime.datetime) -> tuple[bool, float]:
        with self._lock:
            date_key = now_kst.strftime("%Y-%m-%d")

            # KST 자정 날짜 변경 시 일일 통계 및 킬스위치 초기화
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
                self.kill_switch_latched_date = ""
                self._save_state()
                logger.info(f"📅 [일일 손익 기준일 갱신: {date_key}] 시작 총 자산: {self.daily_start_equity:,.0f}원")
            else:
                self.adjust_for_current_equity(current_total_equity)

            self.last_known_equity = current_total_equity

            daily_pnl_pct = (
                (current_total_equity - self.daily_start_equity) / self.daily_start_equity
                if self.daily_start_equity > 0
                else 0.0
            )

            # 킬스위치 발동 및 당일 Latch 고정 (P1-1)
            if daily_pnl_pct <= -self.max_loss_pct:
                if not self.kill_switch_active:
                    self.kill_switch_active = True
                    self.kill_switch_latched_date = date_key
                    self._save_state()
                    logger.warning(
                        f"🛑 [일일 킬 스위치 발동 (Latch)] 당일 손실률({daily_pnl_pct*100:.2f}%)이 한도(-{self.max_loss_pct*100:.1f}%)를 초과했습니다. 오늘 자정까지 신규 매수가 전면 차단됩니다."
                    )
            elif self.kill_switch_latched_date == date_key:
                # 이미 당일 킬스위치가 발동된 경우, 가격이 일시 반등해도 Latch 유지
                self.kill_switch_active = True
            else:
                self.kill_switch_active = False

            return self.kill_switch_active, daily_pnl_pct


class StrategyCacheManager:
    """
    프로그램 재시작 시 불필요한 중복 캔들 조회, 스크리닝 및 Gemini AI API 호출을 방지하고
    최근 유효 분석 데이터를 복원/보존하는 영속 캐시 관리자 (P0 Quota & Efficiency Guard)
    """

    def __init__(self, data_dir: str | None = None, exchange_name: str = "bithumb"):
        self.exchange = (exchange_name or "bithumb").lower()
        base_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        if self.exchange == "upbit":
            self.cache_dir = os.path.join(base_dir, "upbit")
        else:
            self.cache_dir = base_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_file = os.path.join(self.cache_dir, "strategy_cache.json")
        self._lock = threading.Lock()

    def save_cache(self, strategies: dict[str, dict[str, Any]]) -> None:
        if not strategies:
            return
        with self._lock:
            try:
                write_json_atomically(self.cache_file, {
                    "timestamp": time.time(),
                    "datetime": get_kst_now_str(),
                    "exchange": self.exchange,
                    "strategies": strategies,
                })
            except Exception as e:
                logger.debug("전략 캐시 저장 예외: %s", e)

    def load_cache(self) -> dict[str, Any]:
        with self._lock:
            data = load_json_with_backup_recovery(self.cache_file, default={})
            if isinstance(data, dict):
                return data
            return {}

    def get_valid_strategies(self, ttl: float = 270.0) -> tuple[dict[str, dict[str, Any]], float, bool]:
        """
        유효 캐시 반환 (전략 딕셔너리, 경과 시간(초), 유효 여부)
        """
        cache = self.load_cache()
        if not cache:
            return {}, 0.0, False
        cache_ts = float(cache.get("timestamp", 0.0))
        elapsed = time.time() - cache_ts
        is_valid = (0 < elapsed < ttl) and bool(cache.get("strategies"))
        return cache.get("strategies", {}), elapsed, is_valid
