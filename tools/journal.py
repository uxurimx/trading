#!/usr/bin/env python3
"""
tools/journal.py — Manual scalping journal CLI
Uso: python -m tools.journal <comando> [args]

Comandos:
  session new   [--name X] [--target 5.0] [--balance 1.38] [--goal "solo entradas con conf >= 7"]
  session list
  session close [id]
  session status

  trade open    --sym XRPUSDT --side LONG --entry 1.22 --sl 1.206 --tp 1.248
                [--qty 100] [--lev 10] [--tf 5m] [--type EXPRESS_SCALP]
                [--conf 7] [--safety 8] [--notes "thesis"] [--signals "EMA,RSI"]
                [--rsi 42] [--volr 1.5] [--regime TRENDING_UP]

  trade close   --id <id> --exit 1.240 [--reason TP_HIT] [--notes ""] [--mistakes ""]
                [--improve ""]

  list          [--open] [--session <id>]
  stats         [--session <id>]
  review        --id <id>
  types         (muestra taxonomía de oportunidades)
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb

DB_PATH = str(Path(__file__).parent.parent / "storage" / "trading.duckdb")

# ── ANSI ──────────────────────────────────────────────────────────────────────
G  = "\033[92m"; R = "\033[91m"; Y = "\033[93m"; B = "\033[94m"
C  = "\033[96m"; M = "\033[95m"; W = "\033[97m"; DIM= "\033[2m"; BLD= "\033[1m"; RST= "\033[0m"
def g(v): return f"{G}{v}{RST}"
def r(v): return f"{R}{v}{RST}"
def y(v): return f"{Y}{v}{RST}"
def b(v): return f"{B}{v}{RST}"
def c(v): return f"{C}{v}{RST}"
def w(v): return f"{W}{BLD}{v}{RST}"
def dim(v): return f"{DIM}{v}{RST}"
def sep(): print(b("─" * 62))

# ── OPORTUNITY TAXONOMY ───────────────────────────────────────────────────────
TYPES = {
    "EXPRESS_SCALP": {
        "emoji": "⚡",
        "duration": "< 5 min",
        "desc": "Trade express en impulso limpio. Entrada precisa, TP inmediato.",
        "criteria": "RSI pullback en tendencia + vol spike + EMA cross reciente",
        "r_target": 1.5,
    },
    "QUICK_SCALP": {
        "emoji": "🎯",
        "duration": "5–15 min",
        "desc": "Scalp en estructura clara. Más margen que EXPRESS.",
        "criteria": "EMA alineadas 5m+15m + RSI zona clave + S/R definido",
        "r_target": 2.0,
    },
    "BREAKOUT": {
        "emoji": "🚀",
        "duration": "5–30 min",
        "desc": "Ruptura de rango/resistencia con volumen.",
        "criteria": "Consolidación previa + vol > 2x avg en ruptura + retest opcional",
        "r_target": 2.5,
    },
    "REVERSAL": {
        "emoji": "🔄",
        "duration": "10–30 min",
        "desc": "Reversión en zona de agotamiento (RSI extremo + divergencia).",
        "criteria": "RSI < 25 o > 75 + divergencia precio/momentum + señal de vuelta",
        "r_target": 2.5,
    },
    "TRAP_HUNT": {
        "emoji": "🪤",
        "duration": "2–10 min",
        "desc": "Cazar stop-hunt / fake breakout. Entrada contra la trampa.",
        "criteria": "Spike que barre SLs + absorción inmediata + retorno rápido al rango",
        "r_target": 3.0,
    },
    "MOMENTUM": {
        "emoji": "📈",
        "duration": "15–60 min",
        "desc": "Montar tendencia fuerte en múltiples TF.",
        "criteria": "Todos los TF alineados + funding favorable + pullback limpio",
        "r_target": 3.0,
    },
    "RANGE_PLAY": {
        "emoji": "↔️",
        "duration": "5–20 min",
        "desc": "Comprar soporte / vender resistencia en rango establecido.",
        "criteria": "Al menos 2 toques previos + RSI neutral + sin catalizadores externos",
        "r_target": 1.5,
    },
}

# ── HELPERS ───────────────────────────────────────────────────────────────────

def now_ts() -> int:
    return int(time.time())

def fmt_ts(ts: int) -> str:
    if not ts: return "──"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")

def fmt_dur(s: int) -> str:
    if s <= 0: return "──"
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s//60}m{s%60:02d}s"
    return f"{s//3600}h{(s%3600)//60}m"

def make_id(prefix="t") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"

def get_db():
    return duckdb.connect(DB_PATH)

def get_active_session(con) -> dict | None:
    row = con.execute(
        "SELECT * FROM scalp_sessions WHERE status='ACTIVE' ORDER BY start_ts DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    cols = [d[0] for d in con.execute("DESCRIBE scalp_sessions").fetchall()]
    return dict(zip(cols, row))

def score_bar(score: int, max_val: int = 10) -> str:
    filled = int(score / max_val * 10)
    bar = "█" * filled + "░" * (10 - filled)
    col = g if score >= 7 else y if score >= 5 else r
    return col(f"[{bar}] {score}/{max_val}")

def pnl_fmt(v: float) -> str:
    if v > 0: return g(f"+${v:.4f}")
    if v < 0: return r(f"-${abs(v):.4f}")
    return dim("$0.0000")

def r_fmt(v: float) -> str:
    if v == 0: return dim("──")
    if v > 0: return g(f"+{v:.2f}R")
    return r(f"{v:.2f}R")

# ── COMMANDS ──────────────────────────────────────────────────────────────────

def cmd_types():
    print(f"\n{b('━'*62)}")
    print(f"  {w('TAXONOMÍA DE OPORTUNIDADES')}")
    print(b("━"*62))
    for otype, info in TYPES.items():
        print(f"\n  {info['emoji']} {w(otype):<30} {dim(info['duration'])}")
        print(f"     {info['desc']}")
        print(f"     {dim('Criterio:')} {info['criteria']}")
        print(f"     {dim('R target:')} {g(str(info['r_target'])+'R')}")
    print()


def cmd_session_new(args: dict):
    con = get_db()
    existing = get_active_session(con)
    if existing:
        print(y(f"⚠ Sesión activa ya existe: {existing['id']} ({existing['name']})"))
        print(y("  Ciérrala primero con: python -m tools.journal session close"))
        con.close()
        return

    sid          = make_id("ss")
    name         = args.get("name") or datetime.now().strftime("%Y-%m-%d %H:%M")
    initial_bal  = float(args.get("balance") or 1.38)
    target_bal   = float(args.get("target")  or 5.0)
    target_pnl   = round(target_bal - initial_bal, 4)
    goal         = args.get("goal") or ""
    strategy     = args.get("strategy") or "EXPRESS_SCALP"
    max_trades   = int(args.get("max_trades") or 20)
    max_risk_pct = float(args.get("max_risk") or 2.0)

    con.execute("""
        INSERT INTO scalp_sessions
            (id, name, mode, strategy_focus, initial_balance, real_balance_ref,
             current_balance, target_balance, target_pnl, behavioral_goal,
             max_trades, max_risk_per_trade_pct, status, start_ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [sid, name, "PAPER", strategy, initial_bal, 1.38,
          initial_bal, target_bal, target_pnl, goal,
          max_trades, max_risk_pct, "ACTIVE", now_ts()])
    con.commit()

    print(f"\n{b('━'*62)}")
    print(f"  {w('SESIÓN INICIADA')}")
    print(b("━"*62))
    print(f"  {dim('ID'):<20} {c(sid)}")
    print(f"  {dim('Nombre'):<20} {name}")
    print(f"  {dim('Modo'):<20} {y('PAPER')}")
    print(f"  {dim('Capital inicial'):<20} {w(f'${initial_bal:.2f}')}")
    print(f"  {dim('Objetivo'):<20} {g(f'${target_bal:.2f}')}  {dim(f'(+${target_pnl:.2f})')}")
    print(f"  {dim('Estrategia'):<20} {strategy}")
    print(f"  {dim('Max trades'):<20} {max_trades}")
    print(f"  {dim('Riesgo/trade'):<20} {max_risk_pct}% ({max_risk_pct/100*initial_bal:.4f} USD)")
    if goal:
        print(f"  {dim('Objetivo conductual'):<20} {goal}")
    print(b("─"*62))
    print(f"  Riesgo máximo por trade: {w(f'${max_risk_pct/100*initial_bal:.4f}')}")
    print(f"  Notional sugerido (10x): {w(f'${max_risk_pct/100*initial_bal*10:.4f}')}")
    print()
    con.close()


