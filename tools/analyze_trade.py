"""
tools/analyze_trade.py
──────────────────────
Diagnóstico rápido: fetch REST Bybit → muestra posición, balance,
contexto de mercado y análisis de riesgo/oportunidad en la terminal.

Uso:  python -m tools.analyze_trade
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

# Añadir raíz del proyecto al path
sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
from core.config import settings

BASE = "https://api-testnet.bybit.com" if settings.bybit_testnet else "https://api.bybit.com"

# ── Colores ANSI ──────────────────────────────────────────────────────────────

G  = "\033[92m"   # verde
R  = "\033[91m"   # rojo
Y  = "\033[93m"   # amarillo
B  = "\033[94m"   # azul
C  = "\033[96m"   # cyan
M  = "\033[95m"   # magenta
W  = "\033[97m"   # blanco brillante
DIM= "\033[2m"    # dim
BLD= "\033[1m"    # bold
RST= "\033[0m"    # reset

def g(v):  return f"{G}{v}{RST}"
def r(v):  return f"{R}{v}{RST}"
def y(v):  return f"{Y}{v}{RST}"
def b(v):  return f"{B}{v}{RST}"
def c(v):  return f"{C}{v}{RST}"
def m(v):  return f"{M}{v}{RST}"
def w(v):  return f"{W}{BLD}{v}{RST}"
def dim(v):return f"{DIM}{v}{RST}"

def sc(v: float, zero_ok=False) -> str:
    if v > 0:  return g
    if v < 0:  return r
    return dim

def sign(v: float) -> str:
    return "+" if v >= 0 else ""

def fp(p: float) -> str:
    if p <= 0: return "──"
    if p >= 10000: return f"{p:,.1f}"
    if p >= 1000:  return f"{p:,.2f}"
    if p >= 10:    return f"{p:.4f}"
    if p >= 1:     return f"{p:.5f}"
    return f"{p:.6f}"

def fm(v: float) -> str:
    if abs(v) >= 1_000_000: return f"{v/1_000_000:.2f}M"
    if abs(v) >= 1_000:     return f"{v/1_000:.1f}K"
    return f"{v:.2f}"

# ── Auth ──────────────────────────────────────────────────────────────────────

def _headers(params: str = "") -> dict:
    ts  = str(int(time.time() * 1000))
    rw  = "5000"
    pre = f"{ts}{settings.bybit_api_key}{rw}{params}"
    sig = hmac.new(
        settings.bybit_api_secret.encode(),
        pre.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-BAPI-API-KEY":      settings.bybit_api_key,
        "X-BAPI-TIMESTAMP":    ts,
        "X-BAPI-SIGN":         sig,
        "X-BAPI-RECV-WINDOW":  rw,
    }

async def _get(session: aiohttp.ClientSession, path: str, params: dict) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    async with session.get(
        f"{BASE}{path}?{qs}",
        headers=_headers(qs),
    ) as resp:
        return await resp.json()

async def _get_public(session: aiohttp.ClientSession, path: str, params: dict) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    async with session.get(f"{BASE}{path}?{qs}") as resp:
        return await resp.json()

# ── Fetch ─────────────────────────────────────────────────────────────────────

async def fetch_all(session: aiohttp.ClientSession) -> dict:
    import datetime
    today_ms = int(datetime.datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).timestamp() * 1000)

    positions_r, balance_r, daily_pnl_r = await asyncio.gather(
        _get(session, "/v5/position/list", {
            "category":   "linear",
            "settleCoin": "USDT",
            "limit":      "50",
        }),
        _get(session, "/v5/account/wallet-balance", {
            "accountType": "UNIFIED",
        }),
        _get(session, "/v5/position/closed-pnl", {
            "category":  "linear",
            "startTime": str(today_ms),
            "limit":     "200",
        }),
    )
    return {
        "positions": positions_r,
        "balance":   balance_r,
        "daily_pnl": daily_pnl_r,
    }

async def fetch_ticker(session: aiohttp.ClientSession, symbol: str) -> dict:
    data = await _get_public(session, "/v5/market/tickers", {
        "category": "linear",
        "symbol":   symbol,
    })
    items = data.get("result", {}).get("list", [])
    return items[0] if items else {}

async def fetch_funding(session: aiohttp.ClientSession, symbol: str) -> list:
    data = await _get_public(session, "/v5/market/funding/history", {
        "category": "linear",
        "symbol":   symbol,
        "limit":    "8",
    })
    return data.get("result", {}).get("list", [])

async def fetch_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int) -> list:
    data = await _get_public(session, "/v5/market/kline", {
        "category": "linear",
        "symbol":   symbol,
        "interval": interval,
        "limit":    str(limit),
    })
    return data.get("result", {}).get("list", [])

# ── Análisis técnico simple ────────────────────────────────────────────────────

def ema(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    k = 2 / (period + 1)
    val = sum(prices[:period]) / period
    for p in prices[period:]:
        val = p * k + val * (1 - k)
    return val

def rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(0, delta))
        losses.append(max(0, -delta))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)

def atr(klines: list, period: int = 14) -> float:
    """
    klines de Bybit v5 (más reciente primero):
      [0]=startTime [1]=open [2]=high [3]=low [4]=close [5]=vol [6]=turnover
    """
    if len(klines) < period + 1:
        return 0.0
    trs = []
    for i in range(min(len(klines) - 1, period * 2)):
        h      = float(klines[i][2])     # high
        l      = float(klines[i][3])     # low
        c_prev = float(klines[i + 1][4]) # close de la vela anterior
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
    return sum(trs[:period]) / period

def support_resistance(klines: list, n: int = 20) -> tuple[float, float]:
    """
    Soporte = mínimo de lows, Resistencia = máximo de highs en n velas.
    klines[i][2]=high, klines[i][3]=low
    """
    if not klines:
        return 0.0, 0.0
    highs = [float(k[2]) for k in klines[:n]]
    lows  = [float(k[3]) for k in klines[:n]]
    return min(lows), max(highs)

# ── Display ───────────────────────────────────────────────────────────────────

SEP = dim("─" * 62)

def header(title: str) -> None:
    print(f"\n{b('━' * 62)}")
    print(f"  {w(title)}")
    print(b("━" * 62))

def section(title: str) -> None:
    print(f"\n  {c(title)}")
    print(f"  {dim('·' * 50)}")

def row(label: str, value: str, note: str = "") -> None:
    note_str = f"  {dim(note)}" if note else ""
    print(f"  {dim(label):<20} {value}{note_str}")

# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        print(r("❌ API keys no configuradas en .env"))
        return

    print(f"\n{b('⚡ QTS — Análisis de Trade en Vivo')}")
    print(dim(f"  Servidor: {'TESTNET' if settings.bybit_testnet else 'LIVE (bybit.com)'}"))
    print(dim(f"  Hora UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}"))

    async with aiohttp.ClientSession() as session:

        # ── Fetch datos de cuenta ──────────────────────────────────────────
        print(f"\n  {dim('Fetching cuenta...')} ", end="", flush=True)
        acct = await fetch_all(session)
        print(g("✓"))

        # ── Balance ────────────────────────────────────────────────────────
        equity = avail = margin_used = unrealized = daily_pnl = 0.0
        for acc in acct["balance"].get("result", {}).get("list", []):
            if acc.get("accountType") in ("UNIFIED", "CONTRACT"):
                equity      = float(acc.get("totalEquity", 0) or 0)
                avail       = float(acc.get("totalAvailableBalance", 0) or 0)
                margin_used = float(acc.get("totalInitialMargin", 0) or 0)
                unrealized  = float(acc.get("totalUnrealisedPnl", 0) or 0)
                # UNIFIED: si available=0 intentar desde coins
                if avail == 0:
                    for coin in acc.get("coin", []):
                        if coin.get("coin") == "USDT":
                            avail = float(coin.get("availableToWithdraw", 0) or
                                          coin.get("availableToBorrow", 0) or 0)
                            break
                break

        for item in acct["daily_pnl"].get("result", {}).get("list", []):
            daily_pnl += float(item.get("closedPnl", 0) or 0)

        margin_pct = margin_used / equity * 100 if equity > 0 else 0

        # ── Posiciones abiertas ────────────────────────────────────────────
        open_pos = [
            p for p in acct["positions"].get("result", {}).get("list", [])
            if float(p.get("size", 0) or 0) > 0
        ]

        # ═══════════════════════════════════════════════════════════════════
        header("CUENTA")
        # ═══════════════════════════════════════════════════════════════════

        eq_c = g if equity > 0 else dim
        row("Equity",    w(f"${equity:,.2f}"))
        row("Disponible",f"${avail:,.2f}")
        row("Margen usado", f"${margin_used:,.2f}  ({margin_pct:.1f}%)",
            "⚠ ALERTA >80%" if margin_pct > 80 else ("cuidado >60%" if margin_pct > 60 else ""))
        un_c = (g if unrealized > 0 else r) if unrealized != 0 else dim
        row("PnL no realizado", f"{un_c(sign(unrealized) + '$' + fm(unrealized))}")
        dp_c = (g if daily_pnl > 0 else r) if daily_pnl != 0 else dim
        row("PnL día (real.)", f"{dp_c(sign(daily_pnl) + '$' + fm(daily_pnl))}")

        if not open_pos:
            print(f"\n  {y('Sin posiciones abiertas en este momento.')}")
        else:
            # ═══════════════════════════════════════════════════════════════
            for pos in open_pos:
                sym   = pos.get("symbol", "??")
                side  = pos.get("side", "??")   # "Buy" | "Sell"
                size  = float(pos.get("size", 0))
                entry = float(pos.get("avgPrice") or pos.get("entryPrice") or 0)
                mark  = float(pos.get("markPrice", 0) or 0)
                lev   = float(pos.get("leverage", 1) or 1)
                liq   = float(pos.get("liqPrice", 0) or 0)
                tp    = float(pos.get("takeProfit", 0) or 0)
                sl    = float(pos.get("stopLoss", 0) or 0)
                upnl  = float(pos.get("unrealisedPnl", 0) or 0)
                margin= float(pos.get("positionIM", 0) or pos.get("positionMM", 0) or 0)
                notional = size * (mark if mark > 0 else entry)

                pnl_pct  = upnl / margin * 100 if margin > 0 else 0
                is_long  = side == "Buy"
                side_lbl = "LONG" if is_long else "SHORT"
                side_col = g if is_long else r
                pnl_col  = g if upnl >= 0 else r

                # Distancia a puntos clave (%)
                def dist(target: float) -> str:
                    if target <= 0 or entry <= 0:
                        return "──"
                    d = (target - entry) / entry * 100
                    col = g if d > 0 else r
                    return col(f"{sign(d)}{abs(d):.2f}%")

                header(f"POSICIÓN ABIERTA — {sym}")

                row("Dirección",   side_col(w(side_lbl)))
                row("Tamaño",      f"{size} contratos  (notional ~${fm(notional)})")
                row("Entrada",     w(fp(entry)))
                row("Precio actual", w(fp(mark)),
                    f"{sign(pnl_pct)}{pnl_pct:.2f}% desde entrada")
                row("Apalancamiento", f"{lev:.0f}x")
                row("PnL no realizado",
                    pnl_col(w(f"{sign(upnl)}${fm(upnl)}")),
                    f"({sign(pnl_pct)}{pnl_pct:.2f}% del margen)")
                row("Margen asignado", f"${margin:.2f}")

                print()
                if tp > 0:
                    row("Take Profit", g(fp(tp)), f"dist: {dist(tp)}")
                if sl > 0:
                    row("Stop Loss",   r(fp(sl)),  f"dist: {dist(sl)}")
                if liq > 0:
                    row("Liquidación", r(fp(liq)), f"dist: {dist(liq)}")

                # ── Fetch datos de mercado para este símbolo ───────────────
                print(f"\n  {dim('Fetching mercado...')} ", end="", flush=True)
                ticker, funding_hist, k15, k1h, k4h = await asyncio.gather(
                    fetch_ticker(session, sym),
                    fetch_funding(session, sym),
                    fetch_klines(session, sym, "15", 60),
                    fetch_klines(session, sym, "60", 60),
                    fetch_klines(session, sym, "240", 30),
                )
                print(g("✓"))

                # ── Análisis técnico ───────────────────────────────────────
                # Klines Bybit: [startTime, open, high, low, close, volume, turnover]
                # Orden: más RECIENTE primero
                def closes(kl):
                    return [float(k[4]) for k in reversed(kl)]  # cronológico

                prices_15m = closes(k15)
                prices_1h  = closes(k1h)
                prices_4h  = closes(k4h)

                ema9_15   = ema(prices_15m, 9)
                ema21_15  = ema(prices_15m, 21)
                ema50_1h  = ema(prices_1h,  50)
                ema200_1h = ema(prices_1h,  200)
                rsi_15    = rsi(prices_15m, 14)
                rsi_1h    = rsi(prices_1h,  14)
                atr_15    = atr(k15, 14)

                sup, res  = support_resistance(k15, 20)

                last_price = float(ticker.get("lastPrice", 0) or 0)
                funding_now = float(ticker.get("fundingRate", 0) or 0) * 100
                vol_24h    = float(ticker.get("volume24h", 0) or 0)
                chg_24h    = float(ticker.get("price24hPcnt", 0) or 0) * 100

                # Funding history — promedio
                fund_rates = [float(f.get("fundingRate", 0)) * 100 for f in funding_hist]
                fund_avg   = sum(fund_rates) / len(fund_rates) if fund_rates else 0

                # Trend alignment
                trend_15_up = ema9_15 > ema21_15
                trend_1h_up = ema50_1h > ema200_1h
                price_above_ema50_1h = last_price > ema50_1h if last_price > 0 else None

                section("CONTEXTO DE MERCADO")
                row("Precio actual",  w(fp(last_price)))
                row("Cambio 24h",     (g if chg_24h >= 0 else r)(f"{sign(chg_24h)}{chg_24h:.2f}%"))
                row("Volumen 24h",    f"${fm(vol_24h)}")
                row("Funding actual", (r if funding_now > 0 else g)(f"{funding_now:+.4f}%"),
                    "longs pagan" if funding_now > 0 else "shorts pagan")
                row("Funding avg 8x", f"{fund_avg:+.4f}%",
                    "momentum alcista sesgado" if fund_avg > 0.01 else
                    "momentum bajista sesgado" if fund_avg < -0.01 else "neutral")

                section("ANÁLISIS TÉCNICO")
                row("EMA 9/21 (15m)",
                    (g("▲ ALCISTA") if trend_15_up else r("▼ BAJISTA")),
                    f"9:{fp(ema9_15)} / 21:{fp(ema21_15)}")
                row("EMA 50/200 (1h)",
                    (g("▲ GOLDEN CROSS") if trend_1h_up else r("▼ DEATH CROSS")),
                    f"50:{fp(ema50_1h)} / 200:{fp(ema200_1h)}")
                row("RSI 14 (15m)",
                    (r("SOBRECOMPRADO") if rsi_15 > 70 else
                     g("SOBREVENDIDO") if rsi_15 < 30 else
                     w(f"{rsi_15:.1f}")),
                    "zona peligrosa ▲" if rsi_15 > 65 else "zona peligrosa ▼" if rsi_15 < 35 else "")
                row("RSI 14 (1h)",
                    (r(f"{rsi_1h:.1f} SOBRECOMP.") if rsi_1h > 70 else
                     g(f"{rsi_1h:.1f} SOBREVEND.") if rsi_1h < 30 else
                     w(f"{rsi_1h:.1f}")))
                row("ATR (15m)",      f"{fp(atr_15)}",
                    f"= {atr_15/last_price*100:.2f}% del precio" if last_price > 0 else "")
                row("Soporte (20v 15m)", b(fp(sup)))
                row("Resistencia (20v)", b(fp(res)))

                # ── Evaluación del trade ───────────────────────────────────
                section("EVALUACIÓN DEL TRADE")

                good  = []
                risks = []
                tips  = []

                # Alineación de tendencia
                if is_long and trend_15_up and trend_1h_up:
                    good.append("✓ Trade LONG alineado con tendencia 15m Y 1h")
                elif is_long and trend_15_up and not trend_1h_up:
                    risks.append("⚠ LONG en 15m alcista pero 1h bajista (contratendencia en TF mayor)")
                elif is_long and not trend_15_up:
                    risks.append("⚠ LONG pero EMAs 15m apuntan ABAJO — impulso débil")
                elif not is_long and not trend_15_up and not trend_1h_up:
                    good.append("✓ Trade SHORT alineado con tendencia 15m Y 1h")
                elif not is_long and trend_15_up:
                    risks.append("⚠ SHORT pero EMAs alcistas — riesgo de squeeze")

                # RSI
                if is_long and rsi_15 > 70:
                    risks.append(f"⚠ RSI 15m sobrecomprado ({rsi_15:.1f}) — reversión posible")
                elif is_long and rsi_15 < 40:
                    good.append(f"✓ RSI 15m bajo ({rsi_15:.1f}) — hay margen de subida")
                elif not is_long and rsi_15 < 30:
                    risks.append(f"⚠ RSI 15m sobrevendido ({rsi_15:.1f}) — rebote posible")
                elif not is_long and rsi_15 > 60:
                    good.append(f"✓ RSI 15m elevado ({rsi_15:.1f}) — hay margen de caída")

                # Funding
                if is_long and funding_now > 0.05:
                    risks.append(f"⚠ Funding {funding_now:.4f}% — longs pagando, mercado apalancado al alza")
                elif is_long and funding_now < -0.02:
                    good.append(f"✓ Funding negativo — shorts financian tu long")
                elif not is_long and funding_now < -0.05:
                    risks.append(f"⚠ Funding {funding_now:.4f}% — shorts pagando")
                elif not is_long and funding_now > 0.02:
                    good.append(f"✓ Funding positivo — longs financian tu short")

                # SL / TP configurados?
                if sl <= 0:
                    risks.append("🔴 SIN STOP LOSS — riesgo no controlado")
                    tips.append("→ Coloca un SL inmediatamente. Usa el ATR para dimensionarlo.")
                else:
                    sl_dist = abs(sl - entry) / entry * 100
                    if sl_dist > 5.0:
                        risks.append(f"⚠ Stop loss muy alejado ({sl_dist:.2f}%) — pérdida potencial alta")
                    elif sl_dist < 0.3:
                        risks.append(f"⚠ Stop loss muy ajustado ({sl_dist:.2f}%) — riesgo de stop-hunt")
                    else:
                        good.append(f"✓ Stop loss configurado a {sl_dist:.2f}%")

                if tp <= 0:
                    tips.append("→ Define un Take Profit. Mínimo 2:1 ratio riesgo/beneficio.")
                else:
                    if sl > 0:
                        sl_d = abs(sl - entry)
                        tp_d = abs(tp - entry)
                        rr   = tp_d / sl_d if sl_d > 0 else 0
                        if rr < 1.5:
                            risks.append(f"⚠ Ratio R:R bajo ({rr:.2f}:1) — TP demasiado cerca del SL")
                        elif rr >= 2.0:
                            good.append(f"✓ Ratio R:R {rr:.2f}:1 — buen balance riesgo/beneficio")
                        else:
                            tips.append(f"→ R:R {rr:.2f}:1 — considera mover TP para >2:1")

                # Liquidación
                if liq > 0:
                    liq_dist = abs(liq - mark) / mark * 100 if mark > 0 else 0
                    if liq_dist < 5.0:
                        risks.append(f"🔴 Liquidación MUY CERCANA a {liq_dist:.2f}% — PELIGRO")
                    elif liq_dist < 15.0:
                        risks.append(f"⚠ Liquidación a {liq_dist:.2f}% — mantener margen disponible")
                    else:
                        good.append(f"✓ Liquidación lejos ({liq_dist:.2f}%)")

                # Sugerencias basadas en ATR
                if atr_15 > 0 and entry > 0:
                    atr_pct = atr_15 / entry * 100
                    ideal_sl_dist = atr_15 * 1.5
                    ideal_tp_dist = atr_15 * 3.0
                    ideal_sl = entry - ideal_sl_dist if is_long else entry + ideal_sl_dist
                    ideal_tp = entry + ideal_tp_dist if is_long else entry - ideal_tp_dist
                    tips.append(
                        f"→ SL óptimo (1.5x ATR): {fp(ideal_sl)}  "
                        f"TP óptimo (3x ATR): {fp(ideal_tp)}"
                    )

                # Imprimir evaluación
                for item in good:
                    print(f"  {g(item)}")
                for item in risks:
                    print(f"  {y(item) if item.startswith('⚠') else r(item)}")

                if tips:
                    print(f"\n  {c('RECOMENDACIONES:')}")
                    for tip in tips:
                        print(f"  {w(tip)}")

                # ── Score resumido ─────────────────────────────────────────
                score = len(good) * 20 - len(risks) * 15
                score = max(0, min(100, score + 40))
                score_col = g if score >= 65 else y if score >= 40 else r
                section("SCORE DE SETUP")
                bar_len = score // 5
                bar = "█" * bar_len + "░" * (20 - bar_len)
                print(f"  {score_col(f'[{bar}] {score}/100')}")
                if score >= 65:
                    print(f"  {g('Setup SÓLIDO — mantener con gestión de riesgo activa')}")
                elif score >= 40:
                    print(f"  {y('Setup ACEPTABLE — revisa los warnings antes de añadir')}")
                else:
                    print(f"  {r('Setup DÉBIL — considera reducir o cerrar la posición')}")

    print(f"\n{b('━' * 62)}\n")


if __name__ == "__main__":
    asyncio.run(main())
