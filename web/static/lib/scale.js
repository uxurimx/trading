/* QtsScale — transformada precio→% reproyectable con zoom telescópico.
 *
 * El viewport es una ventana [min, max] en el espacio de precio. La fracción
 * `span` se mide relativa al rango total SL↔TP:  span=1 → ventana = [sl,tp].
 *
 * Niveles discretos: L-1 (200%), L0 (100%), L1 (50%), L2 (25%), L3 (12.5%),
 * L4 (6.25%). Cada nivel corta la ventana a la mitad respecto al anterior
 * (geometría log-2 — coincide con la intuición de las temporalidades).
 */
'use strict';

const QtsScale = (() => {
  const LEVELS = [
    { idx: -1, span: 2.00,   label: '200%'  },
    { idx:  0, span: 1.00,   label: '100%'  },
    { idx:  1, span: 0.50,   label: '50%'   },
    { idx:  2, span: 0.25,   label: '25%'   },
    { idx:  3, span: 0.125,  label: '12.5%' },
    { idx:  4, span: 0.0625, label: '6.25%' },
  ];
  const MIN_IDX = -1, MAX_IDX = 4;

  function clampIdx(idx) {
    return Math.max(MIN_IDX, Math.min(MAX_IDX, idx | 0));
  }
  function level(idx) {
    const i = clampIdx(idx);
    return LEVELS.find(l => l.idx === i) || LEVELS[1];
  }

  /** Precio del ancla (entry|be|mid|mark). Devuelve mark si no aplica. */
  function anchorPrice(g, anchor) {
    if (!g) return 0;
    const { sl = 0, entry = 0, tp = 0, be = 0, mark = 0 } = g;
    switch (anchor) {
      case 'entry': return entry || mark;
      case 'be':    return be    || entry || mark;
      case 'mid':   return (sl + tp) / 2;
      case 'mark':
      default:      return mark  || entry;
    }
  }

  /** Imán: si el ancla 'auto', elige el punto crítico más cercano al mark. */
  function smartAnchor(g, levelIdx) {
    const range = Math.abs((g.tp || 0) - (g.sl || 0));
    if (range <= 0) return g.mark || g.entry || 0;
    const mark = g.mark || g.entry || 0;
    const lv = level(levelIdx);
    // Umbral magnético: si un punto crítico está a < 25% del span del nivel,
    // anclamos a él para evitar que oscile fuera de la ventana.
    const magnet = range * lv.span * 0.25;
    const pts = [
      { price: mark,          weight: 0 },
      { price: g.entry || mark, weight: 0.7 },
      { price: g.be    || g.entry || mark, weight: 0.8 },
    ];
    let best = pts[0], bestScore = Infinity;
    for (const p of pts) {
      const d = Math.abs(mark - p.price);
      const score = d - p.weight * magnet * 0.5;
      if (d < magnet && score < bestScore) { best = p; bestScore = score; }
    }
    return best.price;
  }

  /** Ventana [min,max] dado nivel + ancla + geometría. */
  function viewport(levelIdx, anchor, g) {
    const lv = level(levelIdx);
    const sl = g.sl || 0, tp = g.tp || 0;
    const range = Math.abs(tp - sl);
    // L0: vista canónica SL↔TP, sin sesgo del ancla
    if (lv.idx === 0 || range <= 0) {
      return {
        min: Math.min(sl, tp),
        max: Math.max(sl, tp),
        span: lv.span, levelIdx: lv.idx, label: lv.label,
      };
    }
    const c = (typeof anchor === 'number')
      ? anchor
      : anchorPrice(g, anchor || 'mark');
    const half = range * lv.span / 2;
    return {
      min: c - half, max: c + half,
      span: lv.span, levelIdx: lv.idx, label: lv.label,
    };
  }

  /** Transformada precio→% en [0,100] (puede salir del rango). */
  function T(price, view) {
    if (!view || view.max === view.min) return 50;
    return (price - view.min) / (view.max - view.min) * 100;
  }

  /** null si está dentro; sino {side:'left'|'right', delta, deltaPct} */
  function outOfRange(price, view) {
    if (!view || price == null) return null;
    if (price >= view.min && price <= view.max) return null;
    const span = view.max - view.min;
    if (price < view.min) {
      return { side: 'left',  delta: view.min - price, deltaPct: (view.min - price) / span * 100 };
    }
    return { side: 'right', delta: price - view.max, deltaPct: (price - view.max) / span * 100 };
  }

  /** Acota un par de precios al viewport y devuelve {left, width} en %. */
  function clipRange(p1, p2, view) {
    const a = T(p1, view), b = T(p2, view);
    const lo = Math.max(0, Math.min(a, b));
    const hi = Math.min(100, Math.max(a, b));
    return { left: lo, width: Math.max(0, hi - lo) };
  }

  return { LEVELS, MIN_IDX, MAX_IDX, clampIdx, level,
           anchorPrice, smartAnchor, viewport, T, outOfRange, clipRange };
})();

if (typeof window !== 'undefined') window.QtsScale = QtsScale;