def cmd_session_list(args: dict):
    con = get_db()
    rows = con.execute("""
        SELECT id, name, mode, status, initial_balance, current_balance,
               pnl, target_pnl, total_trades, wins, losses, start_ts
        FROM scalp_sessions ORDER BY start_ts DESC LIMIT 10
    """).fetchall()
    con.close()

    print(f"\n{b('━'*62)}")
    print(f"  {w('SESIONES RECIENTES')}")
    print(b("━"*62))
    for row in rows:
        sid, name, mode, status, ib, cb, pnl, tpnl, tt, wins, losses, ts = row
        status_col = g if status == "COMPLETED" else y if status == "ACTIVE" else dim
        pnl_col    = g if pnl > 0 else r if pnl < 0 else dim
        print(f"  {status_col(f'[{status:8}]')} {c(sid[:10])}  {dim(fmt_ts(ts))}  {name[:20]}")
        print(f"            {dim('Balance:')} ${ib:.2f} → ${cb:.2f}  PnL: {pnl_col(f'{pnl:+.4f}')}  "
              f"({tt} trades, {wins}W/{losses}L)")
    print()


def cmd_session_status(args: dict):
    con = get_db()
    sess = get_active_session(con)
    if not sess:
        print(y("Sin sesión activa. Usa: python -m tools.journal session new"))
        con.close()
        return

    # Trades de esta sesión
    trades = con.execute("""
        SELECT id, symbol, side, state, pnl_usd, r_multiple, confidence_score,
               safety_score, opportunity_type, opened_at, duration_s
        FROM scalp_journal WHERE session_id=? ORDER BY opened_at DESC
    """, [sess["id"]]).fetchall()

    progress = (sess["pnl"] / sess["target_pnl"] * 100) if sess["target_pnl"] else 0
    bar_filled = int(min(progress, 100) / 5)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    bar_col = g if progress >= 100 else y if progress >= 50 else r

    print(f"\n{b('━'*62)}")
    print(f"  {w('ESTADO DE SESIÓN ACTIVA')}")
    print(b("━"*62))
    print(f"  {dim('ID'):<22} {c(sess['id'])}")
    cur_bal = sess['current_balance']
    print(f"  {dim('Capital'):<22} ${sess['initial_balance']:.4f} → {w(f'${cur_bal:.4f}')}")
    print(f"  {dim('PnL acumulado'):<22} {pnl_fmt(sess['pnl'])}")
    print(f"  {dim('Objetivo'):<22} ${sess['target_balance']:.2f}  (+${sess['target_pnl']:.4f})")
    print(f"  {dim('Progreso'):<22} {bar_col(f'[{bar}] {progress:.1f}%')}")
    print(f"  {dim('Trades'):<22} {sess['total_trades']} ({sess['wins']}W / {sess['losses']}L)")
    if sess["behavioral_goal"]:
        print(f"  {dim('Objetivo conductual'):<22} {sess['behavioral_goal']}")

    if trades:
        print(f"\n  {c('TRADES:')}")
        for t in trades:
            tid, sym, side, state, pnl, rm, conf, safe, otype, ots, dur = t
            state_col = g if state == "CLOSED" and pnl > 0 else r if state == "CLOSED" else y
            side_col  = g if side == "LONG" else r
            print(f"  {state_col(f'[{state:6}]')} {side_col(f'{side:5}')} {w(sym):<14} "
                  f"{pnl_fmt(pnl)}  {r_fmt(rm or 0)}  "
                  f"{dim(fmt_ts(ots))}  {dim(fmt_dur(dur or 0))}")
    print()
    con.close()


