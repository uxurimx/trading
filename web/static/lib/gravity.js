/*
 * QtsGravity — campo gravitacional de liquidez (canvas 2D).
 *
 * Renderiza un strip horizontal donde el eje X es precio (compartido con la
 * progress bar de la posición). Cada masa de liquidez curva el espacio:
 *   · bids/asks    → barras verticales (sell arriba, buy abajo)
 *   · vp           → halo de fondo (volumen total negociado)
 *   · HVN/LVN/EQ   → tics horizontales con etiqueta
 *   · liquidaciones→ puntos con glow que se disipan con la edad
 *   · my_orders    → pines con triángulo según el side
 *   · current      → línea vertical central con flecha
 *
 * Uso:
 *   QtsGravity.render(canvas, data, { vmin, vmax }, opts)
 *
 * data = payload de /api/liquidity/{symbol}.
 * vmin/vmax pueden venir del zoom de la progress bar para alinear ejes.
 */
(function () {
  'use strict';

  const ZONE_COLORS = {
    HVN:   '#22d3ee',
    LVN:   '#f59e0b',
    EQ_H:  '#ef4444',
    EQ_L:  '#22c55e',
    ROUND: '#a855f7',
  };

  function priceToX(price, vmin, vmax, w) {
    if (vmax <= vmin) return 0;
    const t = (price - vmin) / (vmax - vmin);
    return Math.max(-2, Math.min(w + 2, t * w));
  }

  function dprScale(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width  = w * dpr;
      canvas.height = h * dpr;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  // ── Capas de dibujo ────────────────────────────────────────────────────────

  function drawVP(ctx, vp, maxVol, vmin, vmax, w, h) {
    if (!vp || !vp.length || maxVol <= 0) return;
    const baseY = h * 0.5;
    const maxH  = h * 0.85;
    ctx.save();
    ctx.globalAlpha = 0.18;
    ctx.fillStyle = '#64748b';
    for (const b of vp) {
      const x = priceToX(b.price, vmin, vmax, w);
      const m = b.vol / maxVol;
      const bh = m * maxH;
      ctx.fillRect(x - 1, baseY - bh / 2, 2, bh);
    }
    ctx.restore();
  }

  function drawDepth(ctx, items, maxQty, vmin, vmax, w, h, opts) {
    if (!items || !items.length || maxQty <= 0) return;
    const { color, side } = opts;
    const baseY = h * 0.5;
    const maxH  = h * 0.45;
    ctx.save();
    for (const b of items) {
      const x = priceToX(b.price, vmin, vmax, w);
      const m = Math.pow(b.qty / maxQty, 0.6);
      const bh = m * maxH;
      ctx.globalAlpha = 0.25 + 0.55 * m;
      ctx.fillStyle = color;
      if (side === 'ask') {
        ctx.fillRect(x - 1, baseY - bh, 2, bh);
      } else {
        ctx.fillRect(x - 1, baseY, 2, bh);
      }
    }
    ctx.restore();
  }

  function drawLevels(ctx, levels, vmin, vmax, w, h) {
    if (!levels || !levels.length) return;
    const baseY = h * 0.5;
    ctx.save();
    ctx.lineWidth = 1;
    ctx.font = '9px ui-monospace, Menlo, monospace';
    for (const lv of levels) {
      const x = priceToX(lv.price, vmin, vmax, w);
      const color = ZONE_COLORS[lv.type] || '#94a3b8';
      const strength = Math.min(100, lv.strength || 30);
      ctx.globalAlpha = 0.35 + (strength / 100) * 0.55;
      ctx.strokeStyle = color;
      ctx.beginPath();
      ctx.moveTo(x, 2);
      ctx.lineTo(x, h - 2);
      ctx.stroke();
      // tick superior con etiqueta
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.9;
      ctx.fillText(lv.type, x + 2, 10);
    }
    ctx.restore();
  }

  function drawLiqs(ctx, liqs, maxLiq, vmin, vmax, w, h) {
    if (!liqs || !liqs.length || maxLiq <= 0) return;
    const baseY = h * 0.5;
    ctx.save();
    for (const lq of liqs) {
      const x = priceToX(lq.price, vmin, vmax, w);
      const mass = lq.notional / maxLiq;
      // edad → fade
      const age = lq.age_s || 0;
      const fade = Math.max(0.15, 1 - age / 120);
      const r = 2 + 8 * Math.pow(mass, 0.5);
      const color = lq.is_long_liq ? '#ef4444' : '#22c55e';
      ctx.globalAlpha = 0.85 * fade;
      const grad = ctx.createRadialGradient(x, baseY, 0, x, baseY, r * 2);
      grad.addColorStop(0, color);
      grad.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(x, baseY, r * 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = fade;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, baseY, r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  function drawMyOrders(ctx, orders, vmin, vmax, w, h) {
    if (!orders || !orders.length) return;
    ctx.save();
    ctx.font = '9px ui-monospace, Menlo, monospace';
    for (const od of orders) {
      const x = priceToX(od.price, vmin, vmax, w);
      const up = od.side === 'Sell';
      const color = up ? '#ef4444' : '#22c55e';
      ctx.globalAlpha = 0.95;
      ctx.fillStyle = color;
      ctx.beginPath();
      if (up) {
        ctx.moveTo(x, 2);
        ctx.lineTo(x - 4, 10);
        ctx.lineTo(x + 4, 10);
      } else {
        ctx.moveTo(x, h - 2);
        ctx.lineTo(x - 4, h - 10);
        ctx.lineTo(x + 4, h - 10);
      }
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }

  function drawCurrent(ctx, current, vmin, vmax, w, h) {
    if (!current) return;
    const x = priceToX(current, vmin, vmax, w);
    ctx.save();
    // glow exterior
    ctx.strokeStyle = 'rgba(34,211,238,0.35)';
    ctx.lineWidth = 4;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
    // línea cyan sólida central
    ctx.strokeStyle = '#22d3ee';
    ctx.lineWidth = 1.6;
    ctx.globalAlpha = 1;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
    // marcador rombo en el centro
    ctx.fillStyle = '#22d3ee';
    ctx.beginPath();
    ctx.moveTo(x, h * 0.5 - 4);
    ctx.lineTo(x + 4, h * 0.5);
    ctx.lineTo(x, h * 0.5 + 4);
    ctx.lineTo(x - 4, h * 0.5);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = '#0c1220';
    ctx.lineWidth = 0.8;
    ctx.stroke();
    ctx.restore();
  }

  // ── Rulers: ticks de precio con anti-colisión por prioridad ────────────────

  function fmtPriceLbl(p) {
    if (!isFinite(p) || p == null) return '—';
    if (p >= 1000)  return p.toLocaleString('en-US', { maximumFractionDigits: 0 });
    if (p >= 100)   return p.toFixed(1);
    if (p >= 1)     return p.toFixed(3);
    if (p >= 0.01)  return p.toFixed(4);
    return p.toFixed(6);
  }

  function drawRulers(ctx, current, geom, vmin, vmax, w, h) {
    // Recolecta candidatos con prioridad. Mayor prio = sobrevive a la colisión.
    const candidates = [];

    // Bordes — siempre visibles
    candidates.push({ price: vmin, label: fmtPriceLbl(vmin), color: '#64748b', prio: 50, align: 'left'   });
    candidates.push({ price: vmax, label: fmtPriceLbl(vmax), color: '#64748b', prio: 50, align: 'right'  });

    if (current != null && current >= vmin && current <= vmax) {
      candidates.push({ price: current, label: fmtPriceLbl(current), color: '#e5e7eb', prio: 100, bold: true });
    }

    // Ticks intermedios: 5 marcas a 1/6, 2/6, 3/6, 4/6, 5/6 del rango.
    // Prio baja (20): cualquier label de geom las desplaza si chocan.
    const N_TICKS = 5;
    for (let i = 1; i <= N_TICKS; i++) {
      const p = vmin + (vmax - vmin) * (i / (N_TICKS + 1));
      candidates.push({ price: p, label: fmtPriceLbl(p), color: '#475569', prio: 20 });
    }

    if (geom) {
      const items = [
        { p: geom.sl,    lbl: 'SL',    color: '#ef4444', prio: 90 },
        { p: geom.tp,    lbl: 'TP',    color: '#22c55e', prio: 90 },
        { p: geom.entry, lbl: 'E',     color: '#94a3b8', prio: 70 },
        { p: geom.be,    lbl: 'BE',    color: '#f59e0b', prio: 80 },
      ];
      items.forEach(it => {
        if (it.p && it.p >= vmin && it.p <= vmax) {
          candidates.push({ price: it.p, label: `${it.lbl} ${fmtPriceLbl(it.p)}`, color: it.color, prio: it.prio });
        }
      });
      (geom.milestones || []).forEach(m => {
        if (m.price && m.price >= vmin && m.price <= vmax) {
          candidates.push({ price: m.price, label: `${m.pct}%`, color: '#64748b', prio: 30 });
        }
      });
    }

    if (!candidates.length) return;

    // Pintar primero a un canvas off para medir y resolver colisiones.
    ctx.save();
    ctx.font = '8px ui-monospace, Menlo, monospace';

    const measured = candidates.map(c => {
      const x = priceToX(c.price, vmin, vmax, w);
      const tw = ctx.measureText(c.label).width + 4;   // padding
      // borde-anchored: left-align en x_min, right en x_max
      let x0;
      if (c.align === 'left')       x0 = 1;
      else if (c.align === 'right') x0 = w - tw - 1;
      else                          x0 = Math.max(1, Math.min(w - tw - 1, x - tw / 2));
      const x1 = x0 + tw;
      return { ...c, x, x0, x1, tw };
    });

    // Resolver colisiones: ordenar por prio desc, mantener si no choca con uno ya aceptado.
    measured.sort((a, b) => b.prio - a.prio);
    const accepted = [];
    const PADDING_PX = 3;
    for (const m of measured) {
      const hits = accepted.some(a => !(m.x1 + PADDING_PX < a.x0 || m.x0 > a.x1 + PADDING_PX));
      if (!hits) accepted.push(m);
    }

    // Pintar fondo + texto en la franja inferior.
    const RULER_H = 11;
    const y0 = h - RULER_H;
    for (const m of accepted) {
      // tick line corto sobre la franja
      ctx.globalAlpha = 0.6;
      ctx.strokeStyle = m.color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(m.x, y0);
      ctx.lineTo(m.x, y0 + 3);
      ctx.stroke();

      // fondo del label (mejora legibilidad sobre las barras)
      ctx.globalAlpha = 0.78;
      ctx.fillStyle = '#0c1220';
      ctx.fillRect(m.x0, y0 + 2, m.tw, RULER_H - 2);

      // texto
      ctx.globalAlpha = 1;
      ctx.fillStyle = m.color;
      if (m.bold) ctx.font = 'bold 8px ui-monospace, Menlo, monospace';
      else        ctx.font = '8px ui-monospace, Menlo, monospace';
      ctx.textBaseline = 'middle';
      ctx.fillText(m.label, m.x0 + 2, y0 + RULER_H / 2 + 1);
    }
    ctx.restore();
  }

  // ── Heat: memoria reciente del precio (histograma por bucket) ─────────────

  function drawHeat(ctx, samples, vmin, vmax, w, h, opacityMult) {
    if (!samples || samples.length < 4) return;
    const mult = (opacityMult != null && opacityMult > 0) ? opacityMult : 0.5;
    const now = Date.now();
    const MAX_AGE_MS = 6 * 60 * 1000;
    const BUCKETS = 60;
    const bucketSize = (vmax - vmin) / BUCKETS;
    if (bucketSize <= 0) return;
    const weights = new Array(BUCKETS).fill(0);
    let maxW = 0;
    for (const s of samples) {
      if (s.price < vmin || s.price > vmax) continue;
      const age = (now - s.ts) / MAX_AGE_MS;
      if (age < 0 || age > 1) continue;
      const wgt = Math.exp(-2.5 * age);    // decae con edad
      const i = Math.min(BUCKETS - 1, Math.floor((s.price - vmin) / bucketSize));
      weights[i] += wgt;
      if (weights[i] > maxW) maxW = weights[i];
    }
    if (maxW <= 0) return;
    ctx.save();
    for (let i = 0; i < BUCKETS; i++) {
      const wt = weights[i] / maxW;
      if (wt < 0.06) continue;
      const x0 = (i / BUCKETS) * w;
      const bw = w / BUCKETS;
      const alpha = (0.08 + 0.22 * wt) * mult;
      ctx.fillStyle = `rgba(251,146,60,${alpha.toFixed(3)})`;   // naranja
      ctx.fillRect(x0, 0, bw + 0.5, h);
    }
    ctx.restore();
  }

  // ── Trails: trayectoria reciente de niveles estructurales ─────────────────

  function drawTrails(ctx, trails, vmin, vmax, w, h) {
    if (!trails || !trails.length) return;
    const now = Date.now();
    const MAX_AGE_MS = 5 * 60 * 1000;          // ventana de visibilidad
    const yBase = 3;                            // banda superior
    const yMaxOff = Math.min(8, h * 0.18);      // espesor del rastro
    ctx.save();
    ctx.lineWidth = 1;
    for (const tr of trails) {
      if (!tr.points || tr.points.length < 2) continue;
      const color = ZONE_COLORS[tr.type] || '#94a3b8';
      ctx.strokeStyle = color;
      // línea uniendo puntos consecutivos
      for (let i = 1; i < tr.points.length; i++) {
        const p0 = tr.points[i - 1];
        const p1 = tr.points[i];
        const age = Math.max(0, (now - p1.ts) / MAX_AGE_MS);
        if (age > 1) continue;
        const alpha = Math.max(0.06, 0.5 * (1 - age));
        const y = yBase + age * yMaxOff;
        ctx.globalAlpha = alpha;
        const x0 = priceToX(p0.price, vmin, vmax, w);
        const x1 = priceToX(p1.price, vmin, vmax, w);
        ctx.beginPath();
        ctx.moveTo(x0, y);
        ctx.lineTo(x1, y);
        ctx.stroke();
      }
      // dot final (más reciente) — anclado al punto actual del track
      const last = tr.points[tr.points.length - 1];
      const ageLast = Math.max(0, (now - last.ts) / MAX_AGE_MS);
      if (ageLast <= 1) {
        ctx.globalAlpha = 0.85;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(priceToX(last.price, vmin, vmax, w), yBase, 1.6, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.restore();
  }

  // ── Energía: glow alrededor de niveles + flechas de dirección ─────────────

  function drawEnergy(ctx, energy, vmin, vmax, w, h) {
    if (!energy || !energy.levels || !energy.levels.length) return;
    ctx.save();
    for (const e of energy.levels) {
      if (e.energy <= 5) continue;
      const x = priceToX(e.price, vmin, vmax, w);
      const intensity = Math.min(1, e.energy / 100);
      const radius = 6 + intensity * 18;
      const color = e.dir === 'up'   ? '34,197,94'
                  : e.dir === 'down' ? '239,68,68'
                  : '148,163,184';
      // halo radial centrado vertical
      const grad = ctx.createRadialGradient(x, h * 0.5, 1, x, h * 0.5, radius);
      grad.addColorStop(0,   `rgba(${color},${0.35 * intensity})`);
      grad.addColorStop(0.5, `rgba(${color},${0.18 * intensity})`);
      grad.addColorStop(1,   `rgba(${color},0)`);
      ctx.fillStyle = grad;
      ctx.fillRect(x - radius, 0, radius * 2, h);
      // ⚡ direccional cuando energy ≥ 40
      if (e.energy >= 40 && e.dir !== 'flat') {
        const ay = e.dir === 'up' ? h * 0.18 : h * 0.82;
        ctx.globalAlpha = 0.85;
        ctx.fillStyle = `rgba(${color},1)`;
        ctx.font = 'bold 9px ui-monospace, Menlo, monospace';
        ctx.textBaseline = 'middle';
        ctx.textAlign = 'center';
        ctx.fillText(e.dir === 'up' ? '↑' : '↓', x, ay);
        ctx.globalAlpha = 1;
      }
    }
    ctx.restore();
  }

  // ── API pública ────────────────────────────────────────────────────────────

  function render(canvas, data, viewport, opts) {
    if (!canvas || !data) return;
    const { ctx, w, h } = dprScale(canvas);
    const vmin = (viewport && viewport.vmin) || data.view_min;
    const vmax = (viewport && viewport.vmax) || data.view_max;
    if (!(vmax > vmin)) return;

    // Reservar franja inferior para rulers si están activos
    const showRulers = !(opts && opts.rulers === false);
    const chartH = showRulers ? Math.max(8, h - 11) : h;

    drawHeat(ctx, opts && opts.heat, vmin, vmax, w, chartH, opts && opts.heatOpacity);
    drawVP(ctx, data.vp, data.max_vol, vmin, vmax, w, chartH);
    drawDepth(ctx, data.bids, data.max_qty, vmin, vmax, w, chartH,
      { color: '#22c55e', side: 'bid' });
    drawDepth(ctx, data.asks, data.max_qty, vmin, vmax, w, chartH,
      { color: '#ef4444', side: 'ask' });
    drawTrails(ctx, opts && opts.trails, vmin, vmax, w, chartH);
    drawEnergy(ctx, opts && opts.energy, vmin, vmax, w, chartH);
    drawLevels(ctx, data.levels, vmin, vmax, w, chartH);
    drawLiqs(ctx, data.liqs, data.max_liq, vmin, vmax, w, chartH);
    drawMyOrders(ctx, data.my_orders, vmin, vmax, w, chartH);
    drawCurrent(ctx, data.current, vmin, vmax, w, chartH);
    if (showRulers) {
      drawRulers(ctx, data.current, opts && opts.geom, vmin, vmax, w, h);
    }
  }

  window.QtsGravity = { render, priceToX };
})();
