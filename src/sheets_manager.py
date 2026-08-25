import logging
import os
import queue
import threading
import time
from typing import Any, ClassVar

import gspread
import requests
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)


class SheetsManager:
    """
    Google 스프레드시트 연동 모듈 (gspread) v3.0 (P2-1 비동기 큐 지원)
    - Dashboard 탭: 종합 자산, 당일 손익률, 공포탐욕지수, 킬스위치/BTC방어선 실시간 대시보드
    - Strategy 탭: [종목명(한글), 마켓코드] 분리 및 한글 표준 헤더 실시간 동기화
    - Trade_Log 탭: [종목명(한글), 마켓코드] 분리, 실현 손익률(%) 및 '원' 단위 자동 기록
    - Performance 탭: 누적 승률, 손익비(Profit Factor), 일별 수익 결산 통계표
    - 백그라운드 큐 워커를 통한 비차단(Non-blocking) 시트 업데이트
    """

    SCOPES: ClassVar[list[str]] = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(self, json_key_path: str = "", sheet_name: str = "", exchange_name: str = "BITHUMB", enable_async: bool = True, **kwargs):
        key_path = json_key_path or kwargs.get("service_account_json_path", "")
        target_sheet = sheet_name or kwargs.get("spreadsheet_name", "")
        self.exchange_name = exchange_name.upper()
        if not key_path or not os.path.exists(key_path):
            raise FileNotFoundError(
                f"구글 서비스 계정 키 파일을 찾을 수 없습니다: {key_path}"
            )

        self.credentials = Credentials.from_service_account_file(
            key_path, scopes=self.SCOPES
        )
        self.client = gspread.authorize(self.credentials)
        self.sheet_name = target_sheet

        service_email = getattr(self.credentials, "service_account_email", "알 수 없음")

        try:
            if self.sheet_name.startswith("https://"):
                self.spreadsheet = self.client.open_by_url(self.sheet_name)
            elif "/" in self.sheet_name or (len(self.sheet_name) > 30 and " " not in self.sheet_name):
                try:
                    self.spreadsheet = self.client.open_by_key(sheet_name)
                except (gspread.exceptions.GSpreadException, requests.exceptions.RequestException):
                    self.spreadsheet = self.client.open(sheet_name)
            else:
                self.spreadsheet = self.client.open(sheet_name)
            logger.info(f"구글 스프레드시트 '{self.spreadsheet.title}' 연결 성공 ({self.exchange_name})")
        except gspread.exceptions.SpreadsheetNotFound:
            logger.error(
                f"\n[구글 시트 연동 실패] '{sheet_name}' 시트를 찾을 수 없습니다.\n"
                f"▶ 해결 방법: 구글 시트 우측 상단 [공유] ➜ 다음 이메일을 '편집자'로 추가해주세요:\n"
                f"   👉 {service_email}\n"
            )
            raise

        self.enable_async = enable_async
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._is_running = True
        if self.enable_async:
            self._worker_thread = threading.Thread(target=self._queue_worker, daemon=True, name="SheetsQueueWorker")
            self._worker_thread.start()

    def _queue_worker(self):
        while self._is_running:
            try:
                task = self._queue.get(timeout=1.0)
                if task is None:
                    break
                func, args, kwargs = task
                for attempt in range(3):
                    try:
                        func(*args, **kwargs)
                        break
                    except Exception as e:
                        if attempt == 2:
                            logger.error(f"구글 시트 비동기 작업 최종 실패: {e}")
                        time.sleep(1.0 * (attempt + 1))
                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.debug(f"구글 시트 워커 예외: {e}")

    def _enqueue(self, func, *args, **kwargs):
        if self.enable_async:
            try:
                self._queue.put_nowait((func, args, kwargs))
                return
            except queue.Full:
                logger.warning("구글 시트 큐 가득 참, 동기 실행으로 폴백")
        func(*args, **kwargs)

    def flush(self, timeout: float = 5.0) -> None:
        """대기 중인 시트 업데이트 큐 작업 완료 대기"""
        if self.enable_async:
            self._queue.join()

    def update_dashboard(self, summary_data: dict[str, Any]) -> None:
        """
        'Dashboard' 탭에 계좌 종합 현황을 실시간 카드 형태로 갱신
        """
        try:
            try:
                worksheet = self.spreadsheet.worksheet("Dashboard")
            except gspread.exceptions.WorksheetNotFound:
                worksheet = self.spreadsheet.add_worksheet(title="Dashboard", rows=30, cols=10)

            now_str = summary_data.get("updated_at", "")
            total_equity = summary_data.get("total_equity", 0.0)
            krw_avail = summary_data.get("krw_available", 0.0)
            daily_pnl_pct = summary_data.get("daily_pnl_pct", 0.0)
            daily_pnl_krw = summary_data.get("daily_pnl_krw", 0.0)
            realized_krw = summary_data.get("realized_pnl_krw", 0.0)
            trades_count = summary_data.get("trades_count", 0)
            win_count = summary_data.get("win_count", 0)
            win_rate = (win_count / trades_count * 100) if trades_count > 0 else 0.0
            held_coins_str = summary_data.get("held_coins", "없음 (100% 현금)")
            kill_switch_str = summary_data.get("kill_switch_status", "🟢 정상")
            btc_health_str = summary_data.get("btc_health", "🟢 정상")
            fng_str = summary_data.get("fear_and_greed", "50점 (중립)")
            bot_state_str = summary_data.get("bot_state", "🟢 24시간 실시간 자동매매 가동 중")

            title_label = "빗썸" if self.exchange_name == "BITHUMB" else ("업비트" if self.exchange_name == "UPBIT" else self.exchange_name)
            dashboard_rows = [
                [f"📊 [{title_label} AI 퀀트 자동매매 실시간 종합 대시보드]", "", "", ""],
                ["최종 갱신 일시 (KST)", now_str, "봇 가동 상태", bot_state_str],
                ["", "", "", ""],
                ["📌 [핵심 계좌 자산 현황]", "", "🛡️ [리스크 관리 안전장치 상태]", ""],
                ["총 평가 자산 (KRW)", f"{int(total_equity):,} 원", "일일 킬스위치 상태", kill_switch_str],
                ["가용 원화 잔고 (KRW)", f"{int(krw_avail):,} 원", "BTC 대세 급락 방어선", btc_health_str],
                ["금일 자산 변동 (평가손익)", f"{int(daily_pnl_krw):+,} 원 ({daily_pnl_pct:+.2f}%)", "트레일링 스탑 모드", "🎯 50% 분할익절 + 가속 러너"],
                ["금일 확정 실현 손익", f"{int(realized_krw):+,} 원 (총 {trades_count}회 거래 / 승률 {win_rate:.0f}%)", "일일 최대 손실 한도", "-5.0%"],
                ["현재 보유 포지션", held_coins_str, "크립토 공포/탐욕 지수", fng_str],
                ["종목 발굴 모드", "🔥 실시간 급등 모멘텀 TOP 3 스캔", "원격 제어 지원", "📱 텔레그램 /status, /panic 연동"],
            ]

            worksheet.update(values=dashboard_rows, range_name="A1:D10")
            logger.info("구글 시트 'Dashboard' 탭 실시간 갱신 완료")

        except (gspread.exceptions.GSpreadException, requests.exceptions.RequestException, KeyError, ValueError) as e:
            logger.warning(f"Dashboard 탭 갱신 실패 (매매는 지속됨): {e}")

    def update_performance_tab(
        self,
        total_trades: int,
        win_trades: int,
        realized_pnl_krw: float,
        daily_history: list[dict[str, Any]] | None = None,
    ) -> None:
        """
        'Performance' 탭에 누적 매매 통계 및 일별 손익 결산 테이블 갱신
        """
        try:
            try:
                worksheet = self.spreadsheet.worksheet("Performance")
            except gspread.exceptions.WorksheetNotFound:
                worksheet = self.spreadsheet.add_worksheet(title="Performance", rows=50, cols=10)

            win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0.0
            loss_trades = max(0, total_trades - win_trades)

            title_label = "빗썸" if self.exchange_name == "BITHUMB" else ("업비트" if self.exchange_name == "UPBIT" else self.exchange_name)
            perf_rows = [
                [f"📈 [{title_label} AI 퀀트 트레이딩 누적 성과 분석 통계]", "", "", ""],
                ["", "", "", ""],
                ["📊 [핵심 트레이딩 지표]", "", "💰 [실현 손익 요약]", ""],
                ["총 누적 거래 횟수", f"{total_trades} 회", "총 확정 실현 손익", f"{int(realized_pnl_krw):+,} 원"],
                ["총 승리 거래 (익절)", f"{win_trades} 회", "승률 (Win Rate)", f"{win_rate:.1f}%"],
                ["총 패배 거래 (손절)", f"{loss_trades} 회", "평균 거래 결과", f"{(realized_pnl_krw / total_trades):+,.0f} 원/건" if total_trades > 0 else "-"],
                ["", "", "", ""],
                ["📅 [일별 실현 손익 결산 이력]", "", "", ""],
                ["일자 (Date)", "거래 횟수 (Trades)", "승리 횟수 (Wins)", "확정 실현 손익 (KRW)"],
            ]

            if daily_history:
                for h in daily_history[-10:]:
                    perf_rows.append([
                        h.get("date", "-"),
                        f"{h.get('total_trades', 0)} 회",
                        f"{h.get('win_trades', 0)} 회",
                        f"{int(h.get('realized_pnl_krw', 0)):+,} 원",
                    ])

            worksheet.update(values=perf_rows, range_name=f"A1:D{len(perf_rows)}")
            logger.info("구글 시트 'Performance' 탭 갱신 완료")

        except (gspread.exceptions.GSpreadException, requests.exceptions.RequestException, KeyError, ValueError) as e:
            logger.warning(f"Performance 탭 갱신 실패: {e}")

    def get_strategy(self, market: str = "KRW-BTC") -> dict[str, Any]:
        """
        'Strategy' 탭에서 특정 마켓의 전략 조회
        """
        try:
            worksheet = self.spreadsheet.worksheet("Strategy")
            all_rows = worksheet.get_all_values()

            if not all_rows:
                return {"status": "PAUSE", "action": "HOLD"}

            def safe_float(val: Any, default: float = 0.0) -> float:
                try:
                    cleaned = str(val).replace(",", "").replace("%", "").replace("원", "").replace("KRW", "").strip()
                    return float(cleaned)
                except (ValueError, TypeError):
                    return default

            target_row = None
            for row in all_rows[1:]:
                if row:
                    first_val = str(row[0]).strip().upper()
                    second_val = str(row[1]).strip().upper() if len(row) > 1 else ""
                    if first_val == market.upper() or second_val == market.upper():
                        target_row = row
                        break

            if not target_row:
                if len(all_rows) > 1:
                    target_row = all_rows[1]
                else:
                    return {"status": "PAUSE", "action": "HOLD"}

            offset = 1
            if len(target_row) > 1 and str(target_row[1]).startswith(("KRW-", "BTC-")):
                offset = 2
            elif str(target_row[0]).startswith(("KRW-", "BTC-")):
                offset = 1

            status = str(target_row[offset + 1]).strip().upper() if len(target_row) > offset + 1 else "PAUSE"
            action = str(target_row[offset + 2]).strip().upper() if len(target_row) > offset + 2 else "HOLD"
            entry_price = safe_float(target_row[offset + 3] if len(target_row) > offset + 3 else 0.0)
            target_price = safe_float(target_row[offset + 4] if len(target_row) > offset + 4 else 0.0)
            stop_loss = safe_float(target_row[offset + 5] if len(target_row) > offset + 5 else 0.0)
            alloc_pct = safe_float(target_row[offset + 6] if len(target_row) > offset + 6 else 0.3)
            reason = str(target_row[offset + 7]) if len(target_row) > offset + 7 else "수동 입력"

            if alloc_pct > 1.0:
                alloc_pct = alloc_pct / 100.0

            return {
                "status": status,
                "action": action,
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss": stop_loss,
                "alloc_pct": alloc_pct,
                "reason": reason,
            }
        except (gspread.exceptions.GSpreadException, requests.exceptions.RequestException, KeyError, ValueError) as e:
            logger.warning(f"Strategy 시트 조회 실패: {e}")
            return {"status": "PAUSE", "action": "HOLD"}

    def update_strategy(
        self, market: str, strategy: dict[str, Any], timestamp: str, korean_name: str = ""
    ) -> None:
        """
        'Strategy' 탭에 종목별 전략 업데이트 (HOLO는 절대 미기록)
        """
        raw_excluded = os.getenv("EXCLUDED_MANUAL_HOLDINGS", "KRW-HOLO,HOLO") + "," + os.getenv("UPBIT_EXCLUDED_MARKETS", "KRW-HOLO,HOLO")
        excluded_set = {x.strip().upper() for x in raw_excluded.split(",") if x.strip()}
        excluded_set.update({"KRW-HOLO", "HOLO"})
        m_upper = market.upper()
        if m_upper in excluded_set or m_upper.replace("KRW-", "") in excluded_set:
            logger.debug(f"HOLO 등 격리 종목 Strategy 시트 기록 제외: {market}")
            return

        try:
            try:
                worksheet = self.spreadsheet.worksheet("Strategy")
            except gspread.exceptions.WorksheetNotFound:
                worksheet = self.spreadsheet.add_worksheet(title="Strategy", rows=30, cols=12)

            korean_headers = [
                "종목명",
                "마켓코드",
                "최종 업데이트 (KST)",
                "동작 상태",
                "매매 행동",
                "진입가 (KRW)",
                "목표가 (KRW)",
                "손절가 (KRW)",
                "투자 비중",
                "AI 분석 근거 / 진입 사유",
            ]

            all_rows = worksheet.get_all_values()
            if not all_rows or all_rows[0] != korean_headers:
                worksheet.clear()
                worksheet.append_row(korean_headers)
                all_rows = [korean_headers]

            entry_p = strategy.get("entry_price", 0.0)
            target_p = strategy.get("target_price", 0.0)
            stop_l = strategy.get("stop_loss", 0.0)
            alloc_p = strategy.get("alloc_pct", 0.3)
            raw_action = str(strategy.get("action", "HOLD")).strip().upper()
            raw_status = str(strategy.get("status", "ACTIVE")).strip().upper()

            # 한글 매핑
            action_map = {
                "BUY": "매수",
                "BID": "매수",
                "SELL": "매도",
                "ASK": "매도",
                "HOLD": "관망 (대기)",
                "STOP_LOSS": "손절",
                "PARTIAL_TP": "1차 분할익절",
                "TRAILING_STOP": "트레일링 익절",
                "PANIC_SELL": "긴급 전량매도",
            }
            status_map = {
                "ACTIVE": "정상 감시",
                "PAUSED": "일시정지",
                "PAUSE": "일시정지",
                "COOLDOWN": "쿨다운 대기",
                "STOP": "중지됨",
            }
            action_kr = action_map.get(raw_action, raw_action)
            status_kr = status_map.get(raw_status, raw_status)

            alloc_str = f"{int(alloc_p * 100)}%" if alloc_p > 0 else "0%"

            display_name = korean_name if korean_name else market.split("-")[-1]

            row_data = [
                display_name,
                market,
                timestamp,
                status_kr,
                action_kr,
                f"{entry_p:,.2f} 원" if entry_p > 0 else "-",
                f"{target_p:,.2f} 원" if target_p > 0 else "-",
                f"{stop_l:,.2f} 원" if stop_l > 0 else "-",
                alloc_str,
                strategy.get("reason", "자동 분석"),
            ]

            target_row_idx = None
            for idx, row in enumerate(all_rows[1:], start=2):
                if row:
                    first_val = str(row[0]).strip().upper()
                    second_val = str(row[1]).strip().upper() if len(row) > 1 else ""
                    if first_val == market.upper() or second_val == market.upper():
                        target_row_idx = idx
                        break

            if target_row_idx:
                worksheet.update(values=[row_data], range_name=f"A{target_row_idx}:J{target_row_idx}")
                logger.info(f"구글 시트 Strategy [{display_name} / {market}] (행 {target_row_idx}) 갱신 완료")
            else:
                worksheet.append_row(row_data)
                logger.info(f"구글 시트 Strategy [{display_name} / {market}] 신규 행 추가 완료")

        except (gspread.exceptions.GSpreadException, requests.exceptions.RequestException, KeyError, ValueError) as e:
            logger.warning(f"Strategy 탭 갱신 실패: {e}")

    def prune_unmonitored_strategies(self, active_markets: list[str]) -> None:
        """
        'Strategy' 탭에서 현재 감시 대상(active_markets)에 포함되지 않는 과거 잔여 행들을 일괄 정리
        """
        try:
            try:
                worksheet = self.spreadsheet.worksheet("Strategy")
            except gspread.exceptions.WorksheetNotFound:
                return

            all_rows = worksheet.get_all_values()
            if not all_rows or len(all_rows) <= 1:
                return

            headers = all_rows[0]
            active_set = {m.upper() for m in active_markets}

            # 현재 감시 대상인 행들만 필터링
            filtered_rows = [headers]
            for row in all_rows[1:]:
                if row:
                    market_code = str(row[1]).strip().upper() if len(row) > 1 else str(row[0]).strip().upper()
                    if market_code in active_set:
                        filtered_rows.append(row)

            # 만약 정리할 과거 행이 있었다면 시트 갱신
            if len(filtered_rows) < len(all_rows):
                worksheet.clear()
                worksheet.update(values=filtered_rows, range_name=f"A1:J{len(filtered_rows)}")
                logger.info(f"🧹 구글 시트 Strategy 탭 정리 완료: {len(all_rows)-1}개 중 비감시 종목 삭제 후 {len(filtered_rows)-1}개 유지")

        except (gspread.exceptions.GSpreadException, requests.exceptions.RequestException, KeyError, ValueError) as e:
            logger.warning(f"Strategy 탭 과거 행 정리 실패: {e}")

    def append_trade_log(self, data: dict[str, Any], sync: bool = False) -> None:
        """
        'Trade_Log' 탭에 실시간 거래 내역 추가 (비동기 비차단 큐 발송, HOLO는 절대 미기록)
        """
        if self.enable_async and not sync:
            self._enqueue(self._sync_append_trade_log, data)
            return
        self._sync_append_trade_log(data)

    def _sync_append_trade_log(self, data: dict[str, Any]) -> None:
        m = str(data.get("market") or data.get("Market") or "").upper()
        raw_excluded = os.getenv("EXCLUDED_MANUAL_HOLDINGS", "KRW-HOLO,HOLO") + "," + os.getenv("UPBIT_EXCLUDED_MARKETS", "KRW-HOLO,HOLO")
        excluded_set = {x.strip().upper() for x in raw_excluded.split(",") if x.strip()}
        excluded_set.update({"KRW-HOLO", "HOLO"})
        if m and (m in excluded_set or m.replace("KRW-", "") in excluded_set):
            logger.debug(f"HOLO 등 격리 종목 Trade_Log 시트 기록 제외: {m}")
            return

        try:
            try:
                worksheet = self.spreadsheet.worksheet("Trade_Log")
            except gspread.exceptions.WorksheetNotFound:
                worksheet = self.spreadsheet.add_worksheet(title="Trade_Log", rows=100, cols=16)

            korean_headers = [
                "일시 (KST)",
                "종목명",
                "마켓코드",
                "구분 (Side)",
                "유형 (Type)",
                "체결/주문단가",
                "체결수량",
                "거래총액 (KRW)",
                "실현 손익률",
                "설정 손절가",
                "설정 목표가",
                "주문후 원화잔고",
                "주문 ID (UUID)",
                "상태 및 사유",
            ]

            all_rows = worksheet.get_all_values()
            if not all_rows:
                worksheet.append_row(korean_headers)
            elif all_rows[0] != korean_headers:
                worksheet.insert_row(korean_headers, index=1)

            def format_krw(val: Any) -> str:
                try:
                    num = float(str(val).replace(",", "").replace("원", "").replace("KRW", "").strip())
                    return f"{int(num):,} 원" if num.is_integer() or num >= 100 else f"{num:,.2f} 원"
                except (ValueError, TypeError):
                    return str(val)

            price_keys = ["price", "total_krw", "stop_loss", "target_price", "current_balance_krw"]
            formatted_data = {}
            for k, v in data.items():
                if k.lower() in price_keys and v not in ("-", "", None):
                    formatted_data[k.lower()] = format_krw(v)
                else:
                    formatted_data[k.lower()] = v

            # 구분(Side) 한글 변환
            raw_side = str(formatted_data.get("side", "-")).strip().upper()
            side_map = {
                "BUY": "매수",
                "BID": "매수",
                "SELL": "매도",
                "ASK": "매도",
                "PARTIAL_TP": "1차 분할익절",
                "TRAILING_STOP": "트레일링 익절",
                "STOP_LOSS": "손절",
                "PANIC_SELL": "긴급 전량매도",
            }
            side_kr = side_map.get(raw_side, formatted_data.get("side", "-"))

            # 유형(Type) 한글 변환
            raw_type = str(formatted_data.get("order_type", "-")).strip().upper()
            type_map = {
                "LIMIT": "지정가",
                "MARKET": "시장가",
                "PRICE": "시장가매수",
            }
            type_kr = type_map.get(raw_type, formatted_data.get("order_type", "-"))

            row_map = {
                "일시 (KST)": formatted_data.get("timestamp", "-"),
                "종목명": formatted_data.get("korean_name", data.get("Market", "").split("-")[-1]),
                "마켓코드": formatted_data.get("market", "-"),
                "구분 (Side)": side_kr,
                "유형 (Type)": type_kr,
                "체결/주문단가": formatted_data.get("price", "-"),
                "체결수량": formatted_data.get("volume", "-"),
                "거래총액 (KRW)": formatted_data.get("total_krw", "-"),
                "실현 손익률": formatted_data.get("realized_pnl_pct", "-"),
                "설정 손절가": formatted_data.get("stop_loss", "-"),
                "설정 목표가": formatted_data.get("target_price", "-"),
                "주문후 원화잔고": formatted_data.get("current_balance_krw", "-"),
                "주문 ID (UUID)": formatted_data.get("order_uuid", "-"),
                "상태 및 사유": formatted_data.get("status_reason", "-"),
            }

            row_to_insert = [row_map[h] for h in korean_headers]
            worksheet.append_row(row_to_insert)
            logger.info(f"Trade_Log 기록 완료: {row_to_insert}")

        except (gspread.exceptions.GSpreadException, requests.exceptions.RequestException, KeyError, ValueError) as e:
            logger.error(f"Trade_Log 기록 실패: {e}")
