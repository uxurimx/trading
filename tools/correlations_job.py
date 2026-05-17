"""
tools/correlations_job.py — Calcula correlaciones 30d vs BTC.

Para cada símbolo en monitored_symbols descarga klines 1h × 720 horas desde
Bybit y calcula correlación Pearson y beta contra BTCUSDT (anchor).
Persiste en symbol_correlations vía core.narrative.save_correlations.

Uso:
    python -m tools.correlations_job
    python -m tools.correlations_job --max 80

Pensado para ejecutarse vía cron horaria o desde una tarea background.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.db import load_monitored_symbols
from core.narrative import init_tables, save_correlations

log = logging.getLogger("qts.correlations")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BYBIT_BASE = "https://api.bybit.com"
ANCHOR = "BTCUSDT"
INTERVAL_MIN = 60
WINDOW_H = 720          # 30 días × 24 h
MIN_OVERLAP = 60        # mínimo de retornos solapados para una correlación válida


async def _fetch_klines(session: aiohttp.ClientSession, symbol: str, limit: int = WINDOW_H) -> list[float]:
    """Devuelve precios close 1h ordenados de más viejo a más nuevo. [] si falla."""
    url = f"{BYBIT_BASE}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": str(INTERVAL_MIN),
        "limit": str(limit),
    }
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning("kline %s → HTTP %s", symbol, resp.status)
                return []
            j = await resp.json()
        rows = ((j or {}).get("result") or {}).get("list") or []
        # Bybit devuelve [startTs, open, high, low, close, vol, turnover] — más reciente primero
        rows = list(reversed(rows))
        return [float(r[4]) for r in rows if len(r) >= 5]
    except Exception as e:
        log.warning("kline %s falló: %s", symbol, e)
        return []


def _log_returns(prices: list[float]) -> list[float]:
    if len(prices) < 2:
        return []
    out: list[float] = []
    prev = prices[0]
    for p in prices[1:]:
        if prev > 0 and p > 0:
            out.append(math.log(p / prev))
        prev = p
    return out


def _pearson_beta(x: list[float], y: list[float]) -> tuple[float, float]:
    """Devuelve (corr, beta). beta = cov(x,y)/var(x): sensibilidad de y a x."""
    n = min(len(x), len(y))
    if n < MIN_OVERLAP:
        return 0.0, 0.0
    x = x[-n:]
    y = y[-n:]
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / n
    vx = sum((v - mx) ** 2 for v in x) / n
    vy = sum((v - my) ** 2 for v in y) / n
    if vx <= 0 or vy <= 0:
        return 0.0, 0.0
    corr = cov / math.sqrt(vx * vy)
    beta = cov / vx
    return corr, beta


async def run(max_symbols: int = 60) -> int:
    init_tables()
    symbols = load_monitored_symbols()
    if not symbols:
        log.error("No hay monitored_symbols en la DB. Arranca el servidor antes.")
        return 0
    symbols = list(dict.fromkeys(symbols[:max_symbols]))
    if ANCHOR not in symbols:
        symbols = [ANCHOR] + symbols

    log.info("Calculando correlaciones para %d símbolos (anchor=%s, %dh)", len(symbols), ANCHOR, WINDOW_H)
    sem = asyncio.Semaphore(5)
    closes_map: dict[str, list[float]] = {}

    async with aiohttp.ClientSession() as session:
        async def _fetch_one(sym: str) -> None:
            async with sem:
                closes_map[sym] = await _fetch_klines(session, sym)
                await asyncio.sleep(0.15)

        await asyncio.gather(*[_fetch_one(s) for s in symbols], return_exceptions=True)

    anchor_ret = _log_returns(closes_map.get(ANCHOR) or [])
    if len(anchor_ret) < MIN_OVERLAP:
        log.error("Anchor %s con sólo %d returns; abortando", ANCHOR, len(anchor_ret))
        return 0

    rows: list[tuple[str, str, float, float, int]] = []
    for sym, closes in closes_map.items():
        if sym == ANCHOR:
            rows.append((sym, ANCHOR, 1.0, 1.0, WINDOW_H))
            continue
        corr, beta = _pearson_beta(anchor_ret, _log_returns(closes))
        rows.append((sym, ANCHOR, corr, beta, WINDOW_H))
        if abs(corr) >= 0.4:
            log.info("  %-14s corr=%+.3f β=%+.3f", sym, corr, beta)

    save_correlations(rows)
    log.info("Guardadas %d filas en symbol_correlations", len(rows))
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Job de correlaciones 30d vs BTC")
    parser.add_argument("--max", type=int, default=60, help="Máximo de símbolos a procesar")
    args = parser.parse_args()
    asyncio.run(run(max_symbols=args.max))


if __name__ == "__main__":
    main()
