"""
web/zone_tracker.py — cronotopología del trade.

Clasifica el mark price en una de 8 zonas y acumula tiempo cocinado por zona.
El estado se mantiene en memoria por pos_key. El frontend lee `summary(pos_key)`
desde el snapshot para renderizar el heatmap temporal, bubbles y donut.

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

import time
from threading import Lock
from typing import Optional

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
    """State machine de residencia en zonas, thread-safe."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._st: dict = {}

    def _new_pos(self, now: float) -> dict:
        return {
            'opened_at': now,
            'last_zone': None,
            'last_ts':   now,
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
        }

    def sample(self, pos_key: str, geom: dict, mark: float) -> Optional[str]:
        """Registra una muestra. Devuelve la zona clasificada o None."""
        now  = time.time()
        zone = classify_zone(geom, mark)
        if zone is None:
            return None

        with self._lock:
            st = self._st.get(pos_key)
            if st is None:
                st = self._new_pos(now)
                self._st[pos_key] = st

            prev = st['last_zone']
            dt   = max(0.0, now - st['last_ts'])

            if prev is None:
                # Primera muestra: registrar visita inicial
                z = st['zones'][zone]
                z['visits'] += 1
                z['first_entered'] = z['first_entered'] or now
                z['last_entered']  = now
                z['current_streak'] = 0.0
            elif prev == zone:
                # Continúa en la misma zona: acumular tiempo
                z = st['zones'][zone]
                z['seconds']        += dt
                z['current_streak'] += dt
                if z['current_streak'] > z['max_streak']:
                    z['max_streak'] = z['current_streak']
            else:
                # Transición: el dt cuenta para la zona PREVIA hasta este sample
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
        return zone

    def summary(self, pos_key: str) -> Optional[dict]:
        """Resumen agregado para el snapshot. None si no hay datos."""
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
            return {
                'opened_at':     st['opened_at'],
                'current_zone':  st['last_zone'],
                'total_seconds': round(total_s, 1),
                'zones':         out_zones,
            }

    def forget(self, pos_key: str) -> None:
        with self._lock:
            self._st.pop(pos_key, None)

    def known_keys(self) -> list:
        with self._lock:
            return list(self._st.keys())


_global = ZoneTracker()


def tracker() -> ZoneTracker:
    return _global
