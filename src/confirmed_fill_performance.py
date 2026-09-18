"""확정 체결(processed_executed_volume) 기준 성과 집계 및 운영 리포트.

주문 저널과 daily_stats.json을 읽기 전용으로 조회하며, 저널 원본은 수정하지 않는다.
ACK·미체결·취소(체결량 0) 주문은 실현 성과에 포함하지 않는다.
"""

from __future__ import annotations

import datetime
import os
from typing import Any

from order_safety.types import OrderStatus
from risk_manager import get_kst_now_str
from state_store import load_json_with_backup_recovery

KST = datetime.timezone(datetime.timedelta(hours=9))
REPORT_SCHEMA_VERSION = 1

_MONITOR_ORDER_STATUSES = (
    OrderStatus.RECONCILIATION_PENDING,
    OrderStatus.OPEN,
    OrderStatus.CANCELED,
    OrderStatus.UNKNOWN,
)


def _is_buy_side(side: str) -> bool:
    return str(side or "").lower() in {"bid", "buy"}


def _is_sell_side(side: str) -> bool:
    return str(side or "").lower() in {"ask", "sell"}


def kst_date_from_ts(ts: float) -> str:
    """Unix epoch 초를 KST 거래일(YYYY-MM-DD)로 변환한다."""
    if ts <= 0:
        return ""
    return datetime.datetime.fromtimestamp(ts, tz=KST).strftime("%Y-%m-%d")


def kst_datetime_from_ts(ts: float) -> str:
    if ts <= 0:
        return ""
    return datetime.datetime.fromtimestamp(ts, tz=KST).strftime("%Y-%m-%d %H:%M:%S")


def load_order_journal_orders(journal_path: str) -> tuple[list[dict[str, Any]], str]:
    """order_journal.json을 읽기 전용으로 로드한다. 파일이 없으면 빈 목록."""
    data = load_json_with_backup_recovery(journal_path, default=[])
    exchange_scope = ""
    if isinstance(data, dict):
        exchange_scope = str(data.get("exchange_scope", "")).lower()
        orders = data.get("orders", [])
    elif isinstance(data, list):
        orders = data
    else:
        orders = []
    if not isinstance(orders, list):
        return [], exchange_scope
    return [dict(o) for o in orders if isinstance(o, dict)], exchange_scope


def load_daily_stats_snapshot(stats_path: str) -> dict[str, Any]:
    """daily_stats.json을 읽기 전용으로 로드한다."""
    data = load_json_with_backup_recovery(stats_path, default={})
    return dict(data) if isinstance(data, dict) else {}


def _order_event_ts(order: dict[str, Any]) -> float:
    for key in ("last_event_at", "updated_at", "created_at"):
        val = order.get(key)
        if val is None:
            continue
        try:
            ts = float(val)
        except (TypeError, ValueError):
            continue
        if ts > 0:
            return ts
    return 0.0


def _confirmed_volume(order: dict[str, Any]) -> float:
    """REST/체결 처리기가 반영한 확정 체결 누적량만 사용한다(ACK executed_volume 제외)."""
    return max(0.0, float(order.get("processed_executed_volume", 0.0) or 0.0))


def _confirmed_fee(order: dict[str, Any]) -> float:
    return max(0.0, float(order.get("processed_fee", 0.0) or 0.0))


def _find_entry_order(orders: list[dict[str, Any]], exit_order: dict[str, Any]) -> dict[str, Any] | None:
    position_id = exit_order.get("position_id")
    market = exit_order.get("market")
    for candidate in reversed(orders):
        if not _is_buy_side(str(candidate.get("side", ""))):
            continue
        if position_id and candidate.get("position_id") == position_id:
            if _confirmed_volume(candidate) > 0:
                return candidate
        elif candidate.get("market") == market and _confirmed_volume(candidate) > 0:
            return candidate
    return None


def _strategy_path_from_snapshot(snapshot: dict[str, Any]) -> str:
    if not snapshot:
        return "UNKNOWN"
    reason = str(snapshot.get("entry_reason", "") or "").strip()
    if reason:
        return reason
    phase = str(snapshot.get("momentum_phase", "") or "").strip()
    if phase:
        return f"MOMENTUM:{phase}"
    mode = str(snapshot.get("strategy_mode", "") or "").strip()
    return mode or "STANDARD"