def cmd_session_close(args: dict):
    con = get_db()
    sess = get_active_session(con)
    if not sess:
        print(y("Sin sesión activa."))
        con.close()
        return

    notes = args.get("notes") or ""
    end   = now_ts()
    dur   = end - sess["start_ts"]

    con.execute("""
        UPDATE scalp_sessions SET status='COMPLETED', end_ts=?, notes=?,
               final_balance=current_balance
        WHERE id=?
    """, [end, notes, sess["id"]])
    con.commit()
    con.close()

    wr = sess["wins"] / sess["total_trades"] * 100 if sess["total_trades"] else 0
    print(f"\n{b('━'*62)}")
    print(f"  {w('SESIÓN CERRADA')}")
    print(b("━"*62))
    print(f"  Duración:  {fmt_dur(dur)}")
    print(f"  PnL final: {pnl_fmt(sess['pnl'])}")
    print(f"  Trades:    {sess['total_trades']}  ({sess['wins']}W / {sess['losses']}L) — {wr:.0f}% WR")
    print()


def cmd_trade_open(args: dict):
    con = get_db()
    sess = get_active_session(con)
    if not sess:
        print(r("❌ Sin sesión activa. Inicia una con: python -m tools.journal session new"))
        con.close()
        return

    sym    = (args.get("sym") or "").upper()
    side   = (args.get("side") or "").upper()
    if not sym or side not in ("LONG","SHORT"):
        print(r("❌ --sym y --side (LONG|SHORT) son obligatorios"))
        con.close()
        return

    entry  = float(args.get("entry") or 0)
    sl     = float(args.get("sl") or 0)
    tp     = float(args.get("tp") or 0)
    qty    = float(args.get("qty") or 0)
    lev    = float(args.get("lev") or 10)
    tf     = args.get("tf") or "5m"
    otype  = (args.get("type") or "QUICK_SCALP").upper()
    conf   = int(args.get("conf") or 0)
    safe   = int(args.get("safety") or 0)
    notes  = args.get("notes") or ""
    signals= args.get("signals") or ""
    rsi_v  = float(args.get("rsi") or 0)
    volr   = float(args.get("volr") or 1.0)
    regime = args.get("regime") or ""

    # Risk calc
    risk_usd = 0.0
    rr_plan  = 0.0
    if entry > 0 and sl > 0 and qty > 0:
        sl_dist  = abs(entry - sl)
        risk_usd = sl_dist * qty
        if tp > 0:
            tp_dist  = abs(tp - entry)
            rr_plan  = tp_dist / sl_dist if sl_dist > 0 else 0

    tid = make_id("t")
    signals_json = json.dumps([s.strip() for s in signals.split(",") if s.strip()]) if signals else "[]"

    con.execute("""
        INSERT INTO scalp_journal
            (id, session_id, symbol, side, timeframe, leverage, entry_price, sl_price,
             tp_price, qty, risk_usd, rr_planned, opportunity_type, confidence_score,
             safety_score, setup_signals, market_regime, rsi_entry, volume_ratio,
             state, pre_notes, paper_mode, opened_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [tid, sess["id"], sym, side, tf, lev, entry, sl, tp, qty,
          risk_usd, rr_plan, otype, conf, safe, signals_json,
          regime, rsi_v, volr, "OPEN", notes, True, now_ts()])

    # Actualizar contador de sesión
    con.execute("UPDATE scalp_sessions SET total_trades=total_trades+1 WHERE id=?", [sess["id"]])
    con.commit()

    print(f"\n{b('━'*62)}")
    print(f"  {g('✓ TRADE ABIERTO')}")
    print(b("━"*62))
    print(f"  {dim('ID'):<20} {c(tid)}")
    print(f"  {dim('Símbolo'):<20} {w(sym)} {(g('LONG') if side=='LONG' else r('SHORT'))}")
    print(f"  {dim('Entrada'):<20} {w(str(entry))}")
    if sl: print(f"  {dim('Stop Loss'):<20} {r(str(sl))}  {dim(f'({abs(entry-sl)/entry*100:.2f}%)')}")
    if tp: print(f"  {dim('Take Profit'):<20} {g(str(tp))}  {dim(f'({abs(tp-entry)/entry*100:.2f}%)')}")
    if qty: print(f"  {dim('Qty / Riesgo'):<20} {qty} ctts  |  {y(f'${risk_usd:.4f} en riesgo')}")
    if rr_plan: print(f"  {dim('R:R planificado'):<20} {g(f'{rr_plan:.2f}:1')}")
    print(f"  {dim('Tipo'):<20} {TYPES.get(otype,{}).get('emoji','')}{otype}")
    print(f"  {dim('Certeza'):<20} {score_bar(conf)}")
    print(f"  {dim('Seguridad'):<20} {score_bar(safe)}")
    if notes: print(f"  {dim('Thesis'):<20} {notes}")
    print(b("─"*62))

    # Warning si conf o safety bajos
    if conf < 6:
        print(y(f"  ⚠ Certeza baja ({conf}/10) — ¿es un setup claro?"))
    if safe < 6:
        print(y(f"  ⚠ Seguridad baja ({safe}/10) — ¿SL bien colocado?"))
    print()
    con.close()


def cmd_trade_close(args: dict):
    con = get_db()
    tid = args.get("id") or ""
    if not tid:
        # Buscar el trade abierto más reciente
        row = con.execute("""
            SELECT id FROM scalp_journal WHERE state='OPEN' ORDER BY opened_at DESC LIMIT 1
        """).fetchone()
        if not row:
            print(r("❌ No hay trades abiertos. Usa --id <trade_id>"))
            con.close()
            return
        tid = row[0]
        print(y(f"  Usando último trade abierto: {tid}"))

    trade = con.execute("SELECT * FROM scalp_journal WHERE id=?", [tid]).fetchone()
    if not trade:
        print(r(f"❌ Trade no encontrado: {tid}"))
        con.close()
        return

    cols  = [d[0] for d in con.execute("DESCRIBE scalp_journal").fetchall()]
    t     = dict(zip(cols, trade))

    if t["state"] != "OPEN":
        print(y(f"⚠ Trade ya {t['state']}"))
        con.close()
        return

    exit_price = float(args.get("exit") or 0)
    reason     = (args.get("reason") or "MANUAL").upper()
    post_notes = args.get("notes") or ""
    mistakes   = args.get("mistakes") or ""
    improvement= args.get("improve") or ""

    if not exit_price:
        print(r("❌ --exit <precio> es obligatorio"))
        con.close()
        return

    entry  = t["entry_price"]
    qty    = t["qty"]
    side   = t["side"]
    sl     = t["sl_price"]
    opened = t["opened_at"]
    closed = now_ts()
    dur    = closed - opened

    # PnL calculation
    if side == "LONG":
        pnl_pct = (exit_price - entry) / entry
    else:
        pnl_pct = (entry - exit_price) / entry
    pnl_usd = pnl_pct * entry * qty if qty > 0 else 0

    # R-multiple
    r_mult = 0.0
    if sl > 0 and entry > 0:
        sl_dist  = abs(entry - sl)
        price_mv = abs(exit_price - entry)
        sign_ok  = (side == "LONG" and exit_price > entry) or (side == "SHORT" and exit_price < entry)
        r_mult   = (price_mv / sl_dist) * (1 if sign_ok else -1)

    # Update trade
    con.execute("""
        UPDATE scalp_journal SET
            exit_price=?, state='CLOSED', close_reason=?, pnl_usd=?, r_multiple=?,
            post_notes=?, mistakes=?, improvement=?, closed_at=?, duration_s=?
        WHERE id=?
    """, [exit_price, reason, pnl_usd, r_mult, post_notes, mistakes, improvement,
          closed, dur, tid])

    # Update session
    sess = get_active_session(con)
    if sess:
        win = 1 if pnl_usd > 0 else 0
        loss = 1 if pnl_usd < 0 else 0
        new_bal = sess["current_balance"] + pnl_usd
        new_pnl = sess["pnl"] + pnl_usd
        con.execute("""
            UPDATE scalp_sessions SET
                current_balance=?, pnl=?,
                wins=wins+?, losses=losses+?,
                best_r=GREATEST(best_r, ?),
                worst_r=LEAST(worst_r, ?)
            WHERE id=?
        """, [new_bal, new_pnl, win, loss, r_mult, r_mult, sess["id"]])

    con.commit()

    print(f"\n{b('━'*62)}")
    result_lbl = g("✓ WIN") if pnl_usd > 0 else r("✗ LOSS") if pnl_usd < 0 else dim("── BREAKEVEN")
    print(f"  {result_lbl}  —  {w(t['symbol'])} {t['side']}")
    print(b("━"*62))
    print(f"  {dim('Entrada / Salida'):<22} {w(str(entry))} → {w(str(exit_price))}")
    print(f"  {dim('PnL'):<22} {pnl_fmt(pnl_usd)}")
    print(f"  {dim('R conseguido'):<22} {r_fmt(r_mult)}")
    print(f"  {dim('Duración'):<22} {fmt_dur(dur)}")
    print(f"  {dim('Cierre'):<22} {reason}")
    if post_notes: print(f"  {dim('Review'):<22} {post_notes}")
    if mistakes:   print(f"  {dim('Errores'):<22} {r(mistakes)}")
    if improvement:print(f"  {dim('Mejora'):<22} {y(improvement)}")
    if sess:
        new_bal = sess["current_balance"] + pnl_usd
        new_pnl = sess["pnl"] + pnl_usd
        progress = (new_pnl / sess["target_pnl"] * 100) if sess["target_pnl"] else 0
        print(b("─"*62))
        print(f"  {dim('Balance sesión'):<22} ${new_bal:.4f}  "
              f"({g('+') if new_pnl >= 0 else r('')}${abs(new_pnl):.4f})")
        print(f"  {dim('Progreso objetivo'):<22} {g(f'{progress:.1f}%') if progress >= 50 else y(f'{progress:.1f}%')}")
    print()
    con.close()


def cmd_list(args: dict):
    con = get_db()
    where = []
    params = []

    if args.get("open"):
        where.append("state='OPEN'")
    if args.get("session"):
        where.append("session_id=?")
        params.append(args["session"])
    else:
        sess = get_active_session(con)
        if sess:
            where.append("session_id=?")
            params.append(sess["id"])

    wstr = ("WHERE " + " AND ".join(where)) if where else ""
    rows = con.execute(f"""
        SELECT id, symbol, side, state, entry_price, exit_price, pnl_usd, r_multiple,
               opportunity_type, confidence_score, safety_score, opened_at, duration_s
        FROM scalp_journal {wstr} ORDER BY opened_at DESC LIMIT 30
    """, params).fetchall()
    con.close()

    print(f"\n{b('━'*62)}")
    print(f"  {w('TRADES')}")
    print(b("━"*62))
    for row in rows:
        tid, sym, side, state, entry, exit_p, pnl, rm, otype, conf, safe, ots, dur = row
        side_col  = g if side=="LONG" else r
        state_col = g if state=="CLOSED" and pnl > 0 else r if state=="CLOSED" else y
        print(f"  {state_col(f'[{state:6}]')} {side_col(f'{side:5}')} {w(sym):<14} "
              f"{pnl_fmt(pnl or 0)}  {r_fmt(rm or 0)}  "
              f"{dim(otype or 'SCALP'):<14} {dim(fmt_ts(ots))}")
        exit_str = str(exit_p) if exit_p else "──"
        print(f"            {dim(f'{entry} -> {exit_str}')}  "
              f"conf={score_bar(conf or 0)[:30]}  {dim(fmt_dur(dur or 0))}")
    print()


def cmd_stats(args: dict):
    con = get_db()
    sess = get_active_session(con)

    sid = args.get("session") or (sess["id"] if sess else None)
    if not sid:
        print(y("Sin sesión activa ni --session especificado"))
        con.close()
        return

    row = con.execute("SELECT * FROM scalp_sessions WHERE id=?", [sid]).fetchone()
    if not row:
        print(r(f"Sesión no encontrada: {sid}"))
        con.close()
        return
    cols = [d[0] for d in con.execute("DESCRIBE scalp_sessions").fetchall()]
    s = dict(zip(cols, row))

    # Stats de trades
    stats = con.execute("""
        SELECT
            COUNT(*) total,
            SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
            SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) losses,
            ROUND(SUM(pnl_usd),5) total_pnl,
            ROUND(AVG(pnl_usd),5) avg_pnl,
            ROUND(AVG(r_multiple),3) avg_r,
            ROUND(MAX(r_multiple),3) best_r,
            ROUND(MIN(r_multiple),3) worst_r,
            ROUND(AVG(confidence_score),1) avg_conf,
            ROUND(AVG(safety_score),1) avg_safe,
            ROUND(AVG(duration_s),0) avg_dur
        FROM scalp_journal WHERE session_id=? AND state='CLOSED'
    """, [sid]).fetchone()

    total, wins, losses, tpnl, apnl, ar, br, wr_r, ac, asf, adur = stats
    wr = wins/total*100 if total else 0

    # Por tipo
    by_type = con.execute("""
        SELECT opportunity_type, COUNT(*), SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END),
               ROUND(SUM(pnl_usd),4), ROUND(AVG(r_multiple),2)
        FROM scalp_journal WHERE session_id=? AND state='CLOSED'
        GROUP BY opportunity_type ORDER BY COUNT(*) DESC
    """, [sid]).fetchall()

    progress = (s["pnl"] / s["target_pnl"] * 100) if s["target_pnl"] else 0
    bar_f = int(min(progress, 100) / 5)
    bar_col = g if progress >= 100 else y if progress >= 50 else r

    print(f"\n{b('━'*62)}")
    print(f"  {w('ESTADÍSTICAS DE SESIÓN')}")
    print(b("━"*62))
    s_cur = s['current_balance']
    s_tpnl = s['target_pnl']
    print(f"  {dim('Capital'):<22} ${s['initial_balance']:.4f} -> {w(f'${s_cur:.4f}')}")
    print(f"  {dim('PnL total'):<22} {pnl_fmt(s['pnl'])}  /  objetivo {g(f'${s_tpnl:.4f}')}")
    bar = "█"*bar_f + "░"*(20-bar_f)
    print(f"  {dim('Progreso'):<22} {bar_col(f'[{bar}] {progress:.1f}%')}")
    print()
    if total:
        print(f"  {dim('Trades cerrados'):<22} {total}  ({wins}W / {losses}L)")
        print(f"  {dim('Win rate'):<22} {(g if wr >= 55 else y if wr >= 45 else r)(f'{wr:.1f}%')}")
        print(f"  {dim('PnL promedio'):<22} {pnl_fmt(apnl)}")
        print(f"  {dim('R promedio'):<22} {r_fmt(ar)}")
        print(f"  {dim('Mejor / Peor R'):<22} {g(f'{br:.2f}R')} / {r(f'{wr_r:.2f}R')}")
        print(f"  {dim('Conf. promedio'):<22} {score_bar(int(ac or 0))}")
        print(f"  {dim('Seguridad prom.'):<22} {score_bar(int(asf or 0))}")
        print(f"  {dim('Duración prom.'):<22} {fmt_dur(int(adur or 0))}")

        if by_type:
            print(f"\n  {c('POR TIPO:')}")
            for bt in by_type:
                ot, cnt, w_cnt, tp2, ar2 = bt
                wr2 = w_cnt/cnt*100 if cnt else 0
                emoji = TYPES.get(ot, {}).get("emoji", "")
                print(f"    {emoji} {ot:<18} {cnt:>3} trades  "
                      f"WR={wr2:.0f}%  {pnl_fmt(tp2)}  avg={r_fmt(ar2)}")
    else:
        print(f"  {dim('Sin trades cerrados aún.')}")
    print()
    con.close()


def cmd_review(args: dict):
    con = get_db()
    tid = args.get("id")
    if not tid:
        # Mostrar el último cerrado
        row = con.execute(
            "SELECT id FROM scalp_journal WHERE state='CLOSED' ORDER BY closed_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            print(y("Sin trades cerrados."))
            con.close()
            return
        tid = row[0]

    trade = con.execute("SELECT * FROM scalp_journal WHERE id=?", [tid]).fetchone()
    if not trade:
        print(r(f"Trade no encontrado: {tid}"))
        con.close()
        return

    cols = [d[0] for d in con.execute("DESCRIBE scalp_journal").fetchall()]
    t = dict(zip(cols, trade))
    con.close()

    side_col = g if t["side"] == "LONG" else r
    result_col = g if t["pnl_usd"] > 0 else r if t["pnl_usd"] < 0 else dim

    print(f"\n{b('━'*62)}")
    t_state = t['state']
    print(f"  {w('REVIEW DE TRADE')}  {result_col(f'[{t_state}]')}")
    print(b("━"*62))
    print(f"  {dim('ID'):<22} {c(t['id'])}")
    print(f"  {dim('Símbolo'):<22} {w(t['symbol'])} {side_col(t['side'])}")
    print(f"  {dim('Tipo'):<22} {TYPES.get(t['opportunity_type'],{}).get('emoji','')}{t['opportunity_type']}")
    print(f"  {dim('Timeframe'):<22} {t['timeframe']}  /  lev {t['leverage']}x")
    print()
    print(f"  {dim('Entrada'):<22} {w(str(t['entry_price']))}")
    print(f"  {dim('Stop Loss'):<22} {r(str(t['sl_price']))}")
    print(f"  {dim('Take Profit'):<22} {g(str(t['tp_price']))}")
    print(f"  {dim('Salida'):<22} {w(str(t['exit_price']))}")
    print(f"  {dim('Qty'):<22} {t['qty']}")
    print(f"  {dim('R:R plan'):<22} {g(str(t['rr_planned']))}")
    print()
    print(f"  {dim('PnL'):<22} {pnl_fmt(t['pnl_usd'])}")
    print(f"  {dim('R conseguido'):<22} {r_fmt(t['r_multiple'])}")
    print(f"  {dim('Duración'):<22} {fmt_dur(t['duration_s'])}")
    print(f"  {dim('Cierre'):<22} {t['close_reason']}")
    print()
    print(f"  {dim('Certeza'):<22} {score_bar(t['confidence_score'])}")
    print(f"  {dim('Seguridad'):<22} {score_bar(t['safety_score'])}")
    sigs = json.loads(t["setup_signals"] or "[]")
    if sigs: print(f"  {dim('Señales'):<22} {', '.join(sigs)}")
    if t["market_regime"]: print(f"  {dim('Régimen'):<22} {t['market_regime']}")
    if t["rsi_entry"]: print(f"  {dim('RSI entrada'):<22} {t['rsi_entry']:.1f}")
    if t["volume_ratio"]: print(f"  {dim('Vol ratio'):<22} {t['volume_ratio']:.1f}x")
    print()
    if t["pre_notes"]:   print(f"  {c('Thesis pre-trade:')}  {t['pre_notes']}")
    if t["post_notes"]:  print(f"  {c('Review post-trade:')} {t['post_notes']}")
    if t["mistakes"]:    print(f"  {r('Errores:')}           {t['mistakes']}")
    if t["improvement"]: print(f"  {y('Mejora:')}            {t['improvement']}")
    print()


# ── ARGUMENT PARSER ───────────────────────────────────────────────────────────

def parse_args(argv: list[str]) -> tuple[str, str | None, dict]:
    if len(argv) < 2:
        print(__doc__)
        sys.exit(0)

    args = {}
    cmd  = argv[1].lower()
    sub  = argv[2].lower() if len(argv) > 2 and not argv[2].startswith("-") else None
    rest = argv[3:] if sub else argv[2:]

    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("--"):
            key = tok[2:]
            val = rest[i+1] if i+1 < len(rest) and not rest[i+1].startswith("--") else "true"
            args[key] = val
            i += 2 if val != "true" else 1
        else:
            i += 1

    return cmd, sub, args


def main():
    cmd, sub, args = parse_args(sys.argv)

    if cmd == "types":
        cmd_types()
    elif cmd == "session":
        if sub == "new":    cmd_session_new(args)
        elif sub == "list": cmd_session_list(args)
        elif sub == "close":cmd_session_close(args)
        elif sub == "status" or not sub: cmd_session_status(args)
        else: print(f"Subcomando desconocido: {sub}")
    elif cmd == "trade":
        if sub == "open":  cmd_trade_open(args)
        elif sub == "close": cmd_trade_close(args)
        else: print(f"Subcomando desconocido: {sub}")
    elif cmd == "list":  cmd_list(args)
    elif cmd == "stats": cmd_stats(args)
    elif cmd == "review":cmd_review(args)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
