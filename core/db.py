from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

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
        CREATE TABLE IF NOT EXISTS sessions (
            id         BIGINT PRIMARY KEY,
            start_time TIMESTAMP DEFAULT now(),
            end_time   TIMESTAMP,
            notes      TEXT
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_journal (
            id           VARCHAR  PRIMARY KEY,
            symbol       VARCHAR  NOT NULL,
            side         VARCHAR,
            auto_mode    VARCHAR,
            state        VARCHAR  NOT NULL,
            entry_price  DOUBLE   DEFAULT 0,
            sl_price     DOUBLE   DEFAULT 0,
            tp_price     DOUBLE   DEFAULT 0,
            qty          DOUBLE   DEFAULT 0,
            risk_usd     DOUBLE   DEFAULT 0,
            rr_ratio     DOUBLE   DEFAULT 0,
            opp_score    INTEGER  DEFAULT 0,
            pnl_usd      DOUBLE   DEFAULT 0,
            close_reason VARCHAR  DEFAULT '',
            opened_at    BIGINT   DEFAULT 0,
            closed_at    BIGINT   DEFAULT 0,
            duration_s   INTEGER  DEFAULT 0,
            created_at   TIMESTAMP DEFAULT now()
        )
    """)

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
                 rr_ratio, opp_score, pnl_usd, close_reason,
                 opened_at, closed_at, duration_s)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            trade.opened_at,
            trade.closed_at or int(time.time()),
            trade.duration_s,
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
