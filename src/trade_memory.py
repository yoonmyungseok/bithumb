import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class TradeMemoryManager:
    """
    자가 진화형 AI 매매 복기 및 피드백 메모리 (Self-Learning Trade Memory)
    - data/trade_memory.json 파일에 완료된 거래의 진입 근거, 결과(익절/손절), 수익률 영구 저장
    - 최근 성공 및 실패 패턴을 분석하여 Gemini 퀀트 프롬프트에 '피드백 교훈'으로 자동 주입
    - 시간이 지날수록 동일한 실수를 반복하지 않고 승률이 우상향하도록 진화
    """

    def __init__(self):
        self.data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.memory_file = os.path.join(self.data_dir, "trade_memory.json")
        self.trades: list[dict[str, Any]] = []
        self._load_memory()

    def _load_memory(self):
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    self.trades = json.load(f)
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.warning(f"매매 메모리 로드 실패: {e}")

    def _save_memory(self):
        try:
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(self.trades[-50:], f, indent=2, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"매매 메모리 저장 실패: {e}")

    def record_completed_trade(
        self,
        market: str,
        side: str,
        entry_price: float,
        exit_price: float,
        pnl_pct: float,
        pnl_krw: float,
        reason: str,
        timestamp: str,
    ):
        """완료된 거래 내역 및 결과 복기 기록"""
        is_win = pnl_krw > 0
        lesson = ""
        if not is_win:
            lesson = f"손절 발생 ({pnl_pct:+.2f}%). 진입 당시 지표 과열 여부 및 거래량 지지 결여 확인 필요."
        else:
            lesson = f"성공적 익절 (+{pnl_pct:.2f}%). 명확한 지지선과 1:1.5 이상의 손익비 충족이 효과적이었음."

        trade_item = {
            "timestamp": timestamp,
            "market": market,
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_pct": pnl_pct,
            "pnl_krw": pnl_krw,
            "is_win": is_win,
            "reason": reason,
            "lesson": lesson,
        }
        self.trades.append(trade_item)
        self._save_memory()
        logger.info(f"🧠 [AI 매매 메모리 저장] {market} {side} 결과: {'승리(익절)' if is_win else '패배(손절)'} ({pnl_pct:+.2f}%)")

    def get_feedback_context(self) -> str:
        """Gemini 퀀트 분석 엔진에 주입할 자가 학습 피드백 텍스트 생성"""
        if not self.trades:
            return "최근 매매 이력이 아직 없습니다. 표준 퀀트 원칙에 따라 신중하게 분석하세요."

        recent_losses = [t for t in self.trades if not t.get("is_win", True)][-3:]
        recent_wins = [t for t in self.trades if t.get("is_win", False)][-3:]

        lines = ["### [5. 자가 진화 AI 매매 복기 및 최근 교훈 (Self-Learning Memory)]"]

        if recent_losses:
            lines.append("⚠️ **[최근 손절 사례 및 방지 지침]**:")
            for l in recent_losses:
                lines.append(f"- 종목 {l['market']}: {l['lesson']} (원인: {l['reason']})")
            lines.append("👉 위와 같은 지표 과열, 거래량 미달, 또는 윗꼬리 긴 상태에서는 무리하게 진입하지 마세요.")

        if recent_wins:
            lines.append("✅ **[최근 성공 사례]**:")
            for w in recent_wins:
                lines.append(f"- 종목 {w['market']}: {w['lesson']}")

        return "\n".join(lines)
