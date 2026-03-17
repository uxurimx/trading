"""
streams/klines.py
─────────────────
Fetcher periódico de klines REST para análisis técnico.

Obtiene 15m y 1h klines para el símbolo activo cada REFRESH_SECS segundos.
Los datos se almacenan en KlineStore y son leídos por TradeContextAnalyzer.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List

import aiohttp

from core.config import settings


_BASE = "https://api-testnet.bybit.com" if settings.bybit_testnet else "https://api.bybit.com"

# Intervalo de refresco: 90s es suficiente para klines (las velas son lentas)
REFRESH_SECS = 90


class KlineStore:
    """
    Cache de klines por símbolo.
    klines[symbol]["15"] = [[startTime, o, h, l, c, v], ...] más reciente primero
    """

    def __init__(self) -> None:
        self._data:     Dict[str, Dict[str, List[List]]] = {}
        self._last_ts:  Dict[str, float]                 = {}

    def get(self, symbol: str, interval: str) -> List[List]:
        return self._data.get(symbol, {}).get(interval, [])

    def set(self, symbol: str, interval: str, klines: List[List]) -> None:
        if symbol not in self._data:
            self._data[symbol] = {}
        self._data[symbol][interval] = klines

    def stale(self, symbol: str) -> bool:
        last = self._last_ts.get(symbol, 0)
        return time.monotonic() - last > REFRESH_SECS

    def touch(self, symbol: str) -> None:
        self._last_ts[symbol] = time.monotonic()


class KlineStream:
    """
    Mantiene klines actualizados para todos los símbolos activos.
    Se llama fetch_if_stale() desde el ciclo de UI para actualizar
    klines del símbolo visible sin bloquear la UI.
    """

    def __init__(self) -> None:
        self.store    = KlineStore()
        self._running = False
        self._queue:  asyncio.Queue = asyncio.Queue()

    # ── API pública ───────────────────────────────────────────────────────────

    def request(self, symbol: str) -> None:
        """Encolar una petición de actualización (no bloquea)."""
        try:
            self._queue.put_nowait(symbol)
        except asyncio.QueueFull:
            pass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        while self._running:
            try:
                symbol = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                if self.store.stale(symbol):
                    await self._fetch(symbol)
                    self.store.touch(symbol)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False

    # ── Fetch ─────────────────────────────────────────────────────────────────

    async def _fetch(self, symbol: str) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                k15, k1h = await asyncio.gather(
                    self._get(session, symbol, "15",  80),
                    self._get(session, symbol, "60", 220),
                )
            self.store.set(symbol, "15",  k15)
            self.store.set(symbol, "60",  k1h)
        except Exception:
            pass

    async def _get(
        self,
        session:  aiohttp.ClientSession,
        symbol:   str,
        interval: str,
        limit:    int,
    ) -> List[List]:
        url = f"{_BASE}/v5/market/kline"
        params = {
            "category": "linear",
            "symbol":   symbol,
            "interval": interval,
            "limit":    str(limit),
        }
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        async with session.get(f"{url}?{qs}") as resp:
            data = await resp.json()
            return data.get("result", {}).get("list", [])
