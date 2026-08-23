import io
import logging
from typing import Any

import matplotlib

matplotlib.use("Agg")  # GUI 없는 백엔드 사용
import matplotlib.pyplot as plt
from matplotlib import patches

logger = logging.getLogger(__name__)


class ChartRenderer:
    """
    고해상도 다크 테마 암호화폐 캔들 차트 렌더러
    - 캔들스틱 (양봉: 녹색, 음봉: 적색)
    - 이동평균선: MA5 (황색), MA20 (청색)
    - 볼린저 밴드 음영 영역
    - 목표가(녹색 점선), 손절가(적색 점선), 진입가(흰색 실선)
    - 텔레그램 사진 발송용 바이너리 바이트(PNG) 반환
    """

    def __init__(self):
        # 폰트 및 스타일 초기화
        plt.style.use("dark_background")

    def render_trade_chart(
        self,
        market: str,
        korean_name: str,
        candles: list[dict[str, Any]],
        entry_price: float,
        target_price: float = 0.0,
        stop_loss: float = 0.0,
        action: str = "BUY",
        reason: str = "",
    ) -> bytes | None:
        if not candles or len(candles) < 5:
            return None

        try:
            # 캔들은 최신 순(0이 최신)으로 오므로 시간순(과거->현재)으로 뒤집음
            sorted_candles = candles[::-1]
            n = len(sorted_candles)

            opens = [float(c.get("opening_price", 0)) for c in sorted_candles]
            highs = [float(c.get("high_price", 0)) for c in sorted_candles]
            lows = [float(c.get("low_price", 0)) for c in sorted_candles]
            closes = [float(c.get("trade_price", 0)) for c in sorted_candles]

            # 이동평균선 계산
            ma5, ma20 = [], []
            for i in range(n):
                sub5 = closes[max(0, i - 4) : i + 1]
                sub20 = closes[max(0, i - 19) : i + 1]
                ma5.append(sum(sub5) / len(sub5))
                ma20.append(sum(sub20) / len(sub20))

            fig, ax = plt.subplots(figsize=(10, 5.5), dpi=120)
            fig.patch.set_facecolor("#12141a")
            ax.set_facecolor("#181b22")

            # 캔들스틱 그리기
            width = 0.6
            for i in range(n):
                o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                color = "#00e676" if c >= o else "#ff5252"  # 양봉: 에메랄드 그린, 음봉: 비비드 레드

                # 꼬리선
                ax.plot([i, i], [l, h], color=color, linewidth=1.2, zorder=2)

                # 몸통
                rect_y = min(o, c)
                rect_h = max(abs(c - o), (highs[i] - lows[i]) * 0.01)
                rect = patches.Rectangle(
                    (i - width / 2, rect_y),
                    width,
                    rect_h,
                    facecolor=color,
                    edgecolor=color,
                    linewidth=0.8,
                    zorder=3,
                )
                ax.add_patch(rect)

            # 이동평균선
            ax.plot(range(n), ma5, label="MA 5 (단기)", color="#ffd600", linewidth=1.5, zorder=4)
            ax.plot(range(n), ma20, label="MA 20 (중기)", color="#00b0ff", linewidth=1.5, zorder=4)

            # 주요 가격 라인 (진입가, 목표가, 손절가)
            if entry_price > 0:
                ax.axhline(
                    entry_price,
                    color="#ffffff",
                    linestyle="-",
                    linewidth=1.2,
                    label=f"진입가: {entry_price:,.1f}",
                    zorder=5,
                )
            if target_price > 0:
                ax.axhline(
                    target_price,
                    color="#00e676",
                    linestyle="--",
                    linewidth=1.5,
                    label=f"목표가 (+): {target_price:,.1f}",
                    zorder=5,
                )
            if stop_loss > 0:
                ax.axhline(
                    stop_loss,
                    color="#ff5252",
                    linestyle="--",
                    linewidth=1.5,
                    label=f"손절가 (-): {stop_loss:,.1f}",
                    zorder=5,
                )

            # 그리드 및 서식
            ax.grid(True, linestyle=":", alpha=0.3, color="#555d70")
            ax.set_xlim(-1, n)
            ax.set_ylabel("가격 (KRW)", color="#cfd8dc", fontsize=10)

            # 제목
            action_kor = "매수 진입 (BUY)" if action == "BUY" else ("매도/익절 (SELL)" if action == "SELL" else "모니터링 (HOLD)")
            title_color = "#00e676" if action == "BUY" else ("#ff5252" if action == "SELL" else "#90caf9")
            ax.set_title(
                f"[{korean_name} / {market}] 5분봉 AI 퀀트 차트 | {action_kor}",
                color=title_color,
                fontsize=13,
                fontweight="bold",
                pad=12,
            )

            # 범례
            ax.legend(loc="upper left", facecolor="#212631", edgecolor="#374151", fontsize=8)

            plt.tight_layout()

            # 메모리 바이트로 저장
            buf = io.BytesIO()
            plt.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
            plt.close(fig)
            buf.seek(0)
            return buf.getvalue()

        except (ValueError, IndexError, OSError) as e:
            logger.warning(f"차트 렌더링 실패: {e}")
            plt.close("all")
            return None
