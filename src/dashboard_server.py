"""
통합 퀀트 트레이딩 대시보드 게이트웨이 서버 (v5.0)
- 빗썸(127.0.0.1:17979) 및 업비트(127.0.0.1:17980) 트레이딩 코어와 통신
- 단일 포트(기본 7979)에서 통합 자산/포지션 및 거래소별 개별 모니터링 제공
- 트레이딩 엔진과 UI 프로세스의 완전한 물리적/논리적 분리 보장
- 트레이딩 코어가 오프라인이어도 무중단 안전 서빙 및 상태 표시
"""

import argparse
import hmac
import json
import logging
import math
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import requests
from dotenv import load_dotenv

from confirmed_fill_performance import merge_exchange_reports
from market_intelligence import MarketIntelligenceService
from runtime_config import CommonConfigManager

# UTF-8 표준 출력 보장
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, AttributeError):
        pass

# 로깅 설정 (logs/dashboard.log)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 환경 변수 로드 (.env)
load_dotenv(os.path.join(project_root, ".env"))

log_dir = os.path.join(project_root, "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "dashboard.log")

file_handler = TimedRotatingFileHandler(
    filename=log_file, when="midnight", interval=1, backupCount=14, encoding="utf-8"
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] [DASHBOARD] %(message)s")
)

logger = logging.getLogger("DashboardServer")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.addHandler(file_handler)
if sys.stdout is not None:
    stream_handler = logging.StreamHandler(sys.stdout)
    # 포그라운드 콘솔은 운영 중 확인이 필요한 경고 이상만 출력한다.
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [DASHBOARD] %(message)s")
    )
    logger.addHandler(stream_handler)
logger.propagate = False

# 대시보드에서 조회할 운영 로그를 고정한다. 요청값으로 파일 경로를 받지 않아
# 경로 조작을 통한 임의 파일 노출을 원천 차단한다.
ALERT_LOG_SOURCES = {
    "trading.log": ("빗썸 봇", "bithumb"),
    "trading_upbit.log": ("업비트 봇", "upbit"),
    "watchdog.log": ("빗썸 워치독", "bithumb"),
    "watchdog_upbit.log": ("업비트 워치독", "upbit"),
    # 통합 화면에서만 대시보드 자체 오류를 보조 정보로 제공한다.
    "dashboard.log": ("통합 대시보드", "combined"),
}
ALERT_LEVEL_PATTERN = re.compile(r"\[(WARNING|ERROR|CRITICAL)\]", re.IGNORECASE)
ALERT_TIMESTAMP_PATTERN = re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
ALERT_TAIL_BYTES = 256 * 1024
ALERT_SCAN_CHUNK_BYTES = 256 * 1024
ALERT_MAX_SCAN_BYTES = 10 * 1024 * 1024  # 파일당 최대 역방향 탐색 바이트 (10MB)
ALERT_TARGET_PER_SOURCE = 20  # 소스당 최소 확보 목표 경고 건수
ALERT_LIMIT = 100


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """클라이언트 연결 조기 종료 시 불필요한 스택트레이스 억제"""
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc_type, exc_val, _ = sys.exc_info()
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ConnectionError):
            logger.debug(f"클라이언트({client_address}) 연결 조기 종료: {exc_val}")
            return
        super().handle_error(request, client_address)


