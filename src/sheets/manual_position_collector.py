"""빗썸 및 업비트의 수동 매수(봇 비관리) 종목 잔고 및 시세 수집기."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

# 상위 src 디렉터리를 sys.path에 추가
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from bithumb_api import BithumbAPI
from order_safety import OrderJournal
from upbit_api import UpbitAPI

logger = logging.getLogger(__name__)


class ManualPositionCollector:
    """수동 매수 종목 데이터 수집기 (봇 포지션 완전 격리)."""

    def __init__(
        self,
        min_eval_krw: float = 1000.0,  # 1,000원 미만 극소액 먼지 코인은 제외
    ):
        self.min_eval_krw = min_eval_krw
        self.project_root = os.path.dirname(SRC_DIR)
        self.data_dir_bithumb = os.path.join(self.project_root, "data")
        self.data_dir_upbit = os.path.join(self.project_root, "data", "upbit")

        # 거래소 API 클라이언트 초기화 (격리 유지)
        self.upbit_api: UpbitAPI | None = None
        self.bithumb_api: BithumbAPI | None = None

        from dotenv import dotenv_values
        common_env = dotenv_values(os.path.join(self.project_root, ".env"))
        upbit_env = dotenv_values(os.path.join(self.project_root, ".env.upbit"))
        bithumb_env = dotenv_values(os.path.join(self.project_root, ".env.bithumb"))

        upbit_access = upbit_env.get("UPBIT_ACCESS_KEY") or common_env.get("UPBIT_ACCESS_KEY") or os.getenv("UPBIT_ACCESS_KEY", "")
        upbit_secret = upbit_env.get("UPBIT_SECRET_KEY") or common_env.get("UPBIT_SECRET_KEY") or os.getenv("UPBIT_SECRET_KEY", "")

        if upbit_access and upbit_secret:
            try:
                self.upbit_api = UpbitAPI(access_key=upbit_access, secret_key=upbit_secret)
                logger.info("[ManualPositionCollector] 업비트 API 클라이언트 연결 성공")
            except Exception as e:
                logger.warning(f"[ManualPositionCollector] 업비트 API 초기화 실패: {e}")

        bithumb_access = bithumb_env.get("BITHUMB_ACCESS_KEY") or common_env.get("BITHUMB_ACCESS_KEY") or os.getenv("BITHUMB_ACCESS_KEY", "")
        bithumb_secret = bithumb_env.get("BITHUMB_SECRET_KEY") or common_env.get("BITHUMB_SECRET_KEY") or os.getenv("BITHUMB_SECRET_KEY", "")

        if bithumb_access and bithumb_secret:
            try:
                self.bithumb_api = BithumbAPI(access_key=bithumb_access, secret_key=bithumb_secret)
                logger.info("[ManualPositionCollector] 빗썸 API 클라이언트 연결 성공")
            except Exception as e:
                logger.warning(f"[ManualPositionCollector] 빗썸 API 초기화 실패: {e}")

        # 주문 저널 초기화 (봇 활성 포지션 감지용)
        self.upbit_journal = OrderJournal(data_dir=self.data_dir_upbit, exchange_scope="upbit")
        self.bithumb_journal = OrderJournal(data_dir=self.data_dir_bithumb, exchange_scope="bithumb")

    def collect_upbit_manual_positions(self) -> list[dict[str, Any]]:
        """업비트 수동 매수 포지션 수집."""
        if not self.upbit_api:
            return []

        results: list[dict[str, Any]] = []
        try:
            balances = self.upbit_api.get_balances()
        except Exception as e:
            logger.error(f"[ManualPositionCollector] 업비트 잔고 조회 실패: {e}")
            return []

        for currency, info in balances.items():
            curr_upper = currency.upper()
            if curr_upper in ("KRW", "POINT", "VOTE"):
                continue

            balance = float(info.get("balance", 0.0))
            locked = float(info.get("locked", 0.0))
            total_vol = balance + locked
            if total_vol <= 0:
                continue

            market = f"KRW-{curr_upper}"
            # 봇이 관리 중인 포지션인지 저널로 확인 (봇 포지션이면 제외)
            if self.upbit_journal.has_active_bot_position(market):
                logger.debug(f"[Upbit] {market}은 봇 관리 포지션이므로 수동 목록에서 제외합니다.")
                continue

            avg_buy_price = float(info.get("avg_buy_price", 0.0))

            # 현재가 조회
            try:
                current_price = self.upbit_api.get_current_price(market)
            except Exception as e:
                logger.warning(f"[Upbit] {market} 현재가 조회 실패: {e}")
                current_price = avg_buy_price

            if current_price <= 0:
                current_price = avg_buy_price

            eval_amount = total_vol * current_price
            if eval_amount < self.min_eval_krw:
                continue

            total_buy_price = total_vol * avg_buy_price
            unrealized_pnl = eval_amount - total_buy_price
            unrealized_pnl_pct = (
                (unrealized_pnl / total_buy_price * 100.0) if total_buy_price > 0 else 0.0
            )

            results.append({
                "exchange": "UPBIT",
                "currency": curr_upper,
                "market": market,
                "volume": total_vol,
                "available": balance,
                "locked": locked,
                "avg_buy_price": avg_buy_price,
                "current_price": current_price,
                "total_buy_price": total_buy_price,
                "eval_amount": eval_amount,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": unrealized_pnl_pct,
            })

        return results

    def collect_bithumb_manual_positions(self) -> list[dict[str, Any]]:
        """빗썸 수동 매수 포지션 수집."""
        if not self.bithumb_api:
            return []

        results: list[dict[str, Any]] = []
        try:
            balances = self.bithumb_api.get_balances()
        except Exception as e:
            logger.error(f"[ManualPositionCollector] 빗썸 잔고 조회 실패: {e}")
            return []

        for currency, info in balances.items():
            curr_upper = currency.upper()
            if curr_upper in ("KRW", "POINT"):
                continue

            balance = float(info.get("balance", 0.0))
            locked = float(info.get("locked", 0.0))
            total_vol = balance + locked
            if total_vol <= 0:
                continue

            market = f"KRW-{curr_upper}"
            # 봇이 관리 중인 포지션인지 저널로 확인 (봇 포지션이면 제외)
            if self.bithumb_journal.has_active_bot_position(market):
                logger.debug(f"[Bithumb] {market}은 봇 관리 포지션이므로 수동 목록에서 제외합니다.")
                continue

            avg_buy_price = float(info.get("avg_buy_price", 0.0))

            # 현재가 조회
            try:
                current_price = self.bithumb_api.get_current_price(market)
            except Exception as e:
                logger.warning(f"[Bithumb] {market} 현재가 조회 실패: {e}")
                current_price = avg_buy_price

            if current_price <= 0:
                current_price = avg_buy_price

            eval_amount = total_vol * current_price
            if eval_amount < self.min_eval_krw:
                continue

            total_buy_price = total_vol * avg_buy_price
            unrealized_pnl = eval_amount - total_buy_price
            unrealized_pnl_pct = (
                (unrealized_pnl / total_buy_price * 100.0) if total_buy_price > 0 else 0.0
            )

            results.append({
                "exchange": "BITHUMB",
                "currency": curr_upper,
                "market": market,
                "volume": total_vol,
                "available": balance,
                "locked": locked,
                "avg_buy_price": avg_buy_price,
                "current_price": current_price,
                "total_buy_price": total_buy_price,
                "eval_amount": eval_amount,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": unrealized_pnl_pct,
            })

        return results

    def collect_all_manual_positions(self) -> list[dict[str, Any]]:
        """두 거래소의 모든 수동 매수 포지션을 수집하여 정렬 반환."""
        upbit_positions = self.collect_upbit_manual_positions()
        bithumb_positions = self.collect_bithumb_manual_positions()

        all_positions = upbit_positions + bithumb_positions
        # 평가금액 높은 순으로 정렬
        all_positions.sort(key=lambda x: x.get("eval_amount", 0.0), reverse=True)
        return all_positions
