"""Post-exit cooldown persistence and re-entry guards."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from state_store import load_json_with_backup_recovery, write_json_atomically

logger = logging.getLogger(__name__)

def is_stop_loss_exit(exit_type: str) -> bool:
    """주어진 청산 사유가 손절(Stop Loss / Hard Stop / 손절 방어) 계열인지 판별한다."""
    raw = str(exit_type).strip()
    raw_upper = raw.upper()
    if "TIME" in raw_upper or "TRAILING" in raw_upper or "TP" in raw_upper or "익절" in raw:
        return False
    return "STOP" in raw_upper or "손절" in raw


class CooldownManager:
    """Tracks post-exit cooldown periods and price gaps to prevent rapid whipsaw re-entries, with disk persistence."""

    def __init__(
        self,
        default_sl_cooldown: float = 0.0,  # 손절 시 바닥 재매수 허용을 위해 기본 쿨다운 0초
        default_tp_cooldown: float = 1800.0,
        default_time_stop_cooldown: float = 2700.0,
        state_file: str | None = None,
        data_dir: str | None = None,
    ):
        self._lock = threading.RLock()
        self.default_sl_cooldown = default_sl_cooldown  # 0초 (손절 쿨다운 미적용)
        self.default_tp_cooldown = default_tp_cooldown  # 30 minutes
        self.default_time_stop_cooldown = default_time_stop_cooldown  # 45 minutes
        d_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(d_dir, exist_ok=True)
        self.state_file = state_file or os.path.join(d_dir, "cooldown_state.json")
        self._records: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        data = load_json_with_backup_recovery(self.state_file, default={})
        records: dict[str, dict[str, Any]] = {}
        if isinstance(data, dict):
            now = time.time()
            for k, v in data.items():
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
                    if is_stop_loss_exit(exit_type_str):
                        # 손절 기록은 신규 매수 차단을 하지 않으므로 로드 대상에서 제외
                        continue
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
            write_json_atomically(self.state_file, self._records)
        except OSError as exc:
            logger.warning("쿨다운 상태 파일 저장 실패: %s", exc)

    def record_exit(self, market: str, exit_type: str, exit_price: float = 0.0) -> None:
        m_key = market.upper()
        now = time.time()
        price_str = f" (청산가: {exit_price:,.2f}원)" if exit_price > 0 else ""

        # 손절(Stop Loss / Hard Stop / 손절 방어)은 바닥 재매수 기회를 위해 쿨다운 미적용
        if is_stop_loss_exit(exit_type):
            with self._lock:
                if m_key in self._records:
                    del self._records[m_key]
                    self._save()
            logger.info(f"⚡ [{market}] {exit_type} 발생{price_str} - 바닥 재매수 기회 보존을 위해 쿨다운 미적용(즉시 진입 허용)")
            return

        etype_upper = exit_type.upper()
        if "TIME" in etype_upper:
            duration = self.default_time_stop_cooldown
        else:
            duration = self.default_tp_cooldown

        expire_at = now + duration
        with self._lock:
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
            rec = self._records.get(m_key)
            if not rec:
                return False, 0.0
            if is_stop_loss_exit(str(rec.get("exit_type", ""))):
                del self._records[m_key]
                self._save()
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

    def check_reentry_allowed(
        self,
        market: str,
        current_price: float,
        min_gap_pct: float = 0.015,
        expiry_sec: float = 7200.0,
    ) -> tuple[bool, str]:
        """
        쿨다운 타이머 및 직전 청산가 갭 필터를 검증하여 재진입 허용 여부를 결정한다.
        - 손절(STOP_LOSS/HARD_STOP): 바닥 재매수 허용을 위해 쿨다운 및 갭 필터 미적용 (즉시 허용)
        - 1차: 활성 쿨다운 잔여 시간 확인 (타임스탑/익절 대상)
        - 2차: 타임스탑 청산가 대비 ±1.5% 박스권 횡보 구간 재진입 차단
        - 3차: 트레일링 익절 후 고점 근처(0 ~ +1.5%) 휩쏘 추격 방지
        """
        now = time.time()
        m_key = market.upper()
        with self._lock:
            rec = self._records.get(m_key)
            if not rec:
                return True, "OK"

            exit_type = str(rec.get("exit_type", ""))
            # 손절인 경우 쿨다운 및 갭 필터 없이 즉시 통과
            if is_stop_loss_exit(exit_type):
                del self._records[m_key]
                self._save()
                return True, "OK"

            expire_at = float(rec.get("expire_at", 0.0))
            if expire_at > now:
                cd_rem = expire_at - now
                return False, f"⏳ 쿨다운 대기 중 ({cd_rem/60:.1f}분 남음)"

            ts = float(rec.get("timestamp", 0.0))
            exit_price = float(rec.get("exit_price", 0.0))
            exit_type_upper = exit_type.upper()

            if (now - ts) < expiry_sec and exit_price > 0 and current_price > 0:
                gap_pct = (current_price - exit_price) / exit_price
                if "TIME" in exit_type_upper:
                    if abs(gap_pct) < min_gap_pct:
                        return (
                            False,
                            f"직전 타임스탑 청산가({exit_price:,.2f}원) 대비 박스권 횡보 구간(현재 {current_price:,.2f}원, 갭 {gap_pct*100:+.2f}%)으로 휩쏘 재진입 방지",
                        )
                elif "TRAILING" in exit_type_upper or "TP" in exit_type_upper:
                    # TRAILING_STOP은 트레일링 청산가 아래에서의 재진입 시 하락 재개 구간 추격 방지
                    # 따라서 청산가보다 명확히 회복한 경우에만 다음 진입을 허용한다.
                    if gap_pct < min_gap_pct:
                        return (
                            False,
                            f"직전 트레일링 청산가({exit_price:,.2f}원) 대비 유의미한 회복(+{min_gap_pct*100:.1f}%) 미도달(현재 {current_price:,.2f}원, 갭 {gap_pct*100:+.2f}%)으로 재진입 방지",
                        )

            if now - ts >= expiry_sec:
                del self._records[m_key]
                self._save()

        return True, "OK"
