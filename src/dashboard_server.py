"""
통합 퀀트 트레이딩 대시보드 게이트웨이 서버 (v5.0)
- 빗썸(127.0.0.1:17979) 및 업비트(127.0.0.1:17980) 트레이딩 코어와 통신
- 단일 포트(기본 7979)에서 통합 자산/포지션 및 거래소별 개별 모니터링 제공
- 트레이딩 엔진과 UI 프로세스의 완전한 물리적/논리적 분리 보장
- 트레이딩 코어가 오프라인이어도 무중단 안전 서빙 및 상태 표시
"""

import argparse
import json
import logging
import mimetypes
import os
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import requests
from dotenv import load_dotenv

# UTF-8 표준 출력 보장
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, AttributeError):
        pass

# 로깅 설정 (logs/dashboard.log)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
        port: int = 7979,
        host: str = "0.0.0.0",
        bithumb_api_url: str = "http://127.0.0.1:17979",
        upbit_api_url: str = "http://127.0.0.1:17980",
        static_dir: str | None = None,
    ):
        self.port = port
        self.host = host
        self.bithumb_api_url = bithumb_api_url.rstrip("/")
        self.upbit_api_url = upbit_api_url.rstrip("/")
        self.static_dir = static_dir or self._resolve_static_dir()
        self.server: QuietThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._running = False
        self._cached_status: dict[str, Any] = {}
        self._cache_lock = threading.Lock()
        self._last_known_good: dict[str, dict[str, Any]] = {}
        self._last_success_ts: dict[str, float] = {}
        self.http_session = requests.Session()
        self.http_session.trust_env = False

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
            "message": "봇 프로세스 미구동 또는 응답 없음",
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
            "positions": combined_positions,
            "candidates": combined_candidates,
            "recent_trades": combined_recent_trades,
            "recent_orders": combined_orders,
            "fear_and_greed": fng,
            "bithumb_online": bithumb_data.get("online", False),
            "upbit_online": upbit_data.get("online", False),
            "bithumb_status": bt_status,
            "upbit_status": up_status,
            "active_positions_count": len(combined_positions),
        }

        return {
            "combined": combined,
            "bithumb": bithumb_data,
            "upbit": upbit_data,
            "timestamp": time.time(),
        }

    def forward_action(self, action: str, exchange_target: str = "all") -> dict[str, Any]:
        """지정된 거래소로 명령 전달 (panic, pause, resume 등)"""
        results = {}
        target = exchange_target.lower()

        if target in ("bithumb", "all"):
            try:
                res = self.http_session.post(f"{self.bithumb_api_url}/api/action/{action}", timeout=2.0)
                results["bithumb"] = res.json() if res.status_code == 200 else {"success": False, "message": f"HTTP {res.status_code}"}
            except Exception as e:
                results["bithumb"] = {"success": False, "message": f"빗썸 연결 실패: {e}"}

        if target in ("upbit", "all"):
            try:
                res = self.http_session.post(f"{self.upbit_api_url}/api/action/{action}", timeout=2.0)
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
                logger.info(f"🌐 [통합 퀀트 트레이딩 대시보드 가동] 접속 주소: http://localhost:{self.port}")
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
                    self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Authorization")
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

                    # 2. 정적 SPA 파일 서빙
                    if server_self.static_dir:
                        rel_path = path.lstrip("/")
                        if not rel_path or rel_path == "index.html":
                            target_file = os.path.join(server_self.static_dir, "index.html")
                        else:
                            target_file = os.path.join(server_self.static_dir, rel_path)

                        norm_target = os.path.abspath(target_file)
                        norm_static = os.path.abspath(server_self.static_dir)
                        if norm_target.startswith(norm_static) and os.path.isfile(norm_target):
                            mime_type, _ = mimetypes.guess_type(norm_target)
                            if not mime_type:
                                if norm_target.endswith(".js"):
                                    mime_type = "text/javascript"
                                elif norm_target.endswith(".css"):
                                    mime_type = "text/css"
                                elif norm_target.endswith(".html"):
                                    mime_type = "text/html"
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
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #0b0e14; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .card { background-color: #151923; border: 1px solid #232a3b; border-radius: 12px; }
        .badge { padding: 3px 8px; border-radius: 6px; font-weight: bold; font-size: 0.75rem; white-space: nowrap; display: inline-block; }
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
                <button onclick="triggerAction('panic')" class="px-4 py-2 bg-rose-600 hover:bg-rose-700 font-bold rounded-lg text-sm text-white shadow-lg transition">🚨 긴급 전량 매도</button>
                <button onclick="triggerAction('pause')" class="px-4 py-2 bg-amber-600 hover:bg-amber-700 font-bold rounded-lg text-sm text-white shadow-lg transition">⏸️ 전체 일시정지</button>
                <button onclick="triggerAction('resume')" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 font-bold rounded-lg text-sm text-white shadow-lg transition">▶️ 전체 재개</button>
            </div>
        </div>

        <!-- Exchange Switcher Tabs -->
        <div class="flex space-x-2 border-b border-slate-800 pb-2">
            <button onclick="switchView('combined')" id="tab-combined" class="tab-btn active px-4 py-2 rounded-lg font-bold text-sm bg-slate-800 hover:bg-slate-700 transition">🌐 전체 통합 뷰</button>
            <button onclick="switchView('bithumb')" id="tab-bithumb" class="tab-btn px-4 py-2 rounded-lg font-bold text-sm bg-slate-800 hover:bg-slate-700 transition">🟡 빗썸 (Bithumb)</button>
            <button onclick="switchView('upbit')" id="tab-upbit" class="tab-btn px-4 py-2 rounded-lg font-bold text-sm bg-slate-800 hover:bg-slate-700 transition">🔵 업비트 (Upbit)</button>
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

        <!-- Recent Orders Card -->
        <div class="card p-5 shadow-md space-y-4">
            <h2 class="text-lg font-bold text-white flex items-center gap-2">
                <span>📜</span> 최근 주문 저널 (Order Journal)
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
                            <td colspan="8" class="p-6 text-center text-slate-500">최근 주문 내역이 없습니다.</td>
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

        function render() {
            if (!latestData) return;

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

            // 3. Orders Table
            const otbody = document.getElementById('order_table_body');
            const orders = target.recent_orders || [];
            if (orders.length === 0) {
                otbody.innerHTML = '<tr><td colspan="8" class="p-6 text-center text-slate-500">최근 주문 내역이 없습니다.</td></tr>';
            } else {
                otbody.innerHTML = orders.slice(0, 15).map(o => {
                    const isBuy = (o.side || '').toLowerCase().includes('bid') || (o.side || '').includes('매수');
                    const sideBadge = isBuy ? '<span class="badge bg-emerald-900 text-emerald-300">매수</span>' : '<span class="badge bg-rose-900 text-rose-300">매도</span>';
                    const exBadge = o.exchange === 'upbit' ? '<span class="badge bg-blue-900/60 text-blue-300">업비트</span>' : '<span class="badge bg-amber-900/60 text-amber-300">빗썸</span>';
                    return `
                        <tr class="hover:bg-slate-800/40 transition text-xs">
                            <td class="p-3">${exBadge}</td>
                            <td class="p-3 text-slate-400">${(o.created_at || o.timestamp || '-').substring(5, 19)}</td>
                            <td class="p-3 font-medium text-white">${o.market}</td>
                            <td class="p-3">${sideBadge}</td>
                            <td class="p-3">${formatKrw(o.price || o.avg_price)}</td>
                            <td class="p-3">${Number(o.volume || o.executed_volume || 0).toFixed(4)}</td>
                            <td class="p-3 font-semibold text-slate-300">${o.status || '완료'}</td>
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
    </script>
</body>
</html>
"""


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Unified Quant Trading Dashboard Server")
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

    logger.info("======================================================")
    logger.info("  통합 퀀트 트레이딩 대시보드 게이트웨이 서버 가동")
    logger.info("  접속 URL: http://localhost:%d", args.port)
    logger.info("======================================================")

    server = UnifiedDashboardServer(
        port=args.port,
        host="0.0.0.0",
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
    except Exception as e:
        try:
            with open(os.path.join(project_root, "logs", "dashboard_crash.log"), "w", encoding="utf-8") as f:
                import traceback
                traceback.print_exc(file=f)
        except Exception:
            pass
