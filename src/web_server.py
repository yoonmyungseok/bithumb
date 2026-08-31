import json
import hmac
import logging
import mimetypes
import os
import sys
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """클라이언트 연결 끊김(새로고침, 탭 닫기 등) 시 발생하는 불필요한 스택트레이스 억제"""
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc_type, exc_val, _ = sys.exc_info()
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ConnectionError):
            logger.debug(f"웹 클라이언트({client_address}) 연결 조기 종료 감지: {exc_val}")
            return
        super().handle_error(request, client_address)


class DashboardWebServer:
    """
    로컬 경량 실시간 웹 대시보드 서버 (포트 7979 / 7980)
    - 계좌 종합 현황, 공포탐욕지수, 실시간 보유 포지션 및 AI 전략 상태 모니터링
    - 원격 긴급 매도(Panic Sell), 일시정지(Pause), 재개(Resume) 제어 지원
    - 모던 프론트엔드 SPA (dashboard/ 또는 dashboard/dist/) 정적 서빙 및 CORS 지원
    """

    def __init__(
        self,
        port: int = 7979,
        host: str = "127.0.0.1",
        get_status_data_func: Callable[[], dict[str, Any]] | None = None,
        action_handler_func: Callable[[str], str] | None = None,
        data_provider: Callable[[], dict[str, Any]] | None = None,
        action_handler: Callable[[str], str] | None = None,
        title: str = "Bithumb AI 퀀트 트레이딩 Pro",
        static_dir: str | None = None,
        is_api_only: bool = False,
        **kwargs,
    ):
        self.port = port
        self.host = host
        self.get_status_data = get_status_data_func or data_provider or kwargs.get("data_provider")
        self.action_handler = action_handler_func or action_handler or kwargs.get("action_handler")
        self.title = title or kwargs.get("title", "Bithumb AI 퀀트 트레이딩 Pro")
        self.is_api_only = is_api_only or kwargs.get("is_api_only", False)
        # 설정된 경우에만 원격 제어에 토큰을 요구해 기존 로컬 운영 환경의 호환을 유지한다.
        self.action_token = os.getenv("DASHBOARD_ACTION_TOKEN", "").strip()
        self.static_dir = None if self.is_api_only else (static_dir or self._resolve_static_dir())
        self.server: QuietThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def is_action_authorized(self, supplied_token: str) -> bool:
        """원격 제어 토큰이 설정된 경우에만 상수 시간 비교로 검증한다."""
        return not self.action_token or hmac.compare_digest(supplied_token or "", self.action_token)

    def _resolve_static_dir(self) -> str | None:
        """대시보드 SPA 정적 디렉터리 경로 자동 감지"""
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dashboard"))
        dist_dir = os.path.join(base_dir, "dist")
        if os.path.isdir(dist_dir) and os.path.isfile(os.path.join(dist_dir, "index.html")):
            return dist_dir
        if os.path.isdir(base_dir) and os.path.isfile(os.path.join(base_dir, "index.html")):
            return base_dir
        return None

    def start(self):
        handler_cls = self._create_handler()
        for attempt in range(5):
            try:
                QuietThreadingHTTPServer.allow_reuse_address = True
                self.server = QuietThreadingHTTPServer((self.host, self.port), handler_cls)
                self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="WebDashboard")
                self._thread.start()
                logger.info(f"🌐 [{self.title} 웹 대시보드 가동] 접속 주소: http://localhost:{self.port}")
                return
            except OSError as e:
                if attempt < 4:
                    import time
                    time.sleep(1.0)
                else:
                    logger.warning(f"웹 대시보드 포트 {self.port} 바인딩 실패: {e}")

    def stop(self):
        """웹 대시보드 서버 안전 종료"""
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
                logger.info("🌐 [로컬 웹 대시보드 종료 완료]")
            except Exception as e:
                logger.debug(f"웹 대시보드 종료 예외: {e}")

    def _create_handler(self):
        server_self = self

        class DashboardHandler(BaseHTTPRequestHandler):
            def log_message(self, format_str, *args):
                pass

            def do_OPTIONS(self):
                """CORS Preflight 요청 처리"""
                self.close_connection = True
                try:
                    self.send_response(204)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Authorization, X-Dashboard-Action-Token")
                    self.send_header("Access-Control-Max-Age", "86400")
                    self.send_header("Connection", "close")
                    self.end_headers()
                except Exception as e:
                    logger.debug(f"웹 대시보드 OPTIONS 처리 예외: {e}")

            def do_GET(self):
                self.close_connection = True
                try:
                    parsed_url = urllib.parse.urlparse(self.path)
                    path = parsed_url.path

                    # 1. API Status 반환
                    if path == "/api/status":
                        data = server_self.get_status_data() if server_self.get_status_data else {}
                        # 봇 타이틀 주입 (프론트엔드 자동 인식용)
                        if isinstance(data, dict) and "title" not in data:
                            data["title"] = server_self.title
                        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    # 2. 정적 SPA 파일 서빙 (dashboard/ 디렉터리가 존재하는 경우)
                    if server_self.static_dir:
                        rel_path = path.lstrip("/")
                        if not rel_path or rel_path == "index.html":
                            target_file = os.path.join(server_self.static_dir, "index.html")
                        else:
                            target_file = os.path.join(server_self.static_dir, rel_path)

                        # 디렉터리 트래버설 공격 방어
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

                    # 3. 폴백: API 전용 모드인 경우 간략 상태 JSON, 일반 모드인 경우 내장 HTML 렌더링
                    if server_self.is_api_only:
                        msg = json.dumps({"status": "ok", "service": server_self.title, "mode": "api_only"}, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(msg)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(msg)
                        return

                    body = server_self._render_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ConnectionError):
                    pass
                except Exception as e:
                    logger.debug(f"웹 대시보드 GET 처리 예외: {e}")

            def do_POST(self):
                self.close_connection = True
                try:
                    if self.path.startswith("/api/action/"):
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
                        action_name = self.path.split("/")[-1]
                        reply = ""
                        if server_self.action_handler:
                            reply = server_self.action_handler(action_name)
                        body = json.dumps({"success": True, "message": reply}, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    self.send_response(404)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ConnectionError):
                    pass
                except Exception as e:
                    logger.debug(f"웹 대시보드 POST 처리 예외: {e}")

        return DashboardHandler

    def _render_html(self) -> str:
        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{self.title} 대시보드</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body {{ background-color: #0b0e14; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        .card {{ background-color: #151923; border: 1px solid #232a3b; border-radius: 12px; }}
        .badge {{ padding: 3px 8px; border-radius: 6px; font-weight: bold; font-size: 0.75rem; white-space: nowrap; display: inline-block; }}
        .factor-chip {{ font-size: 0.65rem; padding: 1px 6px; border-radius: 4px; font-family: monospace; }}
        .tab-btn.active {{ background-color: #2563eb; color: #ffffff; border-color: #3b82f6; }}
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
                        {self.title}
                    </h1>
                    <p class="text-xs text-slate-400">포트: {self.port} | 7대 팩터 복합 알파 분석 & 50% 분할익절 가속 트레일링</p>
                </div>
            </div>
            <!-- Quick Actions -->
            <div class="flex flex-wrap gap-2 mt-4 sm:mt-0">
                <button onclick="triggerAction('panic')" class="px-4 py-2 bg-rose-600 hover:bg-rose-700 font-bold rounded-lg text-sm text-white shadow-lg transition">🚨 긴급 전량 매도</button>
                <button onclick="triggerAction('pause')" class="px-4 py-2 bg-amber-600 hover:bg-amber-700 font-bold rounded-lg text-sm text-white shadow-lg transition">⏸️ 일시정지</button>
                <button onclick="triggerAction('resume')" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 font-bold rounded-lg text-sm text-white shadow-lg transition">▶️ 재개</button>
            </div>
        </div>

        <!-- 4 Major Asset Cards -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div class="card p-5 shadow-md">
                <div class="text-xs text-slate-400 uppercase font-semibold">총 평가 자산</div>
                <div id="total_equity" class="text-2xl font-bold text-white mt-1">- 원</div>
                <div class="text-xs text-emerald-400 mt-2">가용 원화: <span id="krw_avail">-</span></div>
            </div>
            <div class="card p-5 shadow-md">
                <div class="text-xs text-slate-400 uppercase font-semibold">금일 자산 변동 (평가)</div>
                <div id="daily_pnl" class="text-2xl font-bold text-white mt-1">- 원 (0.00%)</div>
                <div class="text-xs text-slate-400 mt-2">기준 자산: <span id="start_equity">-</span></div>
            </div>
            <div class="card p-5 shadow-md">
                <div class="text-xs text-slate-400 uppercase font-semibold">금일 확정 실현 손익</div>
                <div id="realized_pnl" class="text-2xl font-bold text-emerald-400 mt-1">+0 원</div>
                <div class="text-xs text-slate-400 mt-2">거래: <span id="trade_stats">-</span></div>
            </div>
            <div class="card p-5 shadow-md">
                <div class="flex justify-between items-center">
                    <span class="text-xs text-slate-400 uppercase font-semibold">크립토 시장 & BTC 레짐</span>
                    <span id="btc_regime_badge" class="badge bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-xs">🟢 정상장</span>
                </div>
                <div id="fear_greed" class="text-2xl font-bold text-amber-400 mt-1">-</div>
                <div class="flex justify-between items-center mt-2 text-xs">
                    <span id="btc_regime_desc" class="text-slate-300 truncate max-w-[170px]">진입 기준: 60점+</span>
                    <span id="bot_state" class="text-emerald-400 font-semibold">🟢 가동 중</span>
                </div>
            </div>
        </div>

        <!-- Strategy View Switcher Tabs -->
        <div class="flex items-center justify-between bg-slate-900/60 p-2 rounded-xl border border-slate-800">
            <div class="flex space-x-2">
                <button id="tab_all" onclick="switchStrategyTab('all')" class="tab-btn active px-4 py-2 rounded-lg text-xs sm:text-sm font-bold border border-slate-700 transition">
                    🌐 전체 전략 뷰
                </button>
                <button id="tab_positions" onclick="switchStrategyTab('positions')" class="tab-btn px-4 py-2 rounded-lg text-xs sm:text-sm font-bold border border-slate-700 text-slate-300 hover:text-white transition">
                    📊 보유 포지션 전략 (<span id="count_positions">0</span>)
                </button>
                <button id="tab_candidates" onclick="switchStrategyTab('candidates')" class="tab-btn px-4 py-2 rounded-lg text-xs sm:text-sm font-bold border border-slate-700 text-slate-300 hover:text-white transition">
                    🎯 신규 진입 후보군 (<span id="count_candidates">0</span>)
                </button>
            </div>
            <div class="text-xs text-slate-400 hidden sm:block">
                ⚡ 5초 주기 실시간 자동 동기화
            </div>
        </div>

        <!-- Section 1: Real-time Positions & Strategies -->
        <div id="section_positions" class="card p-6 shadow-md">
            <div class="flex flex-wrap justify-between items-center mb-4">
                <h2 class="text-lg font-bold text-white flex items-center">
                    <span class="mr-2">📊</span> 현재 보유 포지션 및 실시간 AI 퀀트 전략
                </h2>
                <span class="text-xs text-slate-400">보유 중인 포지션의 목표가, 손절가, 익절 락인 및 AI 실시간 대응</span>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm text-slate-300">
                    <thead class="bg-slate-800/60 text-slate-400 uppercase text-xs">
                        <tr>
                            <th class="p-3 whitespace-nowrap">종목명 (마켓)</th>
                            <th class="p-3 whitespace-nowrap">현재가 / 평단가</th>
                            <th class="p-3 whitespace-nowrap">보유 수량 / 평가액</th>
                            <th class="p-3 whitespace-nowrap">수익률</th>
                            <th class="p-3 whitespace-nowrap min-w-[90px]">AI 행동</th>
                            <th class="p-3 whitespace-nowrap">목표가 / 손절가</th>
                            <th class="p-3 whitespace-nowrap">알파 / 손익비</th>
                            <th class="p-3 min-w-[200px]">AI 분석 근거 / 실시간 대응</th>
                        </tr>
                    </thead>
                    <tbody id="positions_tbody" class="divide-y divide-slate-800">
                        <tr><td colspan="8" class="p-4 text-center text-slate-500">데이터 로딩 중...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Section 2: Scanned Non-Held Candidates Watchlist & AI Entry Strategies -->
        <div id="section_candidates" class="card p-6 shadow-md">
            <div class="flex flex-wrap justify-between items-center mb-4">
                <div>
                    <h2 class="text-lg font-bold text-white flex items-center">
                        <span class="mr-2">🎯</span> 신규 스캔 종목 AI 진입 전략 후보군 (Watchlist)
                    </h2>
                    <p class="text-xs text-slate-400 mt-0.5">실시간 거래대금 & 급등 모멘텀 스크리너가 발굴한 미보유 유망 코인 7대 팩터 분석 결과</p>
                </div>
                <div class="text-xs text-slate-300 bg-slate-800/90 px-3 py-1.5 rounded-lg border border-slate-700 mt-2 sm:mt-0 flex items-center space-x-2">
                    <span>현재 시장 레짐:</span>
                    <span id="cand_regime_indicator" class="badge bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">🟢 정상장 (진입 60점+)</span>
                </div>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm text-slate-300">
                    <thead class="bg-slate-800/60 text-slate-400 uppercase text-xs">
                        <tr>
                            <th class="p-3 whitespace-nowrap">종목명 (마켓)</th>
                            <th class="p-3 whitespace-nowrap">현재가 (KRW)</th>
                            <th class="p-3 whitespace-nowrap min-w-[100px]">7대 알파 스코어</th>
                            <th class="p-3 whitespace-nowrap min-w-[90px]">AI 추천 행동</th>
                            <th class="p-3 whitespace-nowrap">목표가 (+%) / 손절가 (-%)</th>
                            <th class="p-3 whitespace-nowrap">예상 손익비 (R:R)</th>
                            <th class="p-3 min-w-[220px]">AI / 퀀트 진입 분석 근거</th>
                        </tr>
                    </thead>
                    <tbody id="candidates_tbody" class="divide-y divide-slate-800">
                        <tr><td colspan="7" class="p-4 text-center text-slate-500">후보 종목 데이터를 불러오는 중...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Dual Column: Recent Completed Trades & Order Journal -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <!-- Recent Completed Trades -->
            <div class="card p-6 shadow-md">
                <h2 class="text-lg font-bold text-white mb-4 flex items-center">
                    <span class="mr-2">💰</span> 최근 완료 거래 내역
                </h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-xs text-slate-300">
                        <thead class="bg-slate-800/60 text-slate-400 uppercase">
                            <tr>
                                <th class="p-2.5 whitespace-nowrap">일시</th>
                                <th class="p-2.5 whitespace-nowrap">종목명 (마켓)</th>
                                <th class="p-2.5 whitespace-nowrap min-w-[100px]">구분</th>
                                <th class="p-2.5 whitespace-nowrap">실현 손익</th>
                                <th class="p-2.5">체결 사유</th>
                            </tr>
                        </thead>
                        <tbody id="trades_tbody" class="divide-y divide-slate-800">
                            <tr><td colspan="5" class="p-3 text-center text-slate-500">완료된 거래 기록이 없습니다.</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Order Journal -->
            <div class="card p-6 shadow-md">
                <h2 class="text-lg font-bold text-white mb-4 flex items-center">
                    <span class="mr-2">🛡️</span> 실시간 주문 저널
                </h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-xs text-slate-300">
                        <thead class="bg-slate-800/60 text-slate-400 uppercase">
                            <tr>
                                <th class="p-2.5 whitespace-nowrap">주문 일시</th>
                                <th class="p-2.5 whitespace-nowrap">종목명 (마켓)</th>
                                <th class="p-2.5 whitespace-nowrap min-w-[60px]">방향</th>
                                <th class="p-2.5 whitespace-nowrap min-w-[90px]">주문 상태</th>
                                <th class="p-2.5 whitespace-nowrap">체결가 / 주문가</th>
                            </tr>
                        </thead>
                        <tbody id="orders_tbody" class="divide-y divide-slate-800">
                            <tr><td colspan="5" class="p-3 text-center text-slate-500">주문 저널 데이터 로딩 중...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Strategy Policy Guide & Architecture Summary -->
        <div class="card p-5 text-xs text-slate-400 border border-slate-800 bg-slate-900/40">
            <div class="font-bold text-slate-300 mb-2 flex items-center">
                <span class="mr-1.5">💡</span> AI 퀀트 트레이딩 핵심 전략 정책 (Strategy Policy SSOT)
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-3">
                <div class="bg-slate-800/40 p-3 rounded-lg border border-slate-800">
                    <div class="text-slate-200 font-semibold mb-1">🎯 3단계 분할 익절 & 락인</div>
                    <div>• 1차 +2.5% 도달 시 30% 익절</div>
                    <div>• 2차 +5.0% 도달 시 30% 추가익절</div>
                    <div>• 1차 익절 완료 시 본전스탑(+0.3%) 보장</div>
                </div>
                <div class="bg-slate-800/40 p-3 rounded-lg border border-slate-800">
                    <div class="text-slate-200 font-semibold mb-1">🚀 가속 트레일링 러너</div>
                    <div>• +2.0% 수익 시 트레일링 감시 가동</div>
                    <div>• 최고점 대비 1.2% 하락 시 잔여분 익절</div>
                    <div>• 최소 보장 마진 +0.5% 슬리피지 방어</div>
                </div>
                <div class="bg-slate-800/40 p-3 rounded-lg border border-slate-800">
                    <div class="text-slate-200 font-semibold mb-1">🛡️ 7대 팩터 하드 게이트</div>
                    <div>• MTF 1H + VWAP + MACD 가속도</div>
                    <div>• RSI 골든존 + 볼린저 %B + 롤링 호가 잔량비</div>
                    <div>• 60점(정상장) / 75점(약세장) 미만 진입 차단</div>
                </div>
                <div class="bg-slate-800/40 p-3 rounded-lg border border-slate-800">
                    <div class="text-slate-200 font-semibold mb-1">⏳ 타임스탑 & 쿨다운</div>
                    <div>• 40~60분간 ±1% 횡보 시 순환매 청산</div>
                    <div>• 손절/타임스탑 후 25분 쿨다운</div>
                    <div>• 일일 계좌 최대 손실 -5.0% 킬스위치</div>
                </div>
            </div>
        </div>

        <div class="text-center text-xs text-slate-500 py-2">
            {self.title} | 5초마다 실시간 동기화 중 | 포트: {self.port}
        </div>
    </div>

    <script>
        let currentTab = 'all';

        function switchStrategyTab(tab) {{
            currentTab = tab;
            document.querySelectorAll('.tab-btn').forEach(btn => {{
                btn.classList.remove('active');
                btn.classList.remove('bg-blue-600', 'text-white');
            }});
            const activeBtn = document.getElementById('tab_' + tab);
            if (activeBtn) activeBtn.classList.add('active');

            const secPos = document.getElementById('section_positions');
            const secCand = document.getElementById('section_candidates');

            if (tab === 'all') {{
                secPos.style.display = 'block';
                secCand.style.display = 'block';
            }} else if (tab === 'positions') {{
                secPos.style.display = 'block';
                secCand.style.display = 'none';
            }} else if (tab === 'candidates') {{
                secPos.style.display = 'none';
                secCand.style.display = 'block';
            }}
        }}

        async function fetchStatus() {{
            try {{
                const res = await fetch('/api/status');
                const data = await res.json();
                if (data) {{
                    document.getElementById('total_equity').innerText = (data.total_equity || 0).toLocaleString() + ' 원';
                    document.getElementById('krw_avail').innerText = (data.krw_available || 0).toLocaleString() + ' 원';
                    document.getElementById('start_equity').innerText = (data.daily_start_equity || 0).toLocaleString() + ' 원';
                    
                    const pnlKrw = (data.daily_pnl_krw || 0);
                    const pnlPct = (data.daily_pnl_pct || 0);
                    const pnlEl = document.getElementById('daily_pnl');
                    pnlEl.innerText = (pnlKrw >= 0 ? '+' : '') + pnlKrw.toLocaleString() + ' 원 (' + (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2) + '%)';
                    pnlEl.className = 'text-2xl font-bold mt-1 ' + (pnlKrw >= 0 ? 'text-emerald-400' : 'text-rose-400');

                    const realPnl = (data.realized_pnl_krw || 0);
                    const realEl = document.getElementById('realized_pnl');
                    realEl.innerText = (realPnl >= 0 ? '+' : '') + realPnl.toLocaleString() + ' 원';
                    realEl.className = 'text-2xl font-bold mt-1 ' + (realPnl >= 0 ? 'text-emerald-400' : 'text-rose-400');

                    document.getElementById('trade_stats').innerText = (data.total_trades || 0) + '회 중 ' + (data.win_trades || 0) + '승 (승률 ' + (data.win_rate || 0).toFixed(0) + '%)';
                    document.getElementById('fear_greed').innerText = data.fear_and_greed || '-';
                    document.getElementById('bot_state').innerText = data.bot_state || '🟢 정상 가동 중';

                    // Update BTC Market Regime Indicators
                    const btcRegime = String(data.btc_regime || 'NORMAL').toUpperCase();
                    const btcDesc = data.btc_regime_desc || (btcRegime === 'RISK_OFF' ? '🟡 약세 조정장 (진입 75점+)' : (btcRegime === 'CRASH' ? '🚨 급락 경보 (매수 차단)' : '🟢 정상장 (진입 60점+)'));
                    const btcBadgeCls = (btcRegime === 'RISK_OFF') ? 'bg-amber-500/20 text-amber-300 border border-amber-500/40' : ((btcRegime === 'CRASH') ? 'bg-rose-500/20 text-rose-300 border border-rose-500/40' : 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40');

                    const regBadgeEl = document.getElementById('btc_regime_badge');
                    if (regBadgeEl) {{
                        regBadgeEl.innerText = (btcRegime === 'RISK_OFF') ? '🟡 약세장' : ((btcRegime === 'CRASH') ? '🚨 급락' : '🟢 정상장');
                        regBadgeEl.className = 'badge ' + btcBadgeCls;
                    }}

                    const regDescEl = document.getElementById('btc_regime_desc');
                    if (regDescEl) {{
                        regDescEl.innerText = data.btc_regime_reason ? ('BTC: ' + data.btc_regime_reason) : (btcRegime === 'RISK_OFF' ? '1H EMA50 하회 조정' : '1H EMA50 상회 안정세');
                    }}

                    const candRegimeEl = document.getElementById('cand_regime_indicator');
                    if (candRegimeEl) {{
                        candRegimeEl.innerText = btcDesc;
                        candRegimeEl.className = 'badge ' + btcBadgeCls;
                    }}

                    // Update Tab Counters
                    const posCount = (data.positions || []).length;
                    const candCount = (data.candidates || []).length;
                    document.getElementById('count_positions').innerText = posCount;
                    document.getElementById('count_candidates').innerText = candCount;

                    function formatAction(action) {{
                        if (!action) return '-';
                        const a = String(action).toUpperCase();
                        if (a === 'BUY' || a === 'BID') return '매수 승인';
                        if (a === 'SELL' || a === 'ASK') return '매도';
                        if (a === 'MANUAL_EXIT') return '수동 청산';
                        if (a === 'HOLD') return '관망 대기';
                        if (a === 'STOP_LOSS') return '손절';
                        if (a === 'PARTIAL_TP') return '1차 분할익절';
                        if (a === 'TRAILING_STOP') return '트레일링 익절';
                        if (a === 'TIME_STOP') return '타임스탑 청산';
                        if (a === 'MOMENTUM_EARLY_EXIT' || a === 'MOMENTUM_EXIT') return '모멘텀 조기탈출';
                        if (a === 'PANIC_SELL') return '긴급 전량매도';
                        if (a === 'PROFIT_TAKE' || a === 'TAKE_PROFIT') return '전량 익절';
                        return action;
                    }}

                    function formatTradeSide(side) {{
                        if (!side) return '-';
                        const s = String(side).toUpperCase();
                        if (s === 'BUY' || s === 'BID') return '매수';
                        if (s === 'SELL' || s === 'ASK') return '매도';
                        if (s === 'MANUAL_EXIT') return '수동 청산';
                        if (s === 'PARTIAL_TP' || s.includes('TP') || s.includes('WIN')) return '1차 분할익절';
                        if (s === 'TRAILING_STOP') return '트레일링 익절';
                        if (s === 'STOP_LOSS') return '손절';
                        if (s === 'TIME_STOP') return '타임스탑 청산';
                        if (s === 'MOMENTUM_EARLY_EXIT' || s.includes('MOMENTUM') || s.includes('모멘텀')) return '모멘텀 조기탈출';
                        if (s === 'PANIC_SELL') return '긴급 전량매도';
                        if (s === 'PROFIT_TAKE' || s === 'TAKE_PROFIT') return '전량 익절';
                        return side;
                    }}

                    function formatOrderStatus(status) {{
                        if (!status) return '-';
                        const s = String(status).toUpperCase();
                        if (s === 'FILLED' || s === 'DONE') return '체결 완료';
                        if (s === 'RECONCILIATION_PENDING') return '체결 대사중';
                        if (s === 'RECONCILED') return '대사 완료';
                        if (s === 'PARTIALLY_FILLED') return '부분 체결';
                        if (s === 'OPEN' || s === 'WAIT') return '미체결 대기';
                        if (s === 'PENDING' || s === 'SUBMITTED') return '접수/전송중';
                        if (s === 'CANCELED' || s === 'CANCEL' || s === 'CANCELLED') return '취소 완료';
                        if (s === 'FAILED' || s === 'FAIL' || s === 'ERROR') return '주문 실패';
                        if (s === 'EXPIRED') return '기간 만료';
                        if (s === 'UNKNOWN') return '확인 필요';
                        if (s === 'REJECTED') return '주문 거절';
                        return status;
                    }}

                    function formatReason(r) {{
                        if (!r) return '-';
                        return String(r)
                            .replace(/MOMENTUM_EARLY_EXIT/g, '모멘텀 조기 본전탈출')
                            .replace(/MOMENTUM_EXIT/g, '모멘텀 조기탈출')
                            .replace(/MANUAL_EXIT/g, '수동 청산')
                            .replace(/TRAILING_STOP/g, '트레일링 익절')
                            .replace(/TIME_STOP/g, '타임스탑 청산')
                            .replace(/STOP_LOSS/g, '손절')
                            .replace(/PARTIAL_TP/g, '1차 분할익절')
                            .replace(/PANIC_SELL/g, '긴급 전량매도');
                    }}

                    function renderAlphaBadge(score) {{
                        const sc = Number(score || 0);
                        if (sc >= 85) return `<span class="badge bg-amber-500/20 text-amber-300 border border-amber-500/40 font-mono">🔥 ${{sc}}점 (A+특급)</span>`;
                        if (sc >= 75) return `<span class="badge bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 font-mono">🟢 ${{sc}}점 (승인)</span>`;
                        if (sc >= 60) return `<span class="badge bg-blue-500/20 text-blue-300 border border-blue-500/40 font-mono">🔵 ${{sc}}점 (적격)</span>`;
                        if (sc >= 50) return `<span class="badge bg-slate-700 text-slate-300 font-mono">⚪ ${{sc}}점 (관망)</span>`;
                        return `<span class="badge bg-rose-500/20 text-rose-300 font-mono">🛑 ${{sc}}점 (미달)</span>`;
                    }}

                    function renderFactorChips(factors) {{
                        if (!factors || Object.keys(factors).length === 0) return '<span class="text-slate-500 text-xs font-mono">-</span>';
                        let passCount = 0;
                        let totalCount = 0;
                        if (factors.mtf_score !== undefined) {{ totalCount++; if (factors.mtf_score >= 10) passCount++; }}
                        if (factors.vwap_score !== undefined) {{ totalCount++; if (factors.vwap_score >= 10) passCount++; }}
                        if (factors.macd_score !== undefined) {{ totalCount++; if (factors.macd_score >= 10) passCount++; }}
                        if (factors.rsi_score !== undefined) {{ totalCount++; if (factors.rsi_score >= 10) passCount++; }}
                        if (factors.bollinger_score !== undefined || factors.bb_score !== undefined) {{ totalCount++; if ((factors.bollinger_score || factors.bb_score) >= 10) passCount++; }}
                        if (factors.orderflow_score !== undefined || factors.orderbook_score !== undefined) {{ totalCount++; if ((factors.orderflow_score || factors.orderbook_score) >= 10) passCount++; }}
                        if (factors.volume_score !== undefined || factors.vol_score !== undefined) {{ totalCount++; if ((factors.volume_score || factors.vol_score) >= 7) passCount++; }}
                        if (totalCount === 0) return '<span class="text-slate-500 text-xs font-mono">-</span>';
                        const cls = passCount >= 6 ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40' : (passCount >= 4 ? 'bg-blue-500/20 text-blue-300 border border-blue-500/40' : 'bg-slate-800 text-slate-400 border border-slate-700');
                        return `<span class="badge ${{cls}} font-mono">${{passCount}} / ${{totalCount}}개 적격</span>`;
                    }}

                    // 1. Positions Table
                    const tbody = document.getElementById('positions_tbody');
                    if (data.positions && data.positions.length > 0) {{
                        tbody.innerHTML = data.positions.map(p => {{
                            const actKr = formatAction(p.action);
                            const actUpper = String(p.action || '').toUpperCase();
                            const actBadge = (actUpper === 'BUY' || actUpper === 'BID') ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : ((actUpper === 'SELL' || actUpper === 'ASK' || actUpper === 'STOP_LOSS' || actUpper === 'TRAILING_STOP' || actUpper === 'PARTIAL_TP') ? 'bg-rose-500/20 text-rose-300 border border-rose-500/30' : 'bg-slate-700 text-slate-300');
                            const reasonKr = formatReason(p.reason);
                            const targetStr = p.target_price > 0 ? (p.target_price.toLocaleString() + '원 (' + (p.target_pct >= 0 ? '+' : '') + (p.target_pct || 0).toFixed(1) + '%)') : '-';
                            const stopStr = p.stop_loss > 0 ? (p.stop_loss.toLocaleString() + '원 (' + (p.stop_pct || 0).toFixed(1) + '%)') : '-';
                            const rrRatioStr = (p.risk_reward_ratio > 0) ? (p.risk_reward_ratio.toFixed(1) + ' : 1') : '-';
                            const avgP = Number(p.avg_buy_price || 0);

                            return `
                                <tr class="hover:bg-slate-800/60 transition">
                                    <td class="p-3 font-bold text-white whitespace-nowrap">${{p.korean_name}} <span class="text-xs text-slate-400 font-normal">(${{p.market}})</span></td>
                                    <td class="p-3 whitespace-nowrap">
                                        <div class="font-bold text-slate-100">${{p.current_price.toLocaleString()}} 원</div>
                                        <div class="text-xs text-slate-400">평단: ${{avgP > 0 ? avgP.toLocaleString() + '원' : '-'}}</div>
                                    </td>
                                    <td class="p-3 whitespace-nowrap">${{p.balance}}개 <div class="text-xs text-slate-400">(${{p.value.toLocaleString()}}원)</div></td>
                                    <td class="p-3 font-bold whitespace-nowrap ${{p.pnl_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}}">${{p.pnl_pct >= 0 ? '+' : ''}}${{p.pnl_pct.toFixed(2)}}%</td>
                                    <td class="p-3 whitespace-nowrap"><span class="badge ${{actBadge}}">${{actKr}}</span></td>
                                    <td class="p-3 text-xs text-slate-300 whitespace-nowrap">
                                        <div class="text-emerald-400">목표: ${{targetStr}}</div>
                                        <div class="text-rose-400">손절: ${{stopStr}}</div>
                                    </td>
                                    <td class="p-3 whitespace-nowrap text-xs">
                                        ${{p.alpha_score ? renderAlphaBadge(p.alpha_score) : '-'}}
                                        <div class="text-slate-400 mt-1">R:R: <span class="text-slate-200 font-mono">${{rrRatioStr}}</span></div>
                                    </td>
                                    <td class="p-3 text-xs text-slate-300 max-w-sm">
                                        <div>${{reasonKr}}</div>
                                    </td>
                                </tr>
                            `;
                        }}).join('');
                    }} else {{
                        tbody.innerHTML = '<tr><td colspan="8" class="p-6 text-center text-slate-500">현재 보유 중인 코인이 없습니다 (100% 현금 보유 관망 중).</td></tr>';
                    }}

                    // 2. Candidates Watchlist Table
                    const candTbody = document.getElementById('candidates_tbody');
                    if (data.candidates && data.candidates.length > 0) {{
                        candTbody.innerHTML = data.candidates.map((c, idx) => {{
                            const actKr = formatAction(c.action);
                            const isBuy = (c.action === 'BUY' || c.action === 'BID' || c.allow_buy);
                            const actBadge = isBuy ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40' : 'bg-slate-700 text-slate-300';
                            const targetStr = c.target_price > 0 ? (c.target_price.toLocaleString() + '원 (' + (c.target_pct >= 0 ? '+' : '') + (c.target_pct || 0).toFixed(1) + '%)') : '-';
                            const stopStr = c.stop_loss > 0 ? (c.stop_loss.toLocaleString() + '원 (' + (c.stop_pct || 0).toFixed(1) + '%)') : '-';
                            const rrRatioStr = (c.risk_reward_ratio > 0) ? (c.risk_reward_ratio.toFixed(1) + ' : 1') : '-';
                            const reasonKr = formatReason(c.reason);

                            return `
                                <tr class="hover:bg-slate-800/60 transition ${{isBuy ? 'bg-emerald-950/10' : ''}}">
                                    <td class="p-3 font-bold text-white whitespace-nowrap">
                                        <span class="text-slate-500 mr-1 text-xs">#${{idx + 1}}</span>
                                        ${{c.korean_name}} <span class="text-xs text-slate-400 font-normal">(${{c.market}})</span>
                                    </td>
                                    <td class="p-3 whitespace-nowrap font-mono font-bold text-slate-100">
                                        ${{c.current_price.toLocaleString()}} 원
                                    </td>
                                    <td class="p-3 whitespace-nowrap">
                                        ${{renderAlphaBadge(c.alpha_score)}}
                                    </td>
                                    <td class="p-3 whitespace-nowrap">
                                        <span class="badge ${{actBadge}}">${{actKr}}</span>
                                    </td>
                                    <td class="p-3 text-xs text-slate-300 whitespace-nowrap">
                                        <div class="text-emerald-400">목표: ${{targetStr}}</div>
                                        <div class="text-rose-400">손절: ${{stopStr}}</div>
                                    </td>
                                    <td class="p-3 whitespace-nowrap font-mono text-xs text-amber-300 font-semibold">
                                        ${{rrRatioStr}}
                                    </td>
                                    <td class="p-3 text-xs text-slate-300 max-w-xs">
                                        <div>${{reasonKr}}</div>
                                    </td>
                                </tr>
                            `;
                        }}).join('');
                    }} else {{
                        candTbody.innerHTML = '<tr><td colspan="7" class="p-6 text-center text-slate-500">현재 스캔된 신규 후보 코인이 없습니다. (다음 5분 스케줄 분석 대기)</td></tr>';
                    }}

                    // 3. Recent Completed Trades
                    const tradesTbody = document.getElementById('trades_tbody');
                    if (data.recent_trades && data.recent_trades.length > 0) {{
                        tradesTbody.innerHTML = data.recent_trades.map(t => {{
                            const pnlKrw = t.pnl_krw || 0;
                            const pnlPct = t.pnl_pct || 0;
                            const isWin = pnlKrw >= 0;
                            const sideKr = formatTradeSide(t.side);
                            const sideBadge = (t.side && (t.side.includes('TP') || t.side.includes('WIN') || t.side.includes('BUY') || t.side.includes('BID'))) ? 'bg-emerald-500/20 text-emerald-300' : 'bg-rose-500/20 text-rose-300';
                            const reasonKr = formatReason(t.reason);
                            const coinDisp = t.korean_name ? (t.korean_name + ' <span class="text-xs text-slate-400 font-normal">(' + t.market + ')</span>') : t.market;
                            return `
                                <tr class="hover:bg-slate-800/40">
                                    <td class="p-2.5 text-slate-400 whitespace-nowrap">${{(t.timestamp || '').slice(-8)}}</td>
                                    <td class="p-2.5 font-semibold text-white whitespace-nowrap">${{coinDisp}}</td>
                                    <td class="p-2.5 whitespace-nowrap"><span class="badge ${{sideBadge}}">${{sideKr}}</span></td>
                                    <td class="p-2.5 font-bold whitespace-nowrap ${{isWin ? 'text-emerald-400' : 'text-rose-400'}}">${{isWin ? '+' : ''}}${{pnlKrw.toLocaleString()}}원 (${{isWin ? '+' : ''}}${{pnlPct.toFixed(2)}}%)</td>
                                    <td class="p-2.5 text-slate-400 max-w-xs truncate">${{reasonKr}}</td>
                                </tr>
                            `;
                        }}).join('');
                    }} else {{
                        tradesTbody.innerHTML = '<tr><td colspan="5" class="p-3 text-center text-slate-500">완료된 거래 기록이 없습니다.</td></tr>';
                    }}

                    // 4. Recent Orders
                    const ordersTbody = document.getElementById('orders_tbody');
                    if (data.recent_orders && data.recent_orders.length > 0) {{
                        ordersTbody.innerHTML = data.recent_orders.map(o => {{
                            const rawStatus = (o.status || '').toUpperCase();
                            const isFailed = (rawStatus === 'FAILED' || rawStatus === 'FAIL' || rawStatus === 'ERROR' || rawStatus === 'REJECTED' || rawStatus === 'UNKNOWN');
                            const statusColor = (rawStatus === 'FILLED' || rawStatus === 'DONE') ? 'bg-emerald-500/20 text-emerald-300' : ((rawStatus === 'PARTIALLY_FILLED') ? 'bg-amber-500/20 text-amber-300' : (isFailed ? 'bg-rose-500/20 text-rose-300' : 'bg-slate-700 text-slate-300'));
                            const statusKr = formatOrderStatus(o.status);
                            const sideKr = (o.side === 'bid' || o.side === 'BUY' || o.side === 'buy') ? '매수' : '매도';
                            const coinDisp = o.korean_name ? (o.korean_name + ' <span class="text-xs text-slate-400 font-normal">(' + o.market + ')</span>') : o.market;
                            const avgFillPrice = Number(o.avg_price || 0);
                            const executedVolume = Number(o.executed_volume || 0);
                            const requestedPrice = Number(o.price || 0);
                            const priceDisplay = (executedVolume > 0 && avgFillPrice > 0)
                                ? `체결 ${{avgFillPrice.toLocaleString()}}원`
                                : (requestedPrice > 0 ? `주문 ${{requestedPrice.toLocaleString()}}원` : '확인 필요');
                            return `
                                <tr class="hover:bg-slate-800/40 font-mono text-xs">
                                    <td class="p-2.5 text-slate-300 whitespace-nowrap">${{(o.timestamp || '').slice(5, 19)}}</td>
                                    <td class="p-2.5 font-semibold text-white whitespace-nowrap">${{coinDisp}}</td>
                                    <td class="p-2.5 whitespace-nowrap ${{(o.side === 'bid' || o.side === 'BUY' || o.side === 'buy') ? 'text-emerald-400' : 'text-rose-400'}}">${{sideKr}}</td>
                                    <td class="p-2.5 whitespace-nowrap"><span class="badge ${{statusColor}}">${{statusKr}}</span></td>
                                    <td class="p-2.5 text-slate-400 truncate max-w-[120px] whitespace-nowrap">${{priceDisplay}}</td>
                                </tr>
                            `;
                        }}).join('');
                    }} else {{
                        ordersTbody.innerHTML = '<tr><td colspan="5" class="p-3 text-center text-slate-500">주문 기록이 없습니다.</td></tr>';
                    }}
                }}
            }} catch (e) {{
                console.error('Failed to fetch status:', e);
            }}
        }}

        async function triggerAction(action) {{
            if (action === 'panic' && !confirm('🚨 정말로 보유 중인 모든 코인을 즉시 전량 시장가 매도하시겠습니까?')) {{
                return;
            }}
            try {{
                const res = await fetch('/api/action/' + action, {{ method: 'POST' }});
                const data = await res.json();
                alert(data.message || '작업이 완료되었습니다.');
                fetchStatus();
            }} catch (e) {{
                alert('원격 제어 요청 실패: ' + e);
            }}
        }}

        document.addEventListener('DOMContentLoaded', () => {{
            fetchStatus();
            setInterval(fetchStatus, 5000);
        }});
    </script>
</body>
</html>"""
