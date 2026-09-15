"""업비트 RISK_OFF 모멘텀 돌파 경로의 당일 손실 재진입 차단 상태."""

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


def get_kst_date_str(ts: float | None = None) -> str:
    """KST 기준 날짜 문자열(YYYY-MM-DD)을 반환한다."""
    when = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=KST)
    return when.strftime("%Y-%m-%d")


def kst_midnight_after_date(kst_date: str) -> float:
    """해당 KST 날짜의 익일 00:00:00 KST를 Unix timestamp로 반환한다."""
    base = datetime.strptime(kst_date, "%Y-%m-%d").replace(tzinfo=KST)
    next_day = base + timedelta(days=1)
    return next_day.timestamp()


def qualifies_risk_off_loss_reentry_record(exit_reason: str, net_pnl_krw: float) -> bool:
    """
    확정 매도 체결 증가분의 순손익·청산 사유가 당일 재진입 차단 기록 조건을 만족하는지 판별한다.
    ACK·미체결·0원 체결은 이 함수 호출 전에 걸러져야 한다.
    """
    if net_pnl_krw >= 0:
        return False
    upper = str(exit_reason or "").strip().upper()
    if "AI_TIGHTENED" in upper or "TIGHTENED_STOP" in upper:
        return True
    if "TIME_STOP" in upper or ("TIME" in upper and "STOP" in upper):
        return True
    if "STOP_LOSS" in upper or "HARD_STOP" in upper:
        return True
    return False


class RiskOffLossReentryGuard:
    """
    업비트 전용: TIME_STOP·STOP_LOSS·손실 AI_TIGHTENED_STOP 확정 청산 후
    동일 종목의 같은 KST 날짜 RISK_OFF + MOMENTUM_BREAKOUT 재진입을 차단한다.
    """

    def __init__(self, state_file: str | None = None, data_dir: str | None = None):
        self._lock = threading.RLock()
        d_dir = data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "upbit",
        )
        os.makedirs(d_dir, exist_ok=True)
        self.state_file = state_file or os.path.join(d_dir, "risk_off_loss_reentry.json")
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        data = load_json_with_backup_recovery(self.state_file, default={})
        records: dict[str, dict[str, Any]] = {}
        if isinstance(data, dict):
            raw = data.get("markets", data)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if str(key).startswith("_"):
                        continue
                    if isinstance(value, dict):
                        records[str(key).upper()] = dict(value)
        self._records = records
        self._prune_expired_locked()

    def _save(self) -> None:
        try:
            write_json_atomically(
                self.state_file,
                {"schema_version": 1, "markets": self._records},
            )
        except OSError as exc:
            logger.warning("RISK_OFF 손실 재진입 차단 상태 저장 실패: %s", exc)

    def _prune_expired_locked(self) -> None:
        """KST 날짜가 지난 차단 기록을 제거한다."""
        today = get_kst_date_str()
        stale = [m for m, rec in self._records.items() if str(rec.get("kst_date", "")) != today]
        if not stale:
            return
        for market in stale:
            del self._records[market]
        self._save()

    def record_confirmed_loss_exit(
        self,
        *,
        exchange: str,
        market: str,
        exit_reason: str,
        net_pnl_krw: float,
        exit_ts: float | None = None,
    ) -> None:
        """확정 매도 체결 증가분의 손실 청산만 기록한다 (업비트만)."""
        if str(exchange).strip().lower() != "upbit":
            return
        if not qualifies_risk_off_loss_reentry_record(exit_reason, net_pnl_krw):
            return
        ts = float(exit_ts if exit_ts is not None else time.time())
        kst_date = get_kst_date_str(ts)
        m_key = market.upper()
        with self._lock:
            self._prune_expired_locked()
            self._records[m_key] = {
                "kst_date": kst_date,
                "exit_ts": ts,
                "exit_reason": str(exit_reason),
                "confirmed_net_pnl_krw": float(net_pnl_krw),
                "exchange": "upbit",
            }
            self._save()
        logger.info(
            "[%s] RISK_OFF 모멘텀 당일 재진입 차단 기록 (사유=%s, 순손익=%+.0f원, KST %s)",
            market,
            exit_reason,
            net_pnl_krw,
            kst_date,
        )

    def check_reentry_blocked(self, market: str) -> tuple[bool, dict[str, Any]]:
        """
        동일 KST 날짜 재진입 차단 여부를 반환한다.
        두 번째 값은 strategy_decisions payload용 메타데이터다.
        """
        m_key = market.upper()
        today = get_kst_date_str()
        with self._lock:
            self._prune_expired_locked()
            rec = self._records.get(m_key)
            if not rec:
                return False, {}
            if str(rec.get("kst_date", "")) != today:
                del self._records[m_key]
                self._save()
                return False, {}
            kst_date = str(rec["kst_date"])
            exit_ts = float(rec.get("exit_ts", 0.0))
            next_allowed_ts = kst_midnight_after_date(kst_date)
            exit_at_str = datetime.fromtimestamp(exit_ts, tz=KST).strftime("%Y-%m-%d %H:%M:%S")
            next_allowed_str = datetime.fromtimestamp(next_allowed_ts, tz=KST).strftime("%Y-%m-%d %H:%M:%S")
            info = {
                "exchange": "upbit",
                "market": m_key,
                "previous_exit_at": exit_at_str,
                "exit_reason": rec.get("exit_reason", ""),
                "confirmed_net_pnl_krw": rec.get("confirmed_net_pnl_krw", 0.0),
                "next_allowed_at": next_allowed_str,
                "next_allowed_ts": next_allowed_ts,
                "summary": (
                    f"당일 손실 청산({rec.get('exit_reason', '')}, "
                    f"{rec.get('confirmed_net_pnl_krw', 0.0):+,.0f}원) 후 재진입 차단 "
                    f"(다음 허용: {next_allowed_str} KST)"
                ),
            }
            return True, info

    def applies_to_entry_path(self, btc_regime: str, candidate_type: str) -> bool:
        """차단 검사를 적용할 진입 경로인지 판별한다."""
        return (
            str(btc_regime).strip().upper() == "RISK_OFF"
            and str(candidate_type).strip().upper() == "MOMENTUM_BREAKOUT"
        )
