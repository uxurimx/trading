/*
 * QtsGauges — cabina del piloto (SVG).
 *
 * Tres instrumentos sobre el campo gravitacional:
 *   · renderPressure(el, p)  — tacómetro 0-100, sector fear/greed/neutral
 *   · renderVelocity(el, v)  — velocímetro 0-100 referenciado a ATR ambiental
 *   · renderRoad(el, r)      — tipo de carretera + leverage hint
 *
 * Todos producen SVG inline ligero. Tema controlado por CSS variables.
 */
(function () {
  'use strict';

  const TAU = Math.PI * 2;

  function arc(cx, cy, r, a0, a1) {
    // SVG path para un arco entre dos ángulos (radianes)
    const x0 = cx + r * Math.cos(a0);
    const y0 = cy + r * Math.sin(a0);
    const x1 = cx + r * Math.cos(a1);
    const y1 = cy + r * Math.sin(a1);
    const large = (a1 - a0) > Math.PI ? 1 : 0;
    return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
  }

  // Mapea score [0,100] a ángulo dentro de un arco semicircular superior
  // (de 180°=π a 360°/0°). Es decir, desde izquierda (π) hasta derecha (0).
  function scoreToAngle(score) {
    const s = Math.max(0, Math.min(100, score)) / 100;
    return Math.PI - s * Math.PI;   // 0→π (izq); 100→0 (der)
  }

  function needle(cx, cy, r, angle, color) {
    const x = cx + r * Math.cos(angle);
    const y = cy + r * Math.sin(angle);
    return `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(2)}" y2="${y.toFixed(2)}"
             stroke="${color}" stroke-width="2.5" stroke-linecap="round"/>
            <circle cx="${cx}" cy="${cy}" r="3" fill="${color}"/>`;
  }

  // ── Tacómetro de presión ───────────────────────────────────────────────────
  function renderPressure(el, p) {
    if (!el) return;
    const score = (p && p.score) || 0;
    const side  = (p && p.side)  || 'neutral';
    const color = side === 'fear' ? '#ef4444'
                : side === 'greed' ? '#22c55e'
                : '#94a3b8';
    const ang   = scoreToAngle(score);
    const cx = 60, cy = 56, r = 44;

    // 3 sectores: 0-33 verde tenue, 33-66 ámbar, 66-100 rojo
    const aFull   = Math.PI;
    const aMid    = Math.PI - (33/100) * Math.PI;
    const aHigh   = Math.PI - (66/100) * Math.PI;
    const lowArc  = arc(cx, cy, r, aFull, aMid);
    const midArc  = arc(cx, cy, r, aMid,  aHigh);
    const hiArc   = arc(cx, cy, r, aHigh, 0);

    el.innerHTML = `
      <svg viewBox="0 0 120 76" class="g-svg" preserveAspectRatio="xMidYMid meet">
        <path d="${lowArc}" stroke="#22c55e" stroke-width="6" fill="none" opacity=".35"/>
        <path d="${midArc}" stroke="#f59e0b" stroke-width="6" fill="none" opacity=".45"/>
        <path d="${hiArc}"  stroke="#ef4444" stroke-width="6" fill="none" opacity=".55"/>
        ${needle(cx, cy, r - 4, ang, color)}
        <text x="60" y="72" text-anchor="middle" class="g-num">${score.toFixed(0)}</text>
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
    const ang   = scoreToAngle(score);
    const color = score > 80 ? '#ef4444'
                : score > 50 ? '#f59e0b'
                : '#22d3ee';
    const cx = 60, cy = 56, r = 44;
    const baseArc = arc(cx, cy, r, Math.PI, 0);
    const fillEnd = Math.PI - (Math.min(100, score) / 100) * Math.PI;
    const fillArc = arc(cx, cy, r, Math.PI, fillEnd);

    el.innerHTML = `
      <svg viewBox="0 0 120 76" class="g-svg" preserveAspectRatio="xMidYMid meet">
        <path d="${baseArc}" stroke="#1e293b" stroke-width="6" fill="none"/>
        <path d="${fillArc}" stroke="${color}" stroke-width="6" fill="none" stroke-linecap="round"/>
        ${needle(cx, cy, r - 4, ang, color)}
        <text x="60" y="72" text-anchor="middle" class="g-num">${pct.toFixed(2)}<tspan class="g-unit">%/m</tspan></text>
      </svg>
      <div class="g-lbl">
        <span class="g-title">Velocidad</span>
        <span class="g-sub">ref ${ref.toFixed(2)}%</span>
      </div>`;
  }

  // ── Carretera ──────────────────────────────────────────────────────────────
  const ROAD_VIS = {
    highway:  { icon: '═══', color: '#22c55e', tone: 'Autopista' },
    curvy:    { icon: '∿∿∿', color: '#f59e0b', tone: 'Curvas'    },
    rough:    { icon: '≈≈≈', color: '#ef4444', tone: 'Terracería'},
    gridlock: { icon: '▪▪▪', color: '#64748b', tone: 'Atasco'    },
  };

  function renderRoad(el, r) {
    if (!el) return;
    const type = (r && r.type) || 'curvy';
    const vis  = ROAD_VIS[type] || ROAD_VIS.curvy;
    const lev  = (r && r.leverage_hint) || '—';
    const reg  = (r && r.regime_label)  || '?';
    const conf = (r && r.confidence)    || 0;

    el.innerHTML = `
      <div class="g-road g-road-${type}" style="--g-road-color:${vis.color}">
        <div class="g-road-icon">${vis.icon}</div>
        <div class="g-road-meta">
          <div class="g-road-tone">${vis.tone}</div>
          <div class="g-road-lev">leverage ${lev}</div>
          <div class="g-road-reg">${reg} · conf ${conf}</div>
        </div>
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
