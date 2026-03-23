from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from typing import TYPE_CHECKING, List, Tuple

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

    # Migraciones: agregar columnas si no existen (bases de datos previas)
    for migration in [
        "ALTER TABLE trade_journal ADD COLUMN strategy_tag VARCHAR DEFAULT 'absorcion'",
        "ALTER TABLE trade_journal ADD COLUMN ai_reasoning TEXT DEFAULT ''",
        "ALTER TABLE trade_journal ADD COLUMN session_id VARCHAR DEFAULT ''",
        "ALTER TABLE trading_sessions ADD COLUMN name VARCHAR DEFAULT 'Nueva Sesión'",
        "ALTER TABLE trading_sessions ADD COLUMN api_cost DOUBLE DEFAULT 0",
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
                (id, name, start_ts, end_ts, initial_balance, final_balance, pnl, api_cost, status)
            VALUES (?,?,?,?,?,?,?,?,?)
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
        ))
        con.close()
    except Exception as e:
        log.error("save_session falló: %s", e)


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
            SELECT id, name, start_ts, end_ts, initial_balance, final_balance, pnl, api_cost, status
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

