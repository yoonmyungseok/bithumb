"""
High-Performance SQLite Persistence Layer for AI Quant Trading Bot
- Multi-process safe WAL (Write-Ahead Logging) mode
- Automatic migration from legacy JSON files
- Full ACID transactions for trade memory, order journal, daily stats, and position states
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
import gc
from typing import Any

logger = logging.getLogger(__name__)

_DB_LOCK = threading.RLock()
# DB 인스턴스는 경로별로 분리한다. 전역 단일 인스턴스는 빗썸이 먼저 로드된 뒤
# 업비트 상태까지 data/trading.db에 기록하는 교차 거래소 오염을 일으킬 수 있다.
_DB_MANAGER_BY_PATH: dict[str, "DatabaseManager"] = {}


def get_default_db_path() -> str:
    """Returns the default SQLite DB path in the project data directory."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, "data", "trading.db")


def get_exchange_db_path(data_dir: str | None = None) -> str:
    """서비스가 소유한 data_dir 안의 DB 경로를 일관되게 반환한다."""
    directory = data_dir or os.path.dirname(get_default_db_path())
    return os.path.abspath(os.path.normpath(os.path.join(directory, "trading.db")))


def _is_ephemeral_db_path(db_path: str) -> bool:
    """Windows 테스트 임시 폴더 DB는 WAL 대신 DELETE 모드를 사용한다."""
    try:
        temp_root = os.path.abspath(tempfile.gettempdir()).lower()
        return os.path.abspath(db_path).lower().startswith(temp_root)
    except Exception:
        return False


