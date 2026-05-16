"""
web/zone_tracker.py — cronotopología del trade.

Clasifica el mark price en una de 8 zonas y acumula tiempo cocinado por zona.
El estado se mantiene en memoria por pos_key y se persiste en DuckDB
(position_zone_state) con debounce — sobrevive reinicios del server.

Zonas (LONG; en SHORT se invierten los signos):
    below_sl  : mark <  sl
    sl_entry  : sl   ≤ mark <  entry
    entry_be  : entry≤ mark <  be
    be_25     : be   ≤ mark <  milestone[25%]
    25_50     : m25  ≤ mark <  m50
    50_75     : m50  ≤ mark <  m75
    75_tp     : m75  ≤ mark <  tp
    above_tp  : mark ≥  tp
"""
from __future__ import annotations

import logging
import time
from threading import Lock
from typing import Optional

log = logging.getLogger("qts.zones")

ZONES = ['below_sl', 'sl_entry', 'entry_be',
         'be_25', '25_50', '50_75', '75_tp', 'above_tp']

ZONE_LABELS = {
    'below_sl': 'SL−',
    'sl_entry': 'SL→E',
    'entry_be': 'E→BE',
    'be_25':    'BE→25',
    '25_50':    '25→50',
    '50_75':    '50→75',
    '75_tp':    '75→TP',
    'above_tp': 'TP+',
}

# Histograma de alta resolución sobre el rango SL→TP (40 buckets).
# In-memory only: revela patrones de dwell ("dónde se pasea el precio")
# dentro de cada zona. Se reinicia con el server (parent-zone seconds persisten).
HIST_BUCKETS = 40

# Debounce de flush a DB: cada 10s o cada 30 samples por pos_key.
FLUSH_INTERVAL_S = 10.0
FLUSH_EVERY_N    = 30


def classify_zone(geom: dict, mark: float) -> Optional[str]:
    """Mapea (geom, mark) → zone_key. None si la geometría es inválida."""
    try:
        sl     = float(geom.get('sl', 0))
        entry  = float(geom.get('entry', 0))
        tp     = float(geom.get('tp', 0))
        be     = float(geom.get('be', entry))
        is_long = bool(geom.get('is_long', True))
        ms     = geom.get('milestones') or []
        if len(ms) < 3 or sl <= 0 or tp <= 0 or entry <= 0:
            return None
        m25, m50, m75 = ms[0]['price'], ms[1]['price'], ms[2]['price']
    except Exception:
        return None

    if is_long:
        if mark < sl:    return 'below_sl'
        if mark < entry: return 'sl_entry'
        if mark < be:    return 'entry_be'
        if mark < m25:   return 'be_25'
        if mark < m50:   return '25_50'
        if mark < m75:   return '50_75'
        if mark < tp:    return '75_tp'
        return 'above_tp'
    # SHORT — el orden de precios se invierte (tp < m75 < ... < entry < sl)
    if mark > sl:    return 'below_sl'
    if mark > entry: return 'sl_entry'
    if mark > be:    return 'entry_be'
    if mark > m25:   return 'be_25'
    if mark > m50:   return '25_50'
    if mark > m75:   return '50_75'
    if mark > tp:    return '75_tp'
    return 'above_tp'


