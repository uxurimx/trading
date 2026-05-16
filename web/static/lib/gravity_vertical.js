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

  function _touchesByPriceType(trails) {
    const m = new Map();
    if (!trails) return m;
    trails.forEach(tr => {
      if (!tr.points || !tr.points.length) return;
      const last = tr.points[tr.points.length - 1];
      m.set(`${tr.type}:${last.price.toFixed(8)}`, tr.touches || 0);
    });
    return m;
  }

  function buildLevels(levels, vmin, vmax, trails) {
    if (!levels || !levels.length) return { lines: '', labels: '' };
    const touchMap = _touchesByPriceType(trails);
    const lines = [], labels = [];
    const span = vmax - vmin;
    const mid  = (vmin + vmax) / 2;
    const zoomPct = mid > 0 ? span / mid : 0;
    const compact = zoomPct > 0.04;
    const clusterBand = span * 0.018;
    // Filtrar al viewport una vez
    const visible = levels.filter(lv => lv.price >= vmin && lv.price <= vmax);

    // Líneas siempre se dibujan, una por nivel
    visible.forEach(lv => {
      const y    = priceToY(lv.price, vmin, vmax).toFixed(2);
      const col  = ZONE_COLORS[lv.type] || '#94a3b8';
      const str  = Math.min(100, lv.strength || 30);
      const op   = (0.35 + (str / 100) * 0.55).toFixed(2);
      lines.push(`<div class="gvp-level-line"
                    style="top:${y}%;border-top-color:${col};opacity:${op}"></div>`);
    });

    const touchesFor = (lv) => {
      let touches = 0;
      touchMap.forEach((cnt, key) => {
        const [ttype, tpx] = key.split(':');
        if (ttype !== lv.type) return;
        const tp = parseFloat(tpx);
        if (!(tp > 0)) return;
        if (Math.abs(tp - lv.price) / lv.price < 0.0015) {
          touches = Math.max(touches, cnt);
        }
      });
      return touches;
    };

    if (compact && visible.length > 6) {
      // Cluster nearby levels en bandas de ~1.8% del span
      const sorted = visible.slice().sort((a, b) => a.price - b.price);
      const clusters = [];
      sorted.forEach(lv => {
        const last = clusters[clusters.length - 1];
        if (last && Math.abs(lv.price - last.center) <= clusterBand) {
          last.items.push(lv);
          last.center = last.items.reduce((s, x) => s + x.price, 0) / last.items.length;
        } else {
          clusters.push({ center: lv.price, items: [lv] });
        }
      });
      clusters.forEach(cl => {
        if (cl.items.length === 1) {
          const lv = cl.items[0];
          const y    = priceToY(lv.price, vmin, vmax).toFixed(2);
          const col  = ZONE_COLORS[lv.type] || '#94a3b8';
          const str  = Math.min(100, lv.strength || 30);
          const touches = touchesFor(lv);
          const touchBadge = touches > 0
            ? `<span class="gvp-lvl-touch">×${touches}</span>` : '';
          labels.push(`<div class="gvp-lvl-lbl" style="top:${y}%;--c:${col}"
                        title="${esc(lv.type)} @ ${fmtPrice(lv.price)}">
            <span class="gvp-lvl-tag">${esc(ZONE_LABELS[lv.type] || lv.type)}</span>
            <span class="gvp-lvl-px">${fmtPrice(lv.price)}</span>
            <span class="gvp-lvl-str">${str|0}</span>
            ${touchBadge}
          </div>`);
        } else {
          // Meta-icon: dominante por strength
          const dom = cl.items.reduce((a, b) =>
            (a.strength || 0) >= (b.strength || 0) ? a : b);
          const col  = ZONE_COLORS[dom.type] || '#94a3b8';
          const y    = priceToY(cl.center, vmin, vmax).toFixed(2);
          const totalStrength = cl.items.reduce((s, x) => s + (x.strength || 0), 0) | 0;
          const lo = Math.min(...cl.items.map(x => x.price));
          const hi = Math.max(...cl.items.map(x => x.price));
          const totalTouches = cl.items.reduce((s, x) => s + touchesFor(x), 0);
          const tip = `${cl.items.length} niveles · ${fmtPrice(lo)} … ${fmtPrice(hi)} · `
                    + `dominante ${dom.type} (s${dom.strength|0})`;
          const touchBadge = totalTouches > 0
            ? `<span class="gvp-lvl-touch">×${totalTouches}</span>` : '';
          labels.push(`<div class="gvp-lvl-lbl gvp-lvl-cluster" style="top:${y}%;--c:${col}"
                        title="${esc(tip)}">
            <span class="gvp-lvl-tag">⬢${cl.items.length}</span>
            <span class="gvp-lvl-px">${fmtPrice(cl.center)}</span>
            <span class="gvp-lvl-str">${totalStrength}</span>
            ${touchBadge}
          </div>`);
        }
      });
    } else {
      visible.forEach(lv => {
        const y    = priceToY(lv.price, vmin, vmax).toFixed(2);
        const col  = ZONE_COLORS[lv.type] || '#94a3b8';
        const str  = Math.min(100, lv.strength || 30);
        const touches = touchesFor(lv);
        const tip = `${lv.type} @ ${fmtPrice(lv.price)} · strength ${str|0}`
                  + (touches ? ` · ${touches} touches` : '')
                  + (lv.count ? ` · cluster ${lv.count}` : '')
                  + (lv.vol_pct ? ` · ${(lv.vol_pct*100).toFixed(1)}% del vol` : '');
        const touchBadge = touches > 0
          ? `<span class="gvp-lvl-touch" title="${touches} toques recientes">×${touches}</span>`
          : '';
        labels.push(`<div class="gvp-lvl-lbl" style="top:${y}%;--c:${col}" title="${esc(tip)}">
          <span class="gvp-lvl-tag">${esc(ZONE_LABELS[lv.type] || lv.type)}</span>
          <span class="gvp-lvl-px">${fmtPrice(lv.price)}</span>
          <span class="gvp-lvl-str">${str|0}</span>
          ${touchBadge}
        </div>`);
      });
    }

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

  function buildTrails(trails, vmin, vmax) {
    if (!trails || !trails.length) return '';
    const now = Date.now();
    const MAX_AGE_MS = 5 * 60 * 1000;
    const out = [];
    trails.forEach(tr => {
      if (!tr.points || tr.points.length < 2) return;
      const col = ZONE_COLORS[tr.type] || '#94a3b8';
      for (let i = 1; i < tr.points.length; i++) {
        const p0 = tr.points[i - 1];
        const p1 = tr.points[i];
        if (p0.price < vmin || p0.price > vmax) continue;
        if (p1.price < vmin || p1.price > vmax) continue;
        const age = Math.max(0, (now - p1.ts) / MAX_AGE_MS);
        if (age > 1) continue;
        const y0 = priceToY(p0.price, vmin, vmax);
        const y1 = priceToY(p1.price, vmin, vmax);
        const top    = Math.min(y0, y1).toFixed(2);
        const height = Math.max(0.1, Math.abs(y1 - y0)).toFixed(2);
        const alpha  = Math.max(0.06, 0.4 * (1 - age)).toFixed(2);
        out.push(`<div class="gvp-trail" style="top:${top}%;height:${height}%;background:${col};opacity:${alpha}"></div>`);
      }
      const last = tr.points[tr.points.length - 1];
      if (last.price >= vmin && last.price <= vmax) {
        const yL = priceToY(last.price, vmin, vmax).toFixed(2);
        out.push(`<div class="gvp-trail-dot" style="top:${yL}%;background:${col}" title="${esc(tr.type)} track"></div>`);
      }
    });
    return out.join('');
  }

  function buildHeat(samples, vmin, vmax, opacityMult) {
    if (!samples || samples.length < 4) return '';
    const mult = (opacityMult != null && opacityMult > 0) ? opacityMult : 0.5;
    const now = Date.now();
    const MAX_AGE_MS = 6 * 60 * 1000;
    const BUCKETS = 50;
    const bucketSize = (vmax - vmin) / BUCKETS;
    if (bucketSize <= 0) return '';
    const weights = new Array(BUCKETS).fill(0);
    let maxW = 0;
    for (const s of samples) {
      if (s.price < vmin || s.price > vmax) continue;
      const age = (now - s.ts) / MAX_AGE_MS;
      if (age < 0 || age > 1) continue;
      const wgt = Math.exp(-2.5 * age);
      const i = Math.min(BUCKETS - 1, Math.floor((s.price - vmin) / bucketSize));
      weights[i] += wgt;
      if (weights[i] > maxW) maxW = weights[i];
    }
    if (maxW <= 0) return '';
    const out = [];
    const slice = 100 / BUCKETS;
    for (let i = 0; i < BUCKETS; i++) {
      const wt = weights[i] / maxW;
      if (wt < 0.06) continue;
      const priceMid = vmin + (i + 0.5) * bucketSize;
      const y = priceToY(priceMid, vmin, vmax).toFixed(2);
      const alpha = ((0.08 + 0.28 * wt) * mult).toFixed(3);
      out.push(`<div class="gvp-heat-cell"
        style="top:${y}%;height:${slice}%;margin-top:-${slice/2}%;
               background:rgba(251,146,60,${alpha})"></div>`);
    }
    return out.join('');
  }

  function buildEnergy(energy, vmin, vmax) {
    if (!energy || !energy.levels || !energy.levels.length) return { halos: '', arrows: '' };
    const halos = [], arrows = [];
    energy.levels.forEach(e => {
      if (e.energy <= 5) return;
      if (e.price < vmin || e.price > vmax) return;
      const y = priceToY(e.price, vmin, vmax);
      const intensity = Math.min(1, e.energy / 100);
      const col = e.dir === 'up'   ? '34,197,94'
                : e.dir === 'down' ? '239,68,68'
                : '148,163,184';
      const half = (3 + 18 * intensity).toFixed(2);
      const alpha = (0.18 + 0.42 * intensity).toFixed(2);
      halos.push(`<div class="gvp-energy-halo"
        style="top:${y}%;height:${half * 2}%;margin-top:-${half}%;
               background:radial-gradient(ellipse at center,rgba(${col},${alpha}) 0%,rgba(${col},0) 70%)"></div>`);
      if (e.energy >= 40 && e.dir !== 'flat') {
        const arr = e.dir === 'up' ? '⚡↑' : '⚡↓';
        arrows.push(`<div class="gvp-energy-arrow gvp-energy-${e.dir}"
          style="top:${y}%;color:rgb(${col})"
          title="energy ${e.energy} · ${e.dir}">${arr}</div>`);
      }
    });
    return { halos: halos.join(''), arrows: arrows.join('') };
  }

  function buildCurrent(current, vmin, vmax) {
    if (!current || current < vmin || current > vmax) return '';
    const y = priceToY(current, vmin, vmax).toFixed(2);
    return `
      <div class="gvp-current-glow" style="top:${y}%"></div>
      <div class="gvp-current-line" style="top:${y}%"></div>
      <div class="gvp-current-diamond" style="top:${y}%"></div>
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

  function buildAnomalies(data, energy) {
    const tags = [];
    if (energy && Array.isArray(energy.levels)) {
      const spikes = energy.levels
        .filter(e => e.energy >= 70)
        .sort((a, b) => b.energy - a.energy)
        .slice(0, 3);
      spikes.forEach(e => {
        const arr = e.dir === 'up' ? '↑' : e.dir === 'down' ? '↓' : '·';
        tags.push(`<span class="gvp-anom gvp-anom-energy" title="energy ${e.energy} ${e.dir}">⚡ ${e.energy.toFixed(0)}${arr} @ ${fmtPrice(e.price)}</span>`);
      });
      if (energy.vitality >= 80) {
        tags.push(`<span class="gvp-anom gvp-anom-vitality" title="vitality ${energy.vitality}">🔥 vitality ${energy.vitality.toFixed(0)}</span>`);
      }
      if (energy.pulse_hz >= 5) {
        tags.push(`<span class="gvp-anom gvp-anom-pulse" title="${energy.pulse_hz} trades/s">🚀 ${energy.pulse_hz.toFixed(1)} tr/s</span>`);
      }
    }
    if (data && Array.isArray(data.liqs)) {
      const bigLiqs = data.liqs.filter(l => l.notional >= 50000);
      const total = bigLiqs.reduce((s, l) => s + l.notional, 0);
      if (bigLiqs.length >= 3 || total >= 200000) {
        tags.push(`<span class="gvp-anom gvp-anom-liq" title="${bigLiqs.length} liqs · $${(total/1000).toFixed(0)}k">💥 ${bigLiqs.length} liqs · $${(total/1000).toFixed(0)}k</span>`);
      }
    }
    return tags.join('');
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
    const lv     = buildLevels(data.levels, vmin, vmax, opts && opts.trails);
    const gx     = buildGeom(geom, vmin, vmax);
    const orders = buildOrders(data.my_orders, vmin, vmax);
    const cur    = buildCurrent(data.current, vmin, vmax);
    const trails = buildTrails(opts && opts.trails, vmin, vmax);
    const energy = buildEnergy(opts && opts.energy, vmin, vmax);
    const heat   = buildHeat(opts && opts.heat, vmin, vmax, opts && opts.heatOpacity);
    const axisY  = buildAxisY(vmin, vmax, data.current);

    bodyEl.innerHTML = `
      <div class="gvp-axis-y">${axisY}</div>
      <div class="gvp-chart">
        <div class="gvp-heat">${heat}</div>
        <div class="gvp-vp-bg">${vp}</div>
        <div class="gvp-bids-col">${bids}</div>
        <div class="gvp-asks-col">${asks}</div>
        <div class="gvp-trails">${trails}</div>
        <div class="gvp-energy">${energy.halos}</div>
        <div class="gvp-overlay">
          ${gx.lines}
          ${lv.lines}
          ${liqs}
          ${cur}
          ${energy.arrows}
        </div>
      </div>
      <div class="gvp-labels-col">
        ${gx.labels}
        ${lv.labels}
        ${orders}
      </div>`;

    if (statsEl) statsEl.innerHTML = buildStats(data);

    const anomEl = panelEl.querySelector('[data-gvp-anomalies]');
    if (anomEl) anomEl.innerHTML = buildAnomalies(data, opts && opts.energy);
  }

  window.QtsGravityVertical = { render, priceToY };
})();
