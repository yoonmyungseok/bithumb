import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)


class DashboardWebServer:
    """
    로컬 경량 실시간 웹 대시보드 서버 (포트 7979)
    - http://localhost:7979 에서 계좌 종합 현황, 공포탐욕지수, 보유 포지션, 체결 내역 모니터링
    - 웹 UI에서 텔레그램 긴급 매도 / 일시정지 / 재개 버튼 직접 원격 제어
    - 외부 무거운 프레임워크 없이 파이썬 표준 라이브러리로 100% 안정 가동
    """

    def __init__(
        self,
        port: int = 7979,
        get_status_data_func: Callable[[], dict[str, Any]] | None = None,
        action_handler_func: Callable[[str], str] | None = None,
    ):
        self.port = port
        self.get_status_data = get_status_data_func
        self.action_handler = action_handler_func
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        handler_cls = self._create_handler()
        try:
            self.server = ThreadingHTTPServer(("0.0.0.0", self.port), handler_cls)
            self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="WebDashboard")
            self._thread.start()
            logger.info(f"🌐 [로컬 웹 대시보드 가동] 접속 주소: http://localhost:{self.port}")
        except OSError as e:
            logger.warning(f"웹 대시보드 포트 {self.port} 바인딩 실패: {e}")

    def _create_handler(self):
        server_self = self

        class DashboardHandler(BaseHTTPRequestHandler):
            def log_message(self, format_str, *args):
                pass  # 콘솔 노이즈 방지

            def do_GET(self):
                self.close_connection = True
                if self.path == "/api/status":
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

            def do_POST(self):
                self.close_connection = True
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

        return DashboardHandler

    def _render_html(self) -> str:
        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>빗썸 AI 퀀트 자동매매 v3.5 대시보드</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body {{ background-color: #0b0e14; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        .card {{ background-color: #151923; border: 1px solid #232a3b; border-radius: 12px; }}
        .badge {{ padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 0.85rem; }}
        .glow {{ box-shadow: 0 0 15px rgba(0, 230, 118, 0.2); }}
    </style>
</head>
<body class="p-6">
    <div class="max-w-7xl mx-auto space-y-6">
        <!-- Header -->
        <div class="flex flex-wrap justify-between items-center bg-slate-900/80 p-5 rounded-2xl border border-slate-800 backdrop-blur">
            <div>
                <div class="flex items-center space-x-3">
                    <span class="text-3xl">🚀</span>
                    <div>
                        <h1 class="text-2xl font-black bg-gradient-to-r from-blue-400 to-emerald-400 bg-clip-text text-transparent">
                            Bithumb AI 퀀트 자동매매 Pro v3.5
                        </h1>
                        <p class="text-xs text-slate-400">포트: 7979 | 0.1초 실시간 웹소켓 스트리밍 & MTF 1시간봉 분석</p>
                    </div>
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
                <span class="mr-2">📊</span> 현재 보유 포지션 및 AI 퀀트 전략
            </h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm text-slate-300">
                    <thead class="bg-slate-800/60 text-slate-400 uppercase text-xs">
                        <tr>
                            <th class="p-3">종목명 (Market)</th>
                            <th class="p-3">현재가 (KRW)</th>
                            <th class="p-3">보유 수량 / 평가액</th>
                            <th class="p-3">수익률</th>
                            <th class="p-3">AI 행동 (Action)</th>
                            <th class="p-3">목표가 / 손절가</th>
                            <th class="p-3">AI 분석 근거</th>
                        </tr>
                    </thead>
                    <tbody id="positions_tbody" class="divide-y divide-slate-800">
                        <tr><td colspan="7" class="p-4 text-center text-slate-500">데이터 로딩 중...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div class="text-center text-xs text-slate-500 py-2">
            빗썸 API 2.0 AI 퀀트 시스템 v3.5 | 5초마다 자동 실시간 동기화 중 | 포트: {self.port}
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

                    // Update positions table
                    const tbody = document.getElementById('positions_tbody');
                    if (data.positions && data.positions.length > 0) {{
                        tbody.innerHTML = data.positions.map(p => `
                            <tr class="hover:bg-slate-800/40 transition">
                                <td class="p-3 font-bold text-white">${{p.korean_name}} <span class="text-xs text-slate-400 font-normal">(${{p.market}})</span></td>
                                <td class="p-3">${{p.current_price.toLocaleString()}} 원</td>
                                <td class="p-3">${{p.balance}}개 <span class="text-xs text-slate-400">(${{p.value.toLocaleString()}}원)</span></td>
                                <td class="p-3 font-bold ${{p.pnl_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}}">${{p.pnl_pct >= 0 ? '+' : ''}}${{p.pnl_pct.toFixed(2)}}%</td>
                                <td class="p-3"><span class="badge ${{p.action === 'BUY' ? 'bg-emerald-500/20 text-emerald-300' : (p.action === 'SELL' ? 'bg-rose-500/20 text-rose-300' : 'bg-slate-700 text-slate-300')}}">${{p.action}}</span></td>
                                <td class="p-3 text-xs text-slate-300">목표: ${{p.target_price > 0 ? p.target_price.toLocaleString() + '원' : '-'}}<br>손절: ${{p.stop_loss > 0 ? p.stop_loss.toLocaleString() + '원' : '-'}}</td>
                                <td class="p-3 text-xs text-slate-400 max-w-xs truncate">${{p.reason || '-'}}</td>
                            </tr>
                        `).join('');
                    }} else {{
                        tbody.innerHTML = '<tr><td colspan="7" class="p-6 text-center text-slate-500">현재 보유 중인 코인이 없습니다 (100% 현금 보유 관망 중).</td></tr>';
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

        fetchStatus();
        setInterval(fetchStatus, 5000);
    </script>
</body>
</html>"""
