/*
 * QtsGravityVertical — panel independiente del campo gravitacional (HTML+CSS).
 *
 * Versión vertical y expandida del strip horizontal de gravity.js: eje Y =
 * precio (top=vmax, bottom=vmin), eje X dividido en columnas:
 *
 *   ┌────────┬───────────────────┬──────────────┐
 *   │ axis-y │  chart (bids/asks │ labels-col   │
 *   │ ticks  │  vp/levels/liqs/  │ niveles +    │
 *   │ precio │  geom/current)    │ geom + my_ord│
 *   └────────┴───────────────────┴──────────────┘
 *
 * Render 100% HTML/CSS posicionado: cada elemento se ancla por `top: y%`,
 * alineando labels con líneas sin malabares de SVG. Sin canvas → texto crujiente
 * y selección/hover libres.
 *
 * Uso:
 *   QtsGravityVertical.render(panelEl, data, { geom, view })
 *
 * data = payload de /api/liquidity/{symbol}
 * view = { min, max } (alineado con el zoom de la progress bar)
 * geom = { sl, entry, be, tp, milestones, is_long } — opcional, dibuja líneas
 *        horizontales para los puntos críticos del trade.
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
  const ZONE_LABELS = {
    HVN:   'HVN',
    LVN:   'LVN',
    EQ_H:  'EQ↑',
    EQ_L:  'EQ↓',
    ROUND: '◯',
  };

  // y: 0% = top (vmax), 100% = bottom (vmin)
  function priceToY(price, vmin, vmax) {
    if (vmax <= vmin) return 50;
    return (1 - (price - vmin) / (vmax - vmin)) * 100;
  }

  function fmtPrice(p) {
    if (!isFinite(p) || p == null) return '—';
    if (p >= 1000)  return p.toLocaleString('en-US', { maximumFractionDigits: 1 });
    if (p >= 1)     return p.toFixed(3);
    if (p >= 0.01)  return p.toFixed(4);
    return p.toFixed(6);
  }

  function fmtCompact(n) {
    if (!isFinite(n) || n == null) return '—';
    const a = Math.abs(n);
    if (a >= 1e9) return (n/1e9).toFixed(2) + 'B';
    if (a >= 1e6) return (n/1e6).toFixed(2) + 'M';
    if (a >= 1e3) return (n/1e3).toFixed(1) + 'k';
    if (a >= 1)   return n.toFixed(2);
    if (a >= 0.001) return n.toFixed(3);
    return n.toExponential(1);
  }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, ch => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[ch]));
  }

  // ── Capas ──────────────────────────────────────────────────────────────────

  function buildAxisY(vmin, vmax, current) {
    const steps = 8;
    const out = [];
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      const price = vmax - t * (vmax - vmin);
      const y = (t * 100).toFixed(2);
      const distNorm = Math.abs(price - (current || 0)) / (vmax - vmin);
      const isCur = current != null && distNorm < 0.5 / steps;
      out.push(`<div class="gvp-axis-tick${isCur ? ' is-current' : ''}" style="top:${y}%">${fmtPrice(price)}</div>`);
    }
    return out.join('');
  }

  function buildDepth(items, maxQty, vmin, vmax, side) {
    if (!items || !items.length || maxQty <= 0) return '';
    return items.map(b => {
      if (b.price < vmin || b.price > vmax) return '';
      const y  = priceToY(b.price, vmin, vmax).toFixed(2);
      const m  = b.qty / maxQty;
      const w  = Math.max(2, m * 100).toFixed(1);
      const op = (0.30 + 0.60 * Math.pow(m, 0.6)).toFixed(2);
      return `<div class="gvp-depth-bar gvp-${side}"
                style="top:${y}%;width:${w}%;opacity:${op}"
                title="${fmtPrice(b.price)} · ${fmtCompact(b.qty)}"></div>`;
    }).join('');
  }

  function buildVP(items, maxVol, vmin, vmax) {
    if (!items || !items.length || maxVol <= 0) return '';
    return items.map(v => {
      if (v.price < vmin || v.price > vmax) return '';
      const y = priceToY(v.price, vmin, vmax).toFixed(2);
      const w = Math.max(2, (v.vol / maxVol) * 100).toFixed(1);
      return `<div class="gvp-vp-bar"
                style="top:${y}%;width:${w}%"
                title="VP ${fmtPrice(v.price)} · vol ${fmtCompact(v.vol)}"></div>`;
    }).join('');
  }

  function buildLiqs(items, maxLiq, vmin, vmax) {
    if (!items || !items.length || maxLiq <= 0) return '';
    return items.map(lq => {
      if (lq.price < vmin || lq.price > vmax) return '';
      const y    = priceToY(lq.price, vmin, vmax).toFixed(2);
      const mass = Math.max(0.08, Math.sqrt(lq.notional / maxLiq));
      const sz   = Math.round(8 + mass * 18);
      const fade = Math.max(0.2, 1 - (lq.age_s || 0) / 120).toFixed(2);
      const cls  = lq.is_long_liq ? 'gvp-liq-long' : 'gvp-liq-short';
      const ago  = lq.age_s < 60 ? `${(lq.age_s|0)}s` : `${(lq.age_s/60).toFixed(1)}m`;
      const dir  = lq.is_long_liq ? 'Long liq' : 'Short liq';
      return `<div class="gvp-liq ${cls}"
                style="top:${y}%;width:${sz}px;height:${sz}px;opacity:${fade}"
                title="${dir} · $${fmtCompact(lq.notional)} · hace ${ago}"></div>`;
    }).join('');
  }

  function buildLevels(levels, vmin, vmax) {
    if (!levels || !levels.length) return { lines: '', labels: '' };
    const lines = [], labels = [];
    levels.forEach(lv => {
      if (lv.price < vmin || lv.price > vmax) return;
      const y    = priceToY(lv.price, vmin, vmax).toFixed(2);
      const col  = ZONE_COLORS[lv.type] || '#94a3b8';
      const str  = Math.min(100, lv.strength || 30);
      const op   = (0.35 + (str / 100) * 0.55).toFixed(2);
      const ldis = vmin < vmax ? Math.abs(lv.price - (vmin + (vmax - vmin) / 2)) : 0;
      lines.push(`<div class="gvp-level-line"
                    style="top:${y}%;border-top-color:${col};opacity:${op}"></div>`);
      const tip = `${lv.type} @ ${fmtPrice(lv.price)} · strength ${str|0}`
                + (lv.count ? ` · ${lv.count} touches` : '')
                + (lv.vol_pct ? ` · ${(lv.vol_pct*100).toFixed(1)}% del vol` : '');
      labels.push(`<div class="gvp-lvl-lbl" style="top:${y}%;--c:${col}" title="${esc(tip)}">
        <span class="gvp-lvl-tag">${esc(ZONE_LABELS[lv.type] || lv.type)}</span>
        <span class="gvp-lvl-px">${fmtPrice(lv.price)}</span>
        <span class="gvp-lvl-str">${str|0}</span>
      </div>`);
    });
    return { lines: lines.join(''), labels: labels.join('') };
  }

  function buildGeom(geom, vmin, vmax) {
    if (!geom) return { lines: '', labels: '' };
    const base = [
      { key: 'sl',    label: 'SL',    color: '#ef4444', price: geom.sl },
      { key: 'entry', label: 'E',     color: '#94a3b8', price: geom.entry },
      { key: 'be',    label: 'BE',    color: '#f59e0b', price: geom.be },
      { key: 'tp',    label: 'TP',    color: '#22c55e', price: geom.tp },
    ];
    (geom.milestones || []).forEach(m => {
      base.push({ key: `m${m.pct}`, label: `${m.pct}%`, color: '#64748b', price: m.price });
    });
    const lines = [], labels = [];
    base.forEach(it => {
      if (!it.price || it.price < vmin || it.price > vmax) return;
      const y = priceToY(it.price, vmin, vmax).toFixed(2);
      lines.push(`<div class="gvp-geom-line gvp-geom-${esc(it.key)}"
                    style="top:${y}%;border-top-color:${it.color}"></div>`);
      labels.push(`<div class="gvp-geom-lbl" style="top:${y}%;--c:${it.color}"
                    title="${esc(it.label)}: ${fmtPrice(it.price)}">
        <span class="gvp-geom-tag">${esc(it.label)}</span>
        <span class="gvp-geom-px">${fmtPrice(it.price)}</span>
      </div>`);
    });
    return { lines: lines.join(''), labels: labels.join('') };
  }

  function buildOrders(orders, vmin, vmax) {
    if (!orders || !orders.length) return '';
    return orders.map(o => {
      if (o.price < vmin || o.price > vmax) return '';
      const y    = priceToY(o.price, vmin, vmax).toFixed(2);
      const cls  = o.side === 'Sell' ? 'gvp-order-sell' : 'gvp-order-buy';
      const arr  = o.side === 'Sell' ? '▼' : '▲';
      const tip  = `${o.side} ${o.qty} @ ${fmtPrice(o.price)} (${o.status})`;
      return `<div class="gvp-order ${cls}" style="top:${y}%" title="${esc(tip)}">
        <span class="gvp-order-tag">${arr}</span>
        <span class="gvp-order-qty">${fmtCompact(o.qty)}</span>
      </div>`;
    }).join('');
  }

  function buildCurrent(current, vmin, vmax) {
    if (!current || current < vmin || current > vmax) return '';
    const y = priceToY(current, vmin, vmax).toFixed(2);
    return `
      <div class="gvp-current-line" style="top:${y}%"></div>
      <div class="gvp-current-lbl"  style="top:${y}%">${fmtPrice(current)}</div>`;
  }

  // ── Stats footer ───────────────────────────────────────────────────────────

  function buildStats(data) {
    const totalBids = (data.bids || []).reduce((s, b) => s + b.qty, 0);
    const totalAsks = (data.asks || []).reduce((s, a) => s + a.qty, 0);
    const denom     = totalBids + totalAsks;
    const imb       = denom > 0 ? (totalBids - totalAsks) / denom * 100 : 0;
    const liqs      = data.liqs || [];
    const longLiq   = liqs.filter(l =>  l.is_long_liq).reduce((s, l) => s + l.notional, 0);
    const shortLiq  = liqs.filter(l => !l.is_long_liq).reduce((s, l) => s + l.notional, 0);
    const totalVp   = (data.vp || []).reduce((s, v) => s + v.vol, 0);
    const nLv       = (data.levels || []).length;

    const imbCls = imb >  10 ? 'c-green'
                 : imb < -10 ? 'c-red'
                 :             '';
    const imbStr = `${imb >= 0 ? '+' : ''}${imb.toFixed(0)}%`;

    return `
      <div class="gvp-stat"><span class="gvp-stat-lbl">BIDS</span>
        <span class="gvp-stat-v c-green">${fmtCompact(totalBids)}</span></div>
      <div class="gvp-stat"><span class="gvp-stat-lbl">ASKS</span>
        <span class="gvp-stat-v c-red">${fmtCompact(totalAsks)}</span></div>
      <div class="gvp-stat"><span class="gvp-stat-lbl">IMBALANCE</span>
        <span class="gvp-stat-v ${imbCls}">${imbStr}</span></div>
      <div class="gvp-stat"><span class="gvp-stat-lbl">VP ACUM</span>
        <span class="gvp-stat-v">${fmtCompact(totalVp)}</span></div>
      <div class="gvp-stat"><span class="gvp-stat-lbl">LIQS · L</span>
        <span class="gvp-stat-v c-red">$${fmtCompact(longLiq)}</span></div>
      <div class="gvp-stat"><span class="gvp-stat-lbl">LIQS · S</span>
        <span class="gvp-stat-v c-green">$${fmtCompact(shortLiq)}</span></div>
      <div class="gvp-stat"><span class="gvp-stat-lbl">NIVELES</span>
        <span class="gvp-stat-v">${nLv}</span></div>
      <div class="gvp-stat"><span class="gvp-stat-lbl">BUCKET</span>
        <span class="gvp-stat-v">${fmtPrice(data.bucket || 0)}</span></div>`;
  }

  // ── API pública ────────────────────────────────────────────────────────────

  function render(panelEl, data, opts) {
    if (!panelEl || !data) return;
    const view = (opts && opts.view) || { min: data.view_min, max: data.view_max };
    const geom = opts && opts.geom;
    const vmin = view.min, vmax = view.max;
    if (!(vmax > vmin)) return;

    const bodyEl  = panelEl.querySelector('.gvp-body');
    const statsEl = panelEl.querySelector('.gvp-stats');
    const symEl   = panelEl.querySelector('[data-gvp-sym]');
    const curEl   = panelEl.querySelector('[data-gvp-current]');
    const rangeEl = panelEl.querySelector('[data-gvp-range]');

    if (symEl)   symEl.textContent   = data.symbol;
    if (curEl)   curEl.textContent   = fmtPrice(data.current);
    if (rangeEl) rangeEl.textContent = `${fmtPrice(vmin)} … ${fmtPrice(vmax)}`;
    if (!bodyEl) return;

    const vp     = buildVP(data.vp, data.max_vol, vmin, vmax);
    const bids   = buildDepth(data.bids, data.max_qty, vmin, vmax, 'bid');
    const asks   = buildDepth(data.asks, data.max_qty, vmin, vmax, 'ask');
    const liqs   = buildLiqs(data.liqs, data.max_liq, vmin, vmax);
    const lv     = buildLevels(data.levels, vmin, vmax);
    const gx     = buildGeom(geom, vmin, vmax);
    const orders = buildOrders(data.my_orders, vmin, vmax);
    const cur    = buildCurrent(data.current, vmin, vmax);
    const axisY  = buildAxisY(vmin, vmax, data.current);

    bodyEl.innerHTML = `
      <div class="gvp-axis-y">${axisY}</div>
      <div class="gvp-chart">
        <div class="gvp-vp-bg">${vp}</div>
        <div class="gvp-bids-col">${bids}</div>
        <div class="gvp-asks-col">${asks}</div>
        <div class="gvp-overlay">
          ${gx.lines}
          ${lv.lines}
          ${liqs}
          ${cur}
        </div>
      </div>
      <div class="gvp-labels-col">
        ${gx.labels}
        ${lv.labels}
        ${orders}
      </div>`;

    if (statsEl) statsEl.innerHTML = buildStats(data);
  }

  window.QtsGravityVertical = { render, priceToY };
})();
