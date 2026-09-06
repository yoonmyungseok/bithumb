"""Post-exit cooldown persistence, daily loss guards, and re-entry filters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import threading
import time
from typing import Any

from state_store import load_json_with_backup_recovery, write_json_atomically

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def get_kst_date_str() -> str:
    """KST 기준 현재 날짜 YYYY-MM-DD 문자열을 반환한다."""
    return datetime.now(KST).strftime("%Y-%m-%d")


def get_seconds_until_kst_midnight() -> float:
    """KST 기준 금일 자정(익일 00:00:00)까지 남은 초를 반환한다."""
    now_kst = datetime.now(KST)
    tomorrow_kst = (now_kst + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(0.0, (tomorrow_kst - now_kst).total_seconds())


def is_stop_loss_exit(exit_type: str) -> bool:
    """주어진 청산 사유가 손절(Stop Loss / Hard Stop / 손절 방어 / 비상탈출) 계열인지 판별한다."""
    raw = str(exit_type).strip()
    raw_upper = raw.upper()
    if "TIME" in raw_upper or "TRAILING" in raw_upper or "TP" in raw_upper or "익절" in raw:
        return False
    return "STOP" in raw_upper or "손절" in raw or "탈출" in raw or "EMERGENCY" in raw_upper


class CooldownManager:
    """Tracks post-exit cooldown periods, per-market daily loss counts, and price gaps."""

    def __init__(
        self,
        default_sl_cooldown: float = 1800.0,  # 기본 손절 쿨다운 30분 (연속 손절 방지)
        default_tp_cooldown: float = 1800.0,  # 익절 쿨다운 30분
        default_time_stop_cooldown: float = 2700.0,  # 타임스탑 쿨다운 45분
        max_daily_losses_per_market: int = 2,  # 당일 종목당 최대 허용 손절 횟수 (2회 이상 시 당일 차단)
        state_file: str | None = None,
        data_dir: str | None = None,
    ):
        self._lock = threading.RLock()
        self.default_sl_cooldown = default_sl_cooldown
        self.default_tp_cooldown = default_tp_cooldown
        self.default_time_stop_cooldown = default_time_stop_cooldown
        self.max_daily_losses_per_market = max_daily_losses_per_market
        d_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(d_dir, exist_ok=True)
        self.state_file = state_file or os.path.join(d_dir, "cooldown_state.json")
        self._daily_loss_date: str = get_kst_date_str()
        self._daily_loss_counts: dict[str, int] = {}
        self._records: dict[str, dict[str, Any]] = self._load()

    def _check_day_rollover_locked(self) -> None:
        """KST 자정이 지나 날짜가 변경되었으면 당일 손절 카운트를 초기화한다."""
        today = get_kst_date_str()
        if today != self._daily_loss_date:
            logger.info("📅 [자정 날짜 변경 감지] 일일 종목별 손절 카운트 초기화 (%s -> %s)", self._daily_loss_date, today)
            self._daily_loss_date = today
            self._daily_loss_counts.clear()
            self._save()

    def _load(self) -> dict[str, dict[str, Any]]:
        data = load_json_with_backup_recovery(self.state_file, default={})
        records: dict[str, dict[str, Any]] = {}
        if not isinstance(data, dict):
            return records

        now = time.time()
        today = get_kst_date_str()

        saved_date = str(data.get("_daily_loss_date", ""))
        if saved_date == today:
            self._daily_loss_date = saved_date
            raw_counts = data.get("_daily_loss_counts", {})
            if isinstance(raw_counts, dict):
                self._daily_loss_counts = {str(k).upper(): int(v) for k, v in raw_counts.items()}
        else:
            self._daily_loss_date = today
            self._daily_loss_counts = {}

        source_records = data.get("_records", data)
        if isinstance(source_records, dict):
            for k, v in source_records.items():
                if str(k).startswith("_"):
                    continue
                m_key = str(k).upper()
                if isinstance(v, (int, float)):
                    if float(v) > (now - 7200.0):
                        records[m_key] = {
                            "expire_at": float(v),
                            "exit_type": "STOP_LOSS",
                            "exit_price": 0.0,
                            "timestamp": float(v) - self.default_sl_cooldown,
                        }
                elif isinstance(v, dict):
                    exit_type_str = str(v.get("exit_type", "UNKNOWN"))
                    exp = float(v.get("expire_at", 0.0))
                    ts = float(v.get("timestamp", exp - self.default_sl_cooldown))
                    if exp > now or (now - ts < 7200.0):
                        records[m_key] = {
                            "expire_at": exp,
                            "exit_type": exit_type_str,
                            "exit_price": float(v.get("exit_price", 0.0)),
                            "timestamp": ts,
                        }
        return records

    def _save(self) -> None:
        try:
            payload: dict[str, Any] = {
                "_daily_loss_date": self._daily_loss_date,
                "_daily_loss_counts": self._daily_loss_counts,
                "_records": self._records,
            }
            for k, v in self._records.items():
                payload[k] = v
            write_json_atomically(self.state_file, payload)
        except OSError as exc:
            logger.warning("쿨다운 상태 파일 저장 실패: %s", exc)

    def record_exit(self, market: str, exit_type: str, exit_price: float = 0.0) -> None:
        m_key = market.upper()
        now = time.time()
        price_str = f" (청산가: {exit_price:,.2f}원)" if exit_price > 0 else ""

        with self._lock:
            self._check_day_rollover_locked()

            if is_stop_loss_exit(exit_type):
                self._daily_loss_counts[m_key] = self._daily_loss_counts.get(m_key, 0) + 1
                loss_cnt = self._daily_loss_counts[m_key]

                if loss_cnt >= self.max_daily_losses_per_market:
                    rem_midnight = get_seconds_until_kst_midnight()
                    expire_at = now + rem_midnight
                    self._records[m_key] = {
                        "expire_at": expire_at,
                        "exit_type": f"DAILY_LOSS_LIMIT({loss_cnt}회)",
                        "exit_price": float(exit_price),
                        "timestamp": now,
                    }
                    self._save()
                    logger.warning(
                        f"🛑 [{market}] 당일 손절 {loss_cnt}회 누적! 금일 자정까지 해당 종목 매수 완전 차단{price_str} (잔여 {rem_midnight/3600:.1f}시간)"
                    )
                    return

                duration = self.default_sl_cooldown
                expire_at = now + duration
                self._records[m_key] = {
                    "expire_at": expire_at,
                    "exit_type": exit_type,
                    "exit_price": float(exit_price),
                    "timestamp": now,
                }
                self._save()
                logger.info(
                    f"⏳ [{market}] {exit_type} 발생으로 {duration/60:.0f}분간 재진입 쿨다운 적용{price_str} (당일 손절 {loss_cnt}회)"
                )
                return

            etype_upper = exit_type.upper()
            if "TIME" in etype_upper:
                duration = self.default_time_stop_cooldown
            else:
                duration = self.default_tp_cooldown

            expire_at = now + duration
            self._records[m_key] = {
                "expire_at": expire_at,
                "exit_type": exit_type,
                "exit_price": float(exit_price),
                "timestamp": now,
            }
            self._save()
            logger.info(f"⏳ [{market}] {exit_type} 발생으로 {duration/60:.0f}분간 재진입 쿨다운 적용{price_str} (영속 저장)")

    def is_in_cooldown(self, market: str) -> tuple[bool, float]:
        now = time.time()
        m_key = market.upper()
        with self._lock:
            self._check_day_rollover_locked()
            rec = self._records.get(m_key)
            if not rec:
                return False, 0.0
            expire_at = float(rec.get("expire_at", 0.0))
            if expire_at > now:
                return True, expire_at - now
            ts = float(rec.get("timestamp", 0.0))
            if now - ts >= 7200.0:
                del self._records[m_key]
                self._save()
        return False, 0.0

    def get_last_exit_info(self, market: str) -> dict[str, Any] | None:
        m_key = market.upper()
        with self._lock:
            return self._records.get(m_key)

    def get_daily_loss_count(self, market: str) -> int:
        """해당 종목의 당일 손절 누적 횟수를 반환한다."""
        m_key = market.upper()
        with self._lock:
            self._check_day_rollover_locked()
            return self._daily_loss_counts.get(m_key, 0)

    def check_reentry_allowed(
        self,
        market: str,
        current_price: float,
        min_gap_pct: float = 0.015,
        expiry_sec: float = 7200.0,
    ) -> tuple[bool, str]:
        """
        쿨다운 타이머, 당일 손절 한도 및 직전 청산가 갭 필터를 검증하여 재진입 허용 여부를 결정한다.
        - 1차: 당일 종목당 손절 2회 누적 여부 확인 (당일 자정까지 차단)
        - 2차: 활성 쿨다운 잔여 시간 확인 (손절 30분 / 타임스탑 45분 / 익절 30분)
        - 3차: 손절 후 추가 급락(-1.5% 미만) 추세 진행 시 떨어지는 칼날 잡기 방지
        - 4차: 타임스탑 청산가 대비 ±1.5% 박스권 횡보 구간 재진입 차단
        - 5차: 트레일링 익절 후 고점 근처(0 ~ +1.5%) 휩쏘 추격 방지
        """
        now = time.time()
        m_key = market.upper()
        with self._lock:
            self._check_day_rollover_locked()

            loss_cnt = self._daily_loss_counts.get(m_key, 0)
            if loss_cnt >= self.max_daily_losses_per_market:
                rem_h = get_seconds_until_kst_midnight() / 3600.0
                return (
                    False,
                    f"🛑 당일 손절 {loss_cnt}회 누적으로 금일({m_key}) 거래 완전 차단 (자정까지 {rem_h:.1f}시간 남음)",
                )

            rec = self._records.get(m_key)
            if not rec:
                return True, "OK"

            expire_at = float(rec.get("expire_at", 0.0))
            exit_type = str(rec.get("exit_type", ""))
            exit_type_upper = exit_type.upper()

            if expire_at > now:
                cd_rem = expire_at - now
                return False, f"⏳ {exit_type} 쿨다운 대기 중 ({cd_rem/60:.1f}분 남음)"

            ts = float(rec.get("timestamp", 0.0))
            exit_price = float(rec.get("exit_price", 0.0))

            if (now - ts) < expiry_sec and exit_price > 0 and current_price > 0:
                gap_pct = (current_price - exit_price) / exit_price

                if is_stop_loss_exit(exit_type_upper):
                    if gap_pct < -min_gap_pct:
                        return (
                            False,
                            f"직전 손절가({exit_price:,.2f}원) 대비 추가 하락 진행 중(현재 {current_price:,.2f}원, 갭 {gap_pct*100:+.2f}%)으로 칼날 잡기 방지",
                        )
                elif "TIME" in exit_type_upper:
                    if abs(gap_pct) < min_gap_pct:
                        return (
                            False,
                            f"직전 타임스탑 청산가({exit_price:,.2f}원) 대비 박스권 횡보 구간(현재 {current_price:,.2f}원, 갭 {gap_pct*100:+.2f}%)으로 휩쏘 재진입 방지",
                        )
                elif "TRAILING" in exit_type_upper or "TP" in exit_type_upper:
                    if gap_pct < min_gap_pct:
                        return (
                            False,
                            f"직전 트레일링 청산가({exit_price:,.2f}원) 대비 유의미한 회복(+{min_gap_pct*100:.1f}%) 미도달(현재 {current_price:,.2f}원, 갭 {gap_pct*100:+.2f}%)으로 재진입 방지",
                        )

            if now - ts >= expiry_sec:
                del self._records[m_key]
                self._save()

        return True, "OK"
