"""
core/trading_db.py
──────────────────
Bridge QTS → PostgreSQL devmon.
Guarda sesiones, trades, decisiones y señales en tiempo real.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

log = logging.getLogger("qts.trading_db")
DSN = "dbname=devmon user=dev"


@contextmanager
def _db():
    con = psycopg2.connect(DSN)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return f"tr-{uuid.uuid4().hex[:10]}"


# ── SESIONES ──────────────────────────────────────────────────────────────────

def session_open(
    name: str,
    initial_balance: float,
    target_balance: float,
    mode: str = "PAPER",
    agent: str = "CLAUDE",
    strategy_focus: str = "EXPRESS_SCALP",
    behavioral_goal: str = "",
) -> str:
    sid = _id()
    try:
        with _db() as con:
            cur = con.cursor()
            cur.execute("""
                INSERT INTO trading_sessions
                    (id, name, mode, agent, strategy_focus, behavioral_goal,
                     initial_balance, current_balance, target_balance,
                     pnl, status, started_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'ACTIVE',%s)
            """, (sid, name, mode, agent, strategy_focus, behavioral_goal,
                  initial_balance, initial_balance, target_balance, _now()))
        log.info("trading_db: sesión abierta %s — $%.4f → $%.4f", sid, initial_balance, target_balance)
    except Exception as e:
        log.error("trading_db: session_open error: %s", e)
    return sid


def session_close(session_id: str, final_balance: float, notes: str = "") -> None:
    try:
        with _db() as con:
            cur = con.cursor()
            cur.execute("""
                UPDATE trading_sessions SET
                    status='COMPLETED', final_balance=%s,
                    pnl=%s - initial_balance,
                    current_balance=%s, ended_at=%s, notes=%s
                WHERE id=%s
            """, (final_balance, final_balance, final_balance, _now(), notes, session_id))
    except Exception as e:
        log.error("trading_db: session_close error: %s", e)


def session_update(session_id: str, current_balance: float) -> None:
    try:
        with _db() as con:
            cur = con.cursor()
            cur.execute("""
                UPDATE trading_sessions SET
                    current_balance=%s,
                    pnl=%s - initial_balance
                WHERE id=%s
            """, (current_balance, current_balance, session_id))
    except Exception as e:
        log.error("trading_db: session_update error: %s", e)


def session_count_trade(session_id: str, won: bool) -> None:
    try:
        with _db() as con:
            cur = con.cursor()
            cur.execute("""
                UPDATE trading_sessions SET
                    total_trades = total_trades + 1,
                    wins   = wins   + %s,
                    losses = losses + %s
                WHERE id=%s
            """, (1 if won else 0, 0 if won else 1, session_id))
    except Exception as e:
        log.error("trading_db: session_count_trade error: %s", e)


# ── TRADES ────────────────────────────────────────────────────────────────────

def trade_open(
    session_id: str,
    symbol: str,
    side: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    qty: float,
    leverage: float = 10,
    opportunity_type: str = "QUICK_SCALP",
    timeframe: str = "5m",
    confidence_score: int = 0,
    safety_score: int = 0,
    risk_usd: float = 0,
    rr_planned: float = 0,
    paper_mode: bool = True,
    pre_notes: str = "",
) -> str:
    tid = _id()
    try:
        with _db() as con:
            cur = con.cursor()
            cur.execute("""
                INSERT INTO trading_trades
                    (id, session_id, symbol, side, opportunity_type, timeframe,
                     leverage, entry_price, sl_price, tp_price, qty,
                     risk_usd, rr_planned, confidence_score, safety_score,
                     paper_mode, state, pre_notes, opened_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s,%s)
            """, (tid, session_id, symbol, side, opportunity_type, timeframe,
                  leverage, entry_price, sl_price, tp_price, qty,
                  risk_usd, rr_planned, confidence_score, safety_score,
                  paper_mode, pre_notes, _now()))
        log.info("trading_db: trade abierto %s — %s %s @ %.6f", tid, side, symbol, entry_price)
    except Exception as e:
        log.error("trading_db: trade_open error: %s", e)
    return tid


def trade_close(
    trade_id: str,
    exit_price: float,
    pnl_usd: float,
    r_multiple: float,
    close_reason: str = "MANUAL",
    fees_usd: float = 0,
    post_notes: str = "",
    mistakes: str = "",
    improvement: str = "",
) -> None:
    try:
        opened_at = None
        with _db() as con:
            cur = con.cursor()
            cur.execute("SELECT opened_at FROM trading_trades WHERE id=%s", (trade_id,))
            row = cur.fetchone()
            if row:
                opened_at = row[0]
            dur = int((datetime.now(timezone.utc) - opened_at).total_seconds()) if opened_at else 0
            cur.execute("""
                UPDATE trading_trades SET
                    state='CLOSED', exit_price=%s, pnl_usd=%s, r_multiple=%s,
                    close_reason=%s, fees_usd=%s, post_notes=%s,
                    mistakes=%s, improvement=%s, closed_at=%s, duration_s=%s
                WHERE id=%s
            """, (exit_price, pnl_usd, r_multiple, close_reason, fees_usd,
                  post_notes, mistakes, improvement, _now(), dur, trade_id))
        _update_symbol_stats(trade_id, pnl_usd, r_multiple)
        log.info("trading_db: trade cerrado %s — PnL=%.6f R=%.2f", trade_id, pnl_usd, r_multiple)
    except Exception as e:
        log.error("trading_db: trade_close error: %s", e)


def _update_symbol_stats(trade_id: str, pnl_usd: float, r_multiple: float) -> None:
    try:
        with _db() as con:
            cur = con.cursor()
            cur.execute(
                "SELECT symbol FROM trading_trades WHERE id=%s", (trade_id,)
            )
            row = cur.fetchone()
            if not row:
                return
            symbol = row[0]
            won = pnl_usd > 0
            cur.execute("""
                INSERT INTO trading_symbol_stats (symbol) VALUES (%s)
                ON CONFLICT (symbol) DO NOTHING
            """, (symbol,))
            cur.execute("""
                UPDATE trading_symbol_stats SET
                    total_trades = total_trades + 1,
                    wins         = wins   + %s,
                    losses       = losses + %s,
                    total_pnl    = total_pnl + %s,
                    best_r       = GREATEST(best_r, %s),
                    worst_r      = LEAST(worst_r, %s),
                    updated_at   = NOW()
                WHERE symbol = %s
            """, (1 if won else 0, 0 if won else 1, pnl_usd,
                  r_multiple, r_multiple, symbol))
            cur.execute("""
                UPDATE trading_symbol_stats ts SET
                    win_rate       = ROUND(wins::NUMERIC / NULLIF(total_trades,0) * 100, 3),
                    avg_r          = (SELECT ROUND(AVG(r_multiple),4) FROM trading_trades
                                      WHERE symbol=ts.symbol AND state='CLOSED'),
                    avg_confidence = (SELECT ROUND(AVG(confidence_score),2) FROM trading_trades
                                      WHERE symbol=ts.symbol AND state='CLOSED'),
                    avg_safety     = (SELECT ROUND(AVG(safety_score),2) FROM trading_trades
                                      WHERE symbol=ts.symbol AND state='CLOSED'),
                    avg_duration_s = (SELECT ROUND(AVG(duration_s)) FROM trading_trades
                                      WHERE symbol=ts.symbol AND state='CLOSED')
                WHERE symbol = %s
            """, (symbol,))
    except Exception as e:
        log.error("trading_db: _update_symbol_stats error: %s", e)


# ── DECISIONES ────────────────────────────────────────────────────────────────

def decision_save(
    session_id: str,
    symbol: str,
    decision_type: str,
    action: str,
    reasoning: str,
    confidence: int = 0,
    signals_json: dict | None = None,
    entry_price: float = 0,
    sl_price: float = 0,
    tp_price: float = 0,
    executed: bool = False,
    rejection_reason: str = "",
    latency_ms: int = 0,
    trade_id: str | None = None,
    agent: str = "CLAUDE",
) -> int:
    row_id = 0
    try:
        with _db() as con:
            cur = con.cursor()
            cur.execute("""
                INSERT INTO trading_decisions
                    (session_id, trade_id, decision_type, agent, symbol, action,
                     reasoning, confidence, signals_json, entry_price, sl_price,
                     tp_price, executed, rejection_reason, latency_ms, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (session_id, trade_id, decision_type, agent, symbol, action,
                  reasoning, confidence, json.dumps(signals_json or {}),
                  entry_price, sl_price, tp_price, executed, rejection_reason,
                  latency_ms, _now()))
            row_id = cur.fetchone()[0]
    except Exception as e:
        log.error("trading_db: decision_save error: %s", e)
    return row_id


