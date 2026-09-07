"""야간 HOLD 후보의 90분 가상 체결 결과를 거래소별로 재현한다."""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


# 로그 가격은 판단 시점의 관측가다. 진입·청산 양쪽에 보수적으로 비용을 적용한다.
KST = ZoneInfo("Asia/Seoul")
FEE_RATE = 0.0004
SLIPPAGE_RATE = 0.001
HOLD_MINUTES = 90
DATE = "2026-09-07"
ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports"

EXCHANGES = {
    "bithumb": {
        "log": ROOT / "logs" / "trading.log",
        "api": "https://api.bithumb.com/v1/candles/minutes/5",
    },
    "upbit": {
        "log": ROOT / "logs" / "trading_upbit.log",
        "api": "https://api.upbit.com/v1/candles/minutes/5",
    },
}


def parse_candidates(exchange: str, path: Path) -> list[dict[str, object]]:
    """ACTION=HOLD와 바로 뒤의 근거 로그를 묶어 가상 진입 후보로 만든다."""
    rows: list[dict[str, object]] = []
    pending: dict[str, object] | None = None
    action_re = re.compile(
        r"^\[?(?P<time>2026-09-07 \d{2}:\d{2}:\d{2}),\d+.*?"
        r"\[(?P<market>KRW-[A-Z0-9]+)\].*?전략: ACTION=HOLD, 진입가=(?P<price>[\d,.]+)"
    )
    score_re = re.compile(r"알파스코어 (\d+)점")

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        action_match = action_re.search(line)
        if action_match:
            timestamp = datetime.strptime(action_match.group("time"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            pending = {
                "exchange": exchange,
                "timestamp": timestamp,
                "market": action_match.group("market"),
                "observed_entry_price": float(action_match.group("price").replace(",", "")),
            }
            continue

        if pending and "근거:" in line:
            score_match = score_re.search(line)
            if score_match:
                pending["alpha_score"] = int(score_match.group(1))
                pending["reason"] = line.split("근거:", 1)[1].strip()
                rows.append(pending)
            pending = None
    return rows


def fetch_candles(api_url: str, market: str) -> list[dict[str, object]]:
    """공개 API에서 충분한 과거 5분봉을 한 번만 받아 후보별 재사용한다."""
    query = urlencode({"market": market, "count": 200})
    request = Request(f"{api_url}?{query}", headers={"Accept": "application/json", "User-Agent": "bithumb-rejection-audit/1.0"})
    with urlopen(request, timeout=15) as response:  # nosec B310 - 고정된 거래소 공개 API만 사용한다.
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{market}: 캔들 응답이 목록이 아닙니다.")
    return payload


def candle_time(candle: dict[str, object]) -> datetime:
    """거래소가 제공하는 KST 시간을 통일해 비교한다."""
    raw = str(candle.get("candle_date_time_kst", ""))
    return datetime.fromisoformat(raw).replace(tzinfo=KST)


def classify_reason(reason: str) -> str:
    """후보가 탈락한 가장 눈에 띄는 안전·품질 사유를 요약한다."""
    if "이격 과열차단" in reason:
        return "이격 과열"
    if "RSI=차단" in reason:
        return "RSI 범위 이탈"
    if "거래량배수=" in reason:
        match = re.search(r"거래량배수=([0-9.]+)", reason)
        if match and float(match.group(1)) < 1.3:
            return "거래량 부족"
    if "4봉 고점 돌파=차단" in reason:
        return "돌파 미확인"
    if "1H MTF=차단" in reason or "1H " in reason and " < EMA20 " in reason:
        return "상위 추세 미달"
    if "반등확인 차단" in reason:
        return "반등 미확인"
    if "하드게이트 차단" in reason:
        return "하드 게이트"
    return "복합 조건 미달"


def analyze_candidate(candidate: dict[str, object], candles: list[dict[str, object]], now: datetime) -> dict[str, object]:
    """완결된 90분 구간에서 순비용 기준 MFE·MAE·타임스탑 손익을 계산한다."""
    entry_time = candidate["timestamp"]
    assert isinstance(entry_time, datetime)
    deadline = entry_time + timedelta(minutes=HOLD_MINUTES)
    result = dict(candidate)
    result["reason_group"] = classify_reason(str(candidate["reason"]))
    result["deadline"] = deadline.isoformat()

    if deadline > now:
        result["status"] = "PENDING_90M"
        return result

    normalized = []
    for candle in candles:
        try:
            normalized.append(
                {
                    "time": candle_time(candle),
                    "high": float(candle["high_price"]),
                    "low": float(candle["low_price"]),
                    "close": float(candle["trade_price"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    normalized.sort(key=lambda item: item["time"])

    # 미완성 진입 봉의 과거 고저가를 사용하지 않기 위해, 다음 완성 5분봉부터만 평가한다.
    start = entry_time.replace(second=0, microsecond=0) + timedelta(minutes=5 - entry_time.minute % 5)
    window = [bar for bar in normalized if start <= bar["time"] < deadline]
    exit_bars = [bar for bar in normalized if bar["time"] >= deadline]
    if not window or not exit_bars:
        result["status"] = "INSUFFICIENT_CANDLES"
        return result

    entry_price = float(candidate["observed_entry_price"])
    # 매수 때는 더 비싸게, 청산 때는 더 싸게 체결되고 각각 수수료가 차감된다고 가정한다.
    entry_cost_basis = entry_price * (1 + SLIPPAGE_RATE) * (1 + FEE_RATE)
    exit_multiplier = (1 - SLIPPAGE_RATE) * (1 - FEE_RATE)
    max_high = max(bar["high"] for bar in window)
    min_low = min(bar["low"] for bar in window)
    timed_exit = exit_bars[0]["close"]

    result.update(
        {
            "status": "COMPLETE",
            "window_start": start.isoformat(),
            "max_high": max_high,
            "min_low": min_low,
            "time_stop_exit": timed_exit,
            "mfe_net_pct": (max_high * exit_multiplier / entry_cost_basis - 1) * 100,
            "mae_net_pct": (min_low * exit_multiplier / entry_cost_basis - 1) * 100,
            "time_stop_net_pct": (timed_exit * exit_multiplier / entry_cost_basis - 1) * 100,
        }
    )
    return result


def metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    """완결 후보 집합의 비용 반영 타임스탑 성과를 같은 기준으로 요약한다."""
    if not rows:
        return {"count": 0}
    returns = [float(row["time_stop_net_pct"]) for row in rows]
    ordered = sorted(returns)
    return {
        "count": len(rows),
        "positive_time_stop": sum(value > 0 for value in returns),
        "win_rate_pct": round(sum(value > 0 for value in returns) / len(returns) * 100, 2),
        "avg_time_stop_net_pct": round(sum(returns) / len(returns), 4),
        "median_time_stop_net_pct": round(ordered[len(ordered) // 2], 4),
        "avg_mfe_net_pct": round(sum(float(row["mfe_net_pct"]) for row in rows) / len(rows), 4),
        "avg_mae_net_pct": round(sum(float(row["mae_net_pct"]) for row in rows) / len(rows), 4),
    }


def has_near_complete_momentum(reason: str) -> bool:
    """돌파형 완화 검토에 필요한 가격·거래량·RSI 핵심 조건 충족 여부를 판별한다."""
    volume_match = re.search(r"거래량배수=([0-9.]+)", reason)
    return (
        "4봉 고점 돌파=통과" in reason
        and "양봉=통과" in reason
        and "RSI=통과" in reason
        and "1H MTF=통과" in reason
        and volume_match is not None
        and float(volume_match.group(1)) >= 1.3
    )


def remove_overlapping_market_windows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """같은 종목을 90분 안에 반복 평가한 행은 첫 판단만 남겨 독립 표본에 가깝게 만든다."""
    kept: list[dict[str, object]] = []
    next_allowed: dict[tuple[str, str], datetime] = {}
    for row in sorted(rows, key=lambda item: (str(item["exchange"]), str(item["market"]), item["timestamp"])):
        timestamp = row["timestamp"]
        assert isinstance(timestamp, datetime)
        key = (str(row["exchange"]), str(row["market"]))
        if timestamp >= next_allowed.get(key, datetime.min.replace(tzinfo=KST)):
            kept.append(row)
            next_allowed[key] = timestamp + timedelta(minutes=HOLD_MINUTES)
    return kept


def main() -> int:
    now = datetime.now(KST)
    candidates: list[dict[str, object]] = []
    for exchange, config in EXCHANGES.items():
        candidates.extend(parse_candidates(exchange, config["log"]))

    # 동일 종목 캔들은 한 번만 받아 API 호출을 최소화한다.
    candle_cache: dict[tuple[str, str], list[dict[str, object]]] = {}
    fetch_errors: list[str] = []
    for exchange, market in sorted({(str(row["exchange"]), str(row["market"])) for row in candidates}):
        try:
            candle_cache[(exchange, market)] = fetch_candles(EXCHANGES[exchange]["api"], market)
        except Exception as exc:  # 분석 실패를 후보 성과로 오인하지 않도록 별도 기록한다.
            fetch_errors.append(f"{exchange}:{market}: {exc}")
        time.sleep(0.14)

    results = []
    for candidate in candidates:
        key = (str(candidate["exchange"]), str(candidate["market"]))
        candles = candle_cache.get(key, [])
        if not candles:
            item = dict(candidate)
            item["reason_group"] = classify_reason(str(candidate["reason"]))
            item["status"] = "CANDLE_FETCH_FAILED"
            results.append(item)
        else:
            results.append(analyze_candidate(candidate, candles, now))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = REPORT_DIR / "overnight_rejection_virtual_trades_2026-09-07.csv"
    columns = [
        "exchange", "timestamp", "market", "observed_entry_price", "alpha_score", "reason_group", "status",
        "window_start", "deadline", "max_high", "min_low", "time_stop_exit", "mfe_net_pct", "mae_net_pct", "time_stop_net_pct", "reason",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            clean = dict(row)
            clean["timestamp"] = clean["timestamp"].isoformat()
            for key in ("mfe_net_pct", "mae_net_pct", "time_stop_net_pct"):
                if key in clean:
                    clean[key] = round(float(clean[key]), 4)
            writer.writerow(clean)

    completed = [row for row in results if row.get("status") == "COMPLETE"]
    pending = [row for row in results if row.get("status") == "PENDING_90M"]
    by_exchange = {}
    for exchange in EXCHANGES:
        rows = [row for row in completed if row["exchange"] == exchange]
        by_exchange[exchange] = {
            "raw": metrics(rows),
            "non_overlapping_90m": metrics(remove_overlapping_market_windows(rows)),
            "score_buckets": {
                "0_49": metrics([row for row in rows if int(row["alpha_score"]) < 50]),
                "50_64": metrics([row for row in rows if 50 <= int(row["alpha_score"]) < 65]),
                "65_74": metrics([row for row in rows if 65 <= int(row["alpha_score"]) < 75]),
                "75_plus": metrics([row for row in rows if int(row["alpha_score"]) >= 75]),
            },
            "near_complete_momentum": metrics([
                row for row in rows if has_near_complete_momentum(str(row["reason"]))
            ]),
            "reason_groups": Counter(str(row["reason_group"]) for row in rows).most_common(),
        }

    summary = {
        "analysis_generated_at_kst": now.isoformat(),
        "scope": "2026-09-07 trading.log ACTION=HOLD candidates",
        "assumptions": {
            "entry_slippage_rate": SLIPPAGE_RATE,
            "exit_slippage_rate": SLIPPAGE_RATE,
            "entry_fee_rate": FEE_RATE,
            "exit_fee_rate": FEE_RATE,
            "hold_minutes": HOLD_MINUTES,
            "entry_candle_handling": "판단 시점 이후 다음 완성 5분봉부터 MFE/MAE를 계산",
        },
        "total_candidates": len(results),
        "completed_candidates": len(completed),
        "pending_90m_candidates": len(pending),
        "candle_fetch_errors": fetch_errors,
        "by_exchange": by_exchange,
    }
    summary_path = REPORT_DIR / "overnight_rejection_virtual_trades_2026-09-07.summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"CSV={csv_path}")
    print(f"SUMMARY={summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
