from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from typing import TYPE_CHECKING, List, Optional, Tuple

import duckdb

from core.config import settings

if TYPE_CHECKING:
    from core.order_model import TradeRecord

log = logging.getLogger("qts.journal")


def get_connection() -> duckdb.DuckDBPyConnection:
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    return duckdb.connect(settings.db_path)


def initialize_db() -> None:
    con = get_connection()

    con.execute("""
        CREATE SEQUENCE IF NOT EXISTS trades_id_seq START 1
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id         BIGINT  DEFAULT nextval('trades_id_seq') PRIMARY KEY,
            symbol     VARCHAR NOT NULL,
            ts         BIGINT  NOT NULL,
            price      DOUBLE  NOT NULL,
            qty        DOUBLE  NOT NULL,
            side       VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT now()
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS tickers (
            symbol        VARCHAR NOT NULL,
            ts            BIGINT  NOT NULL,
            last_price    DOUBLE,
            funding_rate  DOUBLE,
            open_interest DOUBLE,
            volume_24h    DOUBLE,
            created_at    TIMESTAMP DEFAULT now()
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS trading_sessions (
            id              VARCHAR PRIMARY KEY,
            name            VARCHAR DEFAULT 'Nueva Sesión',
            start_ts        BIGINT  NOT NULL,
            end_ts          BIGINT  DEFAULT 0,
            initial_balance DOUBLE  DEFAULT 0,
            final_balance   DOUBLE  DEFAULT 0,
            pnl             DOUBLE  DEFAULT 0,
            api_cost        DOUBLE  DEFAULT 0,
            status          VARCHAR DEFAULT 'ACTIVE'
        )
    """)

    con.execute("""
        CREATE SEQUENCE IF NOT EXISTS logs_id_seq START 1
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS system_logs (
            id         BIGINT  DEFAULT nextval('logs_id_seq') PRIMARY KEY,
            trace_id   VARCHAR,
            ts         BIGINT  NOT NULL,
            level      VARCHAR NOT NULL,
            component  VARCHAR NOT NULL,
            event      VARCHAR NOT NULL,
            message    TEXT,
            payload    JSON
        )
    """)

    # Índices para consultas analíticas frecuentes
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_logs_trace   ON system_logs (trace_id)",
        "CREATE INDEX IF NOT EXISTS idx_logs_event   ON system_logs (event)",
        "CREATE INDEX IF NOT EXISTS idx_logs_level   ON system_logs (level)",
        "CREATE INDEX IF NOT EXISTS idx_logs_ts      ON system_logs (ts)",
        "CREATE INDEX IF NOT EXISTS idx_logs_comp    ON system_logs (component)",
    ]:
        try:
            con.execute(idx_sql)
        except Exception:
            pass

    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_journal (
            id           VARCHAR PRIMARY KEY,
            symbol       VARCHAR NOT NULL,
            side         VARCHAR,
            auto_mode    VARCHAR,
            state        VARCHAR NOT NULL,
            entry_price  DOUBLE  DEFAULT 0,
            sl_price     DOUBLE  DEFAULT 0,
            tp_price     DOUBLE  DEFAULT 0,
            qty          DOUBLE  DEFAULT 0,
            risk_usd     DOUBLE  DEFAULT 0,
            rr_ratio     DOUBLE  DEFAULT 0,
            opp_score    INTEGER DEFAULT 0,
            pnl_usd      DOUBLE  DEFAULT 0,
            close_reason VARCHAR DEFAULT '',
            strategy_tag VARCHAR DEFAULT 'absorcion',
            ai_reasoning TEXT    DEFAULT '',
            opened_at    BIGINT  DEFAULT 0,
            closed_at    BIGINT  DEFAULT 0,
            duration_s   INTEGER DEFAULT 0,
            created_at   TIMESTAMP DEFAULT now(),
            session_id   VARCHAR DEFAULT ''
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS monitored_symbols (
            symbol     VARCHAR PRIMARY KEY,
            vol_24h    DOUBLE  NOT NULL,
            updated_at BIGINT  NOT NULL
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS symbol_trade_detail (
            trade_id         VARCHAR PRIMARY KEY,
            symbol           VARCHAR NOT NULL,
            side             VARCHAR  DEFAULT '',
            entry_price      DOUBLE   DEFAULT 0,
            exit_price       DOUBLE   DEFAULT 0,
            pnl_usd          DOUBLE   DEFAULT 0,
            duration_s       INTEGER  DEFAULT 0,
            close_reason     VARCHAR  DEFAULT '',
            slippage_pct     DOUBLE   DEFAULT 0,
            risk_usd         DOUBLE   DEFAULT 0,
            rr_ratio         DOUBLE   DEFAULT 0,
            opp_score        INTEGER  DEFAULT 0,
            r_multiple       DOUBLE   DEFAULT 0,
            mfe_usd          DOUBLE   DEFAULT 0,
            mae_usd          DOUBLE   DEFAULT 0,
            regime           VARCHAR  DEFAULT '',
            cvd_momentum     VARCHAR  DEFAULT '',
            rsi              DOUBLE   DEFAULT 0,
            absorption_score INTEGER  DEFAULT 0,
            trend_score      DOUBLE   DEFAULT 0,
            viability_flags  INTEGER  DEFAULT 0,
            session_id       VARCHAR  DEFAULT '',
            timestamp        BIGINT   NOT NULL
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS symbol_stats (
            symbol              VARCHAR PRIMARY KEY,
            total_trades        INTEGER  DEFAULT 0,
            wins                INTEGER  DEFAULT 0,
            losses              INTEGER  DEFAULT 0,
            win_rate            DOUBLE   DEFAULT 0,
            avg_pnl_usd         DOUBLE   DEFAULT 0,
            total_pnl_usd       DOUBLE   DEFAULT 0,
            best_trade_usd      DOUBLE   DEFAULT 0,
            worst_trade_usd     DOUBLE   DEFAULT 0,
            avg_duration_s      INTEGER  DEFAULT 0,
            avg_slippage_pct    DOUBLE   DEFAULT 0,
            avg_rr_ratio        DOUBLE   DEFAULT 0,
            avg_opp_score       DOUBLE   DEFAULT 0,
            avg_mfe_usd         DOUBLE   DEFAULT 0,
            avg_mae_usd         DOUBLE   DEFAULT 0,
            best_side           VARCHAR  DEFAULT '',
            close_reason_dist   JSON     DEFAULT '{}',
            performance_score   DOUBLE   DEFAULT 0,
            notes               TEXT     DEFAULT '',
            last_trade_ts       BIGINT   DEFAULT 0,
            updated_at          BIGINT   DEFAULT 0
        )
    """)

    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_std_symbol ON symbol_trade_detail (symbol)")
    except Exception:
        pass

    # Migraciones: agregar columnas si no existen (bases de datos previas)
    for migration in [
        "ALTER TABLE trade_journal ADD COLUMN strategy_tag VARCHAR DEFAULT 'absorcion'",
        "ALTER TABLE trade_journal ADD COLUMN ai_reasoning TEXT DEFAULT ''",
        "ALTER TABLE trade_journal ADD COLUMN session_id VARCHAR DEFAULT ''",
        "ALTER TABLE trading_sessions ADD COLUMN name VARCHAR DEFAULT 'Nueva Sesión'",
        "ALTER TABLE trading_sessions ADD COLUMN api_cost DOUBLE DEFAULT 0",
        "ALTER TABLE trading_sessions ADD COLUMN target_pnl DOUBLE DEFAULT 0",
        "ALTER TABLE trading_sessions ADD COLUMN max_drawdown DOUBLE DEFAULT 0",
        "ALTER TABLE trading_sessions ADD COLUMN duration_h DOUBLE DEFAULT 0",
    ]:
        try:
            con.execute(migration)
        except Exception:
            pass  # columna ya existe

    con.close()


def save_trade(trade: "TradeRecord") -> None:
    """Persiste un trade cerrado/fallido en trade_journal."""
    req = trade.request
    if not req:
        return
    try:
        con = get_connection()
        con.execute("""
            INSERT OR REPLACE INTO trade_journal
                (id, symbol, side, auto_mode, state,
                 entry_price, sl_price, tp_price, qty, risk_usd,
                 rr_ratio, opp_score, pnl_usd, close_reason, strategy_tag, ai_reasoning,
                 opened_at, closed_at, duration_s, session_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade.id,
            trade.symbol,
            req.side,
            trade.auto_mode.value,
            trade.state.value,
            trade.entry_price or req.entry_price,
            req.sl_price,
            req.tp_price,
            req.qty,
            req.risk_usd,
            req.rr_ratio,
            req.opp_score,
            trade.pnl_usd,
            trade.close_reason or trade.state.value,
            req.strategy_tag,
            trade.ai_reasoning or req.ai_reasoning,
            trade.opened_at,
            trade.closed_at or int(time.time()),
            trade.duration_s,
            getattr(trade, "session_id", ""),
        ))
        con.close()
        log.info("Journal: %s guardado  %s  PnL=$%.2f",
                 trade.id, trade.symbol, trade.pnl_usd)
    except Exception as e:
        log.error("save_trade falló: %s", e)


def get_recent_trades(limit: int = 8) -> list:
    """Retorna los últimos N trades para el historial compacto."""
    try:
        con = get_connection()
        rows = con.execute("""
            SELECT symbol, side, state, pnl_usd, close_reason, closed_at, duration_s
            FROM trade_journal
            ORDER BY closed_at DESC LIMIT ?
        """, (limit,)).fetchall()
        con.close()
        return [
            {
                "symbol":       r[0],
                "side":         r[1],
                "state":        r[2],
                "pnl_usd":      float(r[3] or 0),
                "close_reason": r[4] or "",
                "closed_at":    int(r[5] or 0),
                "duration_s":   int(r[6] or 0),
            }
            for r in rows
        ]
    except Exception as e:
        log.error("get_recent_trades falló: %s", e)
        return []


def get_journal_stats() -> dict:
    """Estadísticas agregadas del historial de trades."""
    try:
        con = get_connection()
        row = con.execute("""
            SELECT
                COUNT(*)                                          AS total,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)    AS wins,
                SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END)    AS losses,
                COALESCE(SUM(pnl_usd),   0)                      AS total_pnl,
                COALESCE(AVG(pnl_usd),   0)                      AS avg_pnl,
                COALESCE(MAX(pnl_usd),   0)                      AS best,
                COALESCE(MIN(pnl_usd),   0)                      AS worst,
                COALESCE(AVG(rr_ratio),  0)                      AS avg_rr,
                COALESCE(AVG(opp_score), 0)                      AS avg_score
            FROM trade_journal WHERE state = 'CLOSED'
        """).fetchone()

        best_sym = con.execute("""
            SELECT symbol FROM trade_journal WHERE state = 'CLOSED'
            GROUP BY symbol ORDER BY SUM(pnl_usd) DESC LIMIT 1
        """).fetchone()

        con.close()

        total = int(row[0] or 0)
        wins  = int(row[1] or 0)
        return {
            "total":       total,
            "wins":        wins,
            "losses":      int(row[2] or 0),
            "win_rate":    round(wins / total * 100, 1) if total > 0 else 0.0,
            "total_pnl":   round(float(row[3]), 2),
            "avg_pnl":     round(float(row[4]), 2),
            "best_trade":  round(float(row[5]), 2),
            "worst_trade": round(float(row[6]), 2),
            "avg_rr":      round(float(row[7]), 2),
            "avg_score":   round(float(row[8]), 1),
            "best_symbol": best_sym[0].replace("USDT", "") if best_sym else "──",
        }
    except Exception as e:
        log.error("get_journal_stats falló: %s", e)
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "avg_pnl": 0.0, "best_trade": 0.0,
            "worst_trade": 0.0, "avg_rr": 0.0, "avg_score": 0.0,
            "best_symbol": "──",
        }


def get_all_trades(limit: int = 200) -> list:
    """Retorna historial completo de trades para el Journal."""
    try:
        con = get_connection()
        rows = con.execute("""
            SELECT id, symbol, side, auto_mode, state,
                   entry_price, sl_price, tp_price, qty, risk_usd,
                   rr_ratio, opp_score, pnl_usd, close_reason, strategy_tag,
                   opened_at, closed_at, duration_s, session_id
            FROM trade_journal
            ORDER BY closed_at DESC LIMIT ?
        """, (limit,)).fetchall()
        con.close()
        return [
            {
                "id":           r[0],
                "symbol":       r[1],
                "side":         r[2] or "",
                "auto_mode":    r[3] or "",
                "state":        r[4],
                "entry_price":  float(r[5] or 0),
                "sl_price":     float(r[6] or 0),
                "tp_price":     float(r[7] or 0),
                "qty":          float(r[8] or 0),
                "risk_usd":     float(r[9] or 0),
                "rr_ratio":     float(r[10] or 0),
                "opp_score":    int(r[11] or 0),
                "pnl_usd":      float(r[12] or 0),
                "close_reason": r[13] or "",
                "strategy_tag": r[14] or "absorcion",
                "opened_at":    int(r[15] or 0),
                "closed_at":    int(r[16] or 0),
                "duration_s":   int(r[17] or 0),
                "session_id":   r[18] or "",
            }
            for r in rows
        ]
    except Exception as e:
        log.error("get_all_trades falló: %s", e)
        return []


def get_cumulative_pnl() -> list:
    """Retorna [(closed_at, cum_pnl)] para el gráfico de equity curve."""
    try:
        con = get_connection()
        rows = con.execute("""
            SELECT closed_at, pnl_usd FROM trade_journal
            WHERE state = 'CLOSED' AND closed_at > 0
            ORDER BY closed_at ASC
        """).fetchall()
        con.close()
        cum = 0.0
        result = []
        for ts, pnl in rows:
            cum += float(pnl or 0)
            result.append((int(ts), cum))
        return result
    except Exception as e:
        log.error("get_cumulative_pnl falló: %s", e)
        return []


def save_session(session_data: dict) -> None:
    """Guarda/actualiza el registro de una sesión TSAA."""
    try:
        con = get_connection()
        con.execute("""
            INSERT OR REPLACE INTO trading_sessions
                (id, name, start_ts, end_ts, initial_balance, final_balance,
                 pnl, api_cost, status, target_pnl, max_drawdown, duration_h)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session_data["id"],
            session_data.get("name", "Nueva Sesión"),
            session_data["start_ts"],
            session_data["end_ts"],
            session_data["initial_balance"],
            session_data["final_balance"],
            session_data["pnl"],
            session_data.get("api_cost", 0.0),
            session_data["status"],
            session_data.get("target_pnl", 0.0),
            session_data.get("max_drawdown", 0.0),
            session_data.get("duration_h", 0.0),
        ))
        con.close()
    except Exception as e:
        log.error("save_session falló: %s", e)


def get_active_session() -> Optional[dict]:
    """Retorna la sesión ACTIVE más reciente, o None si no hay ninguna."""
    try:
        con = get_connection()
        row = con.execute("""
            SELECT id, name, start_ts, end_ts, initial_balance, final_balance,
                   pnl, api_cost, status, target_pnl, max_drawdown, duration_h
            FROM trading_sessions
            WHERE status = 'ACTIVE'
            ORDER BY start_ts DESC LIMIT 1
        """).fetchone()
        con.close()
        if not row:
            return None
        return {
            "id": row[0], "name": row[1], "start_ts": int(row[2]),
            "end_ts": int(row[3]), "initial_balance": float(row[4]),
            "final_balance": float(row[5]), "pnl": float(row[6]),
            "api_cost": float(row[7]), "status": row[8],
            "target_pnl": float(row[9] or 0), "max_drawdown": float(row[10] or 0),
            "duration_h": float(row[11] or 0),
        }
    except Exception as e:
        log.error("get_active_session falló: %s", e)
        return None


def get_session_by_id(session_id: str) -> Optional[dict]:
    """Retorna una sesión por ID, o None."""
    try:
        con = get_connection()
        row = con.execute("""
            SELECT id, name, start_ts, end_ts, initial_balance, final_balance,
                   pnl, api_cost, status, target_pnl, max_drawdown, duration_h
            FROM trading_sessions WHERE id = ?
        """, (session_id,)).fetchone()
        con.close()
        if not row:
            return None
        return {
            "id": row[0], "name": row[1], "start_ts": int(row[2]),
            "end_ts": int(row[3]), "initial_balance": float(row[4]),
            "final_balance": float(row[5]), "pnl": float(row[6]),
            "api_cost": float(row[7]), "status": row[8],
            "target_pnl": float(row[9] or 0), "max_drawdown": float(row[10] or 0),
            "duration_h": float(row[11] or 0),
        }
    except Exception as e:
        log.error("get_session_by_id falló: %s", e)
        return None


def close_all_sessions() -> None:
    """Marca todas las sesiones ACTIVE como CLOSED (útil para limpieza de huérfanas)."""
    try:
        con = get_connection()
        now = int(time.time())
        con.execute("""
            UPDATE trading_sessions 
            SET status = 'CLOSED', end_ts = ? 
            WHERE status != 'CLOSED'
        """, (now,))
        con.close()
        log.info("DB: Todas las sesiones activas han sido cerradas forzosamente.")
    except Exception as e:
        log.error("close_all_sessions falló: %s", e)


def get_session_trades(session_id: str) -> list:
    """Retorna todos los trades asociados a una sesión para la auditoría."""
    try:
        con = get_connection()
        rows = con.execute("""
            SELECT id, symbol, side, state, pnl_usd, opp_score, rr_ratio, close_reason, strategy_tag
            FROM trade_journal
            WHERE session_id = ? AND state = 'CLOSED'
            ORDER BY closed_at ASC
        """, (session_id,)).fetchall()
        con.close()
        return [
            {
                "id":           r[0],
                "symbol":       r[1],
                "side":         r[2] or "",
                "state":        r[3],
                "pnl_usd":      float(r[4] or 0),
                "opp_score":    int(r[5] or 0),
                "rr_ratio":     float(r[6] or 0),
                "close_reason": r[7] or "",
                "strategy_tag": r[8] or "",
            }
            for r in rows
        ]
    except Exception as e:
        log.error("get_session_trades falló: %s", e)
        return []

def get_all_sessions(limit: int = 50) -> list:
    """Retorna historial completo de sesiones para la UI."""
    try:
        con = get_connection()
        rows = con.execute("""
            SELECT id, name, start_ts, end_ts, initial_balance, final_balance,
                   pnl, api_cost, status, target_pnl, max_drawdown, duration_h
            FROM trading_sessions
            ORDER BY start_ts DESC LIMIT ?
        """, (limit,)).fetchall()
        con.close()
        return [
            {
                "id":              r[0],
                "name":            r[1],
                "start_ts":        int(r[2]),
                "end_ts":          int(r[3]),
                "initial_balance": float(r[4]),
                "final_balance":   float(r[5]),
                "pnl":             float(r[6]),
                "api_cost":        float(r[7]),
                "status":          r[8],
                "target_pnl":      float(r[9] or 0),
                "max_drawdown":    float(r[10] or 0),
                "duration_h":      float(r[11] or 0),
            }
            for r in rows
        ]
    except Exception as e:
        log.error("get_all_sessions falló: %s", e)
        return []


# ─── Módulo de Observabilidad (Logs Estructurados) ───────────────────────────

_LOG_QUEUE: queue.Queue = queue.Queue()
_WORKER_THREAD: threading.Thread | None = None


def _log_worker():
    """Worker que procesa la cola de logs de forma asíncrona."""
    con = None
    try:
        con = get_connection()
        while True:
            item = _LOG_QUEUE.get()
            if item is None:
                break
            try:
                con.execute("""
                    INSERT INTO system_logs (trace_id, ts, level, component, event, message, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    item.get("trace_id"),
                    item.get("ts", int(time.time() * 1000)),
                    item.get("level", "INFO"),
                    item.get("component", "SYSTEM"),
                    item.get("event", "UNKNOWN"),
                    item.get("message", ""),
                    json.dumps(item.get("payload", {}))
                ))
            except Exception as e:
                log.error("Error writing system log: %s", e)
            finally:
                _LOG_QUEUE.task_done()
    except Exception as e:
        log.error("Critical error in LogWorker: %s", e)
    finally:
        if con:
            con.close()


def enqueue_system_log(entry: dict) -> None:
    """Envía un log estructurado a la cola asíncrona."""
    global _WORKER_THREAD
    if not settings.system_logging_enabled:
        return

    if _WORKER_THREAD is None or not _WORKER_THREAD.is_alive():
        _WORKER_THREAD = threading.Thread(target=_log_worker, daemon=True, name="LogWorker")
        _WORKER_THREAD.start()

    _LOG_QUEUE.put(entry)


def get_logs_for_analyst(
    hours: int = 24,
    levels: tuple = ("WARNING", "ERROR", "CRITICAL"),
    limit: int = 500,
) -> list:
    """
    Consulta system_logs optimizada para el agente analista.
    Retorna los eventos relevantes de las últimas N horas con su payload JSON.
    """
    try:
        since_ms = int((time.time() - hours * 3600) * 1000)
        placeholders = ", ".join("?" for _ in levels)
        con = get_connection()
        rows = con.execute(f"""
            SELECT trace_id, ts, level, component, event, message, payload
            FROM system_logs
            WHERE ts >= ? AND level IN ({placeholders})
            ORDER BY ts DESC
            LIMIT ?
        """, (since_ms, *levels, limit)).fetchall()
        con.close()
        return [
            {
                "trace_id":  r[0] or "",
                "ts":        int(r[1]),
                "level":     r[2],
                "component": r[3],
                "event":     r[4],
                "message":   r[5] or "",
                "payload":   json.loads(r[6]) if r[6] else {},
            }
            for r in rows
        ]
    except Exception as e:
        log.error("get_logs_for_analyst falló: %s", e)
        return []


def get_trade_analytics(hours: int = 48) -> dict:
    """
    Resumen analítico para el agente: patrones de SL, razones de cierre,
    latencias de ejecución y errores por símbolo.
    Diseñado para responder las preguntas del agente analista:
      · ¿Patrones en trades que terminaron en SL?
      · ¿Oportunidades perdidas por margen?
      · ¿Latencia IA→ejecución?
      · ¿Símbolos con errores frecuentes?
    """
    try:
        since_ts = int(time.time()) - hours * 3600
        con = get_connection()

        # Distribución de razones de cierre
        close_reasons = con.execute("""
            SELECT close_reason, COUNT(*) as n, AVG(pnl_usd) as avg_pnl
            FROM trade_journal
            WHERE closed_at >= ? AND state = 'CLOSED'
            GROUP BY close_reason ORDER BY n DESC
        """, (since_ts,)).fetchall()

        # Símbolos con más errores en system_logs
        error_symbols = con.execute("""
            SELECT
                json_extract_string(payload, '$.symbol') AS sym,
                COUNT(*) as n
            FROM system_logs
            WHERE ts >= ? AND level IN ('ERROR','CRITICAL')
              AND json_extract_string(payload, '$.symbol') IS NOT NULL
            GROUP BY sym ORDER BY n DESC LIMIT 10
        """, (int(since_ts * 1000),)).fetchall()

        # Latencia media entre ANALYSIS_START y ORDER_SUCCESS (mismo trace_id)
        latency = con.execute("""
            SELECT AVG(end_ts - start_ts) / 1000.0 AS avg_latency_s
            FROM (
                SELECT
                    trace_id,
                    MIN(CASE WHEN event='ANALYSIS_START'  THEN ts END) AS start_ts,
                    MAX(CASE WHEN event='ORDER_SUCCESS'   THEN ts END) AS end_ts
                FROM system_logs
                WHERE ts >= ?
                GROUP BY trace_id
                HAVING start_ts IS NOT NULL AND end_ts IS NOT NULL
            )
        """, (int(since_ts * 1000),)).fetchone()

        # Trades en SL con sus indicadores del payload
        sl_trades = con.execute("""
            SELECT symbol, side, pnl_usd, opp_score, rr_ratio, ai_reasoning
            FROM trade_journal
            WHERE closed_at >= ? AND close_reason LIKE '%sl%'
            ORDER BY closed_at DESC LIMIT 20
        """, (since_ts,)).fetchall()

        con.close()

        return {
            "close_reasons": [
                {"reason": r[0], "count": int(r[1]), "avg_pnl": round(float(r[2] or 0), 2)}
                for r in close_reasons
            ],
            "error_symbols": [
                {"symbol": r[0], "error_count": int(r[1])}
                for r in error_symbols
            ],
            "avg_latency_ai_to_fill_s": round(float(latency[0] or 0), 2) if latency else 0.0,
            "sl_trades": [
                {
                    "symbol":       r[0],
                    "side":         r[1],
                    "pnl_usd":      float(r[2] or 0),
                    "opp_score":    int(r[3] or 0),
                    "rr_ratio":     float(r[4] or 0),
                    "ai_reasoning": r[5] or "",
                }
                for r in sl_trades
            ],
        }
    except Exception as e:
        log.error("get_trade_analytics falló: %s", e)
        return {}


# ─── Símbolos Monitoreados ────────────────────────────────────────────────────

def save_monitored_symbols(symbols: List[Tuple[str, float]]) -> None:
    """
    Guarda la lista de símbolos factibles (symbol, vol_24h) en la DB.
    Reemplaza completamente la lista anterior.
    """
    if not symbols:
        return
    try:
        now = int(time.time())
        con = get_connection()
        con.execute("DELETE FROM monitored_symbols")
        con.executemany(
            "INSERT INTO monitored_symbols (symbol, vol_24h, updated_at) VALUES (?, ?, ?)",
            [(sym, vol, now) for sym, vol in symbols],
        )
        con.close()
        log.info("DB: %d símbolos guardados en monitored_symbols", len(symbols))
    except Exception as e:
        log.error("save_monitored_symbols falló: %s", e)


def load_monitored_symbols() -> List[str]:
    """
    Carga la lista de símbolos desde la DB (cache del último fetch exitoso).
    Retorna [] si la tabla está vacía.
    """
    try:
        con = get_connection()
        rows = con.execute(
            "SELECT symbol FROM monitored_symbols ORDER BY vol_24h DESC"
        ).fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("load_monitored_symbols falló: %s", e)
        return []


# ─── Historial y Estadísticas por Símbolo ────────────────────────────────────

def _generate_symbol_notes(
    symbol: str, total: int, win_rate: float, avg_pnl: float,
    avg_slip: float, avg_dur_s: int, best_side: str, reasons: list,
) -> str:
    """Genera una descripción automática del comportamiento del par."""
    parts: list[str] = []
    if total < 3:
        parts.append(f"Solo {total} trade(s) — historial insuficiente.")
    else:
        wr_pct = round(win_rate * 100)
        if win_rate >= 0.65:
            parts.append(f"Par confiable ({wr_pct}% win rate).")
        elif win_rate >= 0.5:
            parts.append(f"Win rate moderado ({wr_pct}%).")
        else:
            parts.append(f"Win rate bajo ({wr_pct}%) — considerar evitar.")

        if avg_pnl > 0.5:
            parts.append(f"PnL promedio positivo (${avg_pnl:.2f}).")
        elif avg_pnl < -0.3:
            parts.append(f"PnL promedio negativo (${avg_pnl:.2f}) — revisar estrategia.")

    if avg_slip > 0.3:
        parts.append(f"Slippage alto ({avg_slip:.2f}%) — usar límites conservadores.")

    avg_dur_m = avg_dur_s / 60 if avg_dur_s else 0
    if avg_dur_m < 3:
        parts.append(f"Trades muy cortos (avg {avg_dur_m:.1f}min) — alta volatilidad.")
    elif avg_dur_m > 30:
        parts.append(f"Trades largos (avg {avg_dur_m:.1f}min).")

    if best_side:
        side_label = "LONG" if best_side == "Buy" else "SHORT"
        parts.append(f"Mejor rendimiento en {side_label}.")

    top_reasons = sorted(reasons, key=lambda r: r[1], reverse=True)[:2]
    if top_reasons:
        labels = ", ".join(f"{r[0]}({r[1]})" for r in top_reasons)
        parts.append(f"Cierres frecuentes: {labels}.")

    return " ".join(parts) if parts else "Sin notas."


def _update_symbol_stats(con: "duckdb.DuckDBPyConnection", symbol: str) -> None:
    """Recomputa y persiste las estadísticas agregadas para un símbolo."""
    now = int(time.time())

    row = con.execute("""
        SELECT
            COUNT(*)                                           AS total,
            SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)     AS wins,
            SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END)     AS losses,
            COALESCE(AVG(pnl_usd), 0)                         AS avg_pnl,
            COALESCE(SUM(pnl_usd), 0)                         AS total_pnl,
            COALESCE(MAX(pnl_usd), 0)                         AS best_trade,
            COALESCE(MIN(pnl_usd), 0)                         AS worst_trade,
            COALESCE(AVG(duration_s), 0)                      AS avg_dur,
            COALESCE(AVG(slippage_pct), 0)                    AS avg_slip,
            COALESCE(AVG(rr_ratio), 0)                        AS avg_rr,
            COALESCE(AVG(opp_score), 0)                       AS avg_score,
            COALESCE(AVG(mfe_usd), 0)                         AS avg_mfe,
            COALESCE(AVG(mae_usd), 0)                         AS avg_mae,
            MAX(timestamp)                                     AS last_ts
        FROM symbol_trade_detail WHERE symbol = ?
    """, (symbol,)).fetchone()

    if not row or not row[0]:
        return

    total      = int(row[0])
    wins       = int(row[1] or 0)
    losses     = int(row[2] or 0)
    win_rate   = wins / total if total > 0 else 0.0
    avg_pnl    = float(row[3] or 0)
    total_pnl  = float(row[4] or 0)
    best_trade = float(row[5] or 0)
    worst_trade= float(row[6] or 0)
    avg_dur    = int(row[7] or 0)
    avg_slip   = float(row[8] or 0)
    avg_rr     = float(row[9] or 0)
    avg_score  = float(row[10] or 0)
    avg_mfe    = float(row[11] or 0)
    avg_mae    = float(row[12] or 0)
    last_ts    = int(row[13] or 0)

    side_row = con.execute("""
        SELECT side, SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS w
        FROM symbol_trade_detail WHERE symbol = ?
        GROUP BY side ORDER BY w DESC LIMIT 1
    """, (symbol,)).fetchone()
    best_side = side_row[0] if side_row else ""

    reasons = con.execute("""
        SELECT close_reason, COUNT(*) FROM symbol_trade_detail
        WHERE symbol = ? GROUP BY close_reason ORDER BY COUNT(*) DESC
    """, (symbol,)).fetchall()
    reason_dist = json.dumps({r[0]: int(r[1]) for r in reasons})

    # Score 0-100: win_rate(40) + rr(20) + avg_pnl(20) + slippage(20)
    score = (
        win_rate * 40.0
        + min(1.0, avg_rr / 2.0) * 20.0
        + min(1.0, max(0.0, avg_pnl) / 0.50) * 20.0
        + max(0.0, 1.0 - avg_slip / 0.30) * 20.0
    )
    score = round(min(100.0, max(0.0, score)), 1)

    notes = _generate_symbol_notes(
        symbol, total, win_rate, avg_pnl, avg_slip, avg_dur, best_side, reasons,
    )

    con.execute("""
        INSERT OR REPLACE INTO symbol_stats
            (symbol, total_trades, wins, losses, win_rate, avg_pnl_usd, total_pnl_usd,
             best_trade_usd, worst_trade_usd, avg_duration_s, avg_slippage_pct, avg_rr_ratio,
             avg_opp_score, avg_mfe_usd, avg_mae_usd, best_side, close_reason_dist,
             performance_score, notes, last_trade_ts, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        symbol, total, wins, losses, win_rate, avg_pnl, total_pnl,
        best_trade, worst_trade, avg_dur, avg_slip, avg_rr,
        avg_score, avg_mfe, avg_mae, best_side, reason_dist,
        score, notes, last_ts, now,
    ))


def save_symbol_trade_detail(trade: "TradeRecord", ms=None) -> None:
    """
    Guarda el detalle del trade en symbol_trade_detail y actualiza symbol_stats.
    ms: MarketState opcional para capturar régimen, CVD, RSI al cierre.
    """
    req = trade.request
    if not req:
        return
    try:
        req_entry = req.entry_price
        act_entry = trade.entry_price or req_entry
        slippage_pct = 0.0
        if req_entry > 0:
            slippage_pct = abs(act_entry - req_entry) / req_entry * 100

        qty = req.qty or 1.0
        if req.side == "Buy":
            exit_price = act_entry + trade.pnl_usd / qty
        else:
            exit_price = act_entry - trade.pnl_usd / qty

        risk   = abs(req.risk_usd) if req.risk_usd else 0.0
        r_mult = trade.pnl_usd / risk if risk > 0 else 0.0

        regime = cvd_momentum = ""
        rsi = absorption_score = trend_score = 0.0
        if ms is not None:
            _regime = getattr(ms, "regime", "")
            regime = _regime.value if hasattr(_regime, "value") else str(_regime)
            cvd_momentum     = str(getattr(ms, "cvd_momentum", ""))
            rsi              = float(getattr(ms, "rsi_1m", 0.0) or 0)
            absorption_score = float(getattr(ms, "absorption_score", 0) or 0)
            trend_score      = float(getattr(ms, "trend_score", 0.0) or 0)

        con = get_connection()
        con.execute("""
            INSERT OR REPLACE INTO symbol_trade_detail
                (trade_id, symbol, side, entry_price, exit_price, pnl_usd, duration_s,
                 close_reason, slippage_pct, risk_usd, rr_ratio, opp_score,
                 r_multiple, mfe_usd, mae_usd, regime, cvd_momentum, rsi,
                 absorption_score, trend_score, viability_flags, session_id, timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade.id, trade.symbol, req.side,
            act_entry, exit_price, trade.pnl_usd, trade.duration_s,
            trade.close_reason, slippage_pct, req.risk_usd, req.rr_ratio, req.opp_score,
            r_mult,
            getattr(trade, "max_pnl", 0.0),
            getattr(trade, "min_pnl", 0.0),
            regime, cvd_momentum, rsi, absorption_score, trend_score,
            getattr(trade, "signal_health", -1),
            getattr(trade, "session_id", ""),
            trade.closed_at or int(time.time()),
        ))
        _update_symbol_stats(con, trade.symbol)
        con.close()
        log.debug("symbol_trade_detail: %s guardado  %s  PnL=$%.2f",
                  trade.id, trade.symbol, trade.pnl_usd)
    except Exception as e:
        log.error("save_symbol_trade_detail falló: %s", e)


def get_symbol_stats(symbol: str = None) -> list:
    """
    Retorna estadísticas de rendimiento por símbolo.
    symbol=None → top 20 pares por performance_score.
    """
    try:
        con = get_connection()
        if symbol:
            rows = con.execute("""
                SELECT symbol, total_trades, wins, losses, win_rate, avg_pnl_usd,
                       total_pnl_usd, best_trade_usd, worst_trade_usd, avg_duration_s,
                       avg_slippage_pct, avg_rr_ratio, avg_opp_score, avg_mfe_usd, avg_mae_usd,
                       best_side, close_reason_dist, performance_score, notes, last_trade_ts
                FROM symbol_stats WHERE symbol = ?
            """, (symbol,)).fetchall()
        else:
            rows = con.execute("""
                SELECT symbol, total_trades, wins, losses, win_rate, avg_pnl_usd,
                       total_pnl_usd, best_trade_usd, worst_trade_usd, avg_duration_s,
                       avg_slippage_pct, avg_rr_ratio, avg_opp_score, avg_mfe_usd, avg_mae_usd,
                       best_side, close_reason_dist, performance_score, notes, last_trade_ts
                FROM symbol_stats ORDER BY performance_score DESC LIMIT 20
            """).fetchall()
        con.close()
        return [
            {
                "symbol":            r[0],
                "total_trades":      int(r[1] or 0),
                "wins":              int(r[2] or 0),
                "losses":            int(r[3] or 0),
                "win_rate_pct":      round(float(r[4] or 0) * 100, 1),
                "avg_pnl_usd":       round(float(r[5] or 0), 3),
                "total_pnl_usd":     round(float(r[6] or 0), 2),
                "best_trade_usd":    round(float(r[7] or 0), 2),
                "worst_trade_usd":   round(float(r[8] or 0), 2),
                "avg_duration_s":    int(r[9] or 0),
                "avg_slippage_pct":  round(float(r[10] or 0), 3),
                "avg_rr_ratio":      round(float(r[11] or 0), 2),
                "avg_opp_score":     round(float(r[12] or 0), 1),
                "avg_mfe_usd":       round(float(r[13] or 0), 3),
                "avg_mae_usd":       round(float(r[14] or 0), 3),
                "best_side":         r[15] or "",
                "close_reason_dist": json.loads(r[16]) if r[16] else {},
                "performance_score": round(float(r[17] or 0), 1),
                "notes":             r[18] or "",
                "last_trade_ts":     int(r[19] or 0),
            }
            for r in rows
        ]
    except Exception as e:
        log.error("get_symbol_stats falló: %s", e)
        return []