def _entry_type_from_snapshot(snapshot: dict[str, Any]) -> str:
    if not snapshot:
        return "UNKNOWN"
    mode = str(snapshot.get("strategy_mode", "") or "").strip()
    if mode:
        return mode.upper()
    return str(snapshot.get("ord_type", "limit")).upper()


def count_non_performance_orders(orders: list[dict[str, Any]]) -> dict[str, int]:
    """리포트 필수: 미확정·비체결 주문 상태별 건수."""
    counts = {status: 0 for status in _MONITOR_ORDER_STATUSES}
    for order in orders:
        status = str(order.get("status", OrderStatus.UNKNOWN) or OrderStatus.UNKNOWN).upper()
        if status in counts:
            counts[status] += 1
    return counts


def build_trade_legs_from_journal(
    orders: list[dict[str, Any]],
    *,
    exchange: str,
) -> list[dict[str, Any]]:
    """확정 매도 체결(processed_executed_volume>0)마다 1개의 실현 손익 레그를 생성한다."""
    legs: list[dict[str, Any]] = []
    # KST 거래일·종목별 이전 청산 횟수 → 재진입 여부
    closed_count_by_day_market: dict[tuple[str, str], int] = {}

    sell_orders = [
        o for o in orders
        if _is_sell_side(str(o.get("side", ""))) and _confirmed_volume(o) > 0
    ]
    sell_orders.sort(key=_order_event_ts)

    for exit_order in sell_orders:
        exit_vol = _confirmed_volume(exit_order)
        exit_price = float(exit_order.get("avg_price", 0.0) or 0.0)
        if exit_price <= 0:
            continue

        entry_order = _find_entry_order(orders, exit_order)
        entry_price = float(exit_order.get("avg_buy_price", 0.0) or 0.0)
        entry_vol = 0.0
        entry_fee_total = 0.0
        entry_slippage_bps = 0.0
        snapshot: dict[str, Any] = {}
        entry_ts = 0.0

        if entry_order:
            entry_price = float(entry_order.get("avg_price", 0.0) or 0.0) or entry_price
            entry_vol = _confirmed_volume(entry_order)
            entry_fee_total = _confirmed_fee(entry_order)
            entry_slippage_bps = float(entry_order.get("slippage_bps", 0.0) or 0.0)
            snapshot = dict(entry_order.get("entry_strategy_snapshot") or {})
            entry_ts = _order_event_ts(entry_order)

        if entry_price <= 0:
            continue

        exit_ts = _order_event_ts(exit_order)
        kst_date = kst_date_from_ts(exit_ts)
        market = str(exit_order.get("market", "") or "")

        exit_fee = _confirmed_fee(exit_order)
        entry_fee_alloc = (entry_fee_total * (exit_vol / entry_vol)) if entry_vol > 0 else 0.0

        # fill_processor와 동일: 매도 수수료만 proceeds에서 차감한 총손익
        proceeds = (exit_price * exit_vol) - exit_fee
        cost_basis = entry_price * exit_vol
        gross_pnl_krw = proceeds - cost_basis
        net_pnl_krw = gross_pnl_krw - entry_fee_alloc

        pnl_pct = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
        exit_slippage_bps = float(exit_order.get("slippage_bps", 0.0) or 0.0)
        hold_sec = max(0.0, exit_ts - entry_ts) if entry_ts > 0 and exit_ts > 0 else 0.0

        day_market_key = (kst_date, market)
        is_reentry = closed_count_by_day_market.get(day_market_key, 0) > 0
        closed_count_by_day_market[day_market_key] = closed_count_by_day_market.get(day_market_key, 0) + 1

        legs.append({
            "kst_trading_date": kst_date,
            "exchange": exchange,
            "market": market,
            "strategy_path": _strategy_path_from_snapshot(snapshot),
            "regime": str(snapshot.get("entry_btc_regime") or snapshot.get("btc_regime") or "UNKNOWN"),
            "entry_type": _entry_type_from_snapshot(snapshot),
            "entry_price": round(entry_price, 8),
            "entry_volume": round(exit_vol, 8),
            "exit_price": round(exit_price, 8),
            "exit_volume": round(exit_vol, 8),
            "entry_fee_krw": round(entry_fee_alloc, 4),
            "exit_fee_krw": round(exit_fee, 4),
            "total_fee_krw": round(entry_fee_alloc + exit_fee, 4),
            "entry_slippage_bps": round(entry_slippage_bps, 2),
            "exit_slippage_bps": round(exit_slippage_bps, 2),
            "hold_duration_sec": int(hold_sec),
            "hold_duration_min": round(hold_sec / 60.0, 2),
            "exit_reason": str(exit_order.get("exit_reason") or "MANUAL_EXIT"),
            "gross_pnl_krw": round(gross_pnl_krw, 2),
            "net_pnl_krw": round(net_pnl_krw, 2),
            "pnl_pct": round(pnl_pct, 4),
            "is_win": net_pnl_krw > 0,
            "is_same_market_reentry": is_reentry,
            "exit_client_order_id": exit_order.get("client_order_id", ""),
            "position_id": exit_order.get("position_id", ""),
            "exit_at_kst": kst_datetime_from_ts(exit_ts),
        })

    return legs