class ZoneTracker:
    """State machine de residencia en zonas, thread-safe y persistente."""

    def __init__(self) -> None:
        self._lock  = Lock()
        self._st: dict = {}
        self._dirty:        dict[str, int] = {}    # pos_key → samples desde último flush
        self._last_flush:   dict[str, float] = {}  # pos_key → ts del último flush
        self._hydrate()

    # ── Hidratación desde DB ─────────────────────────────────────────────────
    def _hydrate(self) -> None:
        """Carga el estado persistido al arrancar el server."""
        try:
            from core.db import load_all_zone_states
            saved = load_all_zone_states()
        except Exception as e:
            log.warning("ZoneTracker hydrate falló: %s", e)
            return
        if not saved:
            return
        now = time.time()
        for pk, s in saved.items():
            zones = s.get("zones") or {}
            # Asegurar todas las zonas presentes (compatibilidad si cambia ZONES)
            full_zones = {
                z: {
                    'seconds':        float(zones.get(z, {}).get('seconds', 0.0)),
                    'visits':         int(zones.get(z, {}).get('visits', 0)),
                    'max_streak':     float(zones.get(z, {}).get('max_streak', 0.0)),
                    'current_streak': float(zones.get(z, {}).get('current_streak', 0.0)),
                    'first_entered':  zones.get(z, {}).get('first_entered'),
                    'last_entered':   zones.get(z, {}).get('last_entered'),
                } for z in ZONES
            }
            self._st[pk] = {
                'opened_at': s.get("opened_at") or now,
                'last_zone': s.get("last_zone"),
                'last_ts':   s.get("last_ts") or now,
                'zones':     full_zones,
                'histogram': [0.0] * HIST_BUCKETS,  # se reconstruye in-memory
            }
            self._last_flush[pk] = now
        log.info("ZoneTracker: rehidratadas %d posiciones desde DB", len(saved))

    # ── Flush a DB ───────────────────────────────────────────────────────────
    def _flush(self, pos_key: str) -> None:
        """Persiste un pos_key. Llamar fuera del lock cuando sea posible."""
        st = self._st.get(pos_key)
        if not st:
            return
        try:
            from core.db import save_zone_state
            save_zone_state(
                pos_key,
                st['opened_at'],
                st['last_zone'],
                st['last_ts'],
                st['zones'],
            )
            self._dirty[pos_key]      = 0
            self._last_flush[pos_key] = time.time()
        except Exception as e:
            log.debug("flush %s falló: %s", pos_key, e)

    def _maybe_flush(self, pos_key: str) -> None:
        n   = self._dirty.get(pos_key, 0)
        ts0 = self._last_flush.get(pos_key, 0)
        if n >= FLUSH_EVERY_N or (n > 0 and time.time() - ts0 >= FLUSH_INTERVAL_S):
            self._flush(pos_key)

    # ── Plantilla de un pos nuevo ────────────────────────────────────────────
    def _new_pos(self, opened_at: float) -> dict:
        return {
            'opened_at': opened_at,
            'last_zone': None,
            'last_ts':   opened_at,
            'zones': {
                z: {
                    'seconds':        0.0,
                    'visits':         0,
                    'max_streak':     0.0,
                    'current_streak': 0.0,
                    'first_entered':  None,
                    'last_entered':   None,
                } for z in ZONES
            },
            'histogram': [0.0] * HIST_BUCKETS,
        }

    @staticmethod
    def _bucket_idx(geom: dict, mark: float) -> Optional[int]:
        """Mapea mark → índice de bucket [0..HIST_BUCKETS-1] sobre SL→TP.
        Valores fuera del rango se clampean al borde. None si geom inválida."""
        try:
            sl = float(geom.get('sl', 0))
            tp = float(geom.get('tp', 0))
            if sl <= 0 or tp <= 0 or sl == tp:
                return None
        except Exception:
            return None
        # Normalizar: 0 = SL, 1 = TP. SHORT tiene sl > tp, igual de válido (signo invertido).
        t = (mark - sl) / (tp - sl)
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        idx = int(t * HIST_BUCKETS)
        if idx >= HIST_BUCKETS:
            idx = HIST_BUCKETS - 1
        return idx

    # ── API ──────────────────────────────────────────────────────────────────
    def sample(
        self,
        pos_key: str,
        geom: dict,
        mark: float,
        opened_at_hint: Optional[float] = None,
    ) -> Optional[str]:
        """
        Registra una muestra. Devuelve la zona clasificada o None.

        opened_at_hint: timestamp epoch en segundos del momento real de apertura
        del trade (típicamente Bybit createdTime/1000). Solo se usa la primera
        vez que se ve este pos_key (cuando se crea el estado en memoria).
        """
        now  = time.time()
        zone = classify_zone(geom, mark)
        if zone is None:
            return None

        do_flush = False
        with self._lock:
            st = self._st.get(pos_key)
            if st is None:
                # Trade nuevo: usar opened_at de Bybit si está disponible
                opened = opened_at_hint if (opened_at_hint and opened_at_hint > 0) else now
                st = self._new_pos(opened)
                self._st[pos_key] = st
                # Si el opened_at es del pasado (típico tras reinicio), el primer
                # sample también debe cubrir el tiempo transcurrido — pero no
                # sabemos en qué zona estuvo. Lo dejamos sin acumular: lo que
                # importa es que el `opened_at` quede correcto.
                self._last_flush[pos_key] = 0  # forzar flush rápido

            prev = st['last_zone']
            dt   = max(0.0, now - st['last_ts'])

            # Histograma fino SL→TP — acumular dt en el bucket actual.
            bidx = self._bucket_idx(geom, mark)
            if bidx is not None and dt > 0.0:
                hist = st.get('histogram')
                if hist is None:
                    hist = [0.0] * HIST_BUCKETS
                    st['histogram'] = hist
                hist[bidx] += dt

            if prev is None:
                z = st['zones'][zone]
                z['visits'] += 1
                z['first_entered'] = z['first_entered'] or now
                z['last_entered']  = now
                z['current_streak'] = 0.0
            elif prev == zone:
                z = st['zones'][zone]
                z['seconds']        += dt
                z['current_streak'] += dt
                if z['current_streak'] > z['max_streak']:
                    z['max_streak'] = z['current_streak']
            else:
                pz = st['zones'][prev]
                pz['seconds']        += dt
                pz['current_streak'] = 0.0
                z = st['zones'][zone]
                z['visits'] += 1
                z['first_entered'] = z['first_entered'] or now
                z['last_entered']  = now
                z['current_streak'] = 0.0

            st['last_zone'] = zone
            st['last_ts']   = now
            self._dirty[pos_key] = self._dirty.get(pos_key, 0) + 1

            n   = self._dirty[pos_key]
            ts0 = self._last_flush.get(pos_key, 0)
            do_flush = n >= FLUSH_EVERY_N or (n > 0 and now - ts0 >= FLUSH_INTERVAL_S)

        if do_flush:
            self._flush(pos_key)
        return zone

    def summary(self, pos_key: str) -> Optional[dict]:
        with self._lock:
            st = self._st.get(pos_key)
            if not st:
                return None
            total_s = sum(z['seconds'] for z in st['zones'].values())
            denom   = max(1e-9, total_s)
            out_zones = []
            for zk in ZONES:
                z = st['zones'][zk]
                if z['seconds'] <= 0.0 and z['visits'] == 0:
                    continue
                out_zones.append({
                    'key':          zk,
                    'label':        ZONE_LABELS[zk],
                    'seconds':      round(z['seconds'], 1),
                    'visits':       z['visits'],
                    'max_streak':   round(z['max_streak'], 1),
                    'pct_of_life':  round(z['seconds'] / denom * 100, 1),
                    'last_entered': z['last_entered'],
                })
            hist = st.get('histogram') or []
            return {
                'opened_at':     st['opened_at'],
                'current_zone':  st['last_zone'],
                'total_seconds': round(total_s, 1),
                'zones':         out_zones,
                'histogram':     [round(v, 1) for v in hist],
                'hist_buckets':  HIST_BUCKETS,
            }

    def forget(self, pos_key: str) -> None:
        with self._lock:
            self._st.pop(pos_key, None)
            self._dirty.pop(pos_key, None)
            self._last_flush.pop(pos_key, None)
        try:
            from core.db import delete_zone_state
            delete_zone_state(pos_key)
        except Exception as e:
            log.debug("forget delete_zone_state %s: %s", pos_key, e)

    def known_keys(self) -> list:
        with self._lock:
            return list(self._st.keys())


_global = ZoneTracker()


def tracker() -> ZoneTracker:
    return _global
