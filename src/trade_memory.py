import logging
import os
import threading
import time
from typing import Any

from db_manager import get_db_manager, get_exchange_db_path
from order_safety import load_json_with_backup_recovery, write_json_atomically

logger = logging.getLogger(__name__)


class TradeMemoryManager:
    """
    자가 진화형 AI 매매 복기 및 피드백 메모리 (SQLite DB + JSON 듀얼 영속성)
    - SQLite trading.db 및 data/trade_memory.json 파일에 완료된 거래의 진입 근거, 결과(익절/손절), 수익률 영구 저장
    - 퀀트 속성(BTC 레짐, 알파 점수 구간, 지표 상태, 보유시간) 정량 태깅 및 성과 분석 (과제 F)
    - 최근 성공 및 실패 패턴을 분석하여 Gemini 퀀트 프롬프트에 '피드백 교훈'으로 자동 주입
    - 시간이 지날수록 동일한 실수를 반복하지 않고 승률이 우상향하도록 진화
    """

    def __init__(
        self,
        memory_file: str | None = None,
        data_dir: str | None = None,
        exchange_scope: str = "",
        legacy_exchange: str = "",
    ):
        self._lock = threading.RLock()
        self.data_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.memory_file = memory_file or os.path.join(self.data_dir, "trade_memory.json")
        # 거래 메모리는 자신이 소유한 거래소 data_dir의 DB에만 기록한다.
        self.db = get_db_manager(get_exchange_db_path(self.data_dir))
        # 거래소별 성과 표본을 분리해 다른 거래소 결과가 AI 피드백에 섞이지 않게 한다.
        self.exchange_scope = exchange_scope.strip().lower()
        # 전용 저장소에서만 허용하는 구형 무표기 기록의 안전한 귀속 대상이다.
        self.legacy_exchange = legacy_exchange.strip().lower()
        self.trades: list[dict[str, Any]] = []
        self._load_memory()

    def _load_memory(self):
        with self._lock:
            data = load_json_with_backup_recovery(self.memory_file, default=[])
            stored_scope = ""
            if isinstance(data, list):
                loaded_trades = data
            elif isinstance(data, dict):
                loaded_trades = data.get("completed_trades", [])
                stored_scope = str(data.get("exchange_scope", "")).lower()
            else:
                loaded_trades = []
            if self.exchange_scope:
                normalized_trades: list[dict[str, Any]] = []
                foreign: list[dict[str, Any]] = []
                legacy_normalized = False
                for trade in loaded_trades:
                    record_scope = str(trade.get("exchange", "")).lower()
                    if record_scope == self.exchange_scope:
                        normalized_trades.append(trade)
                    elif not record_scope and self.legacy_exchange == self.exchange_scope:
                        # 빗썸 전용 data/의 구형 기록만 명시적으로 빗썸 소유로 승격한다.
                        normalized_trade = dict(trade)
                        normalized_trade["exchange"] = self.exchange_scope
                        normalized_trades.append(normalized_trade)
                        legacy_normalized = True
                    else:
                        foreign.append(trade)

                self.trades = normalized_trades
                # 신규 빈 파일은 마이그레이션 대상이 아니며 불필요한 운영 경고를 남기지 않는다.
                needs_migration = bool(loaded_trades) and bool(
                    foreign or legacy_normalized or stored_scope != self.exchange_scope
                )
                if needs_migration:
                    # 원본 전체와 격리 대상 모두 보존하므로 운영 데이터는 손실되지 않는다.
                    archive_path = f"{self.memory_file}.{self.exchange_scope}.pre-migration.audit.json"
                    write_json_atomically(
                        archive_path,
                        {"schema_version": 2, "exchange_scope": self.exchange_scope, "source_records": loaded_trades},
                    )
                    # 타 거래소·판별 불가 구형 기록은 삭제하지 않고 감사 파일에 따로 남긴다.
                    write_json_atomically(
                        f"{self.memory_file}.{self.exchange_scope}.foreign-trades.audit.json",
                        {"schema_version": 2, "completed_trades": foreign},
                    )
                    # 이후 재조회에서는 전용 래퍼만 읽게 되어 동일 경고가 반복되지 않는다.
                    write_json_atomically(self.memory_file, {
                        "schema_version": 2,
                        "exchange_scope": self.exchange_scope,
                        "completed_trades": self.trades[-50:],
                    })
                    logger.warning(
                        "거래 메모리 마이그레이션 완료: %d건 격리, %d건을 %s 범위로 유지했습니다.",
                        len(foreign), len(self.trades), self.exchange_scope,
                    )
            else:
                self.trades = loaded_trades

    def load_memory(self) -> dict[str, Any]:
        """Load memory and return dict for compatibility."""
        with self._lock:
            self._load_memory()
            return {"completed_trades": list(self.trades)}

    def get_recent_trades(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent completed trades (newest first)."""
        with self._lock:
            self._load_memory()
            return list(reversed(self.trades[-limit:]))

    def _save_memory(self):
        with self._lock:
            try:
                write_json_atomically(self.memory_file, {
                    "schema_version": 2,
                    "exchange_scope": self.exchange_scope,
                    "completed_trades": self.trades[-50:],
                })
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
        position_id: str | None = None,
        trade_id: str | None = None,
        filled_volume: float = 0.0,
        fee: float = 0.0,
        slippage: float = 0.0,
        order_status: str = "FILLED",
        exchange: str = "bithumb",
        btc_regime: str = "UNKNOWN",
        alpha_score: int | None = None,
        indicators: dict[str, Any] | None = None,
        bars_held: int | None = None,
        **extra_fields: Any,
    ):
        """완료된 거래 내역 및 실제 체결 결과 복기 기록 (과제 F 정량 태깅)"""
        if self.exchange_scope and exchange.lower() != self.exchange_scope:
            # 호출 실수로 타 거래소 결과가 현재 학습 파일에 섞이는 것을 원천 차단한다.
            logger.error("거래소 범위 불일치 거래 메모리 기록 차단: %s != %s", exchange, self.exchange_scope)
            return
        is_win = pnl_krw > 0
        lesson = ""
        if not is_win:
            lesson = f"손절 발생 ({pnl_pct:+.2f}%). 진입 당시 지표 과열 여부 및 거래량 지지 결여 확인 필요."
        else:
            lesson = f"성공적 익절 (+{pnl_pct:.2f}%). 명확한 지지선과 1:1.5 이상의 손익비 충족이 효과적이었음."

        trade_item = {
            "timestamp": timestamp,
            "trade_id": trade_id or f"tr-{int(time.time() * 1000)}",
            "position_id": position_id or market,
            "exchange": exchange,
            "market": market,
            "side": side,
            "order_status": order_status,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "filled_volume": filled_volume,
            "fee": fee,
            "slippage": slippage,
            "pnl_pct": pnl_pct,
            "pnl_krw": pnl_krw,
            "is_win": is_win,
            "reason": reason,
            "lesson": lesson,
            "btc_regime": btc_regime,
            "alpha_score": alpha_score,
            "indicators": indicators or {},
            "bars_held": bars_held,
        }
        trade_item.update(extra_fields)
        with self._lock:
            self.trades.append(trade_item)
            self._save_memory()
            try:
                self.db.insert_trade(exchange, trade_item)
            except Exception as exc:
                logger.warning("SQLite 매매 기록 저장 오류: %s", exc)
        logger.info(f"🧠 [AI 매매 메모리 저장] {market} {side} 결과: {'승리(익절)' if is_win else '패배(손절)'} ({pnl_pct:+.2f}%, 실현: {pnl_krw:+,.0f}원)")

    def get_regime_performance_stats(self, min_sample_size: int = 3) -> dict[str, Any]:
        """시장 레짐별(NORMAL / RISK_OFF) 매매 성과 및 통계적 신뢰도 분석 (과제 D/F)"""
        with self._lock:
            self._load_memory()
            regime_map: dict[str, list[dict[str, Any]]] = {}
            for t in self.trades:
                rg = t.get("btc_regime", "NORMAL")
                regime_map.setdefault(rg, []).append(t)

            res = {}
            for rg, t_list in regime_map.items():
                n = len(t_list)
                wins = [t for t in t_list if t.get("pnl_krw", 0) > 0]
                losses = [t for t in t_list if t.get("pnl_krw", 0) <= 0]
                win_rate = (len(wins) / n * 100.0) if n > 0 else 0.0
                total_pnl = sum(t.get("pnl_krw", 0) for t in t_list)
                reliable = n >= min_sample_size
                avg_win = (sum(float(t.get("pnl_pct", 0.0)) for t in wins) / len(wins)) if wins else 0.0
                avg_loss = (abs(sum(float(t.get("pnl_pct", 0.0)) for t in losses)) / len(losses)) if losses else 0.0
                expectancy = ((win_rate / 100.0) * avg_win) - (((100.0 - win_rate) / 100.0) * avg_loss)
                gross_win = sum(float(t.get("pnl_krw", 0.0)) for t in wins)
                gross_loss = abs(sum(float(t.get("pnl_krw", 0.0)) for t in losses))
                profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
                # 포지션 단위의 누적 손익으로 레짐 내부 MDD를 계산한다. 자본 기준 MDD는 백테스트 결과를 사용한다.
                cumulative, peak, max_drawdown = 0.0, 0.0, 0.0
                for trade in t_list:
                    cumulative += float(trade.get("pnl_krw", 0.0))
                    peak = max(peak, cumulative)
                    max_drawdown = max(max_drawdown, peak - cumulative)
                avg_bars = sum(float(t.get("bars_held") or 0.0) for t in t_list) / n
                res[rg] = {
                    "sample_count": n,
                    "win_rate_pct": round(win_rate, 1),
                    "total_pnl_krw": round(total_pnl, 0),
                    "is_statistically_reliable": reliable,
                    "status": "OBSERVE_ONLY" if reliable else "INSUFFICIENT_SAMPLE",
                    "expectancy_pct": round(expectancy, 4),
                    "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
                    "max_drawdown_krw": round(max_drawdown, 0),
                    "avg_bars_held": round(avg_bars, 2),
                }
            return res

    def get_alpha_tier_stats(self, min_sample_size: int = 3) -> dict[str, Any]:
        """알파 점수 구간별(65-69, 70-79, 80+) 매매 성과 분석 (과제 F)"""
        with self._lock:
            self._load_memory()
            tiers = {"65-69": [], "70-79": [], "80+": [], "unspecified": []}
            for t in self.trades:
                score = t.get("alpha_score")
                if score is None:
                    tiers["unspecified"].append(t)
                elif score >= 80:
                    tiers["80+"].append(t)
                elif score >= 70:
                    tiers["70-79"].append(t)
                elif score >= 65:
                    tiers["65-69"].append(t)
                else:
                    tiers["unspecified"].append(t)

            res = {}
            for tier_name, t_list in tiers.items():
                if not t_list:
                    continue
                n = len(t_list)
                wins = [t for t in t_list if t.get("pnl_krw", 0) > 0]
                win_rate = (len(wins) / n * 100.0) if n > 0 else 0.0
                total_pnl = sum(t.get("pnl_krw", 0) for t in t_list)
                res[tier_name] = {
                    "sample_count": n,
                    "win_rate_pct": round(win_rate, 1),
                    "total_pnl_krw": round(total_pnl, 0),
                    "is_statistically_reliable": n >= min_sample_size,
                }
            return res

    def get_position_level_stats(self) -> dict[str, Any]:
        """분할익절을 하나의 포지션으로 통합하여 포지션 단위 승률 및 손익 통계 집계"""
        with self._lock:
            self._load_memory()
            positions: dict[str, dict[str, Any]] = {}
            for t in self.trades:
                pos_id = t.get("position_id") or t.get("market") or "unknown"
                if pos_id not in positions:
                    positions[pos_id] = {
                        "market": t.get("market"),
                        "total_pnl_krw": 0.0,
                        "total_fee": 0.0,
                        "trades_count": 0,
                        "last_timestamp": t.get("timestamp"),
                    }
                positions[pos_id]["total_pnl_krw"] += float(t.get("pnl_krw", 0.0))
                positions[pos_id]["total_fee"] += float(t.get("fee", 0.0))
                positions[pos_id]["trades_count"] += 1

            total_positions = len(positions)
            win_positions = sum(1 for p in positions.values() if p["total_pnl_krw"] > 0)
            win_rate = (win_positions / total_positions * 100.0) if total_positions > 0 else 0.0
            total_pnl = sum(p["total_pnl_krw"] for p in positions.values())

            return {
                "total_positions": total_positions,
                "win_positions": win_positions,
                "loss_positions": total_positions - win_positions,
                "position_win_rate_pct": round(win_rate, 2),
                "total_realized_pnl_krw": round(total_pnl, 2),
            }

    def get_feedback_context(self) -> str:
        """Gemini 퀀트 분석 엔진에 주입할 자가 학습 피드백 텍스트 생성"""
        with self._lock:
            self._load_memory()
            if not self.trades:
                return "최근 매매 이력이 아직 없습니다. 표준 퀀트 원칙에 따라 신중하게 분석하세요."

            recent = self.trades[-6:]
            wins = [t for t in recent if t.get("is_win", False)]
            losses = [t for t in recent if not t.get("is_win", False)]

            summary = [f"[최근 매매 학습 피드백: 총 {len(recent)}건 중 승리 {len(wins)}건, 손절 {len(losses)}건]"]
            if losses:
                summary.append("⚠️ 최근 손절 사례 교훈:")
                for loss_item in losses[-2:]:
                    summary.append(f"  - {loss_item['market']}: {loss_item.get('lesson', '손절 발생')}")
            if wins:
                summary.append("✅ 최근 성공 사례 패턴:")
                for w in wins[-2:]:
                    summary.append(f"  - {w['market']}: {w.get('lesson', '익절 성공')}")

            return "\n".join(summary)
