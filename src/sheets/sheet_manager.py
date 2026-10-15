"""Google Sheets API를 통해 포트폴리오 요약 및 종목별 탭을 관리·갱신하는 매니저."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)


class GoogleSheetManager:
    """구글 스프레드시트 조작 및 탭 생성/갱신 전담 클래스."""

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(
        self,
        key_path: str | None = None,
        sheet_id: str | None = None,
    ):
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        raw_key = key_path or os.getenv("GOOGLE_SHEETS_KEY_PATH", "config/service_account.json")
        if not os.path.isabs(raw_key):
            self.key_path = os.path.join(project_root, raw_key)
        else:
            self.key_path = raw_key

        self.sheet_id = (sheet_id or os.getenv("GOOGLE_SHEET_ID", "")).strip()
        self._client: gspread.Client | None = None
        self._spreadsheet: gspread.Spreadsheet | None = None
        self._last_auth_time = 0.0

    def _ensure_connected(self) -> gspread.Spreadsheet:
        """스프레드시트 연결 보장 (토큰 만료 시 재인증)."""
        now = time.time()
        if self._spreadsheet is not None and (now - self._last_auth_time) < 3000:
            return self._spreadsheet

        if not os.path.exists(self.key_path):
            raise FileNotFoundError(f"서비스 계정 키 파일을 찾을 수 없습니다: {self.key_path}")
        if not self.sheet_id:
            raise ValueError("GOOGLE_SHEET_ID 환경변수가 설정되지 않았습니다.")

        creds = Credentials.from_service_account_file(self.key_path, scopes=self.SCOPES)
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(self.sheet_id)
        self._last_auth_time = now
        logger.info(f"[GoogleSheetManager] 스프레드시트 연결 완료: {self._spreadsheet.title}")
        return self._spreadsheet

    def _get_or_create_worksheet(
        self,
        title: str,
        rows: int = 100,
        cols: int = 15,
        index: int | None = None,
    ) -> gspread.Worksheet:
        """탭이 존재하면 반환하고, 없으면 새로 생성."""
        ss = self._ensure_connected()
        try:
            return ss.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            logger.info(f"[GoogleSheetManager] 새 워크시트 생성: '{title}'")
            return ss.add_worksheet(title=title, rows=rows, cols=cols, index=index)

    def update_summary_tab(self, positions: list[dict[str, Any]]) -> None:
        """
        Summary(요약) 탭 갱신:
        - 상단: 총 투자원금, 총 평가금액, 총 손익, 총 수익률, 최근 갱신 시각
        - 목록: 거래소, 마켓, 보유수량, 평단가, 현재가, 평가금액, 평가손익, 수익률(%)
        """
        ss = self._ensure_connected()
        sheet = self._get_or_create_worksheet("Summary", rows=max(50, len(positions) + 15), cols=12, index=0)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        total_buy_krw = sum(p.get("total_buy_price", 0.0) for p in positions)
        total_eval_krw = sum(p.get("eval_amount", 0.0) for p in positions)
        total_pnl_krw = total_eval_krw - total_buy_krw
        total_pnl_pct = (total_pnl_krw / total_buy_krw * 100) if total_buy_krw > 0 else 0.0

        # 배치 업데이트용 2차원 리스트 구성
        rows: list[list[Any]] = [
            ["📊 수동 매수 포트폴리오 종합 요약", "", "", "", "", "", "", "", ""],
            ["최종 동기화 시각", now_str, "", "총 보유 종목수", len(positions), "종목", "", "", ""],
            ["총 매수원금(KRW)", f"{int(round(total_buy_krw)):,}", "", "총 평가금액(KRW)", f"{int(round(total_eval_krw)):,}", "", "총 평가손익(KRW)", f"{int(round(total_pnl_krw)):,}", f"{total_pnl_pct:+.2f}%"],
            ["", "", "", "", "", "", "", "", ""],
            ["[종목별 상세 요약] (탭 이름을 클릭하거나 Gemini에게 질문하세요)", "", "", "", "", "", "", "", ""],
            [
                "거래소",
                "마켓코드",
                "통화",
                "보유수량",
                "매수단가(평단)",
                "현재가",
                "매수금액(KRW)",
                "평가금액(KRW)",
                "평가손익(KRW)",
                "수익률(%)",
                "시트 탭명",
            ],
        ]

        for p in positions:
            exchange = p.get("exchange", "").upper()
            market = p.get("market", "")
            currency = p.get("currency", "")
            volume = p.get("volume", 0.0)
            avg_price = p.get("avg_buy_price", 0.0)
            current_price = p.get("current_price", 0.0)
            buy_krw = p.get("total_buy_price", 0.0)
            eval_krw = p.get("eval_amount", 0.0)
            pnl_krw = p.get("unrealized_pnl", 0.0)
            pnl_pct = p.get("unrealized_pnl_pct", 0.0)
            tab_name = f"{exchange}_{currency}"

            rows.append([
                exchange,
                market,
                currency,
                f"{volume:.6f}".rstrip("0").rstrip(".") if volume < 1 else f"{volume:.4f}",
                f"{avg_price:,.2f}" if avg_price < 100 else f"{int(round(avg_price)):,}",
                f"{current_price:,.2f}" if current_price < 100 else f"{int(round(current_price)):,}",
                f"{int(round(buy_krw)):,}",
                f"{int(round(eval_krw)):,}",
                f"{int(round(pnl_krw)):,}",
                f"{pnl_pct:+.2f}%",
                tab_name,
            ])

        # 기존 내용 초기화 후 배치 업데이트
        sheet.clear()
        sheet.update(range_name=f"A1:K{len(rows)}", values=rows)
        logger.info(f"[GoogleSheetManager] Summary 탭 갱신 완료 (종목수: {len(positions)})")

    def update_coin_tab(self, position: dict[str, Any]) -> None:
        """
        개별 종목 탭 갱신:
        - 탭명: {EXCHANGE}_{CURRENCY} (예: UPBIT_BTC)
        - 상단: 포지션 핵심 지표 카드 (현재가, 평단가, 손익, 수익률)
        - 하단: 5분 주기 시세 히스토리 테이블 (새 행 추가)
        """
        exchange = position.get("exchange", "").upper()
        currency = position.get("currency", "")
        tab_name = f"{exchange}_{currency}"

        sheet = self._get_or_create_worksheet(tab_name, rows=200, cols=10)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        market = position.get("market", "")
        volume = position.get("volume", 0.0)
        avg_price = position.get("avg_buy_price", 0.0)
        current_price = position.get("current_price", 0.0)
        buy_krw = position.get("total_buy_price", 0.0)
        eval_krw = position.get("eval_amount", 0.0)
        pnl_krw = position.get("unrealized_pnl", 0.0)
        pnl_pct = position.get("unrealized_pnl_pct", 0.0)

        # 상단 카드 영역 (A1:F7)
        card_values = [
            [f"🪙 {exchange} - {currency} ({market}) 수동 관리 종목", "", "", "", "", ""],
            ["최종 갱신시각", now_str, "", "보유 상태", "보유 중", ""],
            ["총 보유수량", f"{volume:.8f}".rstrip("0").rstrip("."), "", "평균 매수가(KRW)", f"{avg_price:,.2f}" if avg_price < 100 else f"{int(round(avg_price)):,}", ""],
            ["현재 시장가(KRW)", f"{current_price:,.2f}" if current_price < 100 else f"{int(round(current_price)):,}", "", "총 매수원금(KRW)", f"{int(round(buy_krw)):,}", ""],
            ["현재 평가금액(KRW)", f"{int(round(eval_krw)):,}", "", "평가 손익(KRW)", f"{int(round(pnl_krw)):,}", f"{pnl_pct:+.2f}%"],
            ["", "", "", "", "", ""],
            ["[5분 주기 시세 및 손익 히스토리]", "", "", "", "", ""],
            ["기록 일시", "현재가(KRW)", "평가금액(KRW)", "평가손익(KRW)", "수익률(%)", "비고"],
        ]

        # 탭의 첫 생성 여부 확인 (A8에 헤더가 있는지)
        a8_val = sheet.acell("A8").value
        if not a8_val or "기록 일시" not in a8_val:
            sheet.update(range_name="A1:F8", values=card_values)
        else:
            # 상단 1~7행만 업데이트하여 기존 히스토리 보존
            sheet.update(range_name="A1:F7", values=card_values[:7])

        # 새 히스토리 1행 추가
        new_history_row = [
            now_str,
            f"{current_price:,.2f}" if current_price < 100 else f"{int(round(current_price)):,}",
            f"{int(round(eval_krw)):,}",
            f"{int(round(pnl_krw)):,}",
            f"{pnl_pct:+.2f}%",
            "정기 갱신",
        ]
        sheet.append_row(new_history_row)
        logger.info(f"[GoogleSheetManager] '{tab_name}' 탭 갱신 및 히스토리 추가 완료")

    def sync_all(self, positions: list[dict[str, Any]]) -> None:
        """전체 수동 종목 Summary 및 개별 탭 일괄 갱신."""
        if not positions:
            logger.info("[GoogleSheetManager] 수동 매수 종목이 없습니다.")
            # 비어있더라도 Summary 탭은 0건으로 갱신
            self.update_summary_tab([])
            return

        # 1. Summary 탭 갱신
        self.update_summary_tab(positions)

        # 2. 각 종목별 개별 탭 갱신
        for pos in positions:
            try:
                self.update_coin_tab(pos)
                time.sleep(0.5)  # API Rate Limit (초당 요청) 완화
            except Exception as e:
                logger.error(f"[GoogleSheetManager] 종목 탭 갱신 실패 ({pos.get('market')}): {e}", exc_info=True)