class DatabaseManager:
    """Thread-safe SQLite Database Manager with connection pooling & WAL mode."""

    def __init__(self, db_path: str | None = None):
        self.db_path = os.path.abspath(os.path.normpath(db_path or get_default_db_path()))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def dispose(self) -> None:
        """WAL 체크포인트 후 Windows 테스트 환경의 파일 잠금을 줄인다."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=15.0)
            try:
                if _is_ephemeral_db_path(self.db_path):
                    conn.execute("PRAGMA journal_mode=DELETE;")
                else:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("DatabaseManager dispose skipped for %s: %s", self.db_path, exc)

    def _get_connection(self) -> sqlite3.Connection:
        """Create a configured SQLite connection with row factory and timeout."""
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if _is_ephemeral_db_path(self.db_path):
            conn.execute("PRAGMA journal_mode=DELETE;")
        else:
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        """Initialize database schema and indexes."""
        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Trade Memory (Completed trades and factor analysis)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange TEXT NOT NULL,
                    market TEXT NOT NULL,
                    entry_time TEXT,
                    exit_time TEXT,
                    entry_price REAL DEFAULT 0.0,
                    exit_price REAL DEFAULT 0.0,
                    pnl_krw REAL DEFAULT 0.0,
                    pnl_pct REAL DEFAULT 0.0,
                    hold_duration_min INTEGER DEFAULT 0,
                    exit_reason TEXT DEFAULT '',
                    strategy_tags TEXT DEFAULT '',
                    market_regime TEXT DEFAULT '',
                    btc_trend TEXT DEFAULT '',
                    factor_scores TEXT DEFAULT '{}',
                    raw_data TEXT DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tm_ex_mkt ON trade_memory(exchange, market);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tm_exit_time ON trade_memory(exit_time);")

            # 2. Order Journal (Orders & Execution history)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT UNIQUE,
                    order_uuid TEXT,
                    exchange TEXT NOT NULL,
                    market TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT DEFAULT 'limit',
                    price REAL DEFAULT 0.0,
                    volume REAL DEFAULT 0.0,
                    state TEXT DEFAULT 'wait',
                    filled_volume REAL DEFAULT 0.0,
                    executed_funds REAL DEFAULT 0.0,
                    fee REAL DEFAULT 0.0,
                    is_paper INTEGER DEFAULT 0,
                    raw_payload TEXT DEFAULT '{}',
                    created_at TEXT,
                    updated_at TEXT
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_ex_mkt ON orders(exchange, market);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_client_id ON orders(client_order_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);")

            # 3. Daily Stats (PnL, win rate, kill switch state)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    exchange TEXT NOT NULL,
                    date TEXT NOT NULL,
                    start_equity REAL DEFAULT 0.0,
                    realized_pnl_krw REAL DEFAULT 0.0,
                    total_trades INTEGER DEFAULT 0,
                    win_trades INTEGER DEFAULT 0,
                    consecutive_losses INTEGER DEFAULT 0,
                    cooldown_until_ts REAL DEFAULT 0.0,
                    kill_switch_active INTEGER DEFAULT 0,
                    kill_switch_latched_date TEXT DEFAULT '',
                    history_json TEXT DEFAULT '[]',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (exchange, date)
                );
            """)

            # 4. Position State (Open positions, peaks, partial take profits)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    exchange TEXT NOT NULL,
                    market TEXT NOT NULL,
                    peak_price REAL DEFAULT 0.0,
                    partial_tp_done INTEGER DEFAULT 0,
                    entry_time REAL DEFAULT 0.0,
                    extra_json TEXT DEFAULT '{}',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (exchange, market)
                );
            """)

            # 5. Key-Value Store (Cooldowns, strategy cache, configs)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (namespace, key)
                );
            """)

            # 전략 캐시는 최신 화면 표시용이므로, 매 사이클의 차단·승인 근거는 별도 이력으로 보존한다.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_ts REAL NOT NULL,
                    exchange TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    market TEXT NOT NULL,
                    action TEXT NOT NULL,
                    policy_mode TEXT NOT NULL,
                    block_reasons_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sd_ex_ts ON strategy_decisions(exchange, decision_ts DESC);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sd_ex_mkt_ts ON strategy_decisions(exchange, market, decision_ts DESC);")

            conn.commit()

    # =========================================================================
    # Trade Memory Operations
    # =========================================================================
    def insert_trade(self, exchange: str, trade: dict[str, Any]) -> int:
        """Insert a completed trade record."""
        ex = exchange.strip().lower()
        market = str(trade.get("market", "")).upper()
        entry_time = str(trade.get("entry_time", ""))
        exit_time = str(trade.get("exit_time", ""))
        entry_price = float(trade.get("entry_price", 0.0))
        exit_price = float(trade.get("exit_price", 0.0))
        pnl_krw = float(trade.get("pnl_krw", trade.get("pnl", 0.0)))
        pnl_pct = float(trade.get("pnl_pct", 0.0))
        hold_duration_min = int(trade.get("hold_duration_min", trade.get("hold_duration", 0)))
        exit_reason = str(trade.get("exit_reason", ""))
        strategy_tags = json.dumps(trade.get("strategy_tags", []), ensure_ascii=False) if isinstance(trade.get("strategy_tags"), list) else str(trade.get("strategy_tags", ""))
        market_regime = str(trade.get("market_regime", ""))
        btc_trend = str(trade.get("btc_trend", ""))
        factor_scores = json.dumps(trade.get("factor_scores", {}), ensure_ascii=False)
        raw_data = json.dumps(trade, ensure_ascii=False)

        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trade_memory (
                    exchange, market, entry_time, exit_time, entry_price, exit_price,
                    pnl_krw, pnl_pct, hold_duration_min, exit_reason, strategy_tags,
                    market_regime, btc_trend, factor_scores, raw_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                ex, market, entry_time, exit_time, entry_price, exit_price,
                pnl_krw, pnl_pct, hold_duration_min, exit_reason, strategy_tags,
                market_regime, btc_trend, factor_scores, raw_data
            ))
            conn.commit()
            return cursor.lastrowid or 0

    def get_trades(self, exchange: str | None = None, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch completed trade records with optional exchange/market filter."""
        query = "SELECT * FROM trade_memory WHERE 1=1"
        params: list[Any] = []
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange.strip().lower())
        if market:
            query += " AND market = ?"
            params.append(market.strip().upper())
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(query, params).fetchall()
            results = []
            for row in rows:
                raw_dict = {}
                try:
                    raw_dict = json.loads(row["raw_data"])
                except Exception:
                    pass
                item = dict(row)
                if raw_dict:
                    # Merge structured fields with raw data
                    for k, v in raw_dict.items():
                        if k not in item or not item[k]:
                            item[k] = v
                results.append(item)
            return results

    def get_trade_summary_stats(self, exchange: str | None = None) -> dict[str, Any]:
        """Compute aggregate win rate, total PnL, profit factor from SQLite."""
        query = "SELECT COUNT(*) as total_trades, SUM(pnl_krw) as total_pnl_krw, " \
                "SUM(CASE WHEN pnl_krw > 0 THEN 1 ELSE 0 END) as win_trades, " \
                "SUM(CASE WHEN pnl_krw < 0 THEN 1 ELSE 0 END) as loss_trades, " \
                "SUM(CASE WHEN pnl_krw > 0 THEN pnl_krw ELSE 0 END) as gross_profit, " \
                "SUM(CASE WHEN pnl_krw < 0 THEN ABS(pnl_krw) ELSE 0 END) as gross_loss " \
                "FROM trade_memory WHERE 1=1"
        params: list[Any] = []
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange.strip().lower())

        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute(query, params).fetchone()
            if not row:
                return {"total_trades": 0, "win_rate": 0.0, "total_pnl_krw": 0.0, "profit_factor": 0.0}

            total = row["total_trades"] or 0
            wins = row["win_trades"] or 0
            total_pnl = row["total_pnl_krw"] or 0.0
            gross_profit = row["gross_profit"] or 0.0
            gross_loss = row["gross_loss"] or 0.0

            win_rate = (wins / total * 100.0) if total > 0 else 0.0
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 1.0)

            return {
                "total_trades": total,
                "win_trades": wins,
                "loss_trades": row["loss_trades"] or 0,
                "win_rate": round(win_rate, 2),
                "total_pnl_krw": round(total_pnl, 2),
                "profit_factor": round(profit_factor, 2),
            }

    # =========================================================================
    # Orders & Journal Operations
    # =========================================================================
    def upsert_order(self, exchange: str, order: dict[str, Any]) -> None:
        """Insert or update an order entry."""
        ex = exchange.strip().lower()
        client_id = str(order.get("client_order_id", order.get("uuid", order.get("order_id", ""))))
        if not client_id:
            client_id = f"{ex}_{int(time.time() * 1000)}"

        order_uuid = str(order.get("uuid", order.get("order_id", "")))
        market = str(order.get("market", "")).upper()
        side = str(order.get("side", "")).lower()
        order_type = str(order.get("ord_type", order.get("order_type", "limit")))
        price = float(order.get("price", 0.0) or 0.0)
        volume = float(order.get("volume", 0.0) or 0.0)
        state = str(order.get("state", "wait")).lower()
        filled_volume = float(order.get("executed_volume", order.get("filled_volume", 0.0)) or 0.0)
        executed_funds = float(order.get("executed_funds", order.get("funds", 0.0)) or 0.0)
        fee = float(order.get("paid_fee", order.get("fee", 0.0)) or 0.0)
        is_paper = 1 if order.get("is_paper") else 0
        raw_payload = json.dumps(order, ensure_ascii=False)
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        created_at = str(order.get("created_at", now_str))

        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO orders (
                    client_order_id, order_uuid, exchange, market, side, order_type,
                    price, volume, state, filled_volume, executed_funds, fee,
                    is_paper, raw_payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    order_uuid = COALESCE(NULLIF(excluded.order_uuid, ''), orders.order_uuid),
                    state = excluded.state,
                    filled_volume = excluded.filled_volume,
                    executed_funds = excluded.executed_funds,
                    fee = excluded.fee,
                    raw_payload = excluded.raw_payload,
                    updated_at = excluded.updated_at;
            """, (
                client_id, order_uuid, ex, market, side, order_type,
                price, volume, state, filled_volume, executed_funds, fee,
                is_paper, raw_payload, created_at, now_str
            ))
            conn.commit()

    def get_orders(self, exchange: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Fetch recent orders."""
        query = "SELECT * FROM orders WHERE 1=1"
        params: list[Any] = []
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange.strip().lower())
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(query, params).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    payload = json.loads(row["raw_payload"])
                    if isinstance(payload, dict):
                        for k, v in payload.items():
                            if k not in item or not item[k]:
                                item[k] = v
                except Exception:
                    pass
                results.append(item)
            return results

    # =========================================================================
    # Daily Stats Operations
    # =========================================================================
    def save_daily_stats(self, exchange: str, date_str: str, stats: dict[str, Any]) -> None:
        """Upsert daily statistics for an exchange and date."""
        ex = exchange.strip().lower()
        start_equity = float(stats.get("start_equity", 0.0))
        realized_pnl = float(stats.get("realized_pnl_krw", 0.0))
        total_trades = int(stats.get("total_trades", 0))
        win_trades = int(stats.get("win_trades", 0))
        consecutive_losses = int(stats.get("consecutive_losses", 0))
        cooldown_until_ts = float(stats.get("cooldown_until_ts", 0.0))
        kill_switch_active = 1 if stats.get("kill_switch_active") else 0
        kill_switch_latched_date = str(stats.get("kill_switch_latched_date", ""))
        history_json = json.dumps(stats.get("history", []), ensure_ascii=False)

        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO daily_stats (
                    exchange, date, start_equity, realized_pnl_krw, total_trades,
                    win_trades, consecutive_losses, cooldown_until_ts, kill_switch_active,
                    kill_switch_latched_date, history_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(exchange, date) DO UPDATE SET
                    start_equity = excluded.start_equity,
                    realized_pnl_krw = excluded.realized_pnl_krw,
                    total_trades = excluded.total_trades,
                    win_trades = excluded.win_trades,
                    consecutive_losses = excluded.consecutive_losses,
                    cooldown_until_ts = excluded.cooldown_until_ts,
                    kill_switch_active = excluded.kill_switch_active,
                    kill_switch_latched_date = excluded.kill_switch_latched_date,
                    history_json = excluded.history_json,
                    updated_at = CURRENT_TIMESTAMP;
            """, (
                ex, date_str, start_equity, realized_pnl, total_trades,
                win_trades, consecutive_losses, cooldown_until_ts, kill_switch_active,
                kill_switch_latched_date, history_json
            ))
            conn.commit()

    def load_daily_stats(self, exchange: str, date_str: str | None = None) -> dict[str, Any] | None:
        """Load latest or specific date daily stats for an exchange."""
        ex = exchange.strip().lower()
        query = "SELECT * FROM daily_stats WHERE exchange = ?"
        params: list[Any] = [ex]
        if date_str:
            query += " AND date = ?"
            params.append(date_str)
        query += " ORDER BY date DESC LIMIT 1"

        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute(query, params).fetchone()
            if not row:
                return None
            res = dict(row)
            try:
                res["history"] = json.loads(res.get("history_json", "[]"))
            except Exception:
                res["history"] = []
            res["kill_switch_active"] = bool(res.get("kill_switch_active"))
            return res

    def get_daily_stats_history(self, exchange: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """과거 일별 자산 및 손익 통계 이력을 최신순으로 조회한다."""
        query = "SELECT * FROM daily_stats WHERE 1=1"
        params: list[Any] = []
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange.strip().lower())
        query += " ORDER BY date DESC LIMIT ?"
        params.append(limit)

        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(query, params).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                try:
                    item["history"] = json.loads(item.get("history_json", "[]"))
                except Exception:
                    item["history"] = []
                item["kill_switch_active"] = bool(item.get("kill_switch_active"))
                start_eq = float(item.get("start_equity", 0.0) or 0.0)
                pnl_krw = float(item.get("realized_pnl_krw", 0.0) or 0.0)
                tot_trades = int(item.get("total_trades", 0) or 0)
                w_trades = int(item.get("win_trades", 0) or 0)
                item["pnl_pct"] = round((pnl_krw / start_eq * 100.0), 2) if start_eq > 0 else 0.0
                item["win_rate"] = round((w_trades / tot_trades * 100.0), 1) if tot_trades > 0 else 0.0
                results.append(item)
            return results

    # =========================================================================
    # Position State Operations
    # =========================================================================
    def save_position_state(self, exchange: str, state_dict: dict[str, Any]) -> None:
        """Save position peaks, partial TPs, and entry times into positions table."""
        ex = exchange.strip().lower()
        peaks = state_dict.get("peaks", {})
        partial_tp = state_dict.get("partial_tp_done", {})
        entry_times = state_dict.get("entry_times", {})
        all_markets = set(list(peaks.keys()) + list(partial_tp.keys()) + list(entry_times.keys()))

        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            # Clear previous entries for this exchange
            cursor.execute("DELETE FROM positions WHERE exchange = ?", (ex,))
            for mkt in all_markets:
                m_upper = mkt.upper()
                peak_val = float(peaks.get(mkt, 0.0))
                pt_val = 1 if partial_tp.get(mkt) else 0
                et_val = float(entry_times.get(mkt, 0.0))
                cursor.execute("""
                    INSERT INTO positions (exchange, market, peak_price, partial_tp_done, entry_time)
                    VALUES (?, ?, ?, ?, ?);
                """, (ex, m_upper, peak_val, pt_val, et_val))
            conn.commit()

    def load_position_state(self, exchange: str) -> dict[str, Any]:
        """Load positions into standard peaks, partial_tp_done, and entry_times dicts."""
        ex = exchange.strip().lower()
        peaks: dict[str, float] = {}
        partial_tp_done: dict[str, bool] = {}
        entry_times: dict[str, float] = {}

        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT * FROM positions WHERE exchange = ?", (ex,)).fetchall()
            for row in rows:
                mkt = row["market"]
                if row["peak_price"]:
                    peaks[mkt] = float(row["peak_price"])
                if row["partial_tp_done"]:
                    partial_tp_done[mkt] = bool(row["partial_tp_done"])
                if row["entry_time"]:
                    entry_times[mkt] = float(row["entry_time"])

        return {
            "peaks": peaks,
            "partial_tp_done": partial_tp_done,
            "entry_times": entry_times,
        }

    # =========================================================================
    # Key-Value Store Operations
    # =========================================================================
    def kv_set(self, namespace: str, key: str, value: Any) -> None:
        """Store a JSON-serializable value in kv_store."""
        val_str = json.dumps(value, ensure_ascii=False)
        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO kv_store (namespace, key, value_json, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = CURRENT_TIMESTAMP;
            """, (namespace, key, val_str))
            conn.commit()

    def kv_get(self, namespace: str, key: str, default: Any = None) -> Any:
        """Retrieve a value from kv_store."""
        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT value_json FROM kv_store WHERE namespace = ? AND key = ?",
                (namespace, key)
            ).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value_json"])
            except Exception:
                return default

    # =========================================================================
    # Strategy Decision Audit Operations
    # =========================================================================
    def record_strategy_decision(
        self,
        *,
        exchange: str,
        cycle_id: str,
        market: str,
        action: str,
        policy_mode: str,
        block_reasons: list[str],
        payload: dict[str, Any],
        decision_ts: float | None = None,
    ) -> None:
        """한 사이클의 매수·관망·차단 근거를 거래소별 SQLite에 감사용으로 저장한다."""
        with _DB_LOCK, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO strategy_decisions (
                    decision_ts, exchange, cycle_id, market, action, policy_mode,
                    block_reasons_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(decision_ts if decision_ts is not None else time.time()),
                    exchange.strip().lower(),
                    str(cycle_id),
                    market.strip().upper(),
                    str(action).upper(),
                    str(policy_mode).upper(),
                    json.dumps(block_reasons, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
            conn.commit()

    def has_recovery_entry_since(self, exchange: str, since_ts: float) -> bool:
        """같은 연속손실 회복 구간에서 반등 전용 주문을 하나로 제한한다."""
        with _DB_LOCK, self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM strategy_decisions
                WHERE exchange = ? AND policy_mode = 'RECOVERY_REBOUND'
                  AND action = 'BUY_SUBMITTED' AND decision_ts >= ?
                LIMIT 1
                """,
                (exchange.strip().lower(), float(since_ts)),
            ).fetchone()
            return row is not None

    def purge_strategy_decisions(self, exchange: str, older_than_ts: float) -> int:
        """주문·체결 원장은 보존하고, 기한이 지난 판단 이력만 정리한다."""
        with _DB_LOCK, self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM strategy_decisions WHERE exchange = ? AND decision_ts < ?",
                (exchange.strip().lower(), float(older_than_ts)),
            )
            conn.commit()
            return int(cursor.rowcount)

    def get_orderbook_slippage_decisions(self, exchange: str, since_ts: float) -> list[dict[str, Any]]:
        """호가 슬리피지 관찰/차단 감사 이력을 조회한다."""
        with _DB_LOCK, self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT decision_ts, market, action, block_reasons_json, payload_json
                FROM strategy_decisions
                WHERE exchange = ? AND policy_mode = 'ORDERBOOK_SLIPPAGE' AND decision_ts >= ?
                ORDER BY decision_ts ASC
                """,
                (exchange.strip().lower(), float(since_ts)),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                block_reasons = json.loads(row["block_reasons_json"] or "[]")
            except json.JSONDecodeError:
                block_reasons = []
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(block_reasons, list):
                block_reasons = []
            if not isinstance(payload, dict):
                payload = {}
            results.append({
                "decision_ts": float(row["decision_ts"]),
                "market": str(row["market"]),
                "action": str(row["action"]),
                "block_reasons": block_reasons,
                "payload": payload,
            })
        return results


def reset_db_manager_cache() -> None:
    """테스트 tearDown 등에서 경로별 싱글톤과 WAL 잠금을 해제한다."""
    with _DB_LOCK:
        managers = list(_DB_MANAGER_BY_PATH.values())
        _DB_MANAGER_BY_PATH.clear()
    for manager in managers:
        manager.dispose()
    gc.collect()


def reset_db_manager_for_path(db_path: str) -> None:
    """특정 DB 경로의 캐시된 DatabaseManager만 해제한다."""
    resolved_path = os.path.abspath(os.path.normpath(db_path))
    with _DB_LOCK:
        manager = _DB_MANAGER_BY_PATH.pop(resolved_path, None)
    if manager is not None:
        manager.dispose()


def get_db_manager(db_path: str | None = None) -> DatabaseManager:
    """정규화된 DB 경로별 DatabaseManager를 반환해 거래소 상태를 격리한다."""
    resolved_path = os.path.abspath(os.path.normpath(db_path or get_default_db_path()))
    with _DB_LOCK:
        manager = _DB_MANAGER_BY_PATH.get(resolved_path)
        if manager is None:
            manager = DatabaseManager(resolved_path)
            _DB_MANAGER_BY_PATH[resolved_path] = manager
        return manager


# =============================================================================
# Automated Migration from Legacy JSON files
# =============================================================================
def migrate_legacy_json_to_sqlite(data_dir: str | None = None) -> dict[str, int]:
    """
    Migrate existing JSON data files into SQLite database seamlessly.
    Safe to run multiple times (idempotent).
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base_data_dir = data_dir or os.path.join(project_root, "data")
    # 거래소별 DB가 분리됐으므로 완료 표식과 감사 결과도 각 data_dir에 둔다.
    exchange_dirs = {
        "bithumb": base_data_dir,
        "upbit": os.path.join(base_data_dir, "upbit"),
    }
    migration_flags = {
        exchange: os.path.join(directory, ".migrated_to_sqlite")
        for exchange, directory in exchange_dirs.items()
    }
    if all(os.path.exists(flag) for flag in migration_flags.values()):
        return {
            "bithumb_trades": 0,
            "upbit_trades": 0,
            "bithumb_orders": 0,
            "upbit_orders": 0,
            "daily_stats": 0,
        }

    databases = {
        exchange: get_db_manager(get_exchange_db_path(directory))
        for exchange, directory in exchange_dirs.items()
    }

    migration_stats = {
        "bithumb_trades": 0,
        "upbit_trades": 0,
        "bithumb_orders": 0,
        "upbit_orders": 0,
        "daily_stats": 0,
    }

    # 1. Migrate Trade Memories
    trade_files = [
        ("bithumb", os.path.join(base_data_dir, "trade_memory.json")),
        ("upbit", os.path.join(base_data_dir, "upbit", "trade_memory.json")),
    ]
    for ex, path in trade_files:
        if os.path.exists(path) and not os.path.exists(migration_flags[ex]):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                trades = data if isinstance(data, list) else data.get("completed_trades", [])
                rejected = 0
                for t in trades:
                    if isinstance(t, dict) and t.get("market"):
                        # 해당 거래소 파일은 자기 거래소 레코드만 적재한다.
                        t_ex = str(t.get("exchange", ex)).lower() or ex
                        if t_ex != ex:
                            rejected += 1
                            continue
                        databases[ex].insert_trade(ex, t)
                        migration_stats[f"{ex}_trades"] += 1
                logger.info("✅ [%s] trade_memory.json 마이그레이션 완료 (%d건, 타 거래소 %d건 제외)", ex.upper(), len(trades), rejected)
            except Exception as e:
                logger.warning(f"⚠️ [{ex.upper()}] trade_memory.json 마이그레이션 오류: {e}")

    # 2. Migrate Order Journals
    journal_files = [
        ("bithumb", os.path.join(base_data_dir, "order_journal.json")),
        ("upbit", os.path.join(base_data_dir, "upbit", "order_journal.json")),
    ]
    for ex, path in journal_files:
        if os.path.exists(path) and not os.path.exists(migration_flags[ex]):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                orders = data if isinstance(data, list) else data.get("orders", [])
                rejected = 0
                for o in orders:
                    if isinstance(o, dict) and (o.get("market") or o.get("uuid") or o.get("client_order_id")):
                        o_ex = str(o.get("exchange", ex)).lower() or ex
                        if o_ex != ex:
                            rejected += 1
                            continue
                        databases[ex].upsert_order(ex, o)
                        migration_stats[f"{ex}_orders"] += 1
                logger.info("✅ [%s] order_journal.json 마이그레이션 완료 (%d건, 타 거래소 %d건 제외)", ex.upper(), len(orders), rejected)
            except Exception as e:
                logger.warning(f"⚠️ [{ex.upper()}] order_journal.json 마이그레이션 오류: {e}")

    # 3. Migrate Daily Stats
    stats_files = [
        ("bithumb", os.path.join(base_data_dir, "daily_stats.json")),
        ("upbit", os.path.join(base_data_dir, "upbit", "daily_stats.json")),
    ]
    for ex, path in stats_files:
        if os.path.exists(path) and not os.path.exists(migration_flags[ex]):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("date"):
                    databases[ex].save_daily_stats(ex, data["date"], data)
                    migration_stats["daily_stats"] += 1
                logger.info(f"✅ [{ex.upper()}] daily_stats.json 마이그레이션 완료")
            except Exception as e:
                logger.warning(f"⚠️ [{ex.upper()}] daily_stats.json 마이그레이션 오류: {e}")

    # 4. Migrate Position States
    position_files = [
        ("bithumb", os.path.join(base_data_dir, "position_state.json")),
        ("upbit", os.path.join(base_data_dir, "upbit", "position_state.json")),
    ]
    for ex, path in position_files:
        if os.path.exists(path) and not os.path.exists(migration_flags[ex]):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    databases[ex].save_position_state(ex, data)
                logger.info(f"✅ [{ex.upper()}] position_state.json 마이그레이션 완료")
            except Exception as e:
                logger.warning(f"⚠️ [{ex.upper()}] position_state.json 마이그레이션 오류: {e}")

    for ex, migration_flag in migration_flags.items():
        if os.path.exists(migration_flag):
            continue
        try:
            # 재실행 시 중복 적재를 막는 완료 표식과 감사 가능한 결과를 함께 남긴다.
            audit_path = os.path.join(exchange_dirs[ex], "sqlite_migration.audit.json")
            with open(audit_path, "w", encoding="utf-8") as audit_file:
                json.dump({
                    "exchange": ex,
                    "db_path": databases[ex].db_path,
                    "completed_at": time.time(),
                    "counts": {key: value for key, value in migration_stats.items() if key.startswith(ex)},
                }, audit_file, ensure_ascii=False, indent=2)
            with open(migration_flag, "w", encoding="utf-8") as marker_file:
                marker_file.write("done\n")
        except Exception as exc:
            logger.warning("[%s] SQLite 마이그레이션 감사/완료 표식 기록 실패: %s", ex.upper(), exc)

    return migration_stats


def _maybe_install_test_db_cleanup() -> None:
    """unittest 실행 중 TemporaryDirectory 삭제 전 SQLite 잠금을 해제한다."""
    if "unittest" not in sys.modules:
        return
    try:
        tests_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests")
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import db_test_cleanup  # noqa: F401
    except Exception:
        pass


_maybe_install_test_db_cleanup()
