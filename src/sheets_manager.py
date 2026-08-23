import logging
import os
from typing import Any, Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)


class SheetsManager:
    """
    Google 스프레드시트 연동 모듈 (gspread)
    - Strategy 시트에서 다중 마켓 전략(지침) 로드 및 자동 갱신
    - Trade_Log 시트에 체결/주문 기록 스마트 매핑 추가
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

        # 서비스 계정 이메일 확인
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
                f"▶ 원인: 서비스 계정이 해당 스프레드시트에 공유(초대)되지 않았거나 시트 이름이 다릅니다.\n"
                f"▶ 해결 방법: 구글 시트 우측 상단 [공유] 버튼 클릭 ➜ 다음 이메일을 '편집자'로 추가해주세요:\n"
                f"   👉 {service_email}\n"
            )
            raise e

    def get_strategy(self, market: str = "KRW-BTC") -> Dict[str, Any]:
        """
        'Strategy' 탭에서 특정 마켓의 전략 딕셔너리로 반환
        """
        try:
            worksheet = self.spreadsheet.worksheet("Strategy")
            all_rows = worksheet.get_all_values()

            if not all_rows:
                logger.warning("Strategy 탭이 비어 있습니다.")
                return {"status": "PAUSE", "action": "HOLD"}

            def safe_float(val: Any, default: float = 0.0) -> float:
                try:
                    cleaned = str(val).replace(",", "").replace("%", "").strip()
                    return float(cleaned)
                except (ValueError, TypeError):
                    return default

            # 마켓 일치 행 탐색
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

            # 컬럼 순서: [0: MARKET, 1: UPDATED_AT, 2: STATUS, 3: ACTION, 4: ENTRY_PRICE, 5: TARGET_PRICE, 6: STOP_LOSS, 7: ALLOC_PCT, 8: REASON]
            # 만약 0번째가 MARKET이 아니고 바로 UPDATED_AT인 경우 오프셋 처리
            offset = 1 if str(target_row[0]).startswith("KRW-") or str(target_row[0]).startswith("BTC-") else 0

            status = str(target_row[offset + 1]).strip().upper() if len(target_row) > offset + 1 else "PAUSE"
            action = str(target_row[offset + 2]).strip().upper() if len(target_row) > offset + 2 else "HOLD"
            entry_price = safe_float(target_row[offset + 3]) if len(target_row) > offset + 3 else 0.0
            target_price = safe_float(target_row[offset + 4]) if len(target_row) > offset + 4 else 0.0
            stop_loss = safe_float(target_row[offset + 5]) if len(target_row) > offset + 5 else 0.0
            alloc_raw = safe_float(target_row[offset + 6]) if len(target_row) > offset + 6 else 30.0

            alloc_pct = alloc_raw / 100.0 if alloc_raw > 1.0 else alloc_raw
            alloc_pct = max(0.0, min(alloc_pct, 1.0))

            return {
                "status": status,
                "action": action,
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss": stop_loss,
                "alloc_pct": alloc_pct,
                "reason": str(target_row[offset + 7]) if len(target_row) > offset + 7 else "",
            }

        except Exception as e:
            logger.error(f"Strategy 탭 읽기 실패: {e}")
            raise

    def update_strategy(self, market: str, strategy: Dict[str, Any], timestamp_str: str) -> None:
        """
        Gemini가 생성한 전략을 구글 시트 'Strategy' 탭의 해당 마켓 행에 자동 업데이트
        """
        try:
            worksheet = self.spreadsheet.worksheet("Strategy")
            all_rows = worksheet.get_all_values()

            standard_headers = [
                "MARKET",
                "UPDATED_AT",
                "STATUS",
                "ACTION",
                "ENTRY_PRICE",
                "TARGET_PRICE",
                "STOP_LOSS",
                "ALLOC_PCT",
                "REASON",
            ]

            if not all_rows:
                worksheet.append_row(standard_headers)
                all_rows = [standard_headers]

            # 행 데이터 준비
            row_data = [
                market,
                timestamp_str,
                strategy.get("status", "ACTIVE"),
                strategy.get("action", "HOLD"),
                int(strategy.get("entry_price", 0)),
                int(strategy.get("target_price", 0)),
                int(strategy.get("stop_loss", 0)),
                f"{strategy.get('alloc_pct', 0.3) * 100:.0f}%",
                strategy.get("reason", "Gemini 자동 분석"),
            ]

            # 해당 마켓이 이미 존재하는 행 번호 찾기 (1-based index)
            target_row_num = -1
            for idx, row in enumerate(all_rows):
                if row and str(row[0]).strip().upper() == market.upper():
                    target_row_num = idx + 1
                    break

            if target_row_num != -1:
                # 기존 행 덮어쓰기
                worksheet.update(values=[row_data], range_name=f"A{target_row_num}:I{target_row_num}")
                logger.info(f"구글 시트 Strategy [{market}] (행 {target_row_num}) 업데이트 완료")
            else:
                # 새로운 마켓이면 아래에 새 행 추가
                worksheet.append_row(row_data)
                logger.info(f"구글 시트 Strategy [{market}] 신규 행 추가 완료")

        except Exception as e:
            logger.warning(f"Strategy 탭 업데이트 실패 (매매는 계속 진행됨): {e}")

    def append_trade_log(self, data: Any) -> None:
        """
        'Trade_Log' 탭에 새로운 로그 행을 스마트 매핑하여 추가
        """
        try:
            try:
                worksheet = self.spreadsheet.worksheet("Trade_Log")
            except gspread.exceptions.WorksheetNotFound:
                worksheet = self.spreadsheet.add_worksheet(
                    title="Trade_Log", rows=1000, cols=12
                )

            headers = worksheet.row_values(1)
            standard_headers = [
                "Timestamp",
                "Market",
                "Order_UUID",
                "Side",
                "Order_Type",
                "Price",
                "Volume",
                "Total_KRW",
                "Stop_Loss",
                "Target_Price",
                "Current_Balance_KRW",
                "Status_Reason",
            ]

            if not headers:
                worksheet.append_row(standard_headers)
                headers = standard_headers

            if isinstance(data, dict):
                key_aliases = {
                    "timestamp": ["timestamp", "일시", "시간", "날짜", "updated_at"],
                    "market": ["market", "종목", "마켓", "코인", "symbol"],
                    "order_uuid": ["order_uuid", "uuid", "주문번호", "주문id", "order_id"],
                    "side": ["side", "action", "구분", "매매", "포지션"],
                    "order_type": ["order_type", "type", "주문유형", "유형"],
                    "price": ["price", "order_price", "exec_price", "체결가", "주문가", "가격"],
                    "volume": ["volume", "수량", "주문수량", "amount"],
                    "total_krw": ["total_krw", "총금액", "주문금액", "total"],
                    "stop_loss": ["stop_loss", "손절가", "손절"],
                    "target_price": ["target_price", "목표가", "익절가"],
                    "current_balance_krw": ["current_balance_krw", "balance_krw", "잔고", "원화잔고", "가용원화"],
                    "status_reason": ["status_reason", "status", "reason", "비고", "메모", "status/note"],
                }

                lower_data = {str(k).lower(): v for k, v in data.items()}

                row_to_insert = []
                for h in headers:
                    clean_h = str(h).strip().lower()
                    matched_value = ""

                    if clean_h in lower_data:
                        matched_value = lower_data[clean_h]
                    else:
                        for std_key, aliases in key_aliases.items():
                            if clean_h in aliases:
                                for alias in aliases:
                                    if alias in lower_data:
                                        matched_value = lower_data[alias]
                                        break
                                if matched_value != "":
                                    break

                    row_to_insert.append(matched_value)

                worksheet.append_row(row_to_insert)
                logger.info(f"Trade_Log 헤더 매핑 기록 완료: {row_to_insert}")

            elif isinstance(data, list):
                worksheet.append_row(data)
                logger.info(f"Trade_Log 단순 리스트 기록 완료: {data}")

        except Exception as e:
            logger.error(f"Trade_Log 추가 실패: {e}")
            raise