# ── SEÑALES ───────────────────────────────────────────────────────────────────

def signal_save(
    session_id: str,
    symbol: str,
    price: float,
    change_24h: float = 0,
    volume_ratio: float = 1,
    rsi_5m: float = 0,
    rsi_15m: float = 0,
    rsi_1h: float = 0,
    ema_cross_5m: str = "",
    ema_cross_15m: str = "",
    atr_pct: float = 0,
    regime: str = "",
    trend_score: float = 0,
    absorption_score: int = 0,
    opp_score: int = 0,
    funding_rate: float = 0,
    oi_usd: float = 0,
    raw_json: dict | None = None,
    trade_id: str | None = None,
) -> int:
    row_id = 0
    try:
        with _db() as con:
            cur = con.cursor()
            cur.execute("""
                INSERT INTO trading_signals
                    (session_id, trade_id, symbol, price, change_24h, volume_ratio,
                     rsi_5m, rsi_15m, rsi_1h, ema_cross_5m, ema_cross_15m,
                     atr_pct, regime, trend_score, absorption_score, opp_score,
                     funding_rate, oi_usd, raw_json, captured_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (session_id, trade_id, symbol, price, change_24h, volume_ratio,
                  rsi_5m, rsi_15m, rsi_1h, ema_cross_5m, ema_cross_15m,
                  atr_pct, regime, trend_score, absorption_score, opp_score,
                  funding_rate, oi_usd, json.dumps(raw_json or {}), _now()))
            row_id = cur.fetchone()[0]
    except Exception as e:
        log.error("trading_db: signal_save error: %s", e)
    return row_id


# ── TOKEN USAGE ───────────────────────────────────────────────────────────────

# Precios claude-sonnet-4-6 por token
_PRICE_INPUT       = 3.00  / 1_000_000   # $3.00 / MTok
_PRICE_OUTPUT      = 15.00 / 1_000_000   # $15.00 / MTok
_PRICE_CACHE_READ  = 0.30  / 1_000_000   # $0.30 / MTok
_PRICE_CACHE_WRITE = 3.75  / 1_000_000   # $3.75 / MTok

_CREATE_TOKEN_TABLE = """
CREATE TABLE IF NOT EXISTS trading_token_usage (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT,
    call_type       TEXT DEFAULT 'agent_decision',
    input_tokens    INT  DEFAULT 0,
    output_tokens   INT  DEFAULT 0,
    cache_read      INT  DEFAULT 0,
    cache_write     INT  DEFAULT 0,
    cost_usd        NUMERIC(12,8) DEFAULT 0,
    latency_ms      INT  DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
)
"""


def _ensure_token_table(con) -> None:
    con.cursor().execute(_CREATE_TOKEN_TABLE)


def token_cost(input_tokens: int, output_tokens: int,
               cache_read: int = 0, cache_write: int = 0) -> float:
    return (input_tokens  * _PRICE_INPUT
          + output_tokens * _PRICE_OUTPUT
          + cache_read    * _PRICE_CACHE_READ
          + cache_write   * _PRICE_CACHE_WRITE)


def token_save(
    session_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    call_type: str = "agent_decision",
    latency_ms: int = 0,
    cost_override: float | None = None,
) -> None:
    cost = cost_override if cost_override is not None else token_cost(input_tokens, output_tokens, cache_read, cache_write)
    try:
        with _db() as con:
            _ensure_token_table(con)
            con.cursor().execute("""
                INSERT INTO trading_token_usage
                    (session_id, call_type, input_tokens, output_tokens,
                     cache_read, cache_write, cost_usd, latency_ms, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (session_id, call_type, input_tokens, output_tokens,
                  cache_read, cache_write, cost, latency_ms, _now()))
    except Exception as e:
        log.error("trading_db: token_save error: %s", e)


def token_stats(session_id: str | None = None) -> dict:
    """Retorna totales y proyecciones de costo."""
    try:
        with _db() as con:
            _ensure_token_table(con)
            cur = con.cursor()
            where = "WHERE session_id = %s" if session_id else ""
            params = (session_id,) if session_id else ()
            cur.execute(f"""
                SELECT
                    COUNT(*)            AS calls,
                    SUM(input_tokens)   AS in_tok,
                    SUM(output_tokens)  AS out_tok,
                    SUM(cache_read)     AS c_read,
                    SUM(cache_write)    AS c_write,
                    SUM(cost_usd)       AS total_cost,
                    MIN(created_at)     AS first_at,
                    MAX(created_at)     AS last_at
                FROM trading_token_usage {where}
            """, params)
            row = cur.fetchone()
            if not row or not row[0]:
                return {}
            calls, in_tok, out_tok, c_read, c_write, total_cost, first_at, last_at = row
            elapsed_s = max((last_at - first_at).total_seconds(), 1) if first_at and last_at else 1
            cost_per_call = total_cost / calls
            calls_per_h   = 3600 / 45  # intervalo del agente
            return {
                "calls":         calls,
                "input_tokens":  in_tok  or 0,
                "output_tokens": out_tok or 0,
                "total_cost":    float(total_cost or 0),
                "cost_per_call": float(cost_per_call),
                "proj_1h":       cost_per_call * calls_per_h,
                "proj_24h":      cost_per_call * calls_per_h * 24,
                "proj_30d":      cost_per_call * calls_per_h * 24 * 30,
            }
    except Exception as e:
        log.error("trading_db: token_stats error: %s", e)
        return {}
