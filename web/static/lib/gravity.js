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
    ctx.strokeStyle = '#e5e7eb';
    ctx.globalAlpha = 0.9;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
    ctx.restore();
  }

  // ── API pública ────────────────────────────────────────────────────────────

  function render(canvas, data, viewport, opts) {
    if (!canvas || !data) return;
    const { ctx, w, h } = dprScale(canvas);
    const vmin = (viewport && viewport.vmin) || data.view_min;
    const vmax = (viewport && viewport.vmax) || data.view_max;
    if (!(vmax > vmin)) return;

    drawVP(ctx, data.vp, data.max_vol, vmin, vmax, w, h);
    drawDepth(ctx, data.bids, data.max_qty, vmin, vmax, w, h,
      { color: '#22c55e', side: 'bid' });
    drawDepth(ctx, data.asks, data.max_qty, vmin, vmax, w, h,
      { color: '#ef4444', side: 'ask' });
    drawLevels(ctx, data.levels, vmin, vmax, w, h);
    drawLiqs(ctx, data.liqs, data.max_liq, vmin, vmax, w, h);
    drawMyOrders(ctx, data.my_orders, vmin, vmax, w, h);
    drawCurrent(ctx, data.current, vmin, vmax, w, h);
  }

  window.QtsGravity = { render, priceToX };
})();
