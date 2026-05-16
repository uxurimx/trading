"""
web/level_tracker.py — historial de niveles estructurales.

Cada vez que se construye un liquidity map, se llama a `record()` con los niveles
detectados. El tracker mantiene "tracks" (un track = un nivel persistente en el
tiempo, identificado por proximidad de precio entre snapshots) y devuelve la
trayectoria reciente de cada uno: el frontend dibuja una estela vertical que
muestra cómo el precio del nivel ha migrado en los últimos minutos.

Modelo:
  · Asociación: un nivel del snapshot N se enlaza con un track existente si está
    dentro de TRACK_MATCH_PCT (0.3%) del último punto del track Y comparten
    level_type. Si no hay match → nuevo track.
  · Decay: tracks sin updates en TRACK_TTL_S (5min) son podados.
  · Tamaño: máx MAX_POINTS_PER_TRACK puntos por track (FIFO).

Sin persistencia — el estado vive en memoria del proceso.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

_TRACK_MATCH_PCT      = 0.003       # 0.3% de tolerancia en precio
_TRACK_TTL_S          = 300.0       # 5min sin updates → podar
_MAX_POINTS_PER_TRACK = 90          # ~15min @ 10s sample
_MAX_TRACKS_PER_SYM   = 60


_TOUCH_BAND_PCT = 0.001        # ±0.1% del nivel para contar un toque


class _Track:
    __slots__ = ("id", "level_type", "points", "last_ts", "touches", "in_band", "last_touch_ts")

    def __init__(self, tid: int, level_type: str, ts_ms: int, price: float, strength: float):
        self.id            = tid
        self.level_type    = level_type
        self.points: Deque[Tuple[int, float, float]] = deque(maxlen=_MAX_POINTS_PER_TRACK)
        self.points.append((ts_ms, price, strength))
        self.last_ts       = ts_ms
        self.touches       = 0
        self.in_band       = False
        self.last_touch_ts: Optional[int] = None

    def append(self, ts_ms: int, price: float, strength: float) -> None:
        self.points.append((ts_ms, price, strength))
        self.last_ts = ts_ms

    def last_price(self) -> float:
        return self.points[-1][1]

    def update_touch(self, current_price: float, ts_ms: int) -> None:
        if current_price <= 0:
            return
        ref = self.last_price()
        if ref <= 0:
            return
        dist = abs(current_price - ref) / ref
        inside = dist <= _TOUCH_BAND_PCT
        if inside and not self.in_band:
            self.touches += 1
            self.last_touch_ts = ts_ms
        self.in_band = inside


class LevelTracker:
    def __init__(self) -> None:
        self._lock      = threading.Lock()
        self._tracks: Dict[str, Dict[int, _Track]] = {}   # symbol → {tid: Track}
        self._next_id   = 1
        self._last_record: Dict[str, float] = {}          # symbol → ts_s

    def touch_tick(self, symbol: str, current_price: float) -> None:
        """Actualiza el conteo de toques de cada track con el precio actual.
        Llamar con cadencia alta (cada snapshot, ~1Hz) — barato."""
        if not symbol or current_price <= 0:
            return
        ts_ms = int(time.time() * 1000)
        with self._lock:
            sym_tracks = self._tracks.get(symbol)
            if not sym_tracks:
                return
            for tr in sym_tracks.values():
                tr.update_touch(current_price, ts_ms)

    def record(self, symbol: str, levels: List[Dict], min_interval_s: float = 8.0) -> None:
        """Asocia los niveles del snapshot a tracks existentes. Debounced por símbolo."""
        if not symbol or not levels:
            return
        now_s = time.time()
        with self._lock:
            last = self._last_record.get(symbol, 0.0)
            if now_s - last < min_interval_s:
                return
            self._last_record[symbol] = now_s

            sym_tracks = self._tracks.setdefault(symbol, {})
            ts_ms = int(now_s * 1000)

            # Poda tracks expirados antes de matching
            expired = [tid for tid, tr in sym_tracks.items()
                       if (ts_ms - tr.last_ts) / 1000.0 > _TRACK_TTL_S]
            for tid in expired:
                sym_tracks.pop(tid, None)

            used: set = set()
            for lv in levels:
                try:
                    price = float(lv["price"])
                    ltype = str(lv.get("type") or "UNK")
                    strength = float(lv.get("strength") or 0.0)
                except Exception:
                    continue
                if price <= 0:
                    continue

                best_tid: Optional[int] = None
                best_dist  = 1e18
                for tid, tr in sym_tracks.items():
                    if tid in used or tr.level_type != ltype:
                        continue
                    last_p = tr.last_price()
                    if last_p <= 0:
                        continue
                    dist = abs(price - last_p) / last_p
                    if dist <= _TRACK_MATCH_PCT and dist < best_dist:
                        best_tid  = tid
                        best_dist = dist

                if best_tid is not None:
                    sym_tracks[best_tid].append(ts_ms, price, strength)
                    used.add(best_tid)
                else:
                    if len(sym_tracks) >= _MAX_TRACKS_PER_SYM:
                        # elimina el más viejo
                        oldest_tid = min(sym_tracks, key=lambda t: sym_tracks[t].last_ts)
                        sym_tracks.pop(oldest_tid, None)
                    tid = self._next_id
                    self._next_id += 1
                    sym_tracks[tid] = _Track(tid, ltype, ts_ms, price, strength)
                    used.add(tid)

    def get_trails(
        self,
        symbol: str,
        view_min: float,
        view_max: float,
        min_points: int = 2,
        max_age_s: float = 600.0,
    ) -> List[Dict]:
        """Devuelve los tracks visibles en el viewport con sus puntos serializados."""
        out: List[Dict] = []
        now_ms = int(time.time() * 1000)
        with self._lock:
            sym_tracks = self._tracks.get(symbol)
            if not sym_tracks:
                return out
            for tid, tr in sym_tracks.items():
                pts = [(t, p, s) for (t, p, s) in tr.points
                       if (now_ms - t) / 1000.0 <= max_age_s
                       and view_min <= p <= view_max]
                if len(pts) < min_points:
                    continue
                out.append({
                    "id":       tid,
                    "type":     tr.level_type,
                    "touches":  tr.touches,
                    "last_touch_ts": tr.last_touch_ts,
                    "points":   [
                        {"ts": t, "price": round(p, 8), "strength": round(s, 3)}
                        for (t, p, s) in pts
                    ],
                })
        return out


tracker = LevelTracker()
