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
    거래소별(업비트/빗썸) 손실 재진입 방어 가드:
    1) 당일 1회 손실 시: RISK_OFF + MOMENTUM_BREAKOUT 재진입 차단 (휩소 방어).
    2) 당일 2회 이상 손실 시 (Soft Blacklist): 레짐·진입경로 불문하고 당일 자정까지 해당 종목 신규 매수 전면 차단.
    """

    def __init__(self, state_file: str | None = None, data_dir: str | None = None, exchange: str = "upbit"):
        self._lock = threading.RLock()
        self.exchange = str(exchange or "upbit").strip().lower()
        if not data_dir:
            sub_dir = "bithumb" if self.exchange == "bithumb" else "upbit"
            d_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", sub_dir,
            )
        else:
            d_dir = data_dir
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
                {"schema_version": 2, "markets": self._records},
            )
        except OSError as exc:
            logger.warning("손실 재진입 차단 상태 저장 실패: %s", exc)

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
        """확정 매도 체결 증가분의 손실 청산만 기록한다 (업비트 및 빗썸 지원)."""
        ex_norm = str(exchange or "").strip().lower()
        if ex_norm != self.exchange:
            return
        if not qualifies_risk_off_loss_reentry_record(exit_reason, net_pnl_krw):
            return
        ts = float(exit_ts if exit_ts is not None else time.time())
        kst_date = get_kst_date_str(ts)
        m_key = market.upper()
        with self._lock:
            self._prune_expired_locked()
            prev_rec = self._records.get(m_key, {})
            prev_date = str(prev_rec.get("kst_date", ""))
            current_count = int(prev_rec.get("loss_count", 0) or 0) if prev_date == kst_date else 0
            new_count = current_count + 1

            self._records[m_key] = {
                "kst_date": kst_date,
                "exit_ts": ts,
                "exit_reason": str(exit_reason),
                "confirmed_net_pnl_krw": float(net_pnl_krw),
                "exchange": ex_norm,
                "loss_count": new_count,
            }
            self._save()
        logger.info(
            "[%s] 당일 손실 재진입 차단 기록 (거래소=%s, 사유=%s, 순손익=%+.0f원, 당일손실횟수=%d회, KST %s)",
            market,
            ex_norm,
            exit_reason,
            net_pnl_krw,
            new_count,
            kst_date,
        )

    def check_reentry_blocked(
        self,
        market: str,
        btc_regime: str | None = None,
        candidate_type: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """
        동일 KST 날짜 재진입 차단 여부를 반환한다.
        - 당일 2회 이상 손절 시: 모든 레짐/진입경로에서 당일 전면 차단 (Soft Blacklist).
        - 당일 1회 손절 시: 레거시 호출(인자 생략) 또는 RISK_OFF + MOMENTUM_BREAKOUT 경로 선별 차단.
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

            loss_count = int(rec.get("loss_count", 1) or 1)
            is_legacy_call = (btc_regime is None and candidate_type is None)
            is_regime_momentum = (
                str(btc_regime or "").strip().upper() == "RISK_OFF"
                and str(candidate_type or "").strip().upper() == "MOMENTUM_BREAKOUT"
            )

            # 2연패 이상이면 전면 차단, 1회 손절이면 레거시 호출 또는 RISK_OFF 모멘텀만 차단
            if loss_count < 2 and not (is_legacy_call or is_regime_momentum):
                return False, {}

            kst_date = str(rec["kst_date"])
            exit_ts = float(rec.get("exit_ts", 0.0))
            next_allowed_ts = kst_midnight_after_date(kst_date)
            exit_at_str = datetime.fromtimestamp(exit_ts, tz=KST).strftime("%Y-%m-%d %H:%M:%S")
            next_allowed_str = datetime.fromtimestamp(next_allowed_ts, tz=KST).strftime("%Y-%m-%d %H:%M:%S")

            if loss_count >= 2:
                summary_msg = (
                    f"당일 {loss_count}회 연속 손실(Soft Blacklist, 최근사유: {rec.get('exit_reason', '')})로 "
                    f"당일 전면 재진입 차단 (다음 허용: {next_allowed_str} KST)"
                )
            else:
                summary_msg = (
                    f"당일 손실 청산({rec.get('exit_reason', '')}, "
                    f"{rec.get('confirmed_net_pnl_krw', 0.0):+,.0f}원) 후 RISK_OFF 모멘텀 재진입 차단 "
                    f"(다음 허용: {next_allowed_str} KST)"
                )

            info = {
                "exchange": rec.get("exchange", self.exchange),
                "market": m_key,
                "previous_exit_at": exit_at_str,
                "exit_reason": rec.get("exit_reason", ""),
                "confirmed_net_pnl_krw": rec.get("confirmed_net_pnl_krw", 0.0),
                "loss_count": loss_count,
                "next_allowed_at": next_allowed_str,
                "next_allowed_ts": next_allowed_ts,
                "summary": summary_msg,
            }
            return True, info

    def applies_to_entry_path(self, btc_regime: str, candidate_type: str, market: str | None = None) -> bool:
        """차단 검사를 적용할 진입 경로인지 판별한다."""
        if market:
            m_key = market.upper()
            with self._lock:
                rec = self._records.get(m_key)
                if rec and int(rec.get("loss_count", 0) or 0) >= 2:
                    return True
        return (
            str(btc_regime).strip().upper() == "RISK_OFF"
            and str(candidate_type).strip().upper() == "MOMENTUM_BREAKOUT"
        )