class UnifiedDashboardServer:
    """
    빗썸 및 업비트 듀얼 퀀트 트레이딩 통합 대시보드 서버
    """

    def __init__(
        self,
        port: int | None = None,
        host: str | None = None,
        bithumb_api_url: str = "http://127.0.0.1:17979",
        upbit_api_url: str = "http://127.0.0.1:17980",
        static_dir: str | None = None,
        alert_log_dir: str | None = None,
    ):
        self.port = port if port is not None else int(os.getenv("DASHBOARD_PORT", "7979"))
        self.host = host if host is not None else os.getenv("DASHBOARD_HOST", "100.76.22.126")
        self.bithumb_api_url = bithumb_api_url.rstrip("/")
        self.upbit_api_url = upbit_api_url.rstrip("/")
        self.static_dir = static_dir or self._resolve_static_dir()
        # 설정된 경우에만 원격 제어에 토큰을 요구해 기존 로컬 운영 환경의 호환을 유지한다.
        self.action_token = os.getenv("DASHBOARD_ACTION_TOKEN", "").strip()
        # 테스트에서는 임시 경로를 주입할 수 있지만 운영에서는 프로젝트 logs만 읽는다.
        self.alert_log_dir = os.path.abspath(alert_log_dir or log_dir)
        self.server: QuietThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._running = False
        self._cached_status: dict[str, Any] = {}
        self._cache_lock = threading.Lock()
        self._alert_cache: dict[str, tuple[float, dict[str, Any], dict[str, float]]] = {}
        self._alert_cache_lock = threading.Lock()
        self._last_known_good: dict[str, dict[str, Any]] = {}
        self._last_success_ts: dict[str, float] = {}
        self.http_session = requests.Session()
        self.http_session.trust_env = False

    def is_action_authorized(self, supplied_token: str) -> bool:
        """원격 제어 토큰이 설정된 경우에만 상수 시간 비교로 검증한다."""
        return not self.action_token or hmac.compare_digest(supplied_token or "", self.action_token)

    @staticmethod
    def _mask_sensitive_log_text(text: str) -> str:
        """로그에 우연히 포함된 인증 값이 브라우저로 전달되지 않게 마스킹한다."""
        # 키 이름 뒤의 값만 가려 진단에 필요한 메시지 구조는 보존한다.
        return re.sub(
            r"(?i)(access[_ -]?key|secret|token|password|authorization)(\s*[:=]\s*)([^\s,}\]\"']+)",
            r"\1\2***",
            text,
        )

    @staticmethod
    def _read_recent_lines(
        path: str,
        max_bytes: int = ALERT_MAX_SCAN_BYTES,
        chunk_size: int = ALERT_SCAN_CHUNK_BYTES,
        min_alert_matches: int = ALERT_TARGET_PER_SOURCE,
    ) -> list[str]:
        """대용량 로그도 끝에서부터 역방향으로 청크 단위 스캔하여,
        경고(WARNING 이상) 로그를 최소 min_alert_matches건 확보하거나
        최대 max_bytes까지 거슬러 올라가며 최근 라인들을 반환한다.
        """
        try:
            with open(path, "rb") as log_stream:
                log_stream.seek(0, os.SEEK_END)
                size = log_stream.tell()
                if size == 0:
                    return []

                collected_lines: list[str] = []
                alert_count = 0
                pos = size
                remainder = b""

                while pos > 0 and (size - pos) < max_bytes and alert_count < min_alert_matches:
                    read_size = min(chunk_size, pos)
                    pos -= read_size
                    log_stream.seek(pos)
                    raw_chunk = log_stream.read(read_size) + remainder
                    chunk_lines = raw_chunk.split(b"\n")

                    if pos > 0:
                        # 첫 번째 조각은 앞선 청크의 줄과 합쳐질 수 있으므로 다음 청크의 remainder로 보관
                        remainder = chunk_lines[0]
                        valid_chunk_lines = chunk_lines[1:]
                    else:
                        remainder = b""
                        valid_chunk_lines = chunk_lines

                    decoded_chunk: list[str] = []
                    for raw_line in valid_chunk_lines:
                        line_str = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                        if not line_str.strip():
                            continue
                        decoded_chunk.append(line_str)
                        if ALERT_LEVEL_PATTERN.search(line_str):
                            alert_count += 1

                    # collected_lines의 앞쪽에 누적하여 시간 순서 보존
                    collected_lines = decoded_chunk + collected_lines

                return collected_lines
        except (OSError, ValueError):
            return []

    def get_alert_logs(self, exchange_target: str = "combined") -> dict[str, Any]:
        """선택한 거래소 범위의 WARNING 이상 로그만 최신순으로 반환한다 (2.0초 TTL 및 mtime 캐싱)."""
        target = exchange_target.lower()
        if target not in {"combined", "bithumb", "upbit"}:
            target = "combined"

        now = time.time()
        with self._alert_cache_lock:
            cached_entry = self._alert_cache.get(target)
            if cached_entry:
                cached_ts, cached_data, cached_mtimes = cached_entry
                # 2.0초 미만이면 디스크 I/O 없이 즉시 캐시 반환
                if now - cached_ts < 2.0:
                    return cached_data

                # 2.0초 경과 시 파일들의 mtime 검사
                files_changed = False
                current_mtimes = {}
                for filename, (_, exchange) in ALERT_LOG_SOURCES.items():
                    if target != "combined" and exchange != target:
                        continue
                    path = os.path.join(self.alert_log_dir, filename)
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        mtime = 0.0
                    current_mtimes[filename] = mtime
                    if cached_mtimes.get(filename, 0.0) != mtime:
                        files_changed = True

                if not files_changed:
                    # 파일 변경이 없으면 캐시 타임스탬프만 갱신하고 즉시 반환
                    self._alert_cache[target] = (now, cached_data, current_mtimes)
                    return cached_data

        alerts: list[dict[str, str]] = []
        scan_mtimes: dict[str, float] = {}
        for filename, (source, exchange) in ALERT_LOG_SOURCES.items():
            # 통합 탭은 양쪽 거래소와 통합 게이트웨이 로그를 모두 포함한다.
            if target != "combined" and exchange != target:
                continue
            path = os.path.join(self.alert_log_dir, filename)
            try:
                scan_mtimes[filename] = os.path.getmtime(path)
            except OSError:
                scan_mtimes[filename] = 0.0
            for line in self._read_recent_lines(path):
                level_match = ALERT_LEVEL_PATTERN.search(line)
                if not level_match:
                    continue
                timestamp_match = ALERT_TIMESTAMP_PATTERN.search(line)
                alerts.append({
                    "source": source,
                    "level": level_match.group(1).upper(),
                    "timestamp": timestamp_match.group(1) if timestamp_match else "",
                    "message": self._mask_sensitive_log_text(line.strip()),
                })

        # 로그 포맷의 날짜 문자열은 ISO 순서이므로 문자열 정렬로 최신순을 보장한다.
        alerts.sort(key=lambda item: item["timestamp"], reverse=True)
        result = {"alerts": alerts[:ALERT_LIMIT], "exchange": target, "updated_at": now}
        with self._alert_cache_lock:
            self._alert_cache[target] = (now, result, scan_mtimes)
        return result

    def _resolve_static_dir(self) -> str | None:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dashboard"))
        dist_dir = os.path.join(base_dir, "dist")
        if os.path.isdir(dist_dir) and os.path.isfile(os.path.join(dist_dir, "index.html")):
            return dist_dir
        if os.path.isdir(base_dir) and os.path.isfile(os.path.join(base_dir, "index.html")):
            return base_dir
        return None

    def fetch_exchange_status(self, api_url: str, exchange_name: str) -> dict[str, Any]:
        """개별 거래소 봇의 상태를 5.0초 타임아웃으로 안전 조회 및 Stale-While-Revalidate 지원"""
        now = time.time()
        try:
            res = self.http_session.get(f"{api_url}/api/status", timeout=5.0)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict) and data.get("total_equity", 0) > 0:
                    data["online"] = True
                    data.setdefault("exchange", exchange_name)
                    self._last_known_good[exchange_name] = data
                    self._last_success_ts[exchange_name] = now
                    return data
                elif isinstance(data, dict):
                    data["online"] = True
                    data.setdefault("exchange", exchange_name)
                    return data
        except Exception as e:
            logger.debug(f"[{exchange_name}] status fetch error: {e}")

        # 일시적 지연 시 직전 정상 캐시 데이터 유지 (화면 깜빡임 및 데이터 증발 방지)
        last_good = self._last_known_good.get(exchange_name)
        last_ts = self._last_success_ts.get(exchange_name, 0.0)
        if last_good and (now - last_ts < 60.0):
            fallback_data = dict(last_good)
            fallback_data["online"] = (now - last_ts < 20.0)
            if not fallback_data["online"]:
                fallback_data["status"] = "UPDATING"
                fallback_data["bot_state"] = "⏳ 데이터 동기화 중"
            return fallback_data

        # 오프라인은 신규 매수 가능으로 해석될 여지가 없도록 명시적으로 차단한다.
        fb_mi: dict[str, Any] = {}
        try:
            fb_mi = MarketIntelligenceService.get_instance(exchange_scope=exchange_name).get_latest_intelligence(max_age_sec=3600.0) or {}
        except Exception:
            fb_mi = {}

        return {
            "online": False,
            "status": "OFFLINE",
            "exchange": exchange_name,
            "title": f"{exchange_name.capitalize()} 트레이딩 봇",
            "total_equity": 0.0,
            "krw_available": 0.0,
            "daily_start_equity": 0.0,
            "daily_pnl_krw": 0.0,
            "daily_pnl_pct": 0.0,
            "realized_pnl_krw": 0.0,
            "total_trades": 0,
            "win_trades": 0,
            "win_rate": 0.0,
            "positions": [],
            "candidates": [],
            "recent_trades": [],
            "recent_orders": [],
            "daily_stats_history": [],
            "confirmed_fill_performance": {},
            "message": "봇 프로세스 미구동 또는 응답 없음",
            "safety": {
                "entry_ready": False,
                "entry_block_reasons": ["봇 프로세스 미구동 또는 응답 없음"],
                "entry_blocking_markets": [],
                "order_status_counts": {},
                "feed": {"is_healthy": False, "status": "OFFLINE"},
            },
            "api_usage": {
                "exchange": {},
                "gemini": {},
                "gemini_bithumb": {},
                "gemini_upbit": {},
                "ai_provider_bithumb": {},
            },
            "market_intelligence": fb_mi,
            "market_intelligence_bithumb": fb_mi if exchange_name == "bithumb" else {},
            "market_intelligence_upbit": fb_mi if exchange_name == "upbit" else {},
        }

    def get_aggregated_status(self) -> dict[str, Any]:
        """빗썸과 업비트의 상태를 수집하여 통합 지표 산출"""
        bithumb_data = self.fetch_exchange_status(self.bithumb_api_url, "bithumb")
        upbit_data = self.fetch_exchange_status(self.upbit_api_url, "upbit")

        # 종합 자산 및 손익 지표 산출
        bt_eq = float(bithumb_data.get("total_equity", 0.0) or 0.0)
        up_eq = float(upbit_data.get("total_equity", 0.0) or 0.0)
        total_equity = bt_eq + up_eq

        bt_krw = float(bithumb_data.get("krw_available", 0.0) or 0.0)
        up_krw = float(upbit_data.get("krw_available", 0.0) or 0.0)
        total_krw = bt_krw + up_krw

        bt_start_eq = float(bithumb_data.get("daily_start_equity", 0.0) or 0.0)
        up_start_eq = float(upbit_data.get("daily_start_equity", 0.0) or 0.0)
        total_start_equity = bt_start_eq + up_start_eq

        bt_daily_pnl = float(bithumb_data.get("daily_pnl_krw", 0.0) or 0.0)
        up_daily_pnl = float(upbit_data.get("daily_pnl_krw", 0.0) or 0.0)
        total_daily_pnl = bt_daily_pnl + up_daily_pnl

        total_daily_pnl_pct = (
            (total_daily_pnl / total_start_equity * 100.0)
            if total_start_equity > 0.0
            else 0.0
        )

        bt_realized_pnl = float(bithumb_data.get("realized_pnl_krw", 0.0) or 0.0)
        up_realized_pnl = float(upbit_data.get("realized_pnl_krw", 0.0) or 0.0)
        total_realized_pnl = bt_realized_pnl + up_realized_pnl

        bt_trades = int(bithumb_data.get("total_trades", 0) or 0)
        up_trades = int(upbit_data.get("total_trades", 0) or 0)
        total_trades = bt_trades + up_trades

        bt_wins = int(bithumb_data.get("win_trades", 0) or 0)
        up_wins = int(upbit_data.get("win_trades", 0) or 0)
        total_wins = bt_wins + up_wins
        win_rate = (total_wins / total_trades * 100.0) if total_trades > 0 else 0.0

        # 통합 일일 자산 변동 및 성과 이력 (날짜별 집계)
        daily_map: dict[str, dict[str, Any]] = {}
        for row in bithumb_data.get("daily_stats_history", []):
            if isinstance(row, dict) and row.get("date"):
                d = str(row["date"])
                daily_map[d] = {
                    "date": d,
                    "exchange": "combined",
                    "start_equity": float(row.get("start_equity", 0.0) or 0.0),
                    "realized_pnl_krw": float(row.get("realized_pnl_krw", 0.0) or 0.0),
                    "total_trades": int(row.get("total_trades", 0) or 0),
                    "win_trades": int(row.get("win_trades", 0) or 0),
                    "kill_switch_active": bool(row.get("kill_switch_active")),
                }

        for row in upbit_data.get("daily_stats_history", []):
            if isinstance(row, dict) and row.get("date"):
                d = str(row["date"])
                if d in daily_map:
                    daily_map[d]["start_equity"] += float(row.get("start_equity", 0.0) or 0.0)
                    daily_map[d]["realized_pnl_krw"] += float(row.get("realized_pnl_krw", 0.0) or 0.0)
                    daily_map[d]["total_trades"] += int(row.get("total_trades", 0) or 0)
                    daily_map[d]["win_trades"] += int(row.get("win_trades", 0) or 0)
                    daily_map[d]["kill_switch_active"] = daily_map[d]["kill_switch_active"] or bool(row.get("kill_switch_active"))
                else:
                    daily_map[d] = {
                        "date": d,
                        "exchange": "combined",
                        "start_equity": float(row.get("start_equity", 0.0) or 0.0),
                        "realized_pnl_krw": float(row.get("realized_pnl_krw", 0.0) or 0.0),
                        "total_trades": int(row.get("total_trades", 0) or 0),
                        "win_trades": int(row.get("win_trades", 0) or 0),
                        "kill_switch_active": bool(row.get("kill_switch_active")),
                    }

        combined_daily_history: list[dict[str, Any]] = []
        for d, item in sorted(daily_map.items(), key=lambda x: x[0], reverse=True):
            st_eq = item["start_equity"]
            pnl_k = item["realized_pnl_krw"]
            t_tr = item["total_trades"]
            w_tr = item["win_trades"]
            item["start_equity"] = int(st_eq)
            item["realized_pnl_krw"] = int(pnl_k)
            item["pnl_pct"] = round((pnl_k / st_eq * 100.0), 2) if st_eq > 0 else 0.0
            item["win_rate"] = round((w_tr / t_tr * 100.0), 1) if t_tr > 0 else 0.0
            combined_daily_history.append(item)

        # 통합 포지션 목록 (거래소 태깅)
        combined_positions = []
        for p in bithumb_data.get("positions", []):
            if isinstance(p, dict):
                item = dict(p)
                item["exchange"] = "bithumb"
                item["exchange_label"] = "빗썸"
                combined_positions.append(item)

        for p in upbit_data.get("positions", []):
            if isinstance(p, dict):
                item = dict(p)
                item["exchange"] = "upbit"
                item["exchange_label"] = "업비트"
                combined_positions.append(item)

        # 통합 후보군 목록
        combined_candidates = []
        for c in bithumb_data.get("candidates", []):
            if isinstance(c, dict):
                item = dict(c)
                item["exchange"] = "bithumb"
                item["exchange_label"] = "빗썸"
                combined_candidates.append(item)

        for c in upbit_data.get("candidates", []):
            if isinstance(c, dict):
                item = dict(c)
                item["exchange"] = "upbit"
                item["exchange_label"] = "업비트"
                combined_candidates.append(item)

        def candidate_alpha_score(candidate: dict[str, Any]) -> float:
            """통합 Watchlist 정렬에 사용할 유효한 7대 알파 점수를 반환한다."""
            try:
                score = float(candidate.get("alpha_score", 0) or 0)
                # 누락·비수치·무한대 점수는 최하위 기본값으로 처리해 화면 정렬을 안정화한다.
                return score if math.isfinite(score) else 0.0
            except (TypeError, ValueError):
                return 0.0

        # 통합 탭은 거래소별 수집 순서가 아닌 7대 알파 점수 높은 순으로 후보를 보여준다.
        combined_candidates.sort(key=candidate_alpha_score, reverse=True)

        # 통합 최근 완료 거래 내역 (최신순 정렬)
        combined_recent_trades = []
        for t in bithumb_data.get("recent_trades", []):
            if isinstance(t, dict):
                item = dict(t)
                item["exchange"] = "bithumb"
                combined_recent_trades.append(item)

        for t in upbit_data.get("recent_trades", []):
            if isinstance(t, dict):
                item = dict(t)
                item["exchange"] = "upbit"
                combined_recent_trades.append(item)

        combined_recent_trades.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
        combined_recent_trades = combined_recent_trades[:20]

        # 통합 최근 주문 저널 (최신순 정렬)
        combined_orders = []
        for o in bithumb_data.get("recent_orders", []):
            if isinstance(o, dict):
                item = dict(o)
                item["exchange"] = "bithumb"
                combined_orders.append(item)

        for o in upbit_data.get("recent_orders", []):
            if isinstance(o, dict):
                item = dict(o)
                item["exchange"] = "upbit"
                combined_orders.append(item)

        combined_orders.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
        combined_orders = combined_orders[:20]

        # 공포탐욕지수 (둘 중 정상인 것 우선)
        fng = bithumb_data.get("fear_and_greed") or upbit_data.get("fear_and_greed") or "50점 (중립)"

        # 봇 상태 (온라인 여부 기반)
        bt_status = "ONLINE" if bithumb_data.get("online") else "OFFLINE"
        up_status = "ONLINE" if upbit_data.get("online") else "OFFLINE"
        bot_state = "🟢 정상 가동 중" if (bithumb_data.get("online") or upbit_data.get("online")) else "⚪ 봇 미가동 (오프라인)"

        # 통합 화면은 한 거래소라도 안전 상태가 불명확하면 신규 매수를 허용하지 않는다.
        safety_by_exchange = {
            "bithumb": bithumb_data.get("safety") if isinstance(bithumb_data.get("safety"), dict) else {},
            "upbit": upbit_data.get("safety") if isinstance(upbit_data.get("safety"), dict) else {},
        }
        combined_block_reasons: list[str] = []
        combined_counts: dict[str, int] = {}
        feed_by_exchange: dict[str, dict[str, Any]] = {}
        for exchange, data in (("bithumb", bithumb_data), ("upbit", upbit_data)):
            safety = safety_by_exchange[exchange]
            label = "빗썸" if exchange == "bithumb" else "업비트"
            raw_feed = safety.get("feed")
            feed_by_exchange[exchange] = dict(raw_feed) if isinstance(raw_feed, dict) else {
                "is_healthy": False,
                "status": "DATA_UNAVAILABLE",
                "message": "시세 스트림 상태 미전달",
            }
            if not data.get("online", False):
                combined_block_reasons.append(f"{label} 봇 오프라인")
            if not bool(safety.get("entry_ready", False)):
                for reason in safety.get("entry_block_reasons", []) or ["매수 준비 상태 확인 불가"]:
                    combined_block_reasons.append(f"{label}: {reason}")
            for status, count in (safety.get("order_status_counts", {}) or {}).items():
                try:
                    combined_counts[str(status)] = combined_counts.get(str(status), 0) + int(count)
                except (TypeError, ValueError):
                    continue
        healthy_feed_count = sum(1 for feed in feed_by_exchange.values() if feed.get("is_healthy") is True)
        combined_feed = {
            # 통합 탭은 두 거래소의 시세 스트림이 모두 정상일 때만 정상으로 표기한다.
            "is_healthy": healthy_feed_count == len(feed_by_exchange),
            "status": "DATA_AVAILABLE" if healthy_feed_count == len(feed_by_exchange) else (
                "DEGRADED" if healthy_feed_count > 0 else "DATA_UNAVAILABLE"
            ),
            "by_exchange": feed_by_exchange,
        }
        combined_blocking_markets = sorted(set(
            list(bithumb_data.get("safety", {}).get("entry_blocking_markets", []) or []) +
            list(upbit_data.get("safety", {}).get("entry_blocking_markets", []) or [])
        ))
        combined_safety = {
            "entry_ready": len(combined_block_reasons) == 0,
            "entry_block_reasons": combined_block_reasons,
            "entry_blocking_markets": combined_blocking_markets,
            "order_status_counts": combined_counts,
            "by_exchange": safety_by_exchange,
            "feed": combined_feed,
        }
        combined_new_listing_used = int(bithumb_data.get("new_listing_slot_used", 0) or 0) + int(
            upbit_data.get("new_listing_slot_used", 0) or 0
        )
        combined_new_listing_max = int(bithumb_data.get("new_listing_slot_max", 0) or 0) + int(
            upbit_data.get("new_listing_slot_max", 0) or 0
        )
        combined_new_listing_markets = list(bithumb_data.get("new_listing_markets", []) or []) + list(
            upbit_data.get("new_listing_markets", []) or []
        )
        combined_safety["new_listing_slot_used"] = combined_new_listing_used
        combined_safety["new_listing_slot_max"] = combined_new_listing_max
        combined_safety["new_listing_markets"] = combined_new_listing_markets
        combined_safety["new_listing_enabled"] = bool(
            bithumb_data.get("new_listing_enabled") or upbit_data.get("new_listing_enabled")
        )
        combined_safety["new_listing_enforcement"] = bool(
            bithumb_data.get("new_listing_enforcement") or upbit_data.get("new_listing_enforcement")
        )

        # 거시 시장 인텔리전스 (내부 API 응답 우선, 필드 누락/None 시 crash-safe 로컬 캐시 폴백)
        bt_mi = bithumb_data.get("market_intelligence")
        if bt_mi is None:
            try:
                bt_mi = MarketIntelligenceService.get_instance(exchange_scope="bithumb").get_latest_intelligence(max_age_sec=3600.0) or {}
            except Exception:
                bt_mi = {}
        if "market_intelligence" not in bithumb_data or bithumb_data.get("market_intelligence") is None:
            bithumb_data["market_intelligence"] = bt_mi

        up_mi = upbit_data.get("market_intelligence")
        if up_mi is None:
            try:
                up_mi = MarketIntelligenceService.get_instance(exchange_scope="upbit").get_latest_intelligence(max_age_sec=3600.0) or {}
            except Exception:
                up_mi = {}
        if "market_intelligence" not in upbit_data or upbit_data.get("market_intelligence") is None:
            upbit_data["market_intelligence"] = up_mi

        combined_mi = bt_mi or up_mi or {}

        bt_fill_report = bithumb_data.get("confirmed_fill_performance") or {}
        up_fill_report = upbit_data.get("confirmed_fill_performance") or {}
        combined_fill_report = merge_exchange_reports(bt_fill_report, up_fill_report) if (
            bt_fill_report or up_fill_report
        ) else {}

        combined = {
            "title": "Bithumb & Upbit AI 퀀트 트레이딩 Pro (통합)",
            "total_equity": total_equity,
            "krw_available": total_krw,
            "daily_start_equity": total_start_equity,
            "daily_pnl_krw": total_daily_pnl,
            "daily_pnl_pct": round(total_daily_pnl_pct, 2),
            "realized_pnl_krw": total_realized_pnl,
            "total_trades": total_trades,
            "win_trades": total_wins,
            "win_rate": round(win_rate, 1),
            "bot_state": bot_state,
            "safety": combined_safety,
            # 전략 정책은 양 거래소가 공유하므로 정상 응답을 우선 사용한다.
            "policy": bithumb_data.get("policy") or upbit_data.get("policy") or {},
            "new_listing_markets": combined_new_listing_markets,
            "new_listing_slot_used": combined_new_listing_used,
            "new_listing_slot_max": combined_new_listing_max,
            "new_listing_enabled": combined_safety["new_listing_enabled"],
            "new_listing_enforcement": combined_safety["new_listing_enforcement"],
            "positions": combined_positions,
            "candidates": combined_candidates,
            "recent_trades": combined_recent_trades,
            "recent_orders": combined_orders,
            "daily_stats_history": combined_daily_history,
            "confirmed_fill_performance": combined_fill_report,
            "fear_and_greed": fng,
            "bithumb_online": bithumb_data.get("online", False),
            "upbit_online": upbit_data.get("online", False),
            "bithumb_status": bt_status,
            "upbit_status": up_status,
            "active_positions_count": len(combined_positions),
            "market_intelligence": combined_mi,
            "market_intelligence_bithumb": bt_mi,
            "market_intelligence_upbit": up_mi,
            "api_usage": {
                "bithumb": bithumb_data.get("api_usage", {}).get("exchange", {}),
                "upbit": upbit_data.get("api_usage", {}).get("exchange", {}),
                "gemini_bithumb": bithumb_data.get("api_usage", {}).get("gemini", {}),
                "gemini_upbit": upbit_data.get("api_usage", {}).get("gemini", {}),
                # 기존 Gemini API 키는 유지하고 빗썸 Provider 계측만 표시 전용으로 추가한다.
                "ai_provider_bithumb": bithumb_data.get("api_usage", {}).get("ai_provider", {}),
                "gemini": {
                    "date": bithumb_data.get("api_usage", {}).get("gemini", {}).get("date") or upbit_data.get("api_usage", {}).get("gemini", {}).get("date", ""),
                    "api_calls": (
                        bithumb_data.get("api_usage", {}).get("gemini", {}).get("api_calls", 0) +
                        upbit_data.get("api_usage", {}).get("gemini", {}).get("api_calls", 0)
                    ),
                    "api_success": (
                        bithumb_data.get("api_usage", {}).get("gemini", {}).get("api_success", 0) +
                        upbit_data.get("api_usage", {}).get("gemini", {}).get("api_success", 0)
                    ),
                    "rate_limited": (
                        bithumb_data.get("api_usage", {}).get("gemini", {}).get("rate_limited", 0) +
                        upbit_data.get("api_usage", {}).get("gemini", {}).get("rate_limited", 0)
                    ),
                    "local_fallback": (
                        bithumb_data.get("api_usage", {}).get("gemini", {}).get("local_fallback", 0) +
                        upbit_data.get("api_usage", {}).get("gemini", {}).get("local_fallback", 0)
                    ),
                    "cache_hits": (
                        bithumb_data.get("api_usage", {}).get("gemini", {}).get("cache_hits", 0) +
                        upbit_data.get("api_usage", {}).get("gemini", {}).get("cache_hits", 0)
                    ),
                    "quota_limit": (
                        int(bithumb_data.get("api_usage", {}).get("gemini", {}).get("quota_limit", 1000) or 1000) +
                        int(upbit_data.get("api_usage", {}).get("gemini", {}).get("quota_limit", 1000) or 1000)
                    ),
                    "quota_used_pct": round(
                        (
                            (bithumb_data.get("api_usage", {}).get("gemini", {}).get("api_calls", 0) +
                             upbit_data.get("api_usage", {}).get("gemini", {}).get("api_calls", 0)) /
                            max(1, (
                                int(bithumb_data.get("api_usage", {}).get("gemini", {}).get("quota_limit", 1000) or 1000) +
                                int(upbit_data.get("api_usage", {}).get("gemini", {}).get("quota_limit", 1000) or 1000)
                            )) * 100.0
                        ), 1
                    ),
                    "models": {
                        m: {
                            "calls": (
                                bithumb_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("calls", 0) +
                                upbit_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("calls", 0)
                            ),
                            "quota_limit": (
                                bithumb_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("quota_limit", 500) +
                                upbit_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("quota_limit", 500)
                            ),
                            "quota_used_pct": round(
                                (
                                    (bithumb_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("calls", 0) +
                                     upbit_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("calls", 0)) /
                                    max(1, (
                                        bithumb_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("quota_limit", 500) +
                                        upbit_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("quota_limit", 500)
                                    )) * 100.0
                                ), 1
                            ),
                            "success": (
                                bithumb_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("success", 0) +
                                upbit_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("success", 0)
                            ),
                            "rate_limited": (
                                bithumb_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("rate_limited", 0) +
                                upbit_data.get("api_usage", {}).get("gemini", {}).get("models", {}).get(m, {}).get("rate_limited", 0)
                            ),
                        }
                        for m in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
                    },
                    "reset_info": (
                        bithumb_data.get("api_usage", {}).get("gemini", {}).get("reset_info") or
                        upbit_data.get("api_usage", {}).get("gemini", {}).get("reset_info") or {}
                    ),
                },
            },
        }

        return {
            "combined": combined,
            "bithumb": bithumb_data,
            "upbit": upbit_data,
            "market_intelligence": combined.get("market_intelligence", {}),
            "market_intelligence_bithumb": combined.get("market_intelligence_bithumb", {}),
            "market_intelligence_upbit": combined.get("market_intelligence_upbit", {}),
            "timestamp": time.time(),
        }

    def forward_action(self, action: str, exchange_target: str = "all") -> dict[str, Any]:
        """지정된 거래소로 명령 전달 (panic, pause, resume 등)"""
        results = {}
        target = exchange_target.lower()

        if target in ("bithumb", "all"):
            try:
                headers = {"X-Dashboard-Action-Token": self.action_token} if self.action_token else None
                res = self.http_session.post(f"{self.bithumb_api_url}/api/action/{action}", timeout=2.0, headers=headers)
                results["bithumb"] = res.json() if res.status_code == 200 else {"success": False, "message": f"HTTP {res.status_code}"}
            except Exception as e:
                results["bithumb"] = {"success": False, "message": f"빗썸 연결 실패: {e}"}

        if target in ("upbit", "all"):
            try:
                headers = {"X-Dashboard-Action-Token": self.action_token} if self.action_token else None
                res = self.http_session.post(f"{self.upbit_api_url}/api/action/{action}", timeout=2.0, headers=headers)
                results["upbit"] = res.json() if res.status_code == 200 else {"success": False, "message": f"HTTP {res.status_code}"}
            except Exception as e:
                results["upbit"] = {"success": False, "message": f"업비트 연결 실패: {e}"}

        return {
            "success": True,
            "action": action,
            "target": target,
            "results": results,
            "message": f"[{action.upper()}] 명령 전달 완료 ({target})",
        }

    def get_common_config(self) -> dict[str, Any]:
        """통합 공통 설정 조회 (영구 설정 + 활성 거래소 실시간 상태 병합)"""
        manager = CommonConfigManager()
        settings = manager.get_all_settings()

        # 활성 거래소 코어에서 인메모리 런타임 설정이 있다면 병합
        headers = {"X-Dashboard-Action-Token": self.action_token} if self.action_token else None
        for api_url in (self.bithumb_api_url, self.upbit_api_url):
            try:
                res = self.http_session.get(f"{api_url}/api/config", timeout=1.5, headers=headers)
                if res.status_code == 200:
                    remote_data = res.json()
                    if isinstance(remote_data, dict) and "settings" in remote_data:
                        remote_settings = remote_data["settings"]
                        for k, v in remote_settings.items():
                            # 슬롯 수 및 그에 연동된 자동 합산/계산 필드는 로컬 .env 및 슬롯 무결성을 보호
                            if k in ("MAX_OPEN_POSITIONS", "MAX_POSITION_PCT"):
                                continue
                            if k in settings and isinstance(v, dict):
                                settings[k]["value"] = v.get("value", settings[k]["value"])
                                settings[k]["display_value"] = v.get("display_value", settings[k]["display_value"])
                        break
            except Exception:
                pass

        # 슬롯 3종이 응답에 온전히 포함되도록 보장
        for slot_key, default_val in [
            ("MAX_SCALP_POSITIONS", 3),
            ("MAX_SWING_POSITIONS", 1),
            ("MAX_NEW_LISTING_POSITIONS", 1),
        ]:
            if slot_key not in settings and slot_key in COMMON_CONFIG_SCHEMA:
                try:
                    env_val = int(float(os.getenv(slot_key, str(default_val))))
                except (ValueError, TypeError):
                    env_val = default_val
                field_def = COMMON_CONFIG_SCHEMA[slot_key]
                settings[slot_key] = {
                    "key": slot_key,
                    "name": field_def.name,
                    "category": "portfolio",
                    "type": "int",
                    "value": env_val,
                    "display_value": env_val,
                    "min": field_def.min_val,
                    "max": field_def.max_val,
                    "step": 1,
                    "unit": "개",
                    "description": field_def.description,
                    "default": field_def.default,
                }

        # 슬롯 3종 기반 MAX_OPEN_POSITIONS 및 MAX_POSITION_PCT 자동 계산 정합성 보장
        scalp = settings.get("MAX_SCALP_POSITIONS", {}).get("value", 3)
        swing = settings.get("MAX_SWING_POSITIONS", {}).get("value", 1)
        nl = settings.get("MAX_NEW_LISTING_POSITIONS", {}).get("value", 1)
        auto_total = max(1, int(scalp) + int(swing) + int(nl))
        auto_pct = round(min(0.50, max(0.15, 1.0 / auto_total + 0.05)), 2)

        if "MAX_OPEN_POSITIONS" in settings:
            settings["MAX_OPEN_POSITIONS"]["value"] = auto_total
            settings["MAX_OPEN_POSITIONS"]["display_value"] = auto_total

        if "MAX_POSITION_PCT" in settings:
            settings["MAX_POSITION_PCT"]["value"] = auto_pct
            settings["MAX_POSITION_PCT"]["display_value"] = round(auto_pct * 100.0, 1)

        return {
            "success": True,
            "settings": settings,
        }

    def update_common_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """통합 공통 설정 검증, .env 영구 저장 및 활성 거래소 봇 핫 리로드 전파"""
        manager = CommonConfigManager()
        ok, normalized, errors = manager.update_settings(payload)
        if not ok:
            return {
                "success": False,
                "message": "설정 유효성 검증 실패",
                "errors": errors,
            }

        # 대시보드 프로세스 환경 변수 동기화
        for k, v in normalized.items():
            if isinstance(v, bool):
                os.environ[k] = "true" if v else "false"
            else:
                os.environ[k] = str(v)

        # 거래소 코어로 핫 리로드 전파
        broadcast_results = {}
        headers = {"X-Dashboard-Action-Token": self.action_token} if self.action_token else None

        for name, api_url in [("bithumb", self.bithumb_api_url), ("upbit", self.upbit_api_url)]:
            try:
                res = self.http_session.post(
                    f"{api_url}/api/config",
                    json=normalized,
                    timeout=2.5,
                    headers=headers,
                )
                if res.status_code == 200:
                    broadcast_results[name] = res.json()
                else:
                    broadcast_results[name] = {"success": False, "message": f"HTTP {res.status_code}"}
            except Exception as e:
                broadcast_results[name] = {"success": False, "message": f"연결 불가 (오프라인): {e}"}

        return {
            "success": True,
            "message": "공통 설정이 영구 저장되고 활성 봇에 즉시 반영되었습니다.",
            "applied": normalized,
            "broadcast": broadcast_results,
        }

    def get_cached_status(self) -> dict[str, Any]:
        """메모리에 캐시된 최신 통합 지표를 즉각 반환하며, 캐시가 비어있으면 즉시 조회"""
        with self._cache_lock:
            cached_comb = self._cached_status.get("combined", {}) if isinstance(self._cached_status, dict) else {}
            if cached_comb.get("bithumb_online") or cached_comb.get("upbit_online"):
                return self._cached_status

        # 캐시가 아직 없거나 오프라인 상태이면 즉시 수집
        data = self.get_aggregated_status()
        with self._cache_lock:
            self._cached_status = data
        return data

    def _poll_loop(self):
        """백그라운드에서 1.5초마다 거래소 상태를 폴링하여 캐시 유지"""
        while self._running:
            try:
                data = self.get_aggregated_status()
                with self._cache_lock:
                    self._cached_status = data
            except Exception as e:
                logger.debug(f"대시보드 캐시 갱신 예외: {e}")
            time.sleep(1.5)

    def start(self, block: bool = False):
        """웹 대시보드 서버 가동"""
        handler_cls = self._create_handler()
        self._running = True

        for attempt in range(5):
            try:
                QuietThreadingHTTPServer.allow_reuse_address = True
                self.server = QuietThreadingHTTPServer((self.host, self.port), handler_cls)
                display_host = self.host if self.host not in ("0.0.0.0", "") else "localhost"
                logger.info(f"🌐 [통합 퀀트 트레이딩 대시보드 가동] 접속 주소: http://{display_host}:{self.port}")
                logger.info(f"   • 빗썸 내부 API 연동: {self.bithumb_api_url}")
                logger.info(f"   • 업비트 내부 API 연동: {self.upbit_api_url}")

                # 백그라운드 캐시 폴링 워커 가동
                self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="DashboardPoller")
                self._poll_thread.start()

                if block:
                    self.server.serve_forever()
                else:
                    self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="UnifiedDashboard")
                    self._thread.start()
                return
            except OSError as e:
                if attempt < 4:
                    time.sleep(1.0)
                else:
                    logger.error(f"통합 대시보드 포트 {self.port} 바인딩 실패: {e}")

    def stop(self):
        """서버 안전 종료"""
        self._running = False
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
                logger.info("🌐 [통합 웹 대시보드 서버 종료 완료]")
            except Exception as e:
                logger.debug(f"대시보드 종료 예외: {e}")

    def _create_handler(self):
        server_self = self

        class DashboardHandler(BaseHTTPRequestHandler):
            def log_message(self, format_str, *args):
                pass

            def do_OPTIONS(self):
                self.close_connection = True
                try:
                    self.send_response(204)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Authorization, X-Dashboard-Action-Token")
                    self.send_header("Access-Control-Max-Age", "86400")
                    self.send_header("Connection", "close")
                    self.end_headers()
                except Exception:
                    pass

            def do_GET(self):
                self.close_connection = True
                try:
                    parsed_url = urllib.parse.urlparse(self.path)
                    path = parsed_url.path

                    # 1. API Status 반환 (즉각 캐시 응답)
                    if path == "/api/status":
                        data = server_self.get_cached_status()
                        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    # 2. WARNING 이상 운영 로그만 별도 조회한다.
                    if path == "/api/alerts":
                        target = urllib.parse.parse_qs(parsed_url.query).get("exchange", ["combined"])[0]
                        body = json.dumps(server_self.get_alert_logs(target), ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    # 2-1. 공통 설정 조회 API
                    if path == "/api/config":
                        body = json.dumps(server_self.get_common_config(), ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    # 3. 정적 SPA 파일 서빙
                    if server_self.static_dir:
                        rel_path = path.lstrip("/")
                        if not rel_path or rel_path == "index.html":
                            target_file = os.path.join(server_self.static_dir, "index.html")
                        elif rel_path in ("favicon.ico", "favicon.svg"):
                            target_file = os.path.join(server_self.static_dir, "favicon.svg")
                        else:
                            target_file = os.path.join(server_self.static_dir, rel_path)

                        norm_target = os.path.abspath(target_file)
                        norm_static = os.path.abspath(server_self.static_dir)
                        if norm_target.startswith(norm_static) and os.path.isfile(norm_target):
                            mime_type, _ = mimetypes.guess_type(norm_target)
                            if not mime_type or norm_target.endswith(".svg"):
                                if norm_target.endswith(".js"):
                                    mime_type = "text/javascript"
                                elif norm_target.endswith(".css"):
                                    mime_type = "text/css"
                                elif norm_target.endswith(".html"):
                                    mime_type = "text/html"
                                elif norm_target.endswith(".svg"):
                                    mime_type = "image/svg+xml"
                                else:
                                    mime_type = "application/octet-stream"
                            if "text/" in mime_type or mime_type in ("application/javascript", "application/json"):
                                mime_type += "; charset=utf-8"

                            with open(norm_target, "rb") as f:
                                body = f.read()

                            self.send_response(200)
                            self.send_header("Content-Type", mime_type)
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Content-Length", str(len(body)))
                            self.send_header("Connection", "close")
                            self.end_headers()
                            self.wfile.write(body)
                            return

                    # 3. 폴백: 통합 내장 HTML 렌더링
                    body = server_self._render_unified_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    logger.debug(f"대시보드 GET 예외: {e}")

            def do_POST(self):
                self.close_connection = True
                try:
                    parsed_url = urllib.parse.urlparse(self.path)
                    path = parsed_url.path
                    query = urllib.parse.parse_qs(parsed_url.query)

                    if path.startswith("/api/action/"):
                        if not server_self.is_action_authorized(self.headers.get("X-Dashboard-Action-Token", "")):
                            body = json.dumps({"success": False, "message": "원격 제어 인증이 필요합니다."}, ensure_ascii=False).encode("utf-8")
                            self.send_response(401)
                            self.send_header("Content-Type", "application/json; charset=utf-8")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Content-Length", str(len(body)))
                            self.send_header("Connection", "close")
                            self.end_headers()
                            self.wfile.write(body)
                            return
                        action_name = path.split("/")[-1]
                        exchange_target = query.get("exchange", ["all"])[0]

                        # Request body 지원
                        content_len = int(self.headers.get("Content-Length", 0))
                        if content_len > 0:
                            raw_body = self.rfile.read(content_len).decode("utf-8", errors="ignore")
                            try:
                                json_body = json.loads(raw_body)
                                if "exchange" in json_body:
                                    exchange_target = json_body["exchange"]
                            except Exception:
                                pass

                        res = server_self.forward_action(action_name, exchange_target)
                        body = json.dumps(res, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    # 공통 설정 갱신 엔드포인트 (.env 저장 및 양 거래소 봇 핫 리로드)
                    if path == "/api/config":
                        if not server_self.is_action_authorized(self.headers.get("X-Dashboard-Action-Token", "")):
                            body = json.dumps({"success": False, "message": "원격 제어 인증이 필요합니다."}, ensure_ascii=False).encode("utf-8")
                            self.send_response(401)
                            self.send_header("Content-Type", "application/json; charset=utf-8")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Content-Length", str(len(body)))
                            self.send_header("Connection", "close")
                            self.end_headers()
                            self.wfile.write(body)
                            return

                        content_len = int(self.headers.get("Content-Length", 0))
                        raw_body = self.rfile.read(content_len).decode("utf-8", errors="ignore") if content_len > 0 else "{}"
                        try:
                            payload = json.loads(raw_body)
                        except Exception:
                            payload = {}

                        res = server_self.update_common_config(payload)
                        status_code = 200 if res.get("success") else 400
                        body = json.dumps(res, ensure_ascii=False).encode("utf-8")
                        self.send_response(status_code)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    self.send_response(404)
                    self.end_headers()
                except Exception as e:
                    logger.debug(f"대시보드 POST 예외: {e}")

        return DashboardHandler

    def _render_unified_html(self) -> str:
        """반응형 모던 듀얼 거래소 통합 HTML 템플릿"""
        return """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bithumb & Upbit AI 퀀트 트레이딩 Pro</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #0b0e14; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .card { background-color: #151923; border: 1px solid #232a3b; border-radius: 12px; }
        .card-header { padding: 12px 16px; border-bottom: 1px solid #232a3b; font-weight: 600; font-size: 0.875rem; }
        .card-body { padding: 16px; font-size: 0.875rem; color: #cbd5e1; }
        .badge { padding: 3px 8px; border-radius: 6px; font-weight: bold; font-size: 0.75rem; white-space: nowrap; display: inline-block; }
        .badge-danger { background-color: rgba(225, 29, 72, 0.2); color: #f43f5e; border: 1px solid rgba(225, 29, 72, 0.4); }
        .badge-warning { background-color: rgba(234, 179, 8, 0.2); color: #eab308; border: 1px solid rgba(234, 179, 8, 0.4); }
        .badge-secondary { background-color: rgba(100, 116, 139, 0.2); color: #94a3b8; border: 1px solid rgba(100, 116, 139, 0.4); }
        .badge-success { background-color: rgba(16, 185, 129, 0.2); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.4); }
        .badge-info { background-color: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.4); }
        .tab-btn.active { background-color: #2563eb; color: #ffffff; border-color: #3b82f6; }
    </style>
</head>
<body class="p-4 sm:p-6">
    <div class="max-w-7xl mx-auto space-y-6">
        <!-- Header -->
        <div class="flex flex-wrap justify-between items-center bg-slate-900/90 p-5 rounded-2xl border border-slate-800 backdrop-blur shadow-xl">
            <div class="flex items-center space-x-3">
                <span class="text-3xl">🚀</span>
                <div>
                    <h1 class="text-2xl font-black bg-gradient-to-r from-blue-400 via-indigo-300 to-emerald-400 bg-clip-text text-transparent">
                        Bithumb & Upbit AI 퀀트 트레이딩 Pro
                    </h1>
                    <p class="text-xs text-slate-400">듀얼 거래소 독립 트레이딩 엔진 + 통합 실시간 관제 대시보드</p>
                </div>
            </div>
            <!-- Quick Actions -->
            <div class="flex flex-wrap gap-2 mt-4 sm:mt-0">
                <button onclick="openConfigModal()" class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 font-bold rounded-lg text-sm text-white shadow-lg transition">⚙️ 공통 설정</button>
                <button onclick="triggerAction('panic')" class="px-4 py-2 bg-rose-600 hover:bg-rose-700 font-bold rounded-lg text-sm text-white shadow-lg transition">🚨 긴급 전량 매도</button>
                <button onclick="triggerAction('pause')" class="px-4 py-2 bg-amber-600 hover:bg-amber-700 font-bold rounded-lg text-sm text-white shadow-lg transition">⏸️ 전체 일시정지</button>
                <button onclick="triggerAction('resume')" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 font-bold rounded-lg text-sm text-white shadow-lg transition">▶️ 전체 재개</button>
            </div>
        </div>

        <!-- Exchange Switcher Tabs -->
        <div class="flex space-x-2 border-b border-slate-800 pb-2">
            <button onclick="switchView('combined')" id="tab-combined" class="tab-btn active px-4 py-2 rounded-lg font-bold text-sm bg-slate-800 hover:bg-slate-700 transition inline-flex items-center gap-1.5">
                <svg class="w-4 h-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="10"/>
                    <line x1="2" y1="12" x2="22" y2="12"/>
                    <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
                </svg>
                <span>전체 통합 뷰</span>
            </button>
            <button onclick="switchView('bithumb')" id="tab-bithumb" class="tab-btn px-4 py-2 rounded-lg font-bold text-sm bg-slate-800 hover:bg-slate-700 transition inline-flex items-center gap-1.5">
                <svg class="w-4 h-4 shrink-0 text-amber-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="9" stroke="currentColor" fill="currentColor" fill-opacity="0.15"/>
                    <path d="M9.5 7.5h4a2.2 2.2 0 0 1 0 4.4H9.5v-4.4z" stroke="currentColor"/>
                    <path d="M9.5 11.9h4.5a2.3 2.3 0 0 1 0 4.6H9.5V11.9z" stroke="currentColor"/>
                    <line x1="9.5" y1="6" x2="9.5" y2="18" stroke="currentColor"/>
                </svg>
                <span>빗썸 (Bithumb)</span>
            </button>
            <button onclick="switchView('upbit')" id="tab-upbit" class="tab-btn px-4 py-2 rounded-lg font-bold text-sm bg-slate-800 hover:bg-slate-700 transition inline-flex items-center gap-1.5">
                <svg class="w-4 h-4 shrink-0 text-sky-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                    <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" fill="currentColor" fill-opacity="0.2"/>
                </svg>
                <span>업비트 (Upbit)</span>
            </button>
        </div>

        <!-- Macro Market Intelligence Card (Groq AI) -->
        <div class="card" id="market_intelligence_card" style="margin-bottom: 15px;">
          <div class="card-header" style="display:flex; justify-content:space-between; align-items:center;">
            <span>🌐 거시 시장 인텔리전스 (Groq AI)</span>
            <span id="mi_regime_badge" class="badge badge-secondary">대기 중</span>
          </div>
          <div class="card-body" style="display:flex; gap:20px; align-items:center; flex-wrap:wrap;">
            <div><strong>위험도 점수:</strong> <span id="mi_risk_score">-</span> / 100</div>
            <div><strong>권장 현금 비중:</strong> <span id="mi_cash_ratio">-%</span></div>
            <div style="flex:1; min-width:250px;">
              <strong>시장 요약:</strong> <span id="mi_summary" style="color:#aaa;">데이터 수신 대기 중</span>
            </div>
          </div>
        </div>

        <!-- Major Metric Cards -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div class="card p-5 shadow-md">
                <div class="text-xs text-slate-400 uppercase font-semibold">총 평가 자산</div>
                <div id="total_equity" class="text-2xl font-bold text-white mt-1">- 원</div>
                <div class="text-xs text-emerald-400 mt-2">가용 원화: <span id="krw_avail">-</span></div>
            </div>
            <div class="card p-5 shadow-md">
                <div class="text-xs text-slate-400 uppercase font-semibold">당일 실현 손익</div>
                <div id="daily_pnl" class="text-2xl font-bold text-slate-200 mt-1">- 원</div>
                <div class="text-xs text-slate-400 mt-2">수익률: <span id="daily_pnl_pct">-</span></div>
            </div>
            <div class="card p-5 shadow-md">
                <div class="text-xs text-slate-400 uppercase font-semibold">봇 가동 상태</div>
                <div class="flex items-center space-x-2 mt-1">
                    <span id="bithumb_badge" class="badge bg-slate-700 text-slate-300">빗썸 확인 중</span>
                    <span id="upbit_badge" class="badge bg-slate-700 text-slate-300">업비트 확인 중</span>
                </div>
                <div class="text-xs text-slate-400 mt-2">보유 종목: <span id="positions_count">0</span>개</div>
            </div>
            <div class="card p-5 shadow-md">
                <div class="text-xs text-slate-400 uppercase font-semibold">시장 공포 & 탐욕 지수</div>
                <div id="fng_desc" class="text-lg font-bold text-amber-400 mt-1">-</div>
                <div class="text-xs text-slate-400 mt-2">5분봉 자동매매 사이클 가동 중</div>
            </div>
        </div>

        <!-- Position Table Card -->
        <div class="card p-5 shadow-md space-y-4">
            <div class="flex justify-between items-center">
                <h2 class="text-lg font-bold text-white flex items-center gap-2">
                    <span>📊</span> 실시간 보유 포지션 및 0.1초 리스크 감시
                </h2>
                <span class="text-xs text-slate-400">자동 갱신: 3초</span>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm">
                    <thead class="bg-slate-800/60 text-slate-400 uppercase text-xs">
                        <tr>
                            <th class="p-3">거래소</th>
                            <th class="p-3">종목명 (마켓)</th>
                            <th class="p-3">보유 수량</th>
                            <th class="p-3">평단가</th>
                            <th class="p-3">현재가</th>
                            <th class="p-3">수익률</th>
                            <th class="p-3">목표가 (익절)</th>
                            <th class="p-3">손절가 (Stop)</th>
                            <th class="p-3">AI 알파 스코어</th>
                        </tr>
                    </thead>
                    <tbody id="position_table_body" class="divide-y divide-slate-800">
                        <tr>
                            <td colspan="9" class="p-6 text-center text-slate-500">현재 보유 중인 포지션이 없습니다.</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Daily Asset Performance History Card -->
        <div class="card p-5 shadow-md space-y-4">
            <div class="flex justify-between items-center">
                <h2 class="text-lg font-bold text-white flex items-center gap-2">
                    <span>📅</span> 일일 자산 변동 및 성과 이력 (Daily Performance History)
                </h2>
                <span class="text-xs text-slate-400">최근 14일 기록</span>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm">
                    <thead class="bg-slate-800/60 text-slate-400 uppercase text-xs">
                        <tr>
                            <th class="p-3">일자</th>
                            <th class="p-3">시작 자산</th>
                            <th class="p-3">실현 손익</th>
                            <th class="p-3">수익률</th>
                            <th class="p-3">체결 수 (승/총)</th>
                            <th class="p-3">승률</th>
                            <th class="p-3">리스크 상태</th>
                        </tr>
                    </thead>
                    <tbody id="daily_history_table_body" class="divide-y divide-slate-800">
                        <tr>
                            <td colspan="7" class="p-6 text-center text-slate-500">일일 성과 이력 데이터가 없습니다.</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Recent Orders Card -->
        <div class="card p-5 shadow-md space-y-4">
            <h2 class="text-lg font-bold text-white flex items-center gap-2">
                <span>📜</span> 최근 주문 저널 (Order Journal, 최근 24시간)
            </h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm">
                    <thead class="bg-slate-800/60 text-slate-400 uppercase text-xs">
                        <tr>
                            <th class="p-3">거래소</th>
                            <th class="p-3">일시</th>
                            <th class="p-3">종목</th>
                            <th class="p-3">구분</th>
                            <th class="p-3">가격</th>
                            <th class="p-3">수량</th>
                            <th class="p-3">상태</th>
                            <th class="p-3">슬리피지</th>
                        </tr>
                    </thead>
                    <tbody id="order_table_body" class="divide-y divide-slate-800">
                        <tr>
                            <td colspan="8" class="p-6 text-center text-slate-500">최근 24시간 내 주문 내역이 없습니다.</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        let currentView = 'combined';
        let latestData = null;

        function switchView(view) {
            currentView = view;
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            const activeTab = document.getElementById('tab-' + view);
            if (activeTab) activeTab.classList.add('active');
            render();
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                if (res.ok) {
                    latestData = await res.json();
                    render();
                }
            } catch (e) {
                console.error('Fetch error:', e);
            }
        }

        function formatKrw(val) {
            return Math.round(val || 0).toLocaleString('ko-KR') + ' 원';
        }

        function formatPct(val) {
            const num = Number(val || 0);
            const sign = num > 0 ? '+' : '';
            return `${sign}${num.toFixed(2)}%`;
        }

        function updateUI(data) {
            if (!data) return;
            const mi = data.market_intelligence || (currentView === 'bithumb' ? (data.bithumb && data.bithumb.market_intelligence) : (currentView === 'upbit' ? (data.upbit && data.upbit.market_intelligence) : (data.combined && data.combined.market_intelligence))) || {};
            const badgeEl = document.getElementById('mi_regime_badge');
            const scoreEl = document.getElementById('mi_risk_score');
            const cashEl = document.getElementById('mi_cash_ratio');
            const summaryEl = document.getElementById('mi_summary');

            if (badgeEl && mi.regime) {
                const regimeMap = {
                    'CRASH': '🚨 급락 위기',
                    'BEAR_REGIME': '🔴 하락 추세 (약세장)',
                    'CAUTION_PULLBACK': '🟡 단기 조정 경계',
                    'BULL_TREND': '🟢 강세 상승장',
                    'NORMAL': '🔵 정상 안정세',
                };
                badgeEl.textContent = regimeMap[String(mi.regime).toUpperCase()] || mi.regime;
                // 레짐별 뱃지 색상 클래스 적용
                badgeEl.className = 'badge ' + (
                    mi.regime === 'CRASH' ? 'badge-danger' :
                    mi.regime === 'BEAR_REGIME' ? 'badge-warning' :
                    mi.regime === 'CAUTION_PULLBACK' ? 'badge-secondary' :
                    mi.regime === 'BULL_TREND' ? 'badge-success' : 'badge-info'
                );
                if (scoreEl) scoreEl.textContent = mi.risk_score != null ? mi.risk_score : '-';
                if (cashEl) cashEl.textContent = mi.recommended_cash_ratio != null ? (mi.recommended_cash_ratio * 100).toFixed(0) + '%' : '-%';
                if (summaryEl) summaryEl.textContent = mi.market_summary || '특이사항 없음';
            } else if (badgeEl) {
                badgeEl.textContent = '대기 중';
                badgeEl.className = 'badge badge-secondary';
                if (scoreEl) scoreEl.textContent = '-';
                if (cashEl) cashEl.textContent = '-%';
                if (summaryEl) summaryEl.textContent = '데이터 수신 대기 중';
            }
        }

        function render() {
            if (!latestData) return;
            updateUI(latestData);

            const combined = latestData.combined || {};
            const bithumb = latestData.bithumb || {};
            const upbit = latestData.upbit || {};

            let target = combined;
            if (currentView === 'bithumb') target = bithumb;
            if (currentView === 'upbit') target = upbit;

            // 1. Cards
            document.getElementById('total_equity').textContent = formatKrw(target.total_equity);
            document.getElementById('krw_avail').textContent = formatKrw(target.krw_available);
            
            const pnl = target.realized_pnl_krw || 0;
            const pnlEl = document.getElementById('daily_pnl');
            pnlEl.textContent = (pnl >= 0 ? '+' : '') + formatKrw(pnl);
            pnlEl.className = 'text-2xl font-bold mt-1 ' + (pnl > 0 ? 'text-emerald-400' : (pnl < 0 ? 'text-rose-400' : 'text-slate-200'));

            const pnlPct = target.daily_pnl_pct ? (target.daily_pnl_pct * 100) : 0;
            document.getElementById('daily_pnl_pct').textContent = formatPct(pnlPct);

            // Badges
            const btBadge = document.getElementById('bithumb_badge');
            if (bithumb.online) {
                btBadge.textContent = '🟡 빗썸 정상 가동';
                btBadge.className = 'badge bg-emerald-900/80 text-emerald-300 border border-emerald-700';
            } else {
                btBadge.textContent = '🟡 빗썸 오프라인';
                btBadge.className = 'badge bg-rose-900/80 text-rose-300 border border-rose-700';
            }

            const upBadge = document.getElementById('upbit_badge');
            if (upbit.online) {
                upBadge.textContent = '🔵 업비트 정상 가동';
                upBadge.className = 'badge bg-emerald-900/80 text-emerald-300 border border-emerald-700';
            } else {
                upBadge.textContent = '🔵 업비트 오프라인';
                upBadge.className = 'badge bg-rose-900/80 text-rose-300 border border-rose-700';
            }

            const positions = target.positions || [];
            document.getElementById('positions_count').textContent = positions.length;

            const fng = target.fear_and_greed || combined.fear_and_greed || {};
            document.getElementById('fng_desc').textContent = fng.desc || '50점 (중립)';

            // 2. Positions Table
            const tbody = document.getElementById('position_table_body');
            if (positions.length === 0) {
                tbody.innerHTML = '<tr><td colspan="9" class="p-6 text-center text-slate-500">현재 보유 중인 포지션이 없습니다.</td></tr>';
            } else {
                tbody.innerHTML = positions.map(p => {
                    const pnlPctVal = p.pnl_pct || 0;
                    const pnlClass = pnlPctVal > 0 ? 'text-emerald-400 font-bold' : (pnlPctVal < 0 ? 'text-rose-400 font-bold' : 'text-slate-300');
                    const exLabel = p.exchange === 'upbit' ? '<span class="badge bg-blue-900 text-blue-300">업비트</span>' : '<span class="badge bg-amber-900 text-amber-300">빗썸</span>';
                    return `
                        <tr class="hover:bg-slate-800/40 transition">
                            <td class="p-3">${exLabel}</td>
                            <td class="p-3 font-semibold text-white">${p.korean_name || ''} <span class="text-xs text-slate-400">(${p.market})</span></td>
                            <td class="p-3">${Number(p.balance || 0).toFixed(4)}</td>
                            <td class="p-3">${formatKrw(p.avg_buy_price)}</td>
                            <td class="p-3 font-bold">${formatKrw(p.current_price)}</td>
                            <td class="p-3 ${pnlClass}">${formatPct(pnlPctVal)}</td>
                            <td class="p-3 text-emerald-300">${formatKrw(p.target_price)}</td>
                            <td class="p-3 text-rose-300">${formatKrw(p.stop_loss)}</td>
                            <td class="p-3 font-mono font-bold text-indigo-300">${p.alpha_score || '-'}점</td>
                        </tr>
                    `;
                }).join('');
            }

            // 2.5 Daily Stats History Table
            const dailyHistory = target.daily_stats_history || [];
            const dhTbody = document.getElementById('daily_history_table_body');
            if (dhTbody) {
                if (dailyHistory.length === 0) {
                    dhTbody.innerHTML = '<tr><td colspan="7" class="p-6 text-center text-slate-500">일일 성과 이력 데이터가 없습니다.</td></tr>';
                } else {
                    dhTbody.innerHTML = dailyHistory.map(item => {
                        const pnl = item.realized_pnl_krw || 0;
                        const pnlClass = pnl > 0 ? 'text-emerald-400 font-bold' : (pnl < 0 ? 'text-rose-400 font-bold' : 'text-slate-300');
                        const pnlSign = pnl > 0 ? '+' : '';
                        const pnlPct = item.pnl_pct || 0;
                        const pnlPctClass = pnlPct > 0 ? 'text-emerald-400' : (pnlPct < 0 ? 'text-rose-400' : 'text-slate-400');
                        const isKill = Boolean(item.kill_switch_active);
                        const statusBadge = isKill
                            ? '<span class="badge bg-rose-900/80 text-rose-300 border border-rose-700">🛑 킬스위치</span>'
                            : (item.total_trades > 0
                                ? '<span class="badge bg-emerald-900/80 text-emerald-300 border border-emerald-700">🟢 정상 운용</span>'
                                : '<span class="badge bg-slate-800 text-slate-400 border border-slate-700">⚪ 대기</span>');
                        return `
                            <tr class="hover:bg-slate-800/30">
                                <td class="p-3 font-mono text-slate-200 font-semibold">${item.date || '-'}</td>
                                <td class="p-3 text-slate-300">${formatKrw(item.start_equity)}</td>
                                <td class="p-3 ${pnlClass}">${pnlSign}${formatKrw(pnl)}</td>
                                <td class="p-3 font-mono ${pnlPctClass}">${formatPct(pnlPct)}</td>
                                <td class="p-3 text-slate-300"><span class="text-emerald-400 font-semibold">${item.win_trades || 0}</span> / ${item.total_trades || 0} 회</td>
                                <td class="p-3 font-mono text-slate-300">${(item.total_trades > 0) ? (item.win_rate || 0).toFixed(1) + '%' : '-'}</td>
                                <td class="p-3">${statusBadge}</td>
                            </tr>
                        `;
                    }).join('');
                }
            }

            // 3. Orders Table (최근 24시간 필터링)
            const otbody = document.getElementById('order_table_body');
            const rawOrders = target.recent_orders || [];
            const nowMs = Date.now();
            const orders = rawOrders.filter(o => {
                const ts = o.timestamp || o.created_at || o.updated_at;
                if (!ts) return false;
                let ms;
                if (typeof ts === 'number' || (!isNaN(Number(ts)) && !String(ts).includes('-') && !String(ts).includes(':'))) {
                    const num = Number(ts);
                    ms = (num > 1e11 ? num : num * 1000);
                } else {
                    ms = new Date(String(ts).replace(' ', 'T')).getTime();
                }
                if (isNaN(ms)) return false;
                const diff = nowMs - ms;
                return diff >= -60000 && diff <= 24 * 3600 * 1000;
            });
            if (orders.length === 0) {
                otbody.innerHTML = '<tr><td colspan="8" class="p-6 text-center text-slate-500">최근 24시간 내 주문 내역이 없습니다.</td></tr>';
            } else {
                otbody.innerHTML = orders.slice(0, 15).map(o => {
                    const isBuy = (o.side || '').toLowerCase().includes('bid') || (o.side || '').includes('매수');
                    const sideBadge = isBuy ? '<span class="badge bg-emerald-900 text-emerald-300">매수</span>' : '<span class="badge bg-rose-900 text-rose-300">매도</span>';
                    const exBadge = o.exchange === 'upbit' ? '<span class="badge bg-blue-900/60 text-blue-300">업비트</span>' : '<span class="badge bg-amber-900/60 text-amber-300">빗썸</span>';
                    const statusMap = {
                        FILLED: '체결 완료',
                        DONE: '체결 완료',
                        OPEN: '미체결 대기',
                        WAIT: '미체결 대기',
                        PENDING: '접수 대기',
                        SUBMITTED: '접수 대기',
                        CANCELLED: '취소 완료',
                        CANCELED: '취소 완료',
                        CANCEL: '취소 완료',
                        REJECTED: '주문 거절',
                        FAILED: '주문 실패',
                        FAIL: '주문 실패',
                        ERROR: '주문 실패',
                        PARTIALLY_FILLED: '부분 체결',
                        RECONCILIATION_PENDING: '체결 대사중',
                        RECONCILED: '대사 완료',
                        UNKNOWN: '확인 필요'
                    };
                    const rawStatus = String(o.status || '').toUpperCase();
                    const statusKor = statusMap[rawStatus] || o.status || '완료';
                    return `
                        <tr class="hover:bg-slate-800/40 transition text-xs">
                            <td class="p-3">${exBadge}</td>
                            <td class="p-3 text-slate-400">${(o.created_at || o.timestamp || '-').substring(5, 19)}</td>
                            <td class="p-3 font-medium text-white">${o.korean_name || o.market}</td>
                            <td class="p-3">${sideBadge}</td>
                            <td class="p-3">${formatKrw(o.price || o.avg_price)}</td>
                            <td class="p-3">${Number(o.volume || o.executed_volume || 0).toFixed(4)}</td>
                            <td class="p-3 font-semibold text-slate-300">${statusKor}</td>
                            <td class="p-3 font-mono text-slate-400">${o.slippage_bps ? (o.slippage_bps + ' bps') : '-'}</td>
                        </tr>
                    `;
                }).join('');
            }
        }

        async function triggerAction(action) {
            const target = currentView === 'combined' ? 'all' : currentView;
            if (!confirm(`[${action.toUpperCase()}] 명령을 ${target.toUpperCase()} 봇에 전송하시겠습니까?`)) return;
            try {
                const res = await fetch(`/api/action/${action}?exchange=${target}`, { method: 'POST' });
                const json = await res.json();
                alert(json.message || '명령 전달 완료');
                fetchStatus();
            } catch (e) {
                alert('명령 전송 실패: ' + e);
            }
        }

        fetchStatus();
        setInterval(fetchStatus, 3000);

        // ----------------------------------------------------
        // 공통 설정 모달 (Hot-Reload) 제어 로직
        // ----------------------------------------------------
        let cachedUnifiedConfig = null;

        async function openConfigModal() {
            const modal = document.getElementById('config-modal');
            if (!modal) return;
            modal.classList.remove('hidden');
            switchConfigTab('risk');
            const banner = document.getElementById('cfg-status-banner');
            if (banner) { banner.classList.add('hidden'); banner.innerText = ''; }

            try {
                const res = await fetch('/api/config');
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();
                if (data && data.settings) {
                    cachedUnifiedConfig = data.settings;
                    for (const [k, item] of Object.entries(data.settings)) {
                        const el = document.getElementById('cfg_' + k);
                        if (!el) continue;
                        if (item.type === 'bool') {
                            el.checked = Boolean(item.value);
                        } else if (item.type === 'percent') {
                            el.value = item.display_value !== undefined ? item.display_value : (item.value * 100);
                        } else {
                            el.value = item.value;
                        }
                    }
                }
            } catch (err) {
                showConfigBanner('설정 로드 실패: ' + err.message, 'error');
            }
        }

        function closeConfigModal() {
            const modal = document.getElementById('config-modal');
            if (modal) modal.classList.add('hidden');
        }

        function switchConfigTab(tabName) {
            ['risk', 'screening', 'portfolio', 'ai'].forEach(t => {
                const btn = document.getElementById('cfg-tab-' + t);
                const panel = document.getElementById('cfg-panel-' + t);
                if (t === tabName) {
                    if (btn) btn.className = 'cfg-tab-btn active px-3.5 py-2 rounded-t-lg text-xs font-bold transition border-b-2 border-indigo-500 text-indigo-400 bg-slate-800/60';
                    if (panel) panel.classList.remove('hidden');
                } else {
                    if (btn) btn.className = 'cfg-tab-btn px-3.5 py-2 rounded-t-lg text-xs font-bold transition border-b-2 border-transparent text-slate-400 hover:text-slate-200';
                    if (panel) panel.classList.add('hidden');
                }
            });
        }

        function resetConfigDefaults() {
            if (!cachedUnifiedConfig) return;
            for (const [k, item] of Object.entries(cachedUnifiedConfig)) {
                const el = document.getElementById('cfg_' + k);
                if (!el) continue;
                if (item.type === 'bool') {
                    el.checked = Boolean(item.default);
                } else {
                    el.value = item.default;
                }
            }
            updateSlotsBreakdownDisplay();
            showConfigBanner('기본값으로 복원되었습니다. 적용하려면 [저장 및 즉시 적용]을 누르세요.', 'info');
        }

        function showConfigBanner(msg, type) {
            const banner = document.getElementById('cfg-status-banner');
            if (!banner) return;
            banner.classList.remove('hidden');
            banner.innerText = msg;
            if (type === 'error') {
                banner.className = 'p-3 rounded-xl text-xs font-medium border bg-rose-500/20 text-rose-300 border-rose-500/40';
            } else if (type === 'success') {
                banner.className = 'p-3 rounded-xl text-xs font-medium border bg-emerald-500/20 text-emerald-300 border-emerald-500/40';
            } else {
                banner.className = 'p-3 rounded-xl text-xs font-medium border bg-blue-500/20 text-blue-300 border-blue-500/40';
            }
        }

        async function saveAndApplyConfig() {
            const saveBtn = document.getElementById('cfg-save-btn');
            if (!cachedUnifiedConfig) return;

            const payload = {};
            for (const [k, item] of Object.entries(cachedUnifiedConfig)) {
                const el = document.getElementById('cfg_' + k);
                if (!el) continue;
                if (item.type === 'bool') payload[k] = el.checked;
                else if (item.type === 'int') payload[k] = parseInt(el.value, 10);
                else if (item.type === 'krw') payload[k] = parseFloat(el.value);
                else if (item.type === 'percent') payload[k] = parseFloat(el.value) / 100.0;
                else payload[k] = el.value;
            }

            const dynEl = document.getElementById('cfg_DYNAMIC_SLOTS_ENABLED');
            if (dynEl && payload['DYNAMIC_SLOTS_ENABLED'] === undefined) {
                payload['DYNAMIC_SLOTS_ENABLED'] = dynEl.checked;
            }

            if (saveBtn) {
                saveBtn.disabled = true;
                saveBtn.innerText = '저장 중...';
            }

            try {
                const token = sessionStorage.getItem('dashboardActionToken') || '';
                const headers = { 'Content-Type': 'application/json' };
                if (token) headers['X-Dashboard-Action-Token'] = token;

                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers,
                    body: JSON.stringify(payload)
                });

                if (res.status === 401) {
                    const suppliedToken = prompt('원격 제어 토큰을 입력하세요:');
                    if (!suppliedToken) throw new Error('원격 제어 인증이 필요합니다.');
                    sessionStorage.setItem('dashboardActionToken', suppliedToken);
                    showConfigBanner('토큰이 저장되었습니다. 다시 시도하세요.', 'info');
                    return;
                }

                const json = await res.json();
                if (!res.ok || !json.success) {
                    const errMsg = (json.errors && json.errors.length) ? json.errors.join(', ') : (json.message || '저장 실패');
                    throw new Error(errMsg);
                }

                showConfigBanner('✅ 공통 설정이 .env에 저장되고 실행 중인 봇에 무중단 반영되었습니다!', 'success');
                saveSlotsToLocalStorage();
                setTimeout(() => { closeConfigModal(); fetchStatus(); }, 1200);
            } catch (err) {
                showConfigBanner('❌ 저장 실패: ' + err.message, 'error');
            } finally {
                if (saveBtn) {
                    saveBtn.disabled = false;
                    saveBtn.innerText = '💾 저장 및 즉시 적용';
                }
            }
        }
    </script>

    <!-- Common Config Modal Markup -->
    <div id="config-modal" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
        <div class="bg-slate-900 border border-slate-700/80 rounded-2xl w-full max-w-2xl shadow-2xl overflow-hidden max-h-[90vh] flex flex-col">
            <div class="p-5 border-b border-slate-800 flex justify-between items-center bg-slate-950/50">
                <div>
                    <h3 class="text-base font-bold text-slate-100 flex items-center gap-2">
                        <span>⚙️ 공통 트레이딩 & 리스크 설정</span>
                        <span class="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">Hot-Reload</span>
                    </h3>
                    <p class="text-xs text-slate-400">변경 즉시 .env에 저장되고 빗썸/업비트 봇 코어에 무중단 적용됩니다.</p>
                </div>
                <button onclick="closeConfigModal()" class="text-slate-400 hover:text-white p-1 rounded">✕</button>
            </div>
            <div class="flex border-b border-slate-800 px-5 pt-3 bg-slate-950/30 gap-2 overflow-x-auto">
                <button onclick="switchConfigTab('risk')" id="cfg-tab-risk" class="cfg-tab-btn active px-3.5 py-2 rounded-t-lg text-xs font-bold border-b-2 border-indigo-500 text-indigo-400 bg-slate-800/60">🛡️ 리스크 & 손익</button>
                <button onclick="switchConfigTab('screening')" id="cfg-tab-screening" class="cfg-tab-btn px-3.5 py-2 rounded-t-lg text-xs font-bold text-slate-400 hover:text-slate-200">🔍 스크리닝 & 전략</button>
                <button onclick="switchConfigTab('portfolio')" id="cfg-tab-portfolio" class="cfg-tab-btn px-3.5 py-2 rounded-t-lg text-xs font-bold text-slate-400 hover:text-slate-200">💼 포트폴리오 한도</button>
                <button onclick="switchConfigTab('ai')" id="cfg-tab-ai" class="cfg-tab-btn px-3.5 py-2 rounded-t-lg text-xs font-bold text-slate-400 hover:text-slate-200">🤖 AI 자율권 & 캐시</button>
            </div>
            <div class="p-5 overflow-y-auto flex-1 space-y-4">
                <div id="cfg-status-banner" class="hidden p-3 rounded-xl text-xs font-medium border"></div>
                <div id="cfg-panel-risk" class="cfg-panel space-y-4">
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">트레일링 스탑 시작 수익률 (%)</label>
                            <input type="number" id="cfg_TRAILING_START_PCT" step="0.1" min="0.5" max="20" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">트레일링 청산 하락폭 (%)</label>
                            <input type="number" id="cfg_TRAILING_STOP_PCT" step="0.1" min="0.5" max="10" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">일일 누적 손실 한도 (%)</label>
                            <input type="number" id="cfg_MAX_DAILY_LOSS_PCT" step="0.5" min="1" max="20" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">BTC 급락 감지 임계치 (%)</label>
                            <input type="number" id="cfg_BTC_CRASH_THRESHOLD_PCT" step="0.1" min="0.5" max="10" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                    </div>
                    <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                        <label class="flex items-center justify-between cursor-pointer text-xs font-semibold">
                            <span>호가 슬리피지 강제 차단 집행</span>
                            <input type="checkbox" id="cfg_ORDERBOOK_SLIPPAGE_ENFORCEMENT" class="accent-indigo-500">
                        </label>
                    </div>
                </div>
                <div id="cfg-panel-screening" class="cfg-panel hidden space-y-4">
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800 sm:col-span-2">
                            <div class="flex items-center justify-between mb-1">
                                <label class="text-xs font-semibold">사이클당 최대 분석 종목 수 (상한 캡)</label>
                                <span class="text-[10px] text-indigo-400 bg-indigo-950/60 px-2 py-0.5 rounded border border-indigo-800/50">0 = 제한 없음 (전수 분석)</span>
                            </div>
                            <input type="number" id="cfg_MAX_CYCLE_MARKETS" step="1" min="0" max="30" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                            <p class="text-[11px] text-slate-400 mt-1">보유 종목은 최우선 보장되며, 남은 슬롯을 스크리너 상위 종목으로 채웁니다. (권장: 10개)</p>
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">스크리너 1차 추출 후보 수 (개)</label>
                            <input type="number" id="cfg_TOP_COUNT" step="1" min="1" max="10" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">24시간 최소 거래대금 (원)</label>
                            <input type="number" id="cfg_MIN_TRADE_VALUE" step="100000000" min="100000000" max="100000000000" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                            <div id="cfg_MIN_TRADE_VALUE_display" class="text-[11px] text-indigo-300 font-medium mt-1"></div>
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">후보 최소 등락률 (%)</label>
                            <input type="number" id="cfg_MIN_CHANGE_RATE" step="0.1" min="0" max="10" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">후보 최대 등락률 (%)</label>
                            <input type="number" id="cfg_MAX_CHANGE_RATE" step="1" min="5" max="100" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                    </div>
                    <div class="space-y-2">
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="flex items-center justify-between cursor-pointer text-xs font-semibold">
                                <span>확정봉 모멘텀 돌파 경로 활성화</span>
                                <input type="checkbox" id="cfg_MOMENTUM_BREAKOUT_ENABLED" class="accent-indigo-500">
                            </label>
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="flex items-center justify-between cursor-pointer text-xs font-semibold">
                                <span>신규 상장 코인 추적 활성화</span>
                                <input type="checkbox" id="cfg_NEW_LISTING_ENABLED" class="accent-indigo-500">
                            </label>
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="flex items-center justify-between cursor-pointer text-xs font-semibold">
                                <span>신규 상장 코인 실주문 집행 (Enforcement)</span>
                                <input type="checkbox" id="cfg_NEW_LISTING_ENFORCEMENT" class="accent-indigo-500">
                            </label>
                        </div>
                    </div>
                </div>
                <div id="cfg-panel-portfolio" class="cfg-panel hidden space-y-4">
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <!-- 스마트 동적 슬롯 상태 카드 -->
                        <div class="bg-gradient-to-r from-emerald-950/40 via-teal-950/30 to-indigo-950/40 p-4 rounded-xl border border-emerald-500/40 sm:col-span-2 space-y-2.5">
                            <div class="flex items-center justify-between">
                                <div class="flex items-center gap-2">
                                    <span class="text-sm">🌊</span>
                                    <span class="text-xs font-bold text-emerald-300">스마트 동적 슬롯 시스템 (Dynamic Slots)</span>
                                </div>
                                <span class="text-[11px] font-semibold text-emerald-300 bg-emerald-900/70 px-2.5 py-0.5 rounded-full border border-emerald-500/30">자동 유동 배분 가동 중</span>
                            </div>
                            <p class="text-[11px] text-slate-300 leading-relaxed">
                                슬롯 수를 숫자로 고정하지 않고, 비트코인 시장 국면(레짐)과 가용 자본에 맞춰 슬롯과 스윙 비중을 유동적으로 자동 배분합니다.
                            </p>
                            <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-1 text-[11px]">
                                <div class="bg-slate-900/80 p-2 rounded-lg border border-slate-800">
                                    <div class="text-slate-400 text-[10px]">🚀 강세장 (BULL)</div>
                                    <div class="text-emerald-400 font-bold">노출 85% · 스윙 자율</div>
                                </div>
                                <div class="bg-slate-900/80 p-2 rounded-lg border border-slate-800">
                                    <div class="text-slate-400 text-[10px]">⚖️ 횡보장 (NORMAL)</div>
                                    <div class="text-indigo-300 font-bold">노출 55% · 스윙 1개</div>
                                </div>
                                <div class="bg-slate-900/80 p-2 rounded-lg border border-slate-800">
                                    <div class="text-slate-400 text-[10px]">🛡️ 약세장 (RISK_OFF)</div>
                                    <div class="text-amber-400 font-bold">노출 20% · 스윙 차단</div>
                                </div>
                                <div class="bg-slate-900/80 p-2 rounded-lg border border-slate-800">
                                    <div class="text-slate-400 text-[10px]">🛑 안전 상한선</div>
                                    <div class="text-slate-300 font-bold">최대 8종목 캡</div>
                                </div>
                            </div>
                        </div>

                        <!-- 자금 및 리스크 핵심 설정 -->
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">총 익스포저 최대 비중 (%)</label>
                            <input type="number" id="cfg_MAX_TOTAL_EXPOSURE_PCT" step="5" min="10" max="100" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">단일 포지션 최대 비중 (%)</label>
                            <input type="number" id="cfg_MAX_POSITION_PCT" step="1" min="5" max="50" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">🪙 알트코인 단일 최대 비중 (%)</label>
                            <input type="number" id="cfg_MAX_ALT_ALLOC_PCT" step="1" min="5" max="50" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="text-xs font-semibold block mb-1">🪙 알트코인 단일 최소 비중 (%)</label>
                            <input type="number" id="cfg_MIN_ALT_ALLOC_PCT" step="1" min="5" max="30" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800 sm:col-span-2">
                            <label class="text-xs font-semibold block mb-1">건당 최대 주문 한도 (원, 0은 무제한)</label>
                            <input type="number" id="cfg_MAX_ORDER_KRW" step="10000" min="0" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800 sm:col-span-2">
                            <label class="text-xs font-semibold block mb-1">단일 주문 최대 금액 (원)</label>
                            <input type="number" id="cfg_MAX_ORDER_KRW" step="1000000" min="100000" max="500000000" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                        </div>
                    </div>
                </div>
                <div id="cfg-panel-ai" class="cfg-panel hidden space-y-4">
                    <div class="bg-gradient-to-r from-purple-950/40 via-indigo-950/30 to-blue-950/40 p-4 rounded-xl border border-purple-500/40 space-y-2">
                        <div class="flex items-center justify-between">
                            <div class="flex items-center gap-2">
                                <span class="text-sm">🧠</span>
                                <span class="text-xs font-bold text-purple-300">Gemini AI 자율 분석 및 쿼터 제어</span>
                            </div>
                            <span class="text-[10px] text-purple-300 bg-purple-900/60 px-2 py-0.5 rounded border border-purple-500/30">Hot-Reload</span>
                        </div>
                        <p class="text-[11px] text-slate-300 leading-relaxed">
                            AI에게 로컬 퀀트 규칙을 넘어선 단독 매수 승인 자율권을 부여하고, 실시간 분석 빈도(캐시 주기)를 정밀 제어합니다.
                        </p>
                    </div>
                    <div class="space-y-3">
                        <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                            <label class="flex items-center justify-between cursor-pointer text-xs font-semibold">
                                <div>
                                    <span class="text-slate-100">AI 단독 자율 진입 허용 (ENABLE_AI_DIRECT_ENTRY)</span>
                                    <p class="text-[11px] text-slate-400 font-normal mt-0.5">로컬 퀀트가 관망이더라도, Gemini가 차트·수급 심층 분석으로 매수를 승인하면 자율 진입합니다.</p>
                                </div>
                                <input type="checkbox" id="cfg_ENABLE_AI_DIRECT_ENTRY" class="accent-indigo-500 w-4 h-4 ml-3">
                            </label>
                        </div>
                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                            <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                                <label class="text-xs font-semibold block mb-1">AI 분석 요청 최소 알파 점수 (점)</label>
                                <input type="number" id="cfg_AI_DIRECT_ENTRY_MIN_ALPHA" step="1" min="50" max="100" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                                <p class="text-[11px] text-slate-400 mt-1">로컬 알파 점수가 이 기준 이상인 후보만 AI에게 질의합니다. (낮출수록 AI 호출 증가, 권장: 55점)</p>
                            </div>
                            <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                                <label class="text-xs font-semibold block mb-1">AI 진입 판단 캐시 주기 (초)</label>
                                <input type="number" id="cfg_GEMINI_ENTRY_CACHE_SEC" step="60" min="60" max="3600" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                                <p class="text-[11px] text-slate-400 mt-1">안정 구간(HOLD/가격변동 1.5% 미만)에서 이전 분석 결과를 유지하는 시간입니다. (권장: 300~900초)</p>
                            </div>
                            <div class="bg-slate-950/60 p-3 rounded-xl border border-slate-800 sm:col-span-2">
                                <label class="text-xs font-semibold block mb-1">스크리너 AI 랭킹 캐시 주기 (초)</label>
                                <input type="number" id="cfg_GEMINI_RANK_CACHE_SEC" step="60" min="300" max="7200" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-slate-100">
                                <p class="text-[11px] text-slate-400 mt-1">전체 마켓 스크리닝 시 AI 유망 순위 캐시를 유지하는 시간입니다. (권장: 900~3600초)</p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="p-4 border-t border-slate-800 bg-slate-950/50 flex justify-between items-center">
                <button onclick="resetConfigDefaults()" class="px-3 py-1.5 rounded text-xs text-slate-400 hover:text-white">기본값 불러오기</button>
                <div class="flex gap-2">
                    <button onclick="closeConfigModal()" class="px-4 py-1.5 rounded text-xs bg-slate-800 text-slate-300">닫기</button>
                    <button id="cfg-save-btn" onclick="saveAndApplyConfig()" class="px-4 py-1.5 rounded text-xs font-bold text-white bg-indigo-600 hover:bg-indigo-700">💾 저장 및 즉시 적용</button>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""


def main():
    load_dotenv(os.path.join(project_root, ".env"))

    parser = argparse.ArgumentParser(description="Unified Quant Trading Dashboard Server")
    parser.add_argument("--host", type=str, default=os.getenv("DASHBOARD_HOST", "100.76.22.126"), help="대시보드 바인딩 호스트 IP (기본: 100.76.22.126)")
    parser.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "7979")), help="Dashboard Port (default: 7979)")
    parser.add_argument("--bithumb-url", type=str, default=os.getenv("BITHUMB_API_URL", "http://127.0.0.1:17979"), help="Bithumb Internal API URL")
    parser.add_argument("--upbit-url", type=str, default=os.getenv("UPBIT_API_URL", "http://127.0.0.1:17980"), help="Upbit Internal API URL")
    args = parser.parse_args()

    pid_file = os.path.join(project_root, "data", ".dashboard.pid.json")
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    try:
        with open(pid_file, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "exchange": "dashboard", "created_at": time.time()}, f)
    except Exception:
        pass

    display_host = args.host if args.host not in ("0.0.0.0", "") else "localhost"
    logger.info("======================================================")
    logger.info("  통합 퀀트 트레이딩 대시보드 게이트웨이 서버 가동")
    logger.info("  접속 URL: http://%s:%d", display_host, args.port)
    logger.info("======================================================")

    server = UnifiedDashboardServer(
        port=args.port,
        host=args.host,
        bithumb_api_url=args.bithumb_url,
        upbit_api_url=args.upbit_url,
    )
    server.start(block=False)

    try:
        while server._running:
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("👋 대시보드 서버 종료 신호를 수신했습니다.")
    finally:
        server.stop()
        if os.path.exists(pid_file):
            try:
                os.remove(pid_file)
            except OSError:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            with open(os.path.join(project_root, "logs", "dashboard_crash.log"), "w", encoding="utf-8") as f:
                import traceback
                traceback.print_exc(file=f)
        except Exception:
            pass
