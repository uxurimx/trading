/*
 * QtsGauges — cabina del piloto (SVG).
 *
 * Sistema de coords:
 *   viewBox 0 0 120 70
 *   center  (60, 55)   radius 46
 *   Arco semicircular superior: ángulos en [π, 2π].
 *     π    (180°)   → izquierda     (cos=-1, sin= 0) → (14, 55)
 *     3π/2 (270°)   → arriba        (cos= 0, sin=-1) → (60,  9)
 *     2π   (360°)   → derecha       (cos= 1, sin= 0) → (106,55)
 *   Score 0-100 → ángulo = π + s·π
 */
(function () {
  'use strict';

  const CX = 60, CY = 55, R = 46;

  function polar(angle, r) {
    return { x: CX + r * Math.cos(angle), y: CY + r * Math.sin(angle) };
  }

  // Path entre dos ángulos. Para semicírculo superior siempre a0 < a1 y |Δ| ≤ π.
  function arcPath(a0, a1, r) {
    const p0 = polar(a0, r);
    const p1 = polar(a1, r);
    const large = Math.abs(a1 - a0) > Math.PI ? 1 : 0;
    const sweep = a1 > a0 ? 1 : 0;
    return `M ${p0.x.toFixed(2)} ${p0.y.toFixed(2)} `
         + `A ${r} ${r} 0 ${large} ${sweep} ${p1.x.toFixed(2)} ${p1.y.toFixed(2)}`;
  }

  function scoreToAngle(score) {
    const s = Math.max(0, Math.min(100, score)) / 100;
    return Math.PI + s * Math.PI;          // π (izq) → 2π (der)
  }

  function needle(angle, color) {
    const tip  = polar(angle, R - 6);
    const tail = polar(angle, -6);          // pequeña cola opuesta para anclaje visual
    return `
      <line x1="${tail.x.toFixed(2)}" y1="${tail.y.toFixed(2)}"
            x2="${tip.x.toFixed(2)}"  y2="${tip.y.toFixed(2)}"
            stroke="${color}" stroke-width="2.5" stroke-linecap="round"/>
      <circle cx="${CX}" cy="${CY}" r="3.5" fill="${color}"/>
      <circle cx="${CX}" cy="${CY}" r="1.5" fill="#0f172a"/>`;
  }

  function svgOpen(extraCls) {
    return `<svg viewBox="0 0 120 70" class="g-svg ${extraCls || ''}"
                 preserveAspectRatio="xMidYMid meet">`;
  }

  // ── Tacómetro de Presión ───────────────────────────────────────────────────
  function renderPressure(el, p) {
    if (!el) return;
    const score = (p && p.score) || 0;
    const side  = (p && p.side)  || 'neutral';
    const color = side === 'fear'  ? '#ef4444'
                : side === 'greed' ? '#22c55e'
                :                    '#94a3b8';

    // 3 sectores en [π, 2π]: 0-33 verde, 33-66 ámbar, 66-100 rojo
    const a0   = Math.PI;
    const a33  = Math.PI + 0.33 * Math.PI;
    const a66  = Math.PI + 0.66 * Math.PI;
    const a100 = 2 * Math.PI;
    const lowArc = arcPath(a0,  a33,  R);
    const midArc = arcPath(a33, a66,  R);
    const hiArc  = arcPath(a66, a100, R);
    const ang    = scoreToAngle(score);

    el.innerHTML = `
      ${svgOpen()}
        <path d="${lowArc}" stroke="#22c55e" stroke-width="5" fill="none" opacity=".45"/>
        <path d="${midArc}" stroke="#f59e0b" stroke-width="5" fill="none" opacity=".55"/>
        <path d="${hiArc}"  stroke="#ef4444" stroke-width="5" fill="none" opacity=".65"/>
        ${needle(ang, color)}
        <text x="60" y="68" text-anchor="middle" class="g-num">${score.toFixed(0)}</text>
      </svg>
      <div class="g-lbl">
        <span class="g-title">Presión</span>
        <span class="g-side g-${side}">${side.toUpperCase()}</span>
      </div>`;
  }

  // ── Velocímetro ────────────────────────────────────────────────────────────
  function renderVelocity(el, v) {
    if (!el) return;
    const score = (v && v.score) || 0;
    const pct   = (v && v.pct_per_min) || 0;
    const ref   = (v && v.ref_pct) || 0;
    const color = score > 80 ? '#ef4444'
                : score > 50 ? '#f59e0b'
                :              '#22d3ee';
    const ang     = scoreToAngle(score);
    const baseArc = arcPath(Math.PI, 2 * Math.PI, R);
    const fillEnd = scoreToAngle(score);
    const fillArc = arcPath(Math.PI, fillEnd, R);

    el.innerHTML = `
      ${svgOpen()}
        <path d="${baseArc}" stroke="#1e293b" stroke-width="5" fill="none"/>
        <path d="${fillArc}" stroke="${color}" stroke-width="5" fill="none" stroke-linecap="round"/>
        ${needle(ang, color)}
        <text x="60" y="68" text-anchor="middle" class="g-num">${pct.toFixed(2)}<tspan class="g-unit"> %/m</tspan></text>
      </svg>
      <div class="g-lbl">
        <span class="g-title">Velocidad</span>
        <span class="g-sub">ref ${ref.toFixed(2)}%</span>
      </div>`;
  }

  // ── Carretera ──────────────────────────────────────────────────────────────
  // Render con SVG para igualar footprint visual con los otros dos gauges.
  // Cada tipo dibuja una "vista de carril" distinta dentro del mismo viewBox.
  const ROAD_VIS = {
    highway:  { color: '#22c55e', tone: 'Autopista'  },
    curvy:    { color: '#f59e0b', tone: 'Curvas'     },
    rough:    { color: '#ef4444', tone: 'Terracería' },
    gridlock: { color: '#64748b', tone: 'Atasco'     },
  };

  function roadShape(type, color) {
    // Líneas dentro del viewBox (área de carril ≈ y∈[10,45], x∈[20,100])
    switch (type) {
      case 'highway':
        return `
          <line x1="20" y1="20" x2="100" y2="20" stroke="${color}" stroke-width="3"/>
          <line x1="20" y1="35" x2="100" y2="35" stroke="${color}" stroke-width="3" stroke-dasharray="6 4"/>
          <line x1="20" y1="50" x2="100" y2="50" stroke="${color}" stroke-width="3"/>`;
      case 'curvy':
        return `
          <path d="M 18 35 Q 35 12, 60 35 T 102 35"
                stroke="${color}" stroke-width="3" fill="none" stroke-linecap="round"/>`;
      case 'rough':
        return `
          <polyline points="18,38 28,22 38,40 48,20 58,42 68,22 78,40 88,20 102,38"
                    stroke="${color}" stroke-width="2.5" fill="none" stroke-linejoin="round"/>`;
      case 'gridlock':
      default:
        return `
          <rect x="22" y="18" width="14" height="8" fill="${color}" opacity=".7"/>
          <rect x="42" y="18" width="14" height="8" fill="${color}" opacity=".5"/>
          <rect x="62" y="18" width="14" height="8" fill="${color}" opacity=".5"/>
          <rect x="82" y="18" width="14" height="8" fill="${color}" opacity=".7"/>
          <rect x="22" y="38" width="14" height="8" fill="${color}" opacity=".5"/>
          <rect x="42" y="38" width="14" height="8" fill="${color}" opacity=".7"/>
          <rect x="62" y="38" width="14" height="8" fill="${color}" opacity=".7"/>
          <rect x="82" y="38" width="14" height="8" fill="${color}" opacity=".5"/>`;
    }
  }

  function renderRoad(el, r) {
    if (!el) return;
    const type = (r && r.type) || 'curvy';
    const vis  = ROAD_VIS[type] || ROAD_VIS.curvy;
    const lev  = (r && r.leverage_hint) || '—';
    const reg  = (r && r.regime_label)  || '?';
    const conf = (r && r.confidence)    || 0;

    el.innerHTML = `
      ${svgOpen('g-svg-road')}
        ${roadShape(type, vis.color)}
        <text x="60" y="64" text-anchor="middle" class="g-num"
              fill="${vis.color}">${lev}</text>
      </svg>
      <div class="g-lbl">
        <span class="g-title">Carretera</span>
        <span class="g-side" style="color:${vis.color}">${vis.tone}</span>
        <span class="g-sub">${reg} · conf ${conf}</span>
      </div>`;
  }

  function renderAll(rootEl, payload) {
    if (!rootEl || !payload) return;
    const pEl = rootEl.querySelector('[data-gauge="pressure"]');
    const vEl = rootEl.querySelector('[data-gauge="velocity"]');
    const rEl = rootEl.querySelector('[data-gauge="road"]');
    if (pEl) renderPressure(pEl, payload.pressure);
    if (vEl) renderVelocity(vEl, payload.velocity);
    if (rEl) renderRoad(rEl, payload.road);
  }

  window.QtsGauges = { renderPressure, renderVelocity, renderRoad, renderAll };
})();