def summarize_trade_legs(legs: list[dict[str, Any]]) -> dict[str, Any]:
    """확정 체결 레그 기준 승률·Profit Factor·순손익."""
    if not legs:
        return {
            "confirmed_fill_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate_pct": 0.0,
            "profit_factor": None,
            "gross_win_krw": 0.0,
            "gross_loss_krw": 0.0,
            "total_gross_pnl_krw": 0.0,
            "total_net_pnl_krw": 0.0,
            "total_fee_krw": 0.0,
        }

    wins = [leg for leg in legs if float(leg.get("net_pnl_krw", 0.0)) > 0]
    losses = [leg for leg in legs if float(leg.get("net_pnl_krw", 0.0)) <= 0]
    gross_win = sum(float(leg.get("net_pnl_krw", 0.0)) for leg in wins)
    gross_loss = abs(sum(float(leg.get("net_pnl_krw", 0.0)) for leg in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    return {
        "confirmed_fill_count": len(legs),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": round(len(wins) / len(legs) * 100.0, 2),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "gross_win_krw": round(gross_win, 2),
        "gross_loss_krw": round(gross_loss, 2),
        "total_gross_pnl_krw": round(sum(float(leg.get("gross_pnl_krw", 0.0)) for leg in legs), 2),
        "total_net_pnl_krw": round(sum(float(leg.get("net_pnl_krw", 0.0)) for leg in legs), 2),
        "total_fee_krw": round(sum(float(leg.get("total_fee_krw", 0.0)) for leg in legs), 2),
    }


def aggregate_daily_kst(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """KST 거래일별 확정 체결 성과 롤업."""
    by_date: dict[str, list[dict[str, Any]]] = {}
    for leg in legs:
        d = str(leg.get("kst_trading_date", "") or "")
        if not d:
            continue
        by_date.setdefault(d, []).append(leg)

    rows: list[dict[str, Any]] = []
    for date_str in sorted(by_date.keys(), reverse=True):
        day_legs = by_date[date_str]
        summary = summarize_trade_legs(day_legs)
        rows.append({
            "kst_trading_date": date_str,
            "confirmed_fill_count": summary["confirmed_fill_count"],
            "win_rate_pct": summary["win_rate_pct"],
            "profit_factor": summary["profit_factor"],
            "net_pnl_krw": summary["total_net_pnl_krw"],
            "gross_pnl_krw": summary["total_gross_pnl_krw"],
            "total_fee_krw": summary["total_fee_krw"],
        })
    return rows


def compare_with_daily_stats(
    *,
    kst_date: str,
    legs: list[dict[str, Any]],
    daily_stats: dict[str, Any],
) -> dict[str, Any]:
    """daily_stats.json(자정 경계·누적)과 확정 체결 리포트 차이를 설명 가능하게 반환."""
    day_legs = [leg for leg in legs if leg.get("kst_trading_date") == kst_date]
    day_summary = summarize_trade_legs(day_legs)

    stats_date = str(daily_stats.get("date", "") or "")
    stats_pnl = float(daily_stats.get("realized_pnl_krw", 0.0) or 0.0)
    stats_trades = int(daily_stats.get("total_trades", 0) or 0)
    stats_wins = int(daily_stats.get("win_trades", 0) or 0)

    confirmed_net = float(day_summary["total_net_pnl_krw"])
    delta_pnl = round(confirmed_net - stats_pnl, 2)

    explanations: list[str] = [
        "일일 통계 파일은 확정 매도 체결 시점에 메모리·자정(KST) 경계로 누적된다.",
        "확정 체결 리포트는 주문 저널의 확정 체결량·수수료만 재집계하며 주문 접수·미체결 주문은 제외한다.",
    ]
    if stats_date and stats_date != kst_date:
        explanations.append(
            f"일일 통계 기록일({stats_date})과 비교 KST일({kst_date})이 다르면 당일 수치가 어긋날 수 있다.",
        )
    if delta_pnl != 0:
        explanations.append(
            "순손익 차이는 매수 수수료 배분·분할익절 레그 수·자정 전후 청산일 분류 또는 일일 통계 미동기화 때문일 수 있다.",
        )
    if stats_trades != day_summary["confirmed_fill_count"]:
        explanations.append(
            "체결 횟수 차이는 일일 통계가 청산 이벤트 건수를 세고, 리포트는 저널 매도 확정 레그 건수를 센다.",
        )

    return {
        "kst_date": kst_date,
        "daily_stats_date": stats_date,
        "daily_stats_realized_pnl_krw": round(stats_pnl, 2),
        "daily_stats_total_trades": stats_trades,
        "daily_stats_win_trades": stats_wins,
        "confirmed_fill_net_pnl_krw": confirmed_net,
        "confirmed_fill_count": day_summary["confirmed_fill_count"],
        "delta_pnl_krw": delta_pnl,
        "delta_trade_count": day_summary["confirmed_fill_count"] - stats_trades,
        "explanations": explanations,
    }


def build_confirmed_fill_report(
    *,
    data_dir: str,
    exchange: str,
    journal_path: str | None = None,
    daily_stats_path: str | None = None,
    recent_leg_limit: int = 30,
    daily_rollup_limit: int = 14,
) -> dict[str, Any]:
    """거래소 data_dir 기준 확정 체결 운영 리포트(읽기 전용)."""
    ex = exchange.strip().lower()
    j_path = journal_path or os.path.join(data_dir, "order_journal.json")
    s_path = daily_stats_path or os.path.join(data_dir, "daily_stats.json")

    orders, stored_scope = load_order_journal_orders(j_path)
    if stored_scope and stored_scope != ex:
        # 다른 거래소 저널이 섞이면 성과를 계산하지 않는다.
        orders = []

    daily_stats = load_daily_stats_snapshot(s_path)
    status_counts = count_non_performance_orders(orders)
    legs = build_trade_legs_from_journal(orders, exchange=ex)
    summary = summarize_trade_legs(legs)
    summary["order_status_excluded_counts"] = status_counts

    today_kst = get_kst_now_str()[:10]
    comparison = compare_with_daily_stats(
        kst_date=today_kst,
        legs=legs,
        daily_stats=daily_stats,
    )

    daily_rows = aggregate_daily_kst(legs)[:daily_rollup_limit]
    recent_legs = list(reversed(legs))[:recent_leg_limit]

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "exchange": ex,
        "generated_at_kst": get_kst_now_str(),
        "timezone": "Asia/Seoul",
        "data_sources": {
            "order_journal": os.path.abspath(j_path),
            "daily_stats": os.path.abspath(s_path),
            "read_only": True,
        },
        "summary": summary,
        "daily_kst_rollup": daily_rows,
        "recent_trade_legs": recent_legs,
        "daily_stats_comparison": comparison,
    }


def _merge_performance_summaries(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    """두 거래소 요약을 통합 탭 카드용으로 합산한다."""
    win_count = int(left.get("win_count", 0) or 0) + int(right.get("win_count", 0) or 0)
    loss_count = int(left.get("loss_count", 0) or 0) + int(right.get("loss_count", 0) or 0)
    fill_count = int(left.get("confirmed_fill_count", 0) or 0) + int(
        right.get("confirmed_fill_count", 0) or 0
    )
    gross_win = float(left.get("gross_win_krw", 0.0) or 0.0) + float(
        right.get("gross_win_krw", 0.0) or 0.0
    )
    gross_loss = float(left.get("gross_loss_krw", 0.0) or 0.0) + float(
        right.get("gross_loss_krw", 0.0) or 0.0
    )
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    excluded: dict[str, int] = {}
    for summary in (left, right):
        for status, count in (summary.get("order_status_excluded_counts") or {}).items():
            excluded[status] = excluded.get(status, 0) + int(count or 0)

    return {
        "confirmed_fill_count": fill_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate_pct": round(win_count / fill_count * 100.0, 2) if fill_count > 0 else 0.0,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "gross_win_krw": round(gross_win, 2),
        "gross_loss_krw": round(gross_loss, 2),
        "total_gross_pnl_krw": round(
            float(left.get("total_gross_pnl_krw", 0.0) or 0.0)
            + float(right.get("total_gross_pnl_krw", 0.0) or 0.0),
            2,
        ),
        "total_net_pnl_krw": round(
            float(left.get("total_net_pnl_krw", 0.0) or 0.0)
            + float(right.get("total_net_pnl_krw", 0.0) or 0.0),
            2,
        ),
        "total_fee_krw": round(
            float(left.get("total_fee_krw", 0.0) or 0.0) + float(right.get("total_fee_krw", 0.0) or 0.0),
            2,
        ),
        "order_status_excluded_counts": excluded,
    }


def _merge_recent_trade_legs(
    bithumb_report: dict[str, Any],
    upbit_report: dict[str, Any],
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """최근 레그를 청산 시각 기준으로 합친 뒤 상한만 반환한다(거래소 라벨 보존)."""
    merged: list[dict[str, Any]] = []
    for exchange_key, report in (("bithumb", bithumb_report), ("upbit", upbit_report)):
        for leg in report.get("recent_trade_legs") or []:
            if not isinstance(leg, dict):
                continue
            row = dict(leg)
            row["exchange"] = str(row.get("exchange") or exchange_key).lower()
            merged.append(row)

    merged.sort(
        key=lambda leg: (
            str(leg.get("exit_at_kst") or ""),
            str(leg.get("kst_trading_date") or ""),
            str(leg.get("exit_client_order_id") or ""),
        ),
        reverse=True,
    )
    return merged[:limit]


def merge_exchange_reports(
    bithumb_report: dict[str, Any],
    upbit_report: dict[str, Any],
    *,
    recent_leg_limit: int = 30,
) -> dict[str, Any]:
    """통합 대시보드용 — 거래소별 리포트를 보존하고 통합 요약·최근 레그를 함께 제공한다."""
    bt_summary = dict(bithumb_report.get("summary") or {})
    up_summary = dict(upbit_report.get("summary") or {})
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_kst": get_kst_now_str(),
        "timezone": "Asia/Seoul",
        "summary": _merge_performance_summaries(bt_summary, up_summary),
        "recent_trade_legs": _merge_recent_trade_legs(
            bithumb_report,
            upbit_report,
            limit=recent_leg_limit,
        ),
        "exchanges": {
            "bithumb": bithumb_report,
            "upbit": upbit_report,
        },
    }


def get_24h_quant_summary(
    exchange_name: str,
    end_dt: datetime.datetime | None = None,
) -> dict[str, Any]:
    """직전 24시간 롤링 확정 체결 퀀트 성과(승률, 손익, 수수료, 수수료 잠식률, MDD, 휩소) 산출.

    거래소 격리를 준수하여 지정된 거래소의 DB만 조회하며, DB 예외 발생 시 안전한 기본값을 반환한다.
    """
    import json
    import sqlite3
    from pathlib import Path

    ex_key = str(exchange_name or "").lower().strip()
    root_dir = Path(__file__).resolve().parent.parent
    if ex_key == "upbit":
        db_path = root_dir / "data" / "upbit" / "trading.db"
    else:
        db_path = root_dir / "data" / "trading.db"

    default_res: dict[str, Any] = {
        "exchange": ex_key,
        "total_trades": 0,
        "win_trades": 0,
        "loss_trades": 0,
        "win_rate_pct": 0.0,
        "realized_pnl_krw": 0.0,
        "pnl_pct": 0.0,
        "total_fee_krw": 0.0,
        "fee_erosion_pct": 0.0,
        "fee_erosion_warning": False,
        "fee_eroded_markets": [],
        "mdd_pct": 0.0,
        "mdd_krw": 0.0,
        "whipsaw_count": 0,
        "whipsaw_markets": [],
        "start_time_str": "",
        "end_time_str": "",
        "available": False,
    }

    if not db_path.exists():
        return default_res

    try:
        now = end_dt if end_dt is not None else datetime.datetime.now(KST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=KST)
        # 09:00 정기 결산 시점이면 당일 08:59:59까지, 그 외는 현재 시점 기준 직전 24시간
        end_time = now
        start_time = end_time - datetime.timedelta(hours=24)

        start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
        default_res["start_time_str"] = start_str
        default_res["end_time_str"] = end_str

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM trade_memory
            WHERE exit_time >= ? AND exit_time <= ?
            ORDER BY exit_time ASC
            """,
            (start_str, end_str),
        )
        trades = [dict(r) for r in cur.fetchall()]

        # 시작 자산 조회
        date_part = start_str.split()[0]
        cur.execute("SELECT start_equity FROM daily_stats WHERE date = ?", (date_part,))
        row_eq = cur.fetchone()
        start_equity = float(row_eq[0]) if (row_eq and row_eq[0]) else 1200000.0

        conn.close()

        if not trades:
            default_res["available"] = True
            return default_res

        win_trades = [t for t in trades if (t.get("pnl_krw") or 0.0) > 0.0]
        loss_trades = [t for t in trades if (t.get("pnl_krw") or 0.0) < 0.0]

        total_pnl = sum(float(t.get("pnl_krw") or 0.0) for t in trades)
        win_rate = (len(win_trades) / len(trades) * 100.0) if trades else 0.0
        pnl_pct_equity = (total_pnl / start_equity * 100.0) if start_equity > 0 else 0.0

        # MDD 계산
        cum_pnl = 0.0
        peak_pnl = 0.0
        max_dd_krw = 0.0
        for t in trades:
            cum_pnl += float(t.get("pnl_krw") or 0.0)
            if cum_pnl > peak_pnl:
                peak_pnl = cum_pnl
            dd = peak_pnl - cum_pnl
            if dd > max_dd_krw:
                max_dd_krw = dd
        mdd_pct = (max_dd_krw / start_equity * 100.0) if start_equity > 0 else 0.0

        # 수수료 및 수수료 잠식 분석
        total_fee = 0.0
        fee_eroded_mkts = []
        whipsaw_mkts = []
        gross_pnl_sum = 0.0

        for t in trades:
            raw_str = t.get("raw_data")
            raw = json.loads(raw_str) if raw_str else {}
            trade_fee = float(raw.get("fee", 0.0) or 0.0)
            total_fee += trade_fee
            pnl_krw = float(t.get("pnl_krw") or 0.0)
            gross_pnl = pnl_krw + trade_fee
            gross_pnl_sum += gross_pnl

            # 수수료 잠식: 매매차익은 0 이상인데 수수료로 인해 실현손익이 적자가 된 경우
            if gross_pnl > 0.0 and pnl_krw < 0.0:
                mkt_clean = str(t.get("market", "")).replace("KRW-", "").strip()
                if mkt_clean:
                    fee_eroded_mkts.append(mkt_clean)

            # 휩소 손절 감지
            reason = str(t.get("exit_reason") or "")
            if pnl_krw < 0.0 and any(k in reason for k in ("추세이탈", "횡보", "손절")):
                mkt_clean = str(t.get("market", "")).replace("KRW-", "").strip()
                if mkt_clean:
                    whipsaw_mkts.append(mkt_clean)

        fee_erosion_pct = 0.0
        if gross_pnl_sum > 0:
            fee_erosion_pct = round((total_fee / gross_pnl_sum) * 100.0, 1)
        elif total_pnl < 0 and total_fee > 0:
            fee_erosion_pct = 100.0

        fee_erosion_warning = (len(fee_eroded_mkts) > 0) or (fee_erosion_pct > 50.0)

        return {
            "exchange": ex_key,
            "total_trades": len(trades),
            "win_trades": len(win_trades),
            "loss_trades": len(loss_trades),
            "win_rate_pct": round(win_rate, 1),
            "realized_pnl_krw": round(total_pnl, 1),
            "pnl_pct": round(pnl_pct_equity, 2),
            "total_fee_krw": round(total_fee, 1),
            "fee_erosion_pct": fee_erosion_pct,
            "fee_erosion_warning": fee_erosion_warning,
            "fee_eroded_markets": fee_eroded_mkts,
            "mdd_pct": round(mdd_pct, 2),
            "mdd_krw": round(max_dd_krw, 1),
            "whipsaw_count": len(whipsaw_mkts),
            "whipsaw_markets": whipsaw_mkts,
            "start_time_str": start_str,
            "end_time_str": end_str,
            "available": True,
        }
    except Exception:
        return default_res


def format_24h_quant_telegram_block(summary: dict[str, Any]) -> str:
    """모닝 리포트에 삽입할 직전 24시간 퀀트 성과 분석 HTML 블록을 포맷팅한다."""
    if not summary or not summary.get("available") or summary.get("total_trades", 0) == 0:
        return "\n📈 <b>[직전 24시간 퀀트 성과]</b> 체결 내역 없음 (관망 유지)"

    total = summary["total_trades"]
    wins = summary["win_trades"]
    losses = summary["loss_trades"]
    wr = summary["win_rate_pct"]
    pnl = summary["realized_pnl_krw"]
    pct = summary["pnl_pct"]
    fee = summary["total_fee_krw"]
    erosion = summary["fee_erosion_pct"]
    mdd = summary["mdd_pct"]

    pnl_sign = "+" if pnl > 0 else ""
    pnl_color = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")

    lines = [
        f"\n📊 <b>[직전 24시간 롤링 퀀트 성과 결산]</b>",
        f"• <b>실현 손익:</b> {pnl_color} <b>{pnl_sign}{pnl:,.0f} KRW</b> ({pct:+.2f}%)",
        f"• <b>체결 전적:</b> {total}전 {wins}승 {losses}패 (승률 <b>{wr:.1f}%</b>)",
        f"• <b>차감 수수료:</b> {fee:,.0f} KRW (잠식률: {erosion:.1f}%)",
        f"• <b>최대 낙폭(MDD):</b> {mdd:.2f}%",
    ]

    # 경고 섹션
    warnings = []
    eroded_mkts = [m for m in summary.get("fee_eroded_markets", []) if m]
    if summary.get("fee_erosion_warning") and eroded_mkts:
        eroded_str = ", ".join(eroded_mkts[:3])
        warnings.append(f"⚠️ 수수료 잠식 적자: {eroded_str} (매매차익 대비 수수료 초과)")
    elif summary.get("fee_erosion_warning") and erosion > 50.0:
        warnings.append(f"⚠️ 수수료 과다 잠식: {erosion:.1f}%")

    whipsaw_mkts = [m for m in summary.get("whipsaw_markets", []) if m]
    if whipsaw_mkts:
        whip_str = ", ".join(whipsaw_mkts[:3])
        warnings.append(f"⚠️ 휩소 손절 발생: {whip_str}")

    if warnings:
        lines.append("• <b>리스크 감지:</b> " + " / ".join(warnings))

    return "\n".join(lines)
