import json
import logging
import sys
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """클라이언트 연결 끊김(새로고침, 탭 닫기 등) 시 발생하는 불필요한 스택트레이스 억제"""

    def handle_error(self, request, client_address):
        exc_type, exc_val, _ = sys.exc_info()
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ConnectionError):
            logger.debug(f"웹 클라이언트({client_address}) 연결 조기 종료 감지: {exc_val}")
            return
        super().handle_error(request, client_address)


class DashboardWebServer:
    """
    로컬 경량 실시간 웹 대시보드 서버 (포트 7979)
    - 계좌 종합 현황, 공포탐욕지수, 실시간 보유 포지션 및 AI 전략 상태 모니터링
    - 원격 긴급 매도(Panic Sell), 일시정지(Pause), 재개(Resume) 제어 지원
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
        **kwargs,
    ):
        self.port = port
        self.host = host
        self.get_status_data = get_status_data_func or data_provider or kwargs.get("data_provider")
        self.action_handler = action_handler_func or action_handler or kwargs.get("action_handler")
        self.title = title or kwargs.get("title", "Bithumb AI 퀀트 트레이딩 Pro")
        self.server: QuietThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        handler_cls = self._create_handler()
        try:
            self.server = QuietThreadingHTTPServer((self.host, self.port), handler_cls)
            self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="WebDashboard")
            self._thread.start()
            logger.info(f"🌐 [{self.title} 웹 대시보드 가동] 접속 주소: http://localhost:{self.port}")
        except OSError as e:
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

            def do_GET(self):
                self.close_connection = True
                try:
                    parsed_url = urllib.parse.urlparse(self.path)
                    path = parsed_url.path

                    if path == "/api/status":
                        data = server_self.get_status_data() if server_self.get_status_data else {}
                        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    # 메인 HTML 페이지 렌더링
                    body = server_self._render_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
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
                        action_name = self.path.split("/")[-1]
                        reply = ""
                        if server_self.action_handler:
                            reply = server_self.action_handler(action_name)
                        body = json.dumps({"success": True, "message": reply}, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    self.send_response(404)
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
    </style>
</head>
<body class="p-6">
    <div class="max-w-7xl mx-auto space-y-6">
        <!-- Header -->
        <div class="flex flex-wrap justify-between items-center bg-slate-900/80 p-5 rounded-2xl border border-slate-800 backdrop-blur">
            <div class="flex items-center space-x-3">
                <span class="text-3xl">🚀</span>
                <div>
                    <h1 class="text-2xl font-black bg-gradient-to-r from-blue-400 to-emerald-400 bg-clip-text text-transparent">
                        {self.title}
                    </h1>
                    <p class="text-xs text-slate-400">포트: {self.port} | 0.1초 실시간 웹소켓 리스크 방어 & 50% 분할익절 가속 트레일링</p>
                </div>
            </div>
            <!-- Quick Actions -->
            <div class="flex space-x-2 mt-4 sm:mt-0">
                <button onclick="triggerAction('panic')" class="px-4 py-2 bg-red-600 hover:bg-red-700 font-bold rounded-lg text-sm text-white shadow-lg transition">🚨 긴급 전량 매도</button>
                <button onclick="triggerAction('pause')" class="px-4 py-2 bg-amber-600 hover:bg-amber-700 font-bold rounded-lg text-sm text-white shadow-lg transition">⏸️ 일시정지</button>
                <button onclick="triggerAction('resume')" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 font-bold rounded-lg text-sm text-white shadow-lg transition">▶️ 재개</button>
            </div>
        </div>

        <!-- 4 Major Asset Cards -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div class="card p-5">
                <div class="text-xs text-slate-400 uppercase font-semibold">총 평가 자산</div>
                <div id="total_equity" class="text-2xl font-bold text-white mt-1">- 원</div>
                <div class="text-xs text-emerald-400 mt-2">가용 원화: <span id="krw_avail">-</span></div>
            </div>
            <div class="card p-5">
                <div class="text-xs text-slate-400 uppercase font-semibold">금일 자산 변동 (평가)</div>
                <div id="daily_pnl" class="text-2xl font-bold text-white mt-1">- 원 (0.00%)</div>
                <div class="text-xs text-slate-400 mt-2">기준 자산: <span id="start_equity">-</span></div>
            </div>
            <div class="card p-5">
                <div class="text-xs text-slate-400 uppercase font-semibold">금일 확정 실현 손익</div>
                <div id="realized_pnl" class="text-2xl font-bold text-emerald-400 mt-1">+0 원</div>
                <div class="text-xs text-slate-400 mt-2">거래: <span id="trade_stats">-</span></div>
            </div>
            <div class="card p-5">
                <div class="text-xs text-slate-400 uppercase font-semibold">크립토 공포/탐욕 지수</div>
                <div id="fear_greed" class="text-2xl font-bold text-amber-400 mt-1">-</div>
                <div id="bot_state" class="text-xs text-emerald-400 mt-2">🟢 정상 가동 중</div>
            </div>
        </div>

        <!-- Real-time Positions & Strategies -->
        <div class="card p-6">
            <h2 class="text-lg font-bold text-white mb-4 flex items-center">
                <span class="mr-2">📊</span> 현재 보유 포지션 및 AI 퀀트 전략 모니터링
            </h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm text-slate-300">
                    <thead class="bg-slate-800/60 text-slate-400 uppercase text-xs">
                        <tr>
                            <th class="p-3 whitespace-nowrap">종목명 (마켓)</th>
                            <th class="p-3 whitespace-nowrap">현재가 (원)</th>
                            <th class="p-3 whitespace-nowrap">보유 수량 / 평가액</th>
                            <th class="p-3 whitespace-nowrap">수익률</th>
                            <th class="p-3 whitespace-nowrap min-w-[90px]">AI 행동</th>
                            <th class="p-3 whitespace-nowrap">목표가 / 손절가</th>
                            <th class="p-3">AI 분석 근거</th>
                        </tr>
                    </thead>
                    <tbody id="positions_tbody" class="divide-y divide-slate-800">
                        <tr><td colspan="7" class="p-4 text-center text-slate-500">데이터 로딩 중...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Dual Column: Recent Completed Trades & Order Journal -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <!-- Recent Completed Trades -->
            <div class="card p-6">
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
            <div class="card p-6">
                <h2 class="text-lg font-bold text-white mb-4 flex items-center">
                    <span class="mr-2">🛡️</span> 실시간 주문 저널
                </h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-xs text-slate-300">
                        <thead class="bg-slate-800/60 text-slate-400 uppercase">
                            <tr>
                                <th class="p-2.5 whitespace-nowrap">주문 번호 (ID)</th>
                                <th class="p-2.5 whitespace-nowrap">종목명 (마켓)</th>
                                <th class="p-2.5 whitespace-nowrap min-w-[60px]">방향</th>
                                <th class="p-2.5 whitespace-nowrap min-w-[90px]">주문 상태</th>
                                <th class="p-2.5 whitespace-nowrap">거래소 주문번호</th>
                            </tr>
                        </thead>
                        <tbody id="orders_tbody" class="divide-y divide-slate-800">
                            <tr><td colspan="5" class="p-3 text-center text-slate-500">주문 저널 데이터 로딩 중...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <div class="text-center text-xs text-slate-500 py-2">
            {self.title} | 5초마다 실시간 동기화 중 | 포트: {self.port}
        </div>
    </div>

    <script>
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

                    function formatAction(action) {{
                        if (!action) return '-';
                        const a = String(action).toUpperCase();
                        if (a === 'BUY' || a === 'BID') return '매수';
                        if (a === 'SELL' || a === 'ASK') return '매도';
                        if (a === 'HOLD') return '관망';
                        if (a === 'STOP_LOSS') return '손절';
                        if (a === 'PARTIAL_TP') return '1차 분할익절';
                        if (a === 'TRAILING_STOP') return '트레일링 익절';
                        if (a === 'TIME_STOP') return '타임스탑 청산';
                        if (a === 'PANIC_SELL') return '긴급 전량매도';
                        if (a === 'PROFIT_TAKE' || a === 'TAKE_PROFIT') return '전량 익절';
                        return action;
                    }}

                    function formatTradeSide(side) {{
                        if (!side) return '-';
                        const s = String(side).toUpperCase();
                        if (s === 'BUY' || s === 'BID') return '매수';
                        if (s === 'SELL' || s === 'ASK') return '매도';
                        if (s === 'PARTIAL_TP' || s.includes('TP') || s.includes('WIN')) return '1차 분할익절';
                        if (s === 'TRAILING_STOP') return '트레일링 익절';
                        if (s === 'STOP_LOSS') return '손절';
                        if (s === 'TIME_STOP') return '타임스탑 청산';
                        if (s === 'PANIC_SELL') return '긴급 전량매도';
                        if (s === 'PROFIT_TAKE' || s === 'TAKE_PROFIT') return '전량 익절';
                        return side;
                    }}

                    function formatOrderStatus(status) {{
                        if (!status) return '-';
                        const s = String(status).toUpperCase();
                        if (s === 'FILLED' || s === 'DONE') return '체결 완료';
                        if (s === 'PARTIALLY_FILLED') return '부분 체결';
                        if (s === 'OPEN' || s === 'WAIT') return '미체결 대기';
                        if (s === 'PENDING') return '전송 중';
                        if (s === 'CANCELED' || s === 'CANCEL') return '취소 완료';
                        if (s === 'FAILED' || s === 'FAIL' || s === 'ERROR') return '주문 실패';
                        if (s === 'EXPIRED') return '기간 만료';
                        if (s === 'UNKNOWN') return '확인 필요';
                        if (s === 'REJECTED') return '주문 거절';
                        return status;
                    }}

                    function formatReason(r) {{
                        if (!r) return '-';
                        return String(r)
                            .replace(/TRAILING_STOP/g, '트레일링 익절')
                            .replace(/TIME_STOP/g, '타임스탑 청산')
                            .replace(/STOP_LOSS/g, '손절')
                            .replace(/PARTIAL_TP/g, '1차 분할익절')
                            .replace(/PANIC_SELL/g, '긴급 전량매도');
                    }}

                    // Positions
                    const tbody = document.getElementById('positions_tbody');
                    if (data.positions && data.positions.length > 0) {{
                        tbody.innerHTML = data.positions.map(p => {{
                            const actKr = formatAction(p.action);
                            const actBadge = (p.action === 'BUY' || p.action === 'BID') ? 'bg-emerald-500/20 text-emerald-300' : ((p.action === 'SELL' || p.action === 'ASK' || p.action === 'STOP_LOSS') ? 'bg-rose-500/20 text-rose-300' : 'bg-slate-700 text-slate-300');
                            const reasonKr = formatReason(p.reason);
                            return `
                                <tr class="hover:bg-slate-800/60 transition">
                                    <td class="p-3 font-bold text-white whitespace-nowrap">${{p.korean_name}} <span class="text-xs text-slate-400 font-normal">(${{p.market}})</span></td>
                                    <td class="p-3 whitespace-nowrap">${{p.current_price.toLocaleString()}} 원</td>
                                    <td class="p-3 whitespace-nowrap">${{p.balance}}개 <span class="text-xs text-slate-400">(${{p.value.toLocaleString()}}원)</span></td>
                                    <td class="p-3 font-bold whitespace-nowrap ${{p.pnl_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}}">${{p.pnl_pct >= 0 ? '+' : ''}}${{p.pnl_pct.toFixed(2)}}%</td>
                                    <td class="p-3 whitespace-nowrap"><span class="badge ${{actBadge}}">${{actKr}}</span></td>
                                    <td class="p-3 text-xs text-slate-300 whitespace-nowrap">목표: ${{p.target_price > 0 ? p.target_price.toLocaleString() + '원' : '-'}}<br>손절: ${{p.stop_loss > 0 ? p.stop_loss.toLocaleString() + '원' : '-'}}</td>
                                    <td class="p-3 text-xs text-slate-400 max-w-xs truncate">${{reasonKr}}</td>
                                </tr>
                            `;
                        }}).join('');
                    }} else {{
                        tbody.innerHTML = '<tr><td colspan="7" class="p-6 text-center text-slate-500">현재 보유 중인 코인이 없습니다 (100% 현금 보유 관망 중).</td></tr>';
                    }}

                    // Recent Completed Trades
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

                    // Recent Orders
                    const ordersTbody = document.getElementById('orders_tbody');
                    if (data.recent_orders && data.recent_orders.length > 0) {{
                        ordersTbody.innerHTML = data.recent_orders.map(o => {{
                            const rawStatus = (o.status || '').toUpperCase();
                            const isFailed = (rawStatus === 'FAILED' || rawStatus === 'FAIL' || rawStatus === 'ERROR' || rawStatus === 'REJECTED' || rawStatus === 'UNKNOWN');
                            const statusColor = (rawStatus === 'FILLED' || rawStatus === 'DONE') ? 'bg-emerald-500/20 text-emerald-300' : ((rawStatus === 'PARTIALLY_FILLED') ? 'bg-amber-500/20 text-amber-300' : (isFailed ? 'bg-rose-500/20 text-rose-300' : 'bg-slate-700 text-slate-300'));
                            const statusKr = formatOrderStatus(o.status);
                            const sideKr = (o.side === 'bid' || o.side === 'BUY' || o.side === 'buy') ? '매수' : '매도';
                            const coinDisp = o.korean_name ? (o.korean_name + ' <span class="text-xs text-slate-400 font-normal">(' + o.market + ')</span>') : o.market;
                            return `
                                <tr class="hover:bg-slate-800/40 font-mono text-xs">
                                    <td class="p-2.5 text-slate-400 truncate max-w-[120px] whitespace-nowrap">${{o.client_order_id}}</td>
                                    <td class="p-2.5 font-semibold text-white whitespace-nowrap">${{coinDisp}}</td>
                                    <td class="p-2.5 whitespace-nowrap ${{(o.side === 'bid' || o.side === 'BUY' || o.side === 'buy') ? 'text-emerald-400' : 'text-rose-400'}}">${{sideKr}}</td>
                                    <td class="p-2.5 whitespace-nowrap"><span class="badge ${{statusColor}}">${{statusKr}}</span></td>
                                    <td class="p-2.5 text-slate-400 truncate max-w-[120px] whitespace-nowrap">${{o.exchange_uuid || o.exchange_order_id || '-'}}</td>
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
