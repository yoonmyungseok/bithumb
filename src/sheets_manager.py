import logging
import os
from typing import Any, Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)


class SheetsManager:
    """
    Google 스프레드시트 연동 모듈 (gspread) v2.0
    - Dashboard 탭: 종합 자산, 당일 손익률, 킬스위치/BTC방어선 실시간 대시보드
    - Strategy 탭: 한글 표준 헤더 및 다중 마켓 실시간 전략 동기화
    - Trade_Log 탭: 한글 표준 헤더 및 실현 손익률(%) 자동 기록
    """

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(self, json_key_path: str, sheet_name: str):
        if not os.path.exists(json_key_path):
            raise FileNotFoundError(
                f"구글 서비스 계정 키 파일을 찾을 수 없습니다: {json_key_path}"
            )

        self.credentials = Credentials.from_service_account_file(
            json_key_path, scopes=self.SCOPES
        )
        self.client = gspread.authorize(self.credentials)
        self.sheet_name = sheet_name

        service_email = getattr(self.credentials, "service_account_email", "알 수 없음")

        try:
            if sheet_name.startswith("https://"):
                self.spreadsheet = self.client.open_by_url(sheet_name)
            elif "/" in sheet_name or len(sheet_name) > 30 and " " not in sheet_name:
                try:
                    self.spreadsheet = self.client.open_by_key(sheet_name)
                except Exception:
                    self.spreadsheet = self.client.open(sheet_name)
            else:
                self.spreadsheet = self.client.open(sheet_name)
            logger.info(f"구글 스프레드시트 '{self.spreadsheet.title}' 연결 성공")
        except gspread.exceptions.SpreadsheetNotFound as e:
            logger.error(
                f"\n[구글 시트 연동 실패] '{sheet_name}' 시트를 찾을 수 없습니다.\n"
                f"▶ 해결 방법: 구글 시트 우측 상단 [공유] ➜ 다음 이메일을 '편집자'로 추가해주세요:\n"
                f"   👉 {service_email}\n"
            )
            raise e

    def update_dashboard(self, summary_data: Dict[str, Any]) -> None:
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
            held_coins_str = summary_data.get("held_coins", "없음 (100% 현금)")
            kill_switch_str = summary_data.get("kill_switch_status", "🟢 정상")
            btc_health_str = summary_data.get("btc_health", "🟢 정상")

            dashboard_rows = [
                ["📊 [빗썸 AI 퀀트 자동매매 실시간 종합 대시보드]", "", "", ""],
                ["최종 갱신 일시 (KST)", now_str, "봇 상태", "🟢 정상 가동 중 (5분 주기)"],
                ["", "", "", ""],
                ["📌 [핵심 계좌 자산 현황]", "", "🛡️ [리스크 관리 안전장치 상태]", ""],
                ["총 평가 자산 (KRW)", f"{int(total_equity):,} 원", "일일 킬스위치 상태", kill_switch_str],
                ["가용 원화 잔고 (KRW)", f"{int(krw_avail):,} 원", "BTC 대세 급락 방어선", btc_health_str],
                ["금일 누적 실현 손익", f"{int(daily_pnl_krw):+,} 원 ({daily_pnl_pct:+.2f}%)", "트레일링 스탑 모드", "🎯 활성화 (+2.0% 추적)"],
                ["현재 보유 포지션", held_coins_str, "일일 최대 손실 한도", "-5.0%"],
            ]

            worksheet.update(values=dashboard_rows, range_name="A1:D8")
            logger.info("구글 시트 'Dashboard' 탭 실시간 갱신 완료")

        except Exception as e:
            logger.warning(f"Dashboard 탭 갱신 실패 (매매는 지속됨): {e}")

    def get_strategy(self, market: str = "KRW-BTC") -> Dict[str, Any]:
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
                if row and str(row[0]).strip().upper() == market.upper():
                    target_row = row
                    break

            if not target_row:
                if len(all_rows) > 1:
                    target_row = all_rows[1]
                else:
                    return {"status": "PAUSE", "action": "HOLD"}

            offset = 1 if str(target_row[0]).startswith("KRW-") or str(target_row[0]).startswith("BTC-") else 0

            status = str(target_row[offset + 1]).strip().upper() if len(target_row) > offset + 1 else "PAUSE"
            action = str(target_row[offset + 2]).strip().upper() if len(target_row) > offset + 2 else "HOLD"
            entry_price = safe_float(target_row[offset + 3]) if len(target_row) > offset + 3 else 0.0
            target_price = safe_float(target_row[offset + 4]) if len(target_row) > offset + 4 else 0.0
            stop_loss = safe_float(target_row[offset + 5]) if len(target_row) > offset + 5 else 0.0
            alloc_raw = safe_float(target_row[offset + 6]) if len(target_row) > offset + 6 else 30.0

            alloc_pct = alloc_raw / 100.0 if alloc_raw > 1.0 else alloc_raw
            alloc_pct = max(0.05, min(alloc_pct, 1.0))
            reason = str(target_row[offset + 7]) if len(target_row) > offset + 7 else "Strategy 시트 지침"

            return {
                "status": status,
                "action": action,
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss": stop_loss,
                "alloc_pct": alloc_pct,
                "reason": reason,
            }

        except Exception as e:
            logger.error(f"Strategy 시트 조회 오류 ({market}): {e}")
            return {"status": "PAUSE", "action": "HOLD", "reason": str(e)}

    def update_strategy(
        self, market: str, strategy: Dict[str, Any], timestamp_str: str
    ) -> None:
        """
        'Strategy' 탭에 한글 표준 헤더 및 마켓별 실시간 전략 기록
        """
        try:
            try:
                worksheet = self.spreadsheet.worksheet("Strategy")
            except gspread.exceptions.WorksheetNotFound:
                worksheet = self.spreadsheet.add_worksheet(title="Strategy", rows=50, cols=10)

            all_rows = worksheet.get_all_values()

            # 한글 표준 헤더
            korean_headers = [
                "마켓(종목)",
                "업데이트일시",
                "봇상태",
                "매매판단",
                "진입/현재가(KRW)",
                "목표익절가(KRW)",
                "손절기준가(KRW)",
                "투자비중",
                "AI 퀀트 분석근거",
            ]

            if not all_rows:
                worksheet.append_row(korean_headers)
                all_rows = [korean_headers]
            elif all_rows and ("MARKET" in str(all_rows[0][0]).upper() or not all_rows[0][0]):
                worksheet.update(values=[korean_headers], range_name="A1:I1")
                all_rows[0] = korean_headers

            def format_price(p: Any) -> str:
                try:
                    val = float(str(p).replace(",", "").replace("%", "").replace("원", "").replace("KRW", "").strip())
                    if val <= 0:
                        return "0원"
                    return f"{int(val):,}원" if val >= 100 else f"{val:,.2f}원"
                except Exception:
                    return f"{p}원" if p else "0원"

            entry_p = strategy.get("entry_price", 0)
            target_p = strategy.get("target_price", 0)
            stop_l = strategy.get("stop_loss", 0)

            row_data = [
                market,
                timestamp_str,
                strategy.get("status", "ACTIVE"),
                strategy.get("action", "HOLD"),
                format_price(entry_p),
                format_price(target_p),
                format_price(stop_l),
                f"{strategy.get('alloc_pct', 0.3) * 100:.0f}%",
                strategy.get("reason", "Gemini 자동 분석"),
            ]

            target_row_num = -1
            for idx, row in enumerate(all_rows):
                if row and str(row[0]).strip().upper() == market.upper():
                    target_row_num = idx + 1
                    break

            if target_row_num != -1:
                worksheet.update(values=[row_data], range_name=f"A{target_row_num}:I{target_row_num}")
                logger.info(f"구글 시트 Strategy [{market}] (행 {target_row_num}) 갱신 완료")
            else:
                worksheet.append_row(row_data)
                logger.info(f"구글 시트 Strategy [{market}] 신규 행 추가 완료")

        except Exception as e:
            logger.warning(f"Strategy 탭 업데이트 실패: {e}")

    def append_trade_log(self, data: Any) -> None:
        """
        'Trade_Log' 탭에 한글 표준 헤더 및 실현 손익률(%) 스마트 기록 (자리수 콤마 및 '원' 단위 적용)
        """
        try:
            try:
                worksheet = self.spreadsheet.worksheet("Trade_Log")
            except gspread.exceptions.WorksheetNotFound:
                worksheet = self.spreadsheet.add_worksheet(title="Trade_Log", rows=1000, cols=13)

            headers = worksheet.row_values(1)
            korean_headers = [
                "주문일시",
                "마켓(종목)",
                "주문구분",
                "주문유형",
                "주문/체결단가",
                "수량",
                "총거래금액(KRW)",
                "실현손익률(%)",
                "손절기준가",
                "목표익절가",
                "거래후원화잔고",
                "주문ID",
                "분석및체결사유",
            ]

            if not headers:
                worksheet.append_row(korean_headers)
                headers = korean_headers

            def format_val(key: str, val: Any) -> str:
                if val is None or val == "":
                    return ""
                price_keys = ["price", "total_krw", "stop_loss", "target_price", "current_balance_krw"]
                if any(pk in key.lower() for pk in price_keys):
                    try:
                        num = float(str(val).replace(",", "").replace("%", "").replace("원", "").replace("KRW", "").strip())
                        return f"{int(num):,}원" if num >= 100 else f"{num:,.2f}원"
                    except Exception:
                        return f"{val}원"
                return str(val)

            if isinstance(data, dict):
                key_aliases = {
                    "timestamp": ["timestamp", "주문일시", "일시", "시간", "날짜", "updated_at"],
                    "market": ["market", "마켓(종목)", "종목", "마켓", "코인", "symbol"],
                    "side": ["side", "주문구분", "action", "구분", "매매"],
                    "order_type": ["order_type", "주문유형", "type", "유형"],
                    "price": ["price", "주문/체결단가", "체결단가", "주문단가", "가격", "order_price"],
                    "volume": ["volume", "수량", "체결수량", "amount"],
                    "total_krw": ["total_krw", "총거래금액(krw)", "총금액", "주문금액"],
                    "realized_pnl_pct": ["realized_pnl_pct", "실현손익률(%)", "손익률", "수익률", "pnl_pct"],
                    "stop_loss": ["stop_loss", "손절기준가", "손절가", "손절"],
                    "target_price": ["target_price", "목표익절가", "목표가", "익절가"],
                    "current_balance_krw": ["current_balance_krw", "거래후원화잔고", "원화잔고", "가용원화", "잔고"],
                    "order_uuid": ["order_uuid", "주문id", "주문고유id", "uuid", "order_id"],
                    "status_reason": ["status_reason", "분석및체결사유", "체결사유", "비고", "reason", "메모"],
                }

                lower_data = {str(k).lower(): v for k, v in data.items()}

                row_to_insert = []
                for h in headers:
                    clean_h = str(h).strip().lower()
                    matched_value = ""

                    if clean_h in lower_data:
                        matched_value = format_val(clean_h, lower_data[clean_h])
                    else:
                        for std_key, aliases in key_aliases.items():
                            if clean_h in aliases:
                                for alias in aliases:
                                    if alias in lower_data:
                                        matched_value = format_val(std_key, lower_data[alias])
                                        break
                                if matched_value != "":
                                    break

                    row_to_insert.append(matched_value)

                worksheet.append_row(row_to_insert)
                logger.info(f"Trade_Log 기록 완료: {row_to_insert}")

            elif isinstance(data, list):
                worksheet.append_row(data)
                logger.info(f"Trade_Log 단순 리스트 기록 완료: {data}")

        except Exception as e:
            logger.error(f"Trade_Log 추가 실패: {e}")
            raise
