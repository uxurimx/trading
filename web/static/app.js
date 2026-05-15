/* QTS Dashboard — WebSocket client */
'use strict';

// ── Estado persistido ─────────────────────────────────────────────────────────
let _theme      = localStorage.getItem('qts_theme')    || 'dark';
let _showMxn    = localStorage.getItem('qts_mxn')      === 'true';
let _mxnRate    = parseFloat(localStorage.getItem('qts_mxn_rate') || '17.5');
let _proMode    = localStorage.getItem('qts_pro_mode') !== 'false'; // default: PRO
let lastSnap    = null;

document.documentElement.setAttribute('data-theme', _theme);

// ── Formateo ──────────────────────────────────────────────────────────────────

function fmt(n, d = 2) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toFixed(d);
}

function fmtMoney(usd, d = 2) {
  if (usd == null || isNaN(usd)) return '—';
  const val  = _showMxn ? usd * _mxnRate : usd;
  const sign = val >= 0 ? '+' : '−';
  const pfx  = _showMxn ? 'MX$' : '$';
  return `${sign}${pfx}${Math.abs(val).toFixed(d)}`;
}

function fmtMoneyAbs(usd, d = 2) {
  if (usd == null || isNaN(usd)) return '—';
  const val = _showMxn ? usd * _mxnRate : usd;
  const pfx = _showMxn ? 'MX$' : '$';
  return `${pfx}${Math.abs(val).toFixed(d)}`;
}

function fmtPct(n) {
  if (n == null || isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '−';
  return `${sign}${Math.abs(n).toFixed(2)}%`;
}

function pnlClass(n) {
  if (n == null) return 'c-dim';
  return n >= 0 ? 'c-green' : 'c-red';
}

function fmtPrice(n) {
  if (!n && n !== 0) return '—';
  if (n >= 10000) return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (n >= 100)   return n.toFixed(3);
  if (n >= 1)     return n.toFixed(4);
  return n.toFixed(6);
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtElapsedShort(s) {
  s = Math.max(0, s | 0);
  if (s < 60)    return s + 's';
  if (s < 3600)  return Math.round(s / 60) + 'm';
  if (s < 86400) {
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  return Math.floor(s / 86400) + 'd';
}

// (theme/currency/clock init is at the bottom after DOM is ready)

// ── Render account ────────────────────────────────────────────────────────────

function renderAccount(a) {
  // Actualiza badge en sidenav (desktop) y mobile-topstrip
  const badgeText = a.error ? `● ${a.error}` : a.connected ? '● EN VIVO' : '● CONECTANDO';
  const badgeCls  = a.error ? 'badge badge-error' : a.connected ? 'badge badge-ok' : 'badge badge-connecting';
  ['conn-badge', 'conn-badge-mob'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.className = badgeCls; el.textContent = badgeText; }
  });

  // Equity en sidenav
  const sev = document.getElementById('sidenav-equity');
  if (sev) sev.textContent = fmtMoneyAbs(a.equity);

  // Métricas del dashboard
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  set('equity',      fmtMoneyAbs(a.equity));
  set('available',   fmtMoneyAbs(a.available));
  set('used-margin', fmtMoneyAbs(a.used_margin));
  set('margin-pct',  `${fmt(a.margin_pct)}% del equity`);
  set('open-count',  a.open_count);

  const upnl = document.getElementById('unrealized-pnl');
  if (upnl) { upnl.textContent = fmtMoney(a.unrealized_pnl); upnl.className = `metric-value ${pnlClass(a.unrealized_pnl)}`; }

  const dpnl = document.getElementById('daily-pnl');
  if (dpnl) { dpnl.textContent = fmtMoney(a.daily_pnl); dpnl.className = `metric-value ${pnlClass(a.daily_pnl)}`; }

  const pct  = Math.min(100, Math.max(0, a.margin_pct || 0));
  const fill = document.getElementById('margin-bar-fill');
  const lbl  = document.getElementById('margin-bar-label');
  if (fill) { fill.style.width = `${pct}%`; fill.style.background = pct > 80 ? 'var(--red)' : pct > 60 ? 'var(--orange)' : 'var(--green)'; }
  if (lbl)  lbl.textContent = `Margen ${fmt(pct, 1)}%`;
}

// ── Momentum del mercado (para el anillo del marcador) ────────────────────────

function calcMomentum(pos) {
  const bull = (pos.ab_side === 'BUY'  ? 1 : 0) + (pos.trend_dir === 'UP'   ? 1 : 0);
  const bear = (pos.ab_side === 'SELL' ? 1 : 0) + (pos.trend_dir === 'DOWN' ? 1 : 0);
  const rsi   = pos.rsi   || 50;
  const score = pos.score || 0;
  const rsiStr   = Math.abs(rsi - 50) / 50;          // 0→1
  const scoreStr = Math.min(score, 100) / 100;        // 0→1
  const strength = Math.min(1, 0.5 * rsiStr + 0.5 * scoreStr);

  let color;
  if      (bull > bear) color = 'var(--green)';
  else if (bear > bull) color = 'var(--red)';
  else                  color = 'var(--yellow-dim)';

  return { color, strength };
}

// ── Estado de zoom por posición (persistido en localStorage) ─────────────────
// Key: `${full_sym}_${side}` → { levelIdx: -1..4, anchor: 'mark'|'entry'|'be'|'mid' }
const _zoomState = (() => {
  try { return JSON.parse(localStorage.getItem('qts_zoom') || '{}'); }
  catch (e) { return {}; }
})();

function _getZoom(key) {
  return _zoomState[key] || { levelIdx: 0, anchor: 'mark' };
}
function _setZoom(key, patch) {
  _zoomState[key] = { ..._getZoom(key), ...patch };
  try { localStorage.setItem('qts_zoom', JSON.stringify(_zoomState)); } catch (e) {}
}

// ── Barra de progreso SL→TP — reproyectable con zoom telescópico ────────────

function buildProgressBar(pos) {
  const isLong = pos.direction === 'LONG';
  const key    = `${pos.full_sym}_${pos.side}`;
  const z      = _getZoom(key);

  // Geometría cruda. Si el backend aún no la manda, la reconstruimos.
  const geom = pos.geometry || {
    sl: pos.sl, entry: pos.entry, tp: pos.tp,
    be: pos.breakeven_price || pos.entry,
    mark: pos.mark, is_long: isLong,
    milestones: (pos.milestones || []).map(m => ({ pct: m.pct, price: m.price })),
  };

  // Ventana visible. L0 → SL↔TP literal. L≠0 → centrada en el ancla.
  const view = QtsScale.viewport(z.levelIdx, z.anchor, geom);

  // % de cada precio crítico dentro de la ventana (puede caer fuera)
  const entryPct = QtsScale.T(geom.entry, view);
  const markPct  = QtsScale.T(geom.mark,  view);
  const bePct    = QtsScale.T(geom.be,    view);
  const slPct    = QtsScale.T(geom.sl,    view);
  const tpPct    = QtsScale.T(geom.tp,    view);

  // ── Zonas de fondo (recortadas al viewport) ──────────────────────────────
  const lossR   = QtsScale.clipRange(geom.sl,    geom.entry, view);
  const beZoneR = QtsScale.clipRange(geom.entry, geom.be,    view);
  const profR   = QtsScale.clipRange(geom.be,    geom.tp,    view);

  const lossStyle   = `left:${lossR.left.toFixed(1)}%;width:${lossR.width.toFixed(1)}%`;
  const beZoneStyle = `left:${beZoneR.left.toFixed(1)}%;width:${beZoneR.width.toFixed(1)}%`;
  const profitStyle = `left:${profR.left.toFixed(1)}%;width:${profR.width.toFixed(1)}%`;

  // ── Estado del fill (tricolor según mark vs entry vs be) ─────────────────
  const mark = geom.mark;
  const bePx = geom.be || geom.entry;
  let fillState;
  if (isLong) {
    if      (mark >= bePx)       fillState = 'profit';
    else if (mark >= geom.entry) fillState = 'be';
    else                         fillState = 'loss';
  } else {
    if      (mark <= bePx)       fillState = 'profit';
    else if (mark <= geom.entry) fillState = 'be';
    else                         fillState = 'loss';
  }

  // Fill como rango [entry↔mark] (loss invierte sentido). Recortar al viewport.
  const fillR = (fillState === 'loss')
    ? QtsScale.clipRange(geom.mark, geom.entry, view)
    : QtsScale.clipRange(geom.entry, geom.mark, view);
  const fillClass = fillState === 'profit' ? 'prog-fill-profit'
                  : fillState === 'be'     ? 'prog-fill-be'
                  :                          'prog-fill-loss';

  // ── Marcador del mark con anillo de momentum ─────────────────────────────
  const mom    = calcMomentum(pos);
  const circ   = 37.7;
  const offset = 9.4;
  const filled = ((0.15 + 0.85 * mom.strength) * circ).toFixed(1);

  const fnPct = pos.full_net_pct ?? 0;
  const fnUsd = pos.full_net_pnl ?? 0;
  const fnCls = fillState === 'profit' ? 'c-green'
              : fillState === 'be'     ? 'c-orange'
              :                          'c-red';
  const markLbl = `${fmtPct(fnPct)} ${fmtMoney(fnUsd)}`;
  const markSvg = `
    <svg class="prog-mark-svg" viewBox="0 0 18 18" width="18" height="18">
      <circle cx="9" cy="9" r="8" fill="var(--bg-card)"/>
      <circle class="prog-mark-outer" cx="9" cy="9" r="8" fill="none" stroke-width="1.5"/>
      <circle cx="9" cy="9" r="6" fill="none"
        stroke="${mom.color}" stroke-width="3.5"
        stroke-dasharray="${filled} ${circ}"
        stroke-dashoffset="${offset}"
        stroke-linecap="round"/>
      <circle cx="9" cy="9" r="3" fill="var(--yellow)"/>
    </svg>`;

  // ── Helper: render de un marcador con manejo de fuera-de-ventana ─────────
  // type ∈ 'entry' | 'be' | 'mark' | 'milestone' | 'sl' | 'tp' | 'order'
  function _renderInRange(pct, html) {
    return pct >= 0 && pct <= 100 ? html(pct) : '';
  }
  function _oorChip(label, oor, tip, cls) {
    const side = oor.side; // 'left' | 'right'
    const arrow = side === 'left' ? '◀' : '▶';
    const txt = side === 'left' ? `${arrow} ${label}` : `${label} ${arrow}`;
    return `<div class="prog-oor ${side} ${cls || ''}" title="${esc(tip)}">${esc(txt)}</div>`;
  }

  // ── Marcadores fuera de ventana (flechas en el borde) ────────────────────
  let oorEntry = '', oorBe = '', oorMark = '', oorSl = '', oorTp = '';
  const oorE = QtsScale.outOfRange(geom.entry, view);
  if (oorE) oorEntry = _oorChip(`E ${fmtPrice(geom.entry)}`, oorE,
                                `Entrada · ${oorE.deltaPct.toFixed(1)}% fuera de vista`, 'entry');
  const oorB = QtsScale.outOfRange(geom.be, view);
  if (oorB) oorBe = _oorChip(`BE ${fmtPrice(geom.be)}`, oorB,
                             `Breakeven · ${oorB.deltaPct.toFixed(1)}% fuera de vista`, 'be');
  const oorM = QtsScale.outOfRange(geom.mark, view);
  if (oorM) oorMark = _oorChip(`◉ ${fmtPrice(geom.mark)}`, oorM,
                               `Mark · ${oorM.deltaPct.toFixed(1)}% fuera de vista`, 'mark');
  const oorS = QtsScale.outOfRange(geom.sl, view);
  if (oorS) oorSl = _oorChip(`SL ${fmtPrice(geom.sl)}`, oorS,
                             `SL · ${oorS.deltaPct.toFixed(1)}% fuera de vista`, 'sl');
  const oorT = QtsScale.outOfRange(geom.tp, view);
  if (oorT) oorTp = _oorChip(`TP ${fmtPrice(geom.tp)}`, oorT,
                             `TP · ${oorT.deltaPct.toFixed(1)}% fuera de vista`, 'tp');

  // ── Órdenes límite (in-range) ────────────────────────────────────────────
  const orderMarkers = (pos.orders || []).map(o => {
    if (!o.price) return '';
    const pct = QtsScale.T(o.price, view);
    if (pct < 0 || pct > 100) return '';
    const cls = o.side === 'Buy' ? 'prog-order-buy' : 'prog-order-sell';
    const tip = `${o.side} ${o.qty} @ ${fmtPrice(o.price)} (${o.status})`;
    return `<div class="prog-order-marker ${cls}" style="left:${pct.toFixed(1)}%" title="${esc(tip)}"></div>`;
  }).join('');

  // ── BE marker + label (in-range) ─────────────────────────────────────────
  const beMarker = _renderInRange(bePct, p =>
    `<div class="prog-be-marker" style="left:${p.toFixed(1)}%" title="Breakeven: ${esc(fmtPrice(geom.be))}"></div>
     <div class="prog-be-label"  style="left:${p.toFixed(1)}%">BE</div>`);

  // ── Hitos 25/50/75 (in-range) ────────────────────────────────────────────
  const milestoneMarkers = (pos.milestones || []).map(m => {
    const p = QtsScale.T(m.price, view);
    if (p < 0 || p > 100) return '';
    const tip      = `${m.pct}% → ${fmtPrice(m.price)} | ROI ${m.roi >= 0 ? '+' : ''}${fmt(m.roi, 2)}%`;
    const grossStr = m.gross != null ? ` (${fmtMoneyAbs(m.gross)})` : '';
    return `
      <div class="prog-milestone" style="left:${p.toFixed(1)}%" title="${esc(tip)}"></div>
      <div class="prog-milestone-label" style="left:${p.toFixed(1)}%">${m.pct}% ${fmtPrice(m.price)}${grossStr}</div>`;
  }).join('');

  // ── Marcador de entrada (in-range) ───────────────────────────────────────
  const entryMarker = _renderInRange(entryPct, p =>
    `<div class="prog-entry-line"  style="left:${p.toFixed(1)}%"></div>
     <div class="prog-entry-label" style="left:${p.toFixed(1)}%">Entrada ${esc(fmtPrice(geom.entry))}</div>`);

  // ── Mark + label (in-range) ──────────────────────────────────────────────
  const markBlock = _renderInRange(markPct, p =>
    `<div class="prog-mark-wrap"   style="left:${p.toFixed(1)}%" title="Mark: ${esc(fmtPrice(geom.mark))}">${markSvg}</div>
     <div class="prog-mark-label ${fnCls}" style="left:${p.toFixed(1)}%">${esc(markLbl)}</div>`);

  // ── Controles de zoom ────────────────────────────────────────────────────
  const lv = QtsScale.level(z.levelIdx);
  const atMin = z.levelIdx <= QtsScale.MIN_IDX;
  const atMax = z.levelIdx >= QtsScale.MAX_IDX;
  const anchorLbl = z.anchor === 'be' ? 'BE'
                  : z.anchor === 'entry' ? 'entrada'
                  : z.anchor === 'mid' ? 'centro'
                  : 'mark';
  const zoomCtrls = `
    <div class="prog-zoom-bar" data-zoom-key="${esc(key)}">
      <button class="prog-zoom-btn" data-zoom-act="dec" ${atMin ? 'disabled' : ''} title="Zoom out (−)">−</button>
      <span class="prog-zoom-lbl">${esc(lv.label)}${lv.idx !== 0 ? ' · ' + anchorLbl : ''}</span>
      <button class="prog-zoom-btn" data-zoom-act="inc" ${atMax ? 'disabled' : ''} title="Zoom in (+)">+</button>
      <button class="prog-zoom-btn prog-zoom-reset" data-zoom-act="reset" ${lv.idx === 0 ? 'disabled' : ''} title="Reset (0)">⤬</button>
      ${lv.idx !== 0 ? `
        <select class="prog-zoom-anchor" data-zoom-act="anchor" title="Centro de la vista">
          <option value="mark"  ${z.anchor === 'mark'  ? 'selected' : ''}>◉ mark</option>
          <option value="entry" ${z.anchor === 'entry' ? 'selected' : ''}>E entrada</option>
          <option value="be"    ${z.anchor === 'be'    ? 'selected' : ''}>BE</option>
          <option value="mid"   ${z.anchor === 'mid'   ? 'selected' : ''}>centro</option>
        </select>` : ''}
    </div>`;

  // ── Cronotopología: heatmap espacial + stack temporal + leyenda ─────────
  // Las zonas vienen del backend (web/zone_tracker.py). Solo dibujamos si hay
  // tiempo cocinado suficiente para que la visualización tenga señal (>5s).
  const zonesData = pos.zones;
  let timeLayer = '';
  if (zonesData && (zonesData.total_seconds || 0) > 5 && Array.isArray(zonesData.zones)) {
    const m = geom.milestones || [];
    const m25p = m[0]?.price, m50p = m[1]?.price, m75p = m[2]?.price;
    const rngTotal = Math.abs((geom.tp || 0) - (geom.sl || 0)) || 1;
    // Rango de precios por zona. clipRange normaliza con min/max,
    // así que el orden numérico (LONG/SHORT) no importa.
    const zoneRange = {
      below_sl: [geom.sl - rngTotal * 0.5, geom.sl],
      sl_entry: [geom.sl,    geom.entry],
      entry_be: [geom.entry, geom.be],
      be_25:    [geom.be,    m25p],
      '25_50':  [m25p,       m50p],
      '50_75':  [m50p,       m75p],
      '75_tp':  [m75p,       geom.tp],
      above_tp: [geom.tp,    geom.tp + rngTotal * 0.5],
    };
    const totalS = zonesData.total_seconds || 1;
    const logTot = Math.log(1 + totalS);

    const heatSegs = zonesData.zones.map(z => {
      const rng = zoneRange[z.key];
      if (!rng || rng[0] == null || rng[1] == null) return '';
      const r = QtsScale.clipRange(rng[0], rng[1], view);
      if (r.width <= 0) return '';
      const norm = logTot > 0 ? Math.log(1 + z.seconds) / logTot : 0;
      const op = Math.max(0.08, Math.min(0.55, 0.08 + norm * 0.47));
      const dur = fmtElapsedShort(z.seconds);
      const tip = `${z.label} · cocinado ${dur} · ${z.visits} visita${z.visits===1?'':'s'} · racha máx ${fmtElapsedShort(z.max_streak)}`;
      const inside = r.width > 5 ? `<span class="prog-heat-lbl">${esc(dur)}</span>` : '';
      const cur = z.key === zonesData.current_zone ? ' is-current' : '';
      return `<div class="prog-heat-seg${cur}" data-zone="${esc(z.key)}"
                style="left:${r.left.toFixed(1)}%;width:${r.width.toFixed(1)}%;opacity:${op.toFixed(2)}"
                title="${esc(tip)}">${inside}</div>`;
    }).join('');

    // Stack temporal: barra apilada de pct_of_life. Eje = tiempo, no precio.
    let cur = 0;
    const stack = zonesData.zones.map(z => {
      if (!z.pct_of_life || z.pct_of_life <= 0) return '';
      const left = cur; cur += z.pct_of_life;
      const tip  = `${z.label}: ${z.pct_of_life.toFixed(1)}% del tiempo · ${fmtElapsedShort(z.seconds)}`;
      return `<div class="prog-stack-seg prog-stack-${esc(z.key)}"
                style="left:${left.toFixed(1)}%;width:${z.pct_of_life.toFixed(1)}%"
                title="${esc(tip)}"></div>`;
    }).join('');

    // Leyenda: top 2 zonas más cocinadas + zona actual
    const sorted = zonesData.zones.slice().sort((a, b) => b.seconds - a.seconds);
    const top    = sorted.slice(0, 2)
      .map(z => `<span class="prog-legend-item"><span class="prog-legend-dot prog-stack-${esc(z.key)}"></span>${esc(z.label)} ${z.pct_of_life.toFixed(0)}%</span>`)
      .join(' · ');
    const curZ = zonesData.zones.find(z => z.key === zonesData.current_zone);
    const curHtml = curZ ? `<span class="prog-legend-current">ahora: ${esc(curZ.label)} (${fmtElapsedShort(curZ.max_streak)})</span>` : '';

    timeLayer = `
      <div class="prog-time-heat" title="Tiempo cocinado por zona — opacidad ∝ log(t)">${heatSegs}</div>
      <div class="prog-time-stack" title="Proporción temporal por zona">${stack}</div>
      <div class="prog-time-legend">Fases ${esc(fmtElapsedShort(totalS))}: ${top}${curHtml ? ' · ' + curHtml : ''}</div>`;
  }

  // Strip gravitacional — comparte viewport con la progress bar
  const gravStrip = `
    <div class="prog-gravity">
      <canvas class="prog-grav-canvas"
              data-grav-sym="${esc(pos.full_sym)}"
              data-grav-vmin="${view.min}"
              data-grav-vmax="${view.max}"></canvas>
    </div>`;

  return `
  <div class="prog-wrap">
    ${zoomCtrls}
    <div class="prog-track">
      <div class="prog-zone-loss"   style="${lossStyle}"></div>
      ${beZoneR.width > 0 ? `<div class="prog-zone-be" style="${beZoneStyle}"></div>` : ''}
      <div class="prog-zone-profit" style="${profitStyle}"></div>
      <div class="${fillClass}"     style="left:${fillR.left.toFixed(1)}%;width:${fillR.width.toFixed(1)}%"></div>
      ${orderMarkers}
      ${beMarker}
      ${milestoneMarkers}
      ${entryMarker}
      ${markBlock}
      ${oorSl}${oorEntry}${oorBe}${oorMark}${oorTp}
    </div>
    ${gravStrip}
    ${timeLayer}
  </div>`;
}

// ── Signal chips ──────────────────────────────────────────────────────────────

function buildSignalChips(pos) {
  const tdMap  = { UP: 'bull', DOWN: 'bear', NEUTRAL: '' };
  const tdIcon = { UP: '▲', DOWN: '▼', NEUTRAL: '—' };
  const abMap  = { BUY: 'bull', SELL: 'bear', NEUTRAL: '' };
  return `<div class="pos-signals">
    <span class="sig-chip ${tdMap[pos.trend_dir]  || ''}">${tdIcon[pos.trend_dir] || '—'} TREND</span>
    <span class="sig-chip ${abMap[pos.ab_side]    || ''}">ABS ${pos.ab_side || '—'}</span>
    <span class="sig-chip">${(pos.regime || 'UNKNOWN').replace('_', ' ')}</span>
    <span class="sig-chip ${pos.score >= 70 ? 'bull' : pos.score >= 40 ? 'warn' : ''}">SCORE ${pos.score}</span>
    <span class="sig-chip ${pos.rsi >= 70 ? 'bear' : pos.rsi <= 30 ? 'bull' : ''}">RSI ${fmt(pos.rsi, 1)}</span>
  </div>`;
}

// ── Órdenes pendientes ────────────────────────────────────────────────────────

function buildOrdersRow(orders) {
  if (!orders || orders.length === 0) return '';
  const chips = orders.map(o => {
    const cls = o.side === 'Buy' ? 'c-green' : 'c-red';
    return `<span class="sig-chip">
      <span class="${cls}">${o.side === 'Buy' ? '▲' : '▼'} ${o.side.toUpperCase()}</span>
      &nbsp;${o.qty} @ ${fmtPrice(o.price)}
      <span style="color:var(--text-sub)"> ${o.type} · ${o.status}</span>
    </span>`;
  }).join('');
  return `<div class="pos-signals" style="margin-bottom:8px">${chips}</div>`;
}

// ── Tarjeta de posición — delegador Pro / Lite ────────────────────────────────

function buildPositionCard(pos) {
  return _proMode ? buildPosCardPro(pos) : buildPosCardLite(pos);
}

// ── Pro card ──────────────────────────────────────────────────────────────────

// Estado del panel detalles Pro: key → 'detalles' | 'salud' | null(cerrado)
const _proDetailState = new Map();
// Cache de klines: `${sym}_${tf}` → { klines, tf, ts, error? }
const _klineCache = new Map();
const _KLINE_TTL  = 60000; // 1 minuto

function _klineTf(elapsedS) {
  if (elapsedS < 2 * 3600)   return '5';
  if (elapsedS < 12 * 3600)  return '15';
  if (elapsedS < 3 * 86400)  return '60';
  return '240';
}

async function _fetchKlines(sym, elapsedS) {
  const tf  = _klineTf(elapsedS);
  const key = `${sym}_${tf}`;
  const hit = _klineCache.get(key);
  if (hit && Date.now() - hit.ts < _KLINE_TTL) return; // fresco
  try {
    const res  = await fetch(`/api/klines/${sym}?tf=${tf}&limit=60`);
    const data = await res.json();
    _klineCache.set(key, { klines: data.klines || [], tf, ts: Date.now() });
  } catch (e) {
    _klineCache.set(key, { klines: [], tf, ts: Date.now(), error: String(e) });
  }
  if (lastSnap) renderPositions(lastSnap.positions || []);
}

function buildCandleChart(klines, entry, mark, isLong) {
  if (!klines || klines.length < 2) {
    return `<div class="candle-chart-loading">Sin datos de velas</div>`;
  }
  const W = 300, H = 100, PAD = 2;
  const highs = klines.map(k => k.h);
  const lows  = klines.map(k => k.l);
  let yMax = Math.max(...highs, entry, mark);
  let yMin = Math.min(...lows,  entry, mark);
  const yRange = yMax - yMin || 1;
  yMax += yRange * 0.04;
  yMin -= yRange * 0.04;
  const toX = i  => PAD + (i / (klines.length - 1)) * (W - PAD * 2);
  const toY = p  => H - PAD - ((p - yMin) / (yMax - yMin)) * (H - PAD * 2);
  const candleW = Math.max(1.5, (W - PAD * 2) / klines.length * 0.65);

  const candles = klines.map((k, i) => {
    const x      = toX(i);
    const green  = k.c >= k.o;
    const col    = green ? 'var(--green)' : 'var(--red)';
    const bodyT  = toY(Math.max(k.o, k.c));
    const bodyB  = toY(Math.min(k.o, k.c));
    const bodyH  = Math.max(1, bodyB - bodyT);
    return `<line x1="${x.toFixed(1)}" y1="${toY(k.h).toFixed(1)}" x2="${x.toFixed(1)}" y2="${toY(k.l).toFixed(1)}" stroke="${col}" stroke-width="1" opacity=".5"/>
<rect x="${(x-candleW/2).toFixed(1)}" y="${bodyT.toFixed(1)}" width="${candleW.toFixed(1)}" height="${bodyH.toFixed(1)}" fill="${col}"/>`;
  }).join('');

  const entryY = toY(entry).toFixed(1);
  const markY  = toY(mark).toFixed(1);
  const pnlCls = isLong ? (mark >= entry ? 'var(--green)' : 'var(--red)')
                        : (mark <= entry ? 'var(--green)' : 'var(--red)');

  return `<div class="candle-chart-wrap">
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="none">
      ${candles}
      <line x1="0" y1="${entryY}" x2="${W}" y2="${entryY}" stroke="var(--text-dim)" stroke-width="1" stroke-dasharray="4,3"/>
      <line x1="0" y1="${markY}"  x2="${W}" y2="${markY}"  stroke="${pnlCls}" stroke-width="1" stroke-dasharray="2,2" opacity=".8"/>
      <text x="3" y="${Math.max(8, entryY - 2)}" fill="var(--text-dim)" font-size="8" font-family="monospace">E</text>
      <text x="3" y="${Math.max(8, markY  - 2)}" fill="${pnlCls}" font-size="8" font-family="monospace">M</text>
    </svg>
  </div>`;
}

function buildPosCardPro(pos) {
  const isLong   = pos.direction === 'LONG';
  const dirClass = isLong ? 'long' : 'short';
  const chgStr   = fmtPct(pos.roi_entry_pct);
  const netAtSL  = pos.net_at_sl;
  const netAtTP  = pos.net_at_tp;
  const key      = `${pos.full_sym}_${pos.side}`;
  const detState = _proDetailState.get(key) || null;   // null | 'detalles' | 'salud'
  const detOpen  = !!detState;

  // Botones de acción — misma lógica que Lite
  const slTarget  = _findSLTarget(pos);
  const slBtnHtml = slTarget
    ? `<button class="pos-act-btn warn"
         data-move-sl="${esc(key)}"
         data-sym="${esc(pos.full_sym)}" data-side="${esc(pos.side)}"
         data-new-sl="${slTarget.price}"
         title="Mover SL a ${esc(slTarget.label)}: ${fmtPrice(slTarget.price)}">
         SL → ${esc(slTarget.label)}
       </button>`
    : `<button class="pos-act-btn" disabled title="Ningún marcador superado aún">SL →</button>`;

  const actionsHtml = _closingPos.has(key)
    ? `<div class="pos-actions confirming">
         <button class="pos-act-btn danger" disabled>⏳ Cerrando…</button>
       </div>`
    : _pendingClose.has(key)
    ? `<div class="pos-actions confirming">
         <span class="pos-confirm-lbl">Cerrar ${esc(pos.symbol)} @ ${fmtPrice(pos.mark)}?</span>
         <button class="pos-act-btn secondary" data-cancel-close="${esc(key)}">CANCELAR</button>
         <button class="pos-act-btn danger" data-confirm-close="${esc(key)}"
           data-sym="${esc(pos.full_sym)}" data-side="${esc(pos.side)}">✓ CERRAR</button>
       </div>`
    : `<div class="pos-actions">
         <button class="pos-act-btn danger" data-close="${esc(key)}">Cerrar</button>
         ${slBtnHtml}
         <button class="pos-act-btn" disabled title="Próximamente">+ Más</button>
       </div>`;

  // Panel de detalles con tabs
  let detailsHtml = '';
  if (detOpen) {
    // Tab: Detalles
    const tabDetalles = detState === 'detalles' ? `
      <div class="pos-details-grid">
        <div class="pos-detail-cell lev">
          <div class="lbl">APALANCAMIENTO</div>
          <div class="val">${pos.leverage}x</div>
        </div>
        <div class="pos-detail-cell">
          <div class="lbl">TIEMPO</div>
          <div class="val">⏱ ${esc(pos.elapsed_fmt)}</div>
        </div>
        <div class="pos-detail-cell">
          <div class="lbl">MARGEN</div>
          <div class="val">${fmtMoney(pos.margin)}</div>
        </div>
        <div class="pos-detail-cell entry">
          <div class="lbl">ENTRADA</div>
          <div class="val">${fmtPrice(pos.entry)}</div>
        </div>
        <div class="pos-detail-cell mark">
          <div class="lbl">MARK <span class="${pnlClass(pos.roi_entry_pct)}" style="font-size:8px">${esc(chgStr)}</span></div>
          <div class="val">${fmtPrice(pos.mark)}</div>
        </div>
        <div class="pos-detail-cell">
          <div class="lbl">BREAKEVEN</div>
          <div class="val">${fmtPrice(pos.breakeven_price)}</div>
        </div>
        <div class="pos-detail-cell sl">
          <div class="lbl">STOP LOSS</div>
          <div class="val">${fmtPrice(pos.sl) || '—'}</div>
        </div>
        <div class="pos-detail-cell tp">
          <div class="lbl">TAKE PROFIT</div>
          <div class="val">${fmtPrice(pos.tp) || '—'}</div>
        </div>
        <div class="pos-detail-cell">
          <div class="lbl">R:R</div>
          <div class="val">${fmt(pos.rr_ratio, 2)}</div>
        </div>
      </div>` : '';

    // Tab: Salud (mini chart de velas)
    let tabSalud = '';
    if (detState === 'salud') {
      const tf       = _klineTf(pos.elapsed_s || 0);
      const cacheKey = `${pos.full_sym}_${tf}`;
      const cached   = _klineCache.get(cacheKey);
      if (!cached) {
        _fetchKlines(pos.full_sym, pos.elapsed_s || 0);
        tabSalud = `<div class="candle-chart-loading">Cargando velas…</div>`;
      } else if (cached.error) {
        tabSalud = `<div class="candle-chart-loading" style="color:var(--red)">Error: ${esc(cached.error)}</div>`;
      } else {
        const tfLabels = {'5':'5m','15':'15m','60':'1h','240':'4h'};
        tabSalud = buildCandleChart(cached.klines, pos.entry, pos.mark, isLong)
          + `<div class="candle-chart-tf">${tfLabels[tf]||tf} · ${cached.klines.length} velas
             · <button class="pos-details-toggle" style="font-size:9px;padding:1px 5px"
                 data-reload-klines="${esc(pos.full_sym)}"
                 data-elapsed="${pos.elapsed_s || 0}">↺ actualizar</button></div>`;
      }
    }

    const dActive = detState === 'detalles' ? 'active' : '';
    const sActive = detState === 'salud'    ? 'active' : '';
    detailsHtml = `
    <div class="pos-details">
      <div class="pos-detail-tabs">
        <button class="pos-detail-tab ${dActive}"
          data-detail-tab="detalles" data-pos-key="${esc(key)}">Detalles</button>
        <button class="pos-detail-tab ${sActive}"
          data-detail-tab="salud" data-pos-key="${esc(key)}"
          data-sym="${esc(pos.full_sym)}" data-elapsed="${pos.elapsed_s || 0}">Salud</button>
      </div>
      ${tabDetalles}${tabSalud}
    </div>`;
  }

  return `
  <div class="pos-card" data-pos-key="${esc(key)}">
    <div class="pos-header">
      <span class="pos-symbol">${esc(pos.symbol)}</span>
      <span class="pos-dir ${dirClass}">${pos.direction}</span>
      <span class="pos-time">⏱ ${esc(pos.elapsed_fmt)}</span>
      <button class="pos-details-toggle" data-toggle-details="${esc(key)}">
        ${detOpen ? '▲ ocultar' : '▼ detalles'}
      </button>
    </div>
    <div class="pos-body">
      ${buildProgressBar(pos)}
      <div class="pos-metrics">
        <div class="pnl-cell">
          <div class="lbl">EN SL <span style="color:var(--text-sub);font-size:8px">(${fmtPrice(pos.sl)})</span></div>
          <div class="val ${pnlClass(netAtSL)}">${fmtMoney(netAtSL)}</div>
          <div class="sub">neto · fees incl.</div>
        </div>
        <div class="pnl-cell">
          <div class="lbl">EN TP <span style="color:var(--text-sub);font-size:8px">(${fmtPrice(pos.tp)})</span></div>
          <div class="val ${pnlClass(netAtTP)}">${fmtMoney(netAtTP)}</div>
          <div class="sub">neto · fees incl.</div>
        </div>
      </div>
      ${buildOrdersRow(pos.orders)}
      ${buildSignalChips(pos)}
      ${actionsHtml}
    </div>
    ${detailsHtml}
  </div>`;
}

// ── Lite: detectar marcador SL disponible ────────────────────────────────────

function _findSLTarget(pos) {
  const isLong = pos.direction === 'LONG';
  const mark   = pos.mark;
  const curSL  = pos.sl || 0;

  const candidates = [];
  if (pos.breakeven_price > 0) candidates.push({ label: 'BE', price: pos.breakeven_price });
  (pos.milestones || []).forEach(m => candidates.push({ label: `${m.pct}%`, price: m.price }));

  const passed = candidates.filter(c =>
    isLong ? (mark >= c.price && c.price > curSL)
           : (mark <= c.price && c.price < curSL)
  );
  if (!passed.length) return null;
  return isLong
    ? passed.sort((a, b) => b.price - a.price)[0]
    : passed.sort((a, b) => a.price - b.price)[0];
}

// ── Lite: barra de progreso simplificada ──────────────────────────────────────

function buildProgressBarLite(pos) {
  const isLong   = pos.direction === 'LONG';
  const entryPct = pos.entry_pct_bar;
  const markPct  = pos.mark_pct_bar;
  const bePct    = pos.be_pct_bar ?? entryPct;
  const progress = pos.progress_pct;

  // Zonas de fondo (misma lógica que Pro)
  const lossStyle = isLong
    ? `left:0%;width:${entryPct.toFixed(1)}%`
    : `left:${entryPct.toFixed(1)}%;width:${(100 - entryPct).toFixed(1)}%`;
  let beZoneStyle = '';
  if (pos.be_pct_bar != null) {
    if (isLong) { const w = Math.max(0, bePct - entryPct); beZoneStyle = `left:${entryPct.toFixed(1)}%;width:${w.toFixed(1)}%`; }
    else        { const w = Math.max(0, entryPct - bePct); beZoneStyle = `left:${bePct.toFixed(1)}%;width:${w.toFixed(1)}%`; }
  }
  const profitStyle = isLong
    ? `left:${bePct.toFixed(1)}%;width:${(100 - bePct).toFixed(1)}%`
    : `left:0%;width:${bePct.toFixed(1)}%`;

  // Fill tricolor
  const mark  = pos.mark;
  const bePx  = pos.breakeven_price || pos.entry;
  let fillState;
  if (isLong) {
    if (mark >= bePx)        fillState = 'profit';
    else if (mark >= pos.entry) fillState = 'be';
    else                     fillState = 'loss';
  } else {
    if (mark <= bePx)        fillState = 'profit';
    else if (mark <= pos.entry) fillState = 'be';
    else                     fillState = 'loss';
  }
  let fillLeft, fillWidth;
  if (fillState === 'loss') {
    if (isLong) { fillLeft = Math.max(0, markPct); fillWidth = Math.max(0, entryPct - Math.max(0, markPct)); }
    else        { fillLeft = entryPct; fillWidth = Math.max(0, Math.min(100, markPct) - entryPct); }
  } else {
    if (isLong) { fillLeft = entryPct; fillWidth = Math.max(0, markPct - entryPct); }
    else        { fillLeft = Math.max(0, markPct); fillWidth = Math.max(0, entryPct - Math.max(0, markPct)); }
  }
  const fillClass = fillState === 'profit' ? 'prog-fill-profit'
                  : fillState === 'be'     ? 'prog-fill-be'
                  :                          'prog-fill-loss';
  const fnCls = fillState === 'profit' ? 'c-green' : fillState === 'be' ? 'c-orange' : 'c-red';

  // Labels con $ en vez de precio
  const slLbl  = pos.net_at_sl != null ? fmtMoney(pos.net_at_sl) : '—';
  const tpLbl  = pos.net_at_tp != null ? fmtMoney(pos.net_at_tp) : '—';
  const pctLbl = `${progress >= 0 ? '+' : ''}${fmt(progress, 1)}%`;

  // Marcador plano — dot CSS puro, sin SVG, sin anillo
  const dotColor = fillState === 'profit' ? 'var(--green)' : fillState === 'be' ? 'var(--orange)' : 'var(--red)';

  // BE marker
  const beMarker = (pos.be_pct_bar != null && pos.breakeven_price)
    ? `<div class="prog-be-marker" style="left:${pos.be_pct_bar.toFixed(1)}%" title="BE: ${esc(fmtPrice(pos.breakeven_price))}"></div>
       <div class="prog-be-label"  style="left:${pos.be_pct_bar.toFixed(1)}%">BE</div>`
    : '';

  // Milestone markers (solo %)
  const msMarkers = (pos.milestones || []).map(m =>
    `<div class="prog-milestone" style="left:${m.bar_pct.toFixed(1)}%" title="${m.pct}% → ${fmtPrice(m.price)}"></div>
     <div class="prog-milestone-lbl-lite" style="left:${m.bar_pct.toFixed(1)}%">${m.pct}%</div>`
  ).join('');

  return `
  <div class="prog-wrap">
    <div class="prog-labels">
      <span class="c-red">SL <strong>${esc(slLbl)}</strong></span>
      <span class="prog-pct">${esc(pctLbl)} hacia TP</span>
      <span class="c-green">TP <strong>${esc(tpLbl)}</strong></span>
    </div>
    <div class="prog-track prog-track-lite">
      <div class="prog-zone-loss"   style="${lossStyle}"></div>
      ${beZoneStyle ? `<div class="prog-zone-be" style="${beZoneStyle}"></div>` : ''}
      <div class="prog-zone-profit" style="${profitStyle}"></div>
      <div class="${fillClass}"     style="left:${fillLeft.toFixed(1)}%;width:${fillWidth.toFixed(1)}%"></div>
      ${beMarker}
      ${msMarkers}
      <div class="prog-entry-line" style="left:${entryPct.toFixed(1)}%"></div>
      <div class="prog-dot-lite"   style="left:${markPct.toFixed(1)}%;background:${dotColor}" title="Mark: ${esc(fmtPrice(pos.mark))}"></div>
      <div class="prog-dot-lbl-lite ${fnCls}" style="left:${markPct.toFixed(1)}%">${esc(fmtMoney(pos.full_net_pnl))}</div>
    </div>
  </div>`;
}

// ── Lite card ─────────────────────────────────────────────────────────────────

const _pendingClose   = new Set(); // "BTCUSDT_Buy" — confirming state persiste entre renders
const _closingPos     = new Set(); // en vuelo hacia /api/close — bloquea nuevos clics

function buildPosCardLite(pos) {
  const isLong = pos.direction === 'LONG';
  const dirCls = isLong ? 'long' : 'short';

  // Color del % de progreso: misma lógica que fill
  const mark   = pos.mark;
  const bePx   = pos.breakeven_price || pos.entry;
  let fillState;
  if (isLong) {
    if (mark >= bePx)           fillState = 'profit';
    else if (mark >= pos.entry) fillState = 'be';
    else                        fillState = 'loss';
  } else {
    if (mark <= bePx)           fillState = 'profit';
    else if (mark <= pos.entry) fillState = 'be';
    else                        fillState = 'loss';
  }
  const pctCls = fillState === 'profit' ? 'c-green' : fillState === 'be' ? 'c-orange' : 'c-red';
  const prog   = pos.progress_pct ?? 0;
  const pctStr = `${prog >= 0 ? '+' : ''}${fmt(prog, 1)}%`;

  // SL move button
  const slTarget = _findSLTarget(pos);
  const key      = `${pos.full_sym}_${pos.side}`;

  const slBtnHtml = slTarget
    ? `<button class="pos-act-btn warn"
         data-move-sl="${esc(key)}"
         data-sym="${esc(pos.full_sym)}" data-side="${esc(pos.side)}"
         data-new-sl="${slTarget.price}"
         title="Mover SL a ${esc(slTarget.label)}: ${fmtPrice(slTarget.price)}">
         SL → ${esc(slTarget.label)}
       </button>`
    : `<button class="pos-act-btn" disabled title="Ningún marcador superado aún">SL →</button>`;

  const actionsHtml = _closingPos.has(key)
    ? `<div class="pos-actions confirming">
         <button class="pos-act-btn danger" disabled>⏳ Cerrando…</button>
       </div>`
    : _pendingClose.has(key)
    ? `<div class="pos-actions confirming">
         <span class="pos-confirm-lbl">Cerrar ${esc(pos.symbol)} @ ${fmtPrice(mark)}?</span>
         <button class="pos-act-btn secondary" data-cancel-close="${esc(key)}">CANCELAR</button>
         <button class="pos-act-btn danger" data-confirm-close="${esc(key)}"
           data-sym="${esc(pos.full_sym)}" data-side="${esc(pos.side)}">✓ CERRAR</button>
       </div>`
    : `<div class="pos-actions">
         <button class="pos-act-btn danger" data-close="${esc(key)}">Cerrar</button>
         ${slBtnHtml}
         <button class="pos-act-btn" disabled title="Próximamente">+ Más</button>
       </div>`;

  return `
  <div class="pos-card pos-card-lite" data-pos-key="${esc(key)}">
    <div class="pos-header-lite">
      <span class="pos-symbol pos-sym-lite ${dirCls}">${esc(pos.symbol)}</span>
      <span class="pos-pct-lite ${pctCls}">${esc(pctStr)}</span>
      <span class="pos-time">⏱ ${pos.elapsed_fmt}</span>
    </div>
    <div style="padding:0 14px">
      ${buildProgressBarLite(pos)}
    </div>
    ${actionsHtml}
  </div>`;
}

// ── Render posiciones ─────────────────────────────────────────────────────────

function renderPositions(positions) {
  const n    = positions ? positions.length : 0;

  // Limpiar sets de estado de posiciones que ya desaparecieron del snapshot
  const activeKeys = new Set((positions || []).map(p => `${p.full_sym}_${p.side}`));
  for (const k of [..._closingPos])           if (!activeKeys.has(k)) _closingPos.delete(k);
  for (const k of [..._pendingClose])         if (!activeKeys.has(k)) _pendingClose.delete(k);
  for (const k of _proDetailState.keys())    if (!activeKeys.has(k)) _proDetailState.delete(k);

  const html = n ? positions.map(buildPositionCard).join('') : '<div class="empty-state">Sin posiciones abiertas</div>';

  const c  = document.getElementById('positions-container');
  const pc = document.getElementById('pos-count');
  if (c)  c.innerHTML    = html;
  if (pc) pc.textContent = n > 0 ? String(n) : '';

  // Badge en side-tab "Trades" (suma posiciones + análisis)
  const sb  = document.getElementById('side-pos-count');
  const acn = parseInt(document.getElementById('ana-count')?.textContent || '0', 10);
  if (sb) sb.textContent = (n + acn) > 0 ? String(n + acn) : '';
}

// ── Analysis cards ────────────────────────────────────────────────────────────

function buildAnalysisCard(a) {
  const isLong   = a.direction === 'LONG';
  const dirCls   = isLong ? 'long' : 'short';
  const chgStr   = fmtPct(a.roi_entry_pct);
  const netAtSL  = a.net_at_sl;
  const netAtTP  = a.net_at_tp;

  const date = a.created_at ? (() => {
    const d = new Date(a.created_at);
    const p = n => String(n).padStart(2,'0');
    return `${d.getMonth()+1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
  })() : '';

  return `
  <div class="ana-card" data-aid="${esc(a.id)}">
    <div class="ana-card-hdr">
      <span class="pos-symbol">${esc(a.symbol)}</span>
      <span class="ana-virtual-badge">VIRTUAL</span>
      <span class="pos-dir ${dirCls}">${a.direction}</span>
      <span class="pos-leverage">${a.leverage}x</span>
      <span class="pos-leverage c-dim">$${a.size_usdt}</span>
      <span class="ana-date">${esc(date)}</span>
    </div>
    ${a.notes ? `<div class="ana-notes">${esc(a.notes)}</div>` : ''}
    <div class="ana-card-body">
      <div class="pos-prices" style="margin-top:10px">
        <div class="price-cell entry">
          <div class="lbl">ENTRADA</div>
          <div class="val">${fmtPrice(a.entry)}</div>
        </div>
        <div class="price-cell mark">
          <div class="lbl">MARK &nbsp;<span class="${pnlClass(a.roi_entry_pct)}">${esc(chgStr)}</span></div>
          <div class="val">${fmtPrice(a.mark)}</div>
        </div>
        <div class="price-cell sl">
          <div class="lbl">STOP LOSS</div>
          <div class="val">${fmtPrice(a.sl) || '—'}</div>
        </div>
        <div class="price-cell tp">
          <div class="lbl">TAKE PROFIT</div>
          <div class="val">${fmtPrice(a.tp) || '—'}</div>
        </div>
      </div>
      ${buildProgressBar(a)}
      <div class="pos-metrics">
        <div class="pnl-cell">
          <div class="lbl">EN SL <span style="color:var(--text-sub);font-size:8px">(${fmtPrice(a.sl)})</span></div>
          <div class="val ${pnlClass(netAtSL)}">${fmtMoney(netAtSL)}</div>
          <div class="sub">neto · fees incl.</div>
        </div>
        <div class="pnl-cell">
          <div class="lbl">EN TP <span style="color:var(--text-sub);font-size:8px">(${fmtPrice(a.tp)})</span></div>
          <div class="val ${pnlClass(netAtTP)}">${fmtMoney(netAtTP)}</div>
          <div class="sub">neto · fees incl.</div>
        </div>
      </div>
    </div>
    <div class="ana-actions">
      <button class="ana-btn danger" data-del="${esc(a.id)}">✕ Eliminar</button>
      <button class="ana-btn promote" data-promote="${esc(a.id)}"
        data-sym="${esc(a.symbol)}" data-dir="${esc(a.side)}"
        data-entry="${a.entry}" data-sl="${a.sl}" data-tp="${a.tp}"
        data-size="${a.size_usdt}" data-lev="${a.leverage}">
        → Convertir a real
      </button>
    </div>
  </div>`;
}

function renderAnalyses(analyses) {
  const n   = analyses ? analyses.length : 0;
  const c   = document.getElementById('analyses-container');
  const acn = document.getElementById('ana-count');
  if (acn) acn.textContent = n > 0 ? String(n) : '';

  if (!c) return;
  if (!n) { c.innerHTML = '<div class="empty-state">Sin análisis guardados</div>'; return; }

  c.innerHTML = analyses.map(buildAnalysisCard).join('');

  // Wire delete
  c.querySelectorAll('[data-del]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const aid = btn.dataset.del;
      await fetch(`/api/analyses/${aid}`, { method: 'DELETE' });
      // WS snapshot se actualizará solo — no necesitamos re-renderizar aquí
    });
  });

  // Wire promote → open trade panel in Manual mode pre-filled
  c.querySelectorAll('[data-promote]').forEach(btn => {
    btn.addEventListener('click', () => {
      openTradePanel();
      _tpMode = 'manual';
      document.querySelectorAll('.trade-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === 'manual'));
      document.getElementById('trade-manual').style.display   = 'flex';
      document.getElementById('trade-analysis').style.display = 'none';
      const lbl = btn.dataset.sym.replace('USDT','');
      const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v ?? ''; };
      setVal('tm-symbol', lbl);
      setVal('tm-sl',     btn.dataset.sl);
      setVal('tm-tp',     btn.dataset.tp);
      setVal('tm-size',   btn.dataset.size);
      setVal('tm-leverage', btn.dataset.lev);
      setVal('tm-lev-range', btn.dataset.lev);
      _tmDir = btn.dataset.dir;
      document.querySelectorAll('#trade-manual .trade-dir-btn').forEach(b => b.classList.toggle('active', b.dataset.dir === _tmDir));
      _updateTradePreview();
    });
  });

  // Update side-tab badge
  const sb  = document.getElementById('side-pos-count');
  const pcn = parseInt(document.getElementById('pos-count')?.textContent || '0', 10);
  if (sb) sb.textContent = (pcn + n) > 0 ? String(pcn + n) : '';
}

// ── Render PnL por símbolo ────────────────────────────────────────────────────

function renderSymbolPnl(items) {
  const c = document.getElementById('symbol-pnl-container');
  if (!items || items.length === 0) {
    c.innerHTML = '<span class="c-dim" style="font-size:11px">Sin trades cerrados hoy</span>';
    return;
  }
  c.innerHTML = items.map(item => `
    <div class="sym-pnl-chip">
      <span class="sym-name">${esc(item.symbol)}</span>
      <span class="sym-val ${pnlClass(item.pnl)}">${fmtMoney(item.pnl)}</span>
    </div>`).join('');
}

// ── MXN rate label ────────────────────────────────────────────────────────────

function renderMxnLabel(rate) {
  const el  = document.getElementById('mxn-rate-lbl');
  const el2 = document.getElementById('cfg-mxn-rate');
  if (el)  el.textContent  = `1 USD = ${rate.toFixed(2)} MXN`;
  if (el2) el2.textContent = `1 USD = ${rate.toFixed(2)} MXN`;
}

// ── Historial de trades ───────────────────────────────────────────────────────

let _historyLoaded = false;

function fmtTs(ms) {
  if (!ms) return '—';
  const d = new Date(ms);
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ── Historia: estado global ───────────────────────────────────────────────────
let _historyData    = [];          // todos los trades cargados
let _histExpanded   = new Set();   // ids de cards expandidas
let _histAICache    = new Map();   // id → analysis result
let _histAILoading  = new Set();   // ids con análisis en curso
let _histFilterDir  = 'all';       // 'all' | 'long' | 'short'
let _histFilterRes  = 'all';       // 'all' | 'win' | 'loss'
let _histFilterSym  = '';          // texto de búsqueda

function _histId(t) {
  return `${t.full_sym}_${t.open_ts}_${t.close_ts}`;
}

function _histFiltered() {
  return _historyData.filter(t => {
    if (_histFilterDir === 'long'  && t.direction !== 'LONG')  return false;
    if (_histFilterDir === 'short' && t.direction !== 'SHORT') return false;
    if (_histFilterRes === 'win'   && t.closed_pnl <= 0)       return false;
    if (_histFilterRes === 'loss'  && t.closed_pnl > 0)        return false;
    if (_histFilterSym && !t.symbol.toLowerCase().includes(_histFilterSym.toLowerCase())) return false;
    return true;
  });
}

function _renderHistDashboard(trades) {
  if (!trades.length) return;
  const wins   = trades.filter(t => t.closed_pnl > 0);
  const losses = trades.filter(t => t.closed_pnl <= 0);
  const total  = trades.reduce((s, t) => s + t.closed_pnl, 0);
  const best   = Math.max(...trades.map(t => t.closed_pnl));
  const worst  = Math.min(...trades.map(t => t.closed_pnl));
  const wr     = trades.length ? (wins.length / trades.length * 100).toFixed(0) : 0;

  document.getElementById('hstat-total').textContent  = fmtMoney(total);
  document.getElementById('hstat-total').className    = 'hist-stat-val ' + pnlClass(total);
  document.getElementById('hstat-wins').textContent   = `${wins.length}`;
  document.getElementById('hstat-losses').textContent = `${losses.length}`;
  document.getElementById('hstat-wr').textContent     = `${wr}%`;
  document.getElementById('hstat-wr').className       = 'hist-stat-val ' + (wr >= 50 ? 'c-green' : 'c-red');
  document.getElementById('hstat-best').textContent   = fmtMoney(best);
  document.getElementById('hstat-worst').textContent  = fmtMoney(worst);
  document.getElementById('hist-dashboard').style.display = 'grid';
  document.getElementById('hist-filters').style.display   = 'flex';
}

function buildHistoryCard(t) {
  const isLong   = t.direction === 'LONG';
  const dirCls   = isLong ? 'long' : 'short';
  const pnlCls   = pnlClass(t.closed_pnl);
  const roi      = t.entry_price > 0
    ? (t.exit_price - t.entry_price) / t.entry_price * 100 * (isLong ? 1 : -1) * t.leverage
    : 0;
  const id       = _histId(t);
  const expanded = _histExpanded.has(id);

  // Panel expandido
  let detailHtml = '';
  if (expanded) {
    const gross   = (t.exit_price - t.entry_price) * t.qty * (isLong ? 1 : -1);
    const aiRes   = _histAICache.get(id);
    const aiLoading = _histAILoading.has(id);

    let aiBlock = '';
    if (aiRes) {
      const v = aiRes;
      const scoreColor = v.score >= 7 ? 'var(--green)' : v.score >= 4 ? 'var(--orange)' : 'var(--red)';
      const fmtList = arr => (arr || []).map(s => `<li>${esc(s)}</li>`).join('');
      aiBlock = `<div class="hist-ai-result">
        <div class="hist-ai-score" style="border-color:${scoreColor};color:${scoreColor}">${v.score||'?'}</div>
        <div class="hist-ai-section">
          <div class="hist-ai-section-title">Resumen</div>
          <div>${esc(v.resumen || '')}</div>
        </div>
        ${v.fortalezas?.length ? `<div class="hist-ai-section">
          <div class="hist-ai-section-title">Fortalezas</div>
          <ul class="hist-ai-list">${fmtList(v.fortalezas)}</ul></div>` : ''}
        ${v.debilidades?.length ? `<div class="hist-ai-section">
          <div class="hist-ai-section-title">Debilidades</div>
          <ul class="hist-ai-list">${fmtList(v.debilidades)}</ul></div>` : ''}
        ${v.lecciones?.length ? `<div class="hist-ai-section">
          <div class="hist-ai-section-title">Lecciones</div>
          <ul class="hist-ai-list">${fmtList(v.lecciones)}</ul></div>` : ''}
        ${v.patron ? `<div class="hist-ai-patron">Patrón detectado: ${esc(v.patron)}</div>` : ''}
      </div>`;
    }

    detailHtml = `<div class="hist-detail-panel">
      <div class="hist-detail-grid">
        <div class="hist-detail-cell">
          <div class="lbl">PnL BRUTO</div>
          <div class="val ${pnlClass(gross)}">${fmtMoney(gross)}</div>
        </div>
        <div class="hist-detail-cell">
          <div class="lbl">FEES TOTALES</div>
          <div class="val c-red">−${fmtMoney(t.total_fees || 0)}</div>
        </div>
        <div class="hist-detail-cell">
          <div class="lbl">PnL NETO</div>
          <div class="val ${pnlCls}">${fmtMoney(t.closed_pnl)}</div>
        </div>
        <div class="hist-detail-cell">
          <div class="lbl">CONTRATOS</div>
          <div class="val">${t.qty}</div>
        </div>
        <div class="hist-detail-cell">
          <div class="lbl">APERTURA</div>
          <div class="val">${fmtTs(t.open_ts)}</div>
        </div>
        <div class="hist-detail-cell">
          <div class="lbl">CIERRE</div>
          <div class="val">${fmtTs(t.close_ts)}</div>
        </div>
      </div>
      <button class="hist-ai-btn" ${aiLoading ? 'disabled' : ''}
        data-analyze-trade="${esc(id)}"
        data-trade-idx="${_historyData.indexOf(t)}">
        ${aiLoading ? '⏳ Analizando…' : aiRes ? '↺ Re-analizar con IA' : '✦ Analizar con IA'}
      </button>
      ${aiBlock}
    </div>`;
  }

  return `
  <div class="hist-card ${expanded ? 'expanded' : ''}" data-hist-toggle="${esc(id)}">
    <div class="hist-card-summary">
      <div class="hist-header">
        <span class="pos-symbol">${esc(t.symbol)}</span>
        <span class="pos-dir ${dirCls}">${t.direction}</span>
        <span class="pos-leverage">${t.leverage}x</span>
        <span class="hist-pnl ${pnlCls}">${fmtMoney(t.closed_pnl)}</span>
        <span class="hist-expand-icon">${expanded ? '▲' : '▼'}</span>
      </div>
      <div class="hist-prices">
        <div class="hist-price-pair">
          <span class="lbl">ENTRADA</span>
          <span class="val">${fmtPrice(t.entry_price)}</span>
        </div>
        <div class="hist-arrow">→</div>
        <div class="hist-price-pair">
          <span class="lbl">SALIDA</span>
          <span class="val">${fmtPrice(t.exit_price)}</span>
        </div>
        <div class="hist-roi ${pnlCls}">${roi >= 0 ? '+' : ''}${roi.toFixed(2)}% ROI</div>
      </div>
      <div class="hist-meta">
        <span>📅 ${fmtTs(t.open_ts)}</span>
        <span>⏱ ${esc(t.duration_fmt || '—')}</span>
        <span>${t.qty} @ ${t.leverage}x</span>
      </div>
    </div>
    ${detailHtml}
  </div>`;
}

function _renderHistoryList() {
  const c      = document.getElementById('history-container');
  const trades = _histFiltered();
  if (!trades.length) {
    c.innerHTML = '<div class="empty-state">Sin trades que coincidan con los filtros</div>';
    return;
  }
  c.innerHTML = trades.map(buildHistoryCard).join('');
}

async function _analyzeTradeWithAI(id, tradeIdx) {
  const t = _historyData[tradeIdx];
  if (!t) return;
  _histAILoading.add(id);
  _renderHistoryList();
  try {
    // Intentar obtener velas del período del trade
    let klines = [];
    try {
      const elapsed = t.duration_s || 0;
      const tf = elapsed < 7200 ? '5' : elapsed < 43200 ? '15' : '60';
      const kr = await fetch(`/api/klines/${t.full_sym}?tf=${tf}&limit=60`);
      const kd = await kr.json();
      klines = kd.klines || [];
    } catch (_) {}

    const res  = await fetch('/api/trade-analysis', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ trade: t, klines }),
    });
    const data = await res.json();
    if (data.ok && data.analysis) {
      _histAICache.set(id, data.analysis);
    } else {
      _histAICache.set(id, { resumen: data.error || 'Error al analizar', score: null });
    }
  } catch (e) {
    _histAICache.set(id, { resumen: String(e), score: null });
  }
  _histAILoading.delete(id);
  _renderHistoryList();
}

async function loadHistory() {
  const c = document.getElementById('history-container');
  c.innerHTML = '<div class="empty-state">Cargando…</div>';
  try {
    const res  = await fetch('/api/history?limit=100');
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    if (!data.history || data.history.length === 0) {
      c.innerHTML = '<div class="empty-state">Sin trades cerrados recientes</div>';
      return;
    }
    _historyData   = data.history;
    _histExpanded  = new Set();
    _histAICache   = new Map();
    _histAILoading = new Set();
    _renderHistDashboard(_historyData);
    _renderHistoryList();
    _historyLoaded = true;
  } catch (e) {
    c.innerHTML = `<div class="empty-state c-red">Error: ${esc(String(e))}</div>`;
  }
}

// ── Event delegation para historial ──────────────────────────────────────────
document.getElementById('history-container')?.addEventListener('click', async e => {
  // Toggle expand card
  const card = e.target.closest('[data-hist-toggle]');
  const analyzeBtn = e.target.closest('[data-analyze-trade]');

  if (analyzeBtn) {
    e.stopPropagation();
    const id  = analyzeBtn.dataset.analyzeTrade;
    const idx = parseInt(analyzeBtn.dataset.tradeIdx, 10);
    if (!_histAILoading.has(id)) await _analyzeTradeWithAI(id, idx);
    return;
  }

  if (card) {
    const id = card.dataset.histToggle;
    if (_histExpanded.has(id)) _histExpanded.delete(id);
    else _histExpanded.add(id);
    _renderHistoryList();
  }
});

// Filtros
document.getElementById('hist-filters')?.addEventListener('click', e => {
  const dirBtn = e.target.closest('[data-filter-dir]');
  if (dirBtn) {
    _histFilterDir = dirBtn.dataset.filterDir;
    document.querySelectorAll('[data-filter-dir]').forEach(b => b.classList.toggle('active', b === dirBtn));
    _renderHistoryList();
  }
  const resBtn = e.target.closest('[data-filter-result]');
  if (resBtn) {
    _histFilterRes = resBtn.dataset.filterResult;
    document.querySelectorAll('[data-filter-result]').forEach(b => b.classList.toggle('active', b === resBtn));
    _renderHistoryList();
  }
});

document.getElementById('hist-search')?.addEventListener('input', e => {
  _histFilterSym = e.target.value.trim();
  _renderHistoryList();
});

// ── Pro / Lite switch ─────────────────────────────────────────────────────────

function applyProModeCfg() {
  const btn  = document.getElementById('btn-promode-cfg');
  const desc = document.getElementById('cfg-mode-desc');
  if (btn)  btn.textContent  = _proMode ? 'PRO' : 'LITE';
  if (desc) desc.textContent = _proMode ? 'Datos técnicos completos' : 'Vista simplificada e intuitiva';
  localStorage.setItem('qts_pro_mode', _proMode);
  if (lastSnap) renderPositions(lastSnap.positions || []);
}

document.getElementById('btn-promode-cfg')?.addEventListener('click', () => {
  _proMode = !_proMode;
  applyProModeCfg();
});

applyProModeCfg();

// ── Event delegation: botones de acción Lite (sobreviven re-renders) ──────────

// ── Zoom telescópico: handlers (botones + select de ancla + teclado) ─────────

let _hoveredPosKey = null;   // se actualiza con mouseover/out sobre cards

function _applyZoomAction(key, act) {
  const z = _getZoom(key);
  if (act === 'inc')        _setZoom(key, { levelIdx: QtsScale.clampIdx(z.levelIdx + 1) });
  else if (act === 'dec')   _setZoom(key, { levelIdx: QtsScale.clampIdx(z.levelIdx - 1) });
  else if (act === 'reset') _setZoom(key, { levelIdx: 0, anchor: 'mark' });
  if (lastSnap) renderPositions(lastSnap.positions || []);
}

document.getElementById('positions-container')?.addEventListener('change', e => {
  const sel = e.target.closest('select[data-zoom-act="anchor"]');
  if (!sel) return;
  const bar = sel.closest('[data-zoom-key]');
  if (!bar) return;
  _setZoom(bar.dataset.zoomKey, { anchor: sel.value });
  if (lastSnap) renderPositions(lastSnap.positions || []);
});

document.getElementById('positions-container')?.addEventListener('mouseover', e => {
  const card = e.target.closest('[data-pos-key]');
  _hoveredPosKey = card ? card.dataset.posKey : null;
});
document.getElementById('positions-container')?.addEventListener('mouseleave', () => {
  _hoveredPosKey = null;
});

window.addEventListener('keydown', e => {
  // Ignorar si se está escribiendo en un input/textarea/select
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (/INPUT|TEXTAREA|SELECT/.test(tag)) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (!_hoveredPosKey) return;
  if (e.key === '+' || e.key === '=') { e.preventDefault(); _applyZoomAction(_hoveredPosKey, 'inc'); }
  else if (e.key === '-' || e.key === '_') { e.preventDefault(); _applyZoomAction(_hoveredPosKey, 'dec'); }
  else if (e.key === '0') { e.preventDefault(); _applyZoomAction(_hoveredPosKey, 'reset'); }
});

document.getElementById('positions-container')?.addEventListener('click', async e => {
  // Zoom: −/+/reset
  const zoomBtn = e.target.closest('button[data-zoom-act]');
  if (zoomBtn) {
    const bar = zoomBtn.closest('[data-zoom-key]');
    if (bar) _applyZoomAction(bar.dataset.zoomKey, zoomBtn.dataset.zoomAct);
    return;
  }

  // Toggle detalles Pro (abrir → tab detalles por defecto; cerrar)
  const toggleBtn = e.target.closest('[data-toggle-details]');
  if (toggleBtn) {
    const key = toggleBtn.dataset.toggleDetails;
    if (_proDetailState.has(key)) _proDetailState.delete(key);
    else _proDetailState.set(key, 'detalles');
    if (lastSnap) renderPositions(lastSnap.positions || []);
    return;
  }

  // Cambiar tab (Detalles / Salud)
  const tabBtn = e.target.closest('[data-detail-tab]');
  if (tabBtn) {
    const key = tabBtn.dataset.posKey;
    const tab = tabBtn.dataset.detailTab;
    _proDetailState.set(key, tab);
    if (tab === 'salud') _fetchKlines(tabBtn.dataset.sym, parseInt(tabBtn.dataset.elapsed || '0', 10));
    if (lastSnap) renderPositions(lastSnap.positions || []);
    return;
  }

  // Reload klines manualmente
  const reloadBtn = e.target.closest('[data-reload-klines]');
  if (reloadBtn) {
    const sym     = reloadBtn.dataset.reloadKlines;
    const elapsed = parseInt(reloadBtn.dataset.elapsed || '0', 10);
    const tf      = _klineTf(elapsed);
    _klineCache.delete(`${sym}_${tf}`);   // forzar re-fetch
    _fetchKlines(sym, elapsed);
    return;
  }

  // Cerrar — primer clic: pedir confirmación
  const closeBtn = e.target.closest('[data-close]');
  if (closeBtn) {
    _pendingClose.add(closeBtn.dataset.close);
    if (lastSnap) renderPositions(lastSnap.positions || []);
    return;
  }

  // Cancelar cierre
  const cancelBtn = e.target.closest('[data-cancel-close]');
  if (cancelBtn) {
    _pendingClose.delete(cancelBtn.dataset.cancelClose);
    if (lastSnap) renderPositions(lastSnap.positions || []);
    return;
  }

  // Confirmar cierre → llamada al servidor
  const confirmBtn = e.target.closest('[data-confirm-close]');
  if (confirmBtn) {
    const key  = confirmBtn.dataset.confirmClose;
    const sym  = confirmBtn.dataset.sym;
    const side = confirmBtn.dataset.side;
    if (_closingPos.has(key)) return;   // ya hay una llamada en vuelo
    _pendingClose.delete(key);
    _closingPos.add(key);
    if (lastSnap) renderPositions(lastSnap.positions || []);   // muestra "Cerrando…"
    try {
      const res  = await fetch(`/api/close/${sym}/${side}`, { method: 'POST' });
      const data = await res.json();
      if (!data.success) {
        alert(`No se pudo cerrar: ${data.error}`);
        _closingPos.delete(key);
        if (lastSnap) renderPositions(lastSnap.positions || []);
      }
      // Éxito: el WS + refresh server eliminarán la posición del snapshot pronto
    } catch (err) {
      alert(`Error de red: ${err}`);
      _closingPos.delete(key);
      if (lastSnap) renderPositions(lastSnap.positions || []);
    }
    return;
  }

  // Mover SL al marcador detectado
  const moveSLBtn = e.target.closest('[data-move-sl]');
  if (moveSLBtn) {
    const sym   = moveSLBtn.dataset.sym;
    const side  = moveSLBtn.dataset.side;
    const newSL = parseFloat(moveSLBtn.dataset.newSl);
    const lbl   = moveSLBtn.dataset.lbl;
    moveSLBtn.disabled = true;
    moveSLBtn.textContent = '…';
    try {
      const res  = await fetch('/api/move-sl', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ symbol: sym, side, new_sl: newSL }),
      });
      const data = await res.json();
      if (!data.success) alert(`No se pudo mover SL: ${data.error}`);
    } catch (err) {
      alert(`Error: ${err}`);
    }
    // El WS actualizará el SL en ~1 segundo
    return;
  }
});

// ── Render completo ───────────────────────────────────────────────────────────

function render(data) {
  renderAccount(data.account);
  renderPositions(data.positions);
  renderAnalyses(data.analyses || []);
  renderSymbolPnl(data.symbol_pnl);

  if (data.mxn_rate && data.mxn_rate > 1) {
    _mxnRate = data.mxn_rate;
    localStorage.setItem('qts_mxn_rate', _mxnRate);
    renderMxnLabel(_mxnRate);
  }

  // Config tab: estado cuenta
  const cfgConn  = document.getElementById('cfg-conn-badge');
  const cfgWal   = document.getElementById('cfg-wallet');
  const cfgUpd   = document.getElementById('cfg-last-update');
  if (cfgConn) {
    cfgConn.className   = data.account.connected ? 'badge badge-ok' : 'badge badge-connecting';
    cfgConn.textContent = data.account.connected ? '● EN VIVO' : '● CONECTANDO';
  }
  if (cfgWal)  cfgWal.textContent  = fmtMoneyAbs(data.account.wallet_balance ?? data.account.equity);
  if (cfgUpd) {
    const ts = new Date(data.ts);
    const p  = n => String(n).padStart(2, '0');
    cfgUpd.textContent = `${p(ts.getHours())}:${p(ts.getMinutes())}:${p(ts.getSeconds())}`;
  }

  const luEl = document.getElementById('last-update');
  if (luEl) {
    const ts = new Date(data.ts);
    const p  = n => String(n).padStart(2, '0');
    luEl.textContent = `actualizado ${p(ts.getHours())}:${p(ts.getMinutes())}:${p(ts.getSeconds())}`;
  }
}

// ── Tab router ────────────────────────────────────────────────────────────────

let _activeTab = localStorage.getItem('qts_tab') || 'dashboard';

function switchTab(name) {
  _activeTab = name;
  localStorage.setItem('qts_tab', name);

  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  // Sincroniza ambos tipos de botones (sidenav + mobile-nav)
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.remove('active'));

  const view = document.getElementById(`view-${name}`);
  if (view) view.classList.add('active');

  document.querySelectorAll(`[data-tab="${name}"]`).forEach(b => b.classList.add('active'));

  if (name === 'historial' && !_historyLoaded) loadHistory();
}

document.querySelectorAll('[data-tab]').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

document.getElementById('btn-refresh-history')?.addEventListener('click', () => {
  _historyLoaded = false;
  loadHistory();
});

switchTab(_activeTab);

// ── Trades sub-tabs (Posiciones / Análisis) ───────────────────────────────────

let _activeSub = 'positions';

function switchSubTab(name) {
  _activeSub = name;
  document.querySelectorAll('.trades-sub-btn').forEach(b => b.classList.toggle('active', b.dataset.sub === name));
  document.getElementById('positions-container').style.display = name === 'positions' ? '' : 'none';
  document.getElementById('analyses-container').style.display  = name === 'analyses'  ? '' : 'none';
}

document.querySelectorAll('.trades-sub-btn').forEach(btn => {
  btn.addEventListener('click', () => switchSubTab(btn.dataset.sub));
});

// ── Config tab: controles ─────────────────────────────────────────────────────

function applyCurrencyCfg() {
  const btn    = document.getElementById('btn-currency-cfg');
  const btnTop = document.getElementById('btn-currency');
  const lbl    = document.getElementById('mxn-rate-lbl');
  const label  = _showMxn ? 'MXN' : 'USD';
  if (btn) { btn.textContent = label; btn.classList.toggle('active', _showMxn); }
  if (btnTop) { btnTop.textContent = label; btnTop.classList.toggle('active', _showMxn); }
  if (lbl) lbl.style.display = _showMxn ? 'inline' : 'none';
  localStorage.setItem('qts_mxn', _showMxn);
}

function applyThemeCfg() {
  document.documentElement.setAttribute('data-theme', _theme);
  const btnTop = document.getElementById('btn-theme');
  const btnCfg = document.getElementById('btn-theme-cfg');
  const label  = _theme === 'dark' ? 'OSCURO' : 'CLARO';
  if (btnTop) btnTop.textContent = _theme === 'dark' ? '☀' : '☾';
  if (btnCfg) btnCfg.textContent = label;
  localStorage.setItem('qts_theme', _theme);
}

document.getElementById('btn-theme-cfg')?.addEventListener('click', () => {
  _theme = _theme === 'dark' ? 'light' : 'dark';
  applyThemeCfg();
  if (lastSnap) render(lastSnap);
});

document.getElementById('btn-currency-cfg')?.addEventListener('click', () => {
  _showMxn = !_showMxn;
  applyCurrencyCfg();
  if (lastSnap) render(lastSnap);
});

// Topbar desktop buttons
document.getElementById('btn-theme')?.addEventListener('click', () => {
  _theme = _theme === 'dark' ? 'light' : 'dark';
  applyThemeCfg();
  if (lastSnap) render(lastSnap);
});
document.getElementById('btn-currency')?.addEventListener('click', () => {
  _showMxn = !_showMxn;
  applyCurrencyCfg();
  if (lastSnap) render(lastSnap);
});

applyThemeCfg();
applyCurrencyCfg();

// ── Clock (sidenav desktop + mobile-topstrip) ─────────────────────────────────
function tickClock() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  const t = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  ['clock', 'clock-mob'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = t;
  });
}
setInterval(tickClock, 1000);
tickClock();

// ── WebSocket ─────────────────────────────────────────────────────────────────

let ws = null;
let reconnectTimer = null;

function connect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onmessage = (e) => {
    try {
      lastSnap = JSON.parse(e.data);
      render(lastSnap);
    } catch { /* ignore */ }
  };

  ws.onclose = () => {
    const b = document.getElementById('conn-badge');
    if (b) { b.className = 'badge badge-connecting'; b.textContent = '● RECONECTANDO'; }
    reconnectTimer = setTimeout(connect, 2000);
  };
}

setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send('ping');
}, 15000);

connect();

// ── Trade Panel ───────────────────────────────────────────────────────────────

// State
let _tpOpen      = false;
let _tpMode      = 'manual';      // 'manual' | 'analysis'
let _tmDir       = 'Buy';
let _tmType      = 'Market';
let _taDir       = 'Buy';
let _liveMarks   = {};            // sym → price from WS snapshot

// ── Open / close ─────────────────────────────────────────────────────────────

function openTradePanel() {
  _tpOpen = true;
  document.getElementById('trade-overlay').classList.add('open');
  document.getElementById('trade-panel').classList.add('open');
  _loadSymbolList();
  _updateTradePreview();
}

function closeTradePanel() {
  _tpOpen = false;
  document.getElementById('trade-overlay').classList.remove('open');
  document.getElementById('trade-panel').classList.remove('open');
}

document.getElementById('trade-overlay')?.addEventListener('click', closeTradePanel);
document.getElementById('trade-panel-close')?.addEventListener('click', closeTradePanel);
document.getElementById('btn-open-trade')?.addEventListener('click', openTradePanel);
document.getElementById('btn-open-trade-fab')?.addEventListener('click', openTradePanel);

// ── Mode toggle ───────────────────────────────────────────────────────────────

document.querySelectorAll('.trade-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    _tpMode = btn.dataset.mode;
    document.querySelectorAll('.trade-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === _tpMode));
    document.getElementById('trade-manual').style.display   = _tpMode === 'manual'   ? 'flex' : 'none';
    document.getElementById('trade-analysis').style.display = _tpMode === 'analysis' ? 'flex' : 'none';
    if (_tpMode === 'analysis') _updateAnalysisPreview();
  });
});

// ── Symbol list from /api/symbols ────────────────────────────────────────────

async function _loadSymbolList() {
  try {
    const res  = await fetch('/api/symbols');
    const data = await res.json();
    const dl   = document.getElementById('trade-sym-list');
    if (!dl) return;
    dl.innerHTML = (data.symbols || []).map(s =>
      `<option value="${esc(s.label)}" data-full="${esc(s.symbol)}" label="${esc(s.label)} ${fmtPrice(s.mark)} (score ${s.score})">`
    ).join('');
    // Update live marks cache
    (data.symbols || []).forEach(s => { _liveMarks[s.symbol] = s.mark; });
  } catch { /* silent */ }
}

// ── Mark price updates from WS ────────────────────────────────────────────────

function _updateMarkFromSnap(snap) {
  if (snap.marks) Object.assign(_liveMarks, snap.marks);
  if (!_tpOpen) return;
  if (_tpMode === 'manual')   _updateMarkLabelManual();
  if (_tpMode === 'analysis') _updateMarkLabelAnalysis();
}

function _symFull(label) {
  // Convert label ("BTC") to full symbol ("BTCUSDT")
  if (!label) return '';
  const upper = label.toUpperCase().trim();
  return upper.endsWith('USDT') ? upper : upper + 'USDT';
}

function _getMark(label) {
  return _liveMarks[_symFull(label)] || 0;
}

function _updateMarkLabelManual() {
  const sym  = document.getElementById('tm-symbol')?.value || '';
  const mark = _getMark(sym);
  const el   = document.getElementById('tm-mark');
  if (el) el.textContent = mark ? fmtPrice(mark) : '—';
}

function _updateMarkLabelAnalysis() {
  const sym  = document.getElementById('ta-symbol')?.value || '';
  const mark = _getMark(sym);
  const el   = document.getElementById('ta-mark');
  if (el) el.textContent = mark ? fmtPrice(mark) : '—';
}

// ── Direction toggles ─────────────────────────────────────────────────────────

document.querySelectorAll('#trade-manual .trade-dir-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    _tmDir = btn.dataset.dir;
    document.querySelectorAll('#trade-manual .trade-dir-btn').forEach(b => b.classList.toggle('active', b.dataset.dir === _tmDir));
    _updateTradePreview();
  });
});

document.querySelectorAll('#trade-analysis .trade-dir-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    _taDir = btn.dataset.dir;
    document.querySelectorAll('#trade-analysis .trade-dir-btn').forEach(b => b.classList.toggle('active', b.dataset.dir === _taDir));
    _updateAnalysisPreview();
  });
});

// ── Order type toggle ─────────────────────────────────────────────────────────

document.querySelectorAll('.trade-type-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    _tmType = btn.dataset.type;
    document.querySelectorAll('.trade-type-btn').forEach(b => b.classList.toggle('active', b.dataset.type === _tmType));
    const ew = document.getElementById('tm-entry-wrap');
    if (ew) ew.style.display = _tmType === 'Limit' ? 'flex' : 'none';
    _updateTradePreview();
  });
});

// ── Live R:R preview (manual) ─────────────────────────────────────────────────

function _calcRR(entry, sl, tp, dir, size, leverageVal) {
  if (!entry || !sl || !tp || !size) return null;
  const isLong = dir === 'Buy';
  const slDist = isLong ? entry - sl : sl - entry;
  const tpDist = isLong ? tp - entry : entry - tp;
  if (slDist <= 0 || tpDist <= 0) return null;
  const rr     = tpDist / slDist;
  const qty    = size / entry;
  const risk   = slDist * qty;
  const reward = tpDist * qty;
  const fee    = size * 0.00055;
  return { rr, qty, risk, reward, fee };
}

function _updateTradePreview() {
  if (!_tpOpen) return;
  const sym  = document.getElementById('tm-symbol')?.value || '';
  const slv  = parseFloat(document.getElementById('tm-sl')?.value) || 0;
  const tpv  = parseFloat(document.getElementById('tm-tp')?.value) || 0;
  const size = parseFloat(document.getElementById('tm-size')?.value) || 0;
  const lev  = parseFloat(document.getElementById('tm-leverage')?.value) || 10;
  const mark = _getMark(sym);
  const entryRaw = parseFloat(document.getElementById('tm-entry')?.value) || 0;
  const entry = _tmType === 'Limit' && entryRaw > 0 ? entryRaw : mark;

  _updateMarkLabelManual();

  const r = entry > 0 ? _calcRR(entry, slv, tpv, _tmDir, size, lev) : null;
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

  if (r) {
    const rrCls = r.rr >= 2 ? 'c-green' : r.rr >= 1.5 ? 'c-orange' : 'c-red';
    const rrEl  = document.getElementById('tm-rr');
    if (rrEl) { rrEl.textContent = `${r.rr.toFixed(2)}:1`; rrEl.className = rrCls; }
    set('tm-risk',   `−$${r.risk.toFixed(2)}`);
    set('tm-reward', `+$${r.reward.toFixed(2)}`);
    set('tm-qty',    `${r.qty.toFixed(4)} contratos`);
    set('tm-fee',    `~$${r.fee.toFixed(3)}`);
  } else {
    ['tm-rr','tm-risk','tm-reward','tm-qty','tm-fee'].forEach(id => set(id, '—'));
  }

  // Fetch signals if symbol changed
  _loadSignalsManual(sym);
}

let _lastManualSym = '';
async function _loadSignalsManual(rawSym) {
  if (!rawSym) return;
  const sym = _symFull(rawSym);
  if (sym === _lastManualSym) return;
  _lastManualSym = sym;
  try {
    const res  = await fetch(`/api/analyze/${sym}`);
    const data = await res.json();
    const el   = document.getElementById('tm-signals');
    if (el) el.innerHTML = _buildSignalChipsRaw(data);
  } catch { /* silent */ }
}

let _lastAnalysisSym = '';
async function _loadSignalsAnalysis(rawSym) {
  if (!rawSym) return;
  const sym = _symFull(rawSym);
  if (sym === _lastAnalysisSym) return;
  _lastAnalysisSym = sym;
  try {
    const res  = await fetch(`/api/analyze/${sym}`);
    const data = await res.json();
    const el   = document.getElementById('ta-signals');
    if (el) el.innerHTML = _buildSignalChipsRaw(data);
  } catch { /* silent */ }
}

function _buildSignalChipsRaw(d) {
  if (!d || !d.symbol) return '';
  const tdMap  = { UP: 'bull', DOWN: 'bear', NEUTRAL: '' };
  const tdIcon = { UP: '▲', DOWN: '▼', NEUTRAL: '—' };
  const abMap  = { BUY: 'bull', SELL: 'bear', NEUTRAL: '' };
  return `<div class="pos-signals" style="flex-wrap:wrap;gap:4px">
    <span class="sig-chip ${tdMap[d.trend_dir]||''}">${tdIcon[d.trend_dir]||'—'} TREND</span>
    <span class="sig-chip ${abMap[d.ab_side]||''}">ABS ${d.ab_side||'—'}</span>
    <span class="sig-chip">${(d.regime||'UNKNOWN').replace('_',' ')}</span>
    <span class="sig-chip ${d.score>=70?'bull':d.score>=40?'warn':''}">SCORE ${d.score}</span>
    <span class="sig-chip ${d.rsi>=70?'bear':d.rsi<=30?'bull':''}">RSI ${fmt(d.rsi,1)}</span>
    <span class="sig-chip">ATR ${fmtPrice(d.atr)}</span>
  </div>`;
}

// Live preview (analysis)
function _updateAnalysisPreview() {
  if (!_tpOpen) return;
  const sym   = document.getElementById('ta-symbol')?.value || '';
  const entry = parseFloat(document.getElementById('ta-entry')?.value) || 0;
  const slv   = parseFloat(document.getElementById('ta-sl')?.value)    || 0;
  const tpv   = parseFloat(document.getElementById('ta-tp')?.value)    || 0;
  const size  = parseFloat(document.getElementById('ta-size')?.value)  || 0;
  const mark  = _getMark(sym);

  _updateMarkLabelAnalysis();
  _loadSignalsAnalysis(sym);

  const r = entry > 0 ? _calcRR(entry, slv, tpv, _taDir, size, 1) : null;
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

  if (r) {
    const rrCls = r.rr >= 2 ? 'c-green' : r.rr >= 1.5 ? 'c-orange' : 'c-red';
    const rrEl  = document.getElementById('ta-rr');
    if (rrEl) { rrEl.textContent = `${r.rr.toFixed(2)}:1`; rrEl.className = rrCls; }
    set('ta-risk',   `−$${r.risk.toFixed(2)}`);
    set('ta-reward', `+$${r.reward.toFixed(2)}`);
    // Virtual PnL vs mark
    if (mark > 0 && entry > 0) {
      const isLong = _taDir === 'Buy';
      const qty    = size / entry;
      const virPnl = (isLong ? mark - entry : entry - mark) * qty;
      const pEl    = document.getElementById('ta-pnl-mark');
      if (pEl) {
        pEl.textContent = `${virPnl >= 0 ? '+' : ''}$${virPnl.toFixed(2)}`;
        pEl.className   = virPnl >= 0 ? 'c-green' : 'c-red';
      }
    } else {
      set('ta-pnl-mark', '—');
    }
  } else {
    ['ta-rr','ta-risk','ta-reward','ta-pnl-mark'].forEach(id => set(id, '—'));
  }
}

// Wire input listeners
['tm-symbol','tm-sl','tm-tp','tm-size','tm-entry'].forEach(id => {
  document.getElementById(id)?.addEventListener('input', _updateTradePreview);
});
['ta-symbol','ta-sl','ta-tp','ta-size','ta-entry'].forEach(id => {
  document.getElementById(id)?.addEventListener('input', _updateAnalysisPreview);
});

// Leverage slider sync
document.getElementById('tm-leverage')?.addEventListener('input', e => {
  const r = document.getElementById('tm-lev-range');
  if (r) r.value = e.target.value;
  _updateTradePreview();
});
document.getElementById('tm-lev-range')?.addEventListener('input', e => {
  const i = document.getElementById('tm-leverage');
  if (i) i.value = e.target.value;
  _updateTradePreview();
});

// ── Manual: Review → Confirm → Execute ───────────────────────────────────────

document.getElementById('tm-btn-submit')?.addEventListener('click', () => {
  const sym  = document.getElementById('tm-symbol')?.value || '';
  const slv  = parseFloat(document.getElementById('tm-sl')?.value) || 0;
  const tpv  = parseFloat(document.getElementById('tm-tp')?.value) || 0;
  const size = parseFloat(document.getElementById('tm-size')?.value) || 0;
  const lev  = parseFloat(document.getElementById('tm-leverage')?.value) || 10;
  const mark = _getMark(sym);
  const entryRaw = parseFloat(document.getElementById('tm-entry')?.value) || 0;
  const entry = _tmType === 'Limit' && entryRaw > 0 ? entryRaw : mark;

  if (!sym || !slv || !tpv || !size) {
    alert('Completa símbolo, SL, TP y tamaño.');
    return;
  }

  const r = _calcRR(entry, slv, tpv, _tmDir, size, lev);
  if (!r) { alert('Verifica los precios de SL/TP vs entrada.'); return; }

  const dirLbl = _tmDir === 'Buy' ? '▲ LONG' : '▼ SHORT';
  const rows = [
    `<div><span class="c-dim">Símbolo:</span> <strong>${esc(_symFull(sym))}</strong></div>`,
    `<div><span class="c-dim">Dirección:</span> <strong class="${_tmDir==='Buy'?'c-green':'c-red'}">${dirLbl}</strong></div>`,
    `<div><span class="c-dim">Tipo:</span> ${esc(_tmType)}</div>`,
    _tmType === 'Limit' ? `<div><span class="c-dim">Precio límite:</span> ${fmtPrice(entry)}</div>` : '',
    `<div><span class="c-dim">SL:</span> <span class="c-red">${fmtPrice(slv)}</span></div>`,
    `<div><span class="c-dim">TP:</span> <span class="c-green">${fmtPrice(tpv)}</span></div>`,
    `<div><span class="c-dim">Tamaño:</span> $${size.toFixed(2)} USDT</div>`,
    `<div><span class="c-dim">Qty est.:</span> ${r.qty.toFixed(4)} contratos</div>`,
    `<div><span class="c-dim">R:R:</span> <strong>${r.rr.toFixed(2)}:1</strong></div>`,
    `<div><span class="c-dim">Riesgo máx.:</span> <span class="c-red">−$${r.risk.toFixed(2)}</span></div>`,
    `<div><span class="c-dim">Objetivo:</span> <span class="c-green">+$${r.reward.toFixed(2)}</span></div>`,
  ].filter(Boolean).join('');

  document.getElementById('tm-confirm-body').innerHTML = rows;
  document.getElementById('tm-confirm').style.display  = 'block';
  document.getElementById('tm-footer').style.display   = 'none';
  document.getElementById('tm-result').style.display   = 'none';
});

document.getElementById('tm-btn-cancel')?.addEventListener('click', () => {
  document.getElementById('tm-confirm').style.display = 'none';
  document.getElementById('tm-footer').style.display  = '';
});

document.getElementById('tm-btn-confirm')?.addEventListener('click', async () => {
  const btn  = document.getElementById('tm-btn-confirm');
  btn.disabled = true;
  btn.textContent = 'ENVIANDO…';

  const sym  = document.getElementById('tm-symbol')?.value || '';
  const slv  = parseFloat(document.getElementById('tm-sl')?.value) || 0;
  const tpv  = parseFloat(document.getElementById('tm-tp')?.value) || 0;
  const size = parseFloat(document.getElementById('tm-size')?.value) || 0;
  const lev  = parseFloat(document.getElementById('tm-leverage')?.value) || 10;
  const entryRaw = parseFloat(document.getElementById('tm-entry')?.value) || 0;

  const body = {
    symbol:     _symFull(sym),
    side:       _tmDir,
    order_type: _tmType,
    entry:      entryRaw,
    sl:         slv,
    tp:         tpv,
    size_usdt:  size,
    leverage:   lev,
  };

  try {
    const res  = await fetch('/api/trade', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const data = await res.json();
    const rEl  = document.getElementById('tm-result');
    rEl.style.display = 'block';
    if (data.success) {
      rEl.className   = 'trade-result ok';
      rEl.textContent = `✓ Orden enviada — ID: ${data.order_id} · qty ${data.qty}`;
      document.getElementById('tm-confirm').style.display = 'none';
    } else {
      rEl.className   = 'trade-result err';
      rEl.textContent = `✗ Error: ${data.error}`;
      document.getElementById('tm-footer').style.display = '';
    }
  } catch (e) {
    const rEl = document.getElementById('tm-result');
    rEl.style.display = 'block';
    rEl.className     = 'trade-result err';
    rEl.textContent   = `✗ ${String(e)}`;
    document.getElementById('tm-footer').style.display = '';
  } finally {
    btn.disabled    = false;
    btn.textContent = 'EJECUTAR';
  }
});

document.getElementById('ta-btn-save')?.addEventListener('click', async () => {
  const sym   = document.getElementById('ta-symbol')?.value?.trim() || '';
  const entry = parseFloat(document.getElementById('ta-entry')?.value) || 0;
  const slv   = parseFloat(document.getElementById('ta-sl')?.value) || 0;
  const tpv   = parseFloat(document.getElementById('ta-tp')?.value) || 0;
  const size  = parseFloat(document.getElementById('ta-size')?.value) || 0;
  const notes = document.getElementById('ta-notes')?.value?.trim() || '';
  const lev   = parseInt(document.getElementById('tm-leverage')?.value || '10');

  if (!sym || !entry || !slv || !tpv) { alert('Completa símbolo, entrada, SL y TP.'); return; }

  const btn = document.getElementById('ta-btn-save');
  btn.disabled = true;
  try {
    const res  = await fetch('/api/analyses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol: _symFull(sym), direction: _taDir,
        entry, sl: slv, tp: tpv, size, leverage: lev, notes,
      }),
    });
    const data = await res.json();
    if (!data.success) throw new Error(data.error || 'error');
  } catch (e) {
    alert(`Error al guardar: ${e}`);
    return;
  } finally {
    btn.disabled = false;
  }

  // Clear form
  ['ta-symbol','ta-entry','ta-sl','ta-tp','ta-size','ta-notes'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  ['ta-rr','ta-risk','ta-reward','ta-pnl-mark'].forEach(id => {
    const el = document.getElementById(id); if (el) el.textContent = '—';
  });
  _lastAnalysisSym = '';
  document.getElementById('ta-signals').innerHTML = '';
  // El WS snapshot lo actualizará en ~1 segundo
});

// Sync trade panel marks every second from lastSnap
setInterval(() => {
  if (!_tpOpen || !lastSnap) return;
  _updateMarkFromSnap(lastSnap);
  if (_tpMode === 'analysis') _updateAnalysisPreview();
}, 1000);

// ── Gravity poller — refresca el mapa de liquidez de cada pos-card Pro ──────
// Cada canvas trae data-grav-sym + view_min/view_max (sincronizados con el
// zoom de la progress bar). Para cada símbolo único, hace UNA petición con el
// viewport más amplio y re-renderiza todos los canvases de ese símbolo con su
// propio viewport.
const _gravCache = new Map();  // sym → { ts, data }
let   _gravBusy  = false;

async function _gravTick() {
  if (_gravBusy) return;
  _gravBusy = true;
  try {
    const canvases = document.querySelectorAll('canvas[data-grav-sym]');
    if (!canvases.length) return;

    // Agrupa por símbolo y calcula viewport envolvente
    const bySym = new Map();
    canvases.forEach(c => {
      const sym = c.dataset.gravSym;
      const lo  = parseFloat(c.dataset.gravVmin);
      const hi  = parseFloat(c.dataset.gravVmax);
      if (!sym || !(hi > lo)) return;
      const cur = bySym.get(sym) || { vmin: lo, vmax: hi, canvases: [] };
      cur.vmin = Math.min(cur.vmin, lo);
      cur.vmax = Math.max(cur.vmax, hi);
      cur.canvases.push(c);
      bySym.set(sym, cur);
    });

    const now = Date.now();
    await Promise.all(Array.from(bySym.entries()).map(async ([sym, grp]) => {
      const cached = _gravCache.get(sym);
      let data = cached && (now - cached.ts < 1800) ? cached.data : null;
      if (!data) {
        try {
          const url = `/api/liquidity/${encodeURIComponent(sym)}`
            + `?view_min=${grp.vmin}&view_max=${grp.vmax}`;
          const r = await fetch(url);
          if (!r.ok) return;
          data = await r.json();
          _gravCache.set(sym, { ts: now, data });
        } catch (_) { return; }
      }
      grp.canvases.forEach(c => {
        const lo = parseFloat(c.dataset.gravVmin);
        const hi = parseFloat(c.dataset.gravVmax);
        QtsGravity.render(c, data, { vmin: lo, vmax: hi });
      });
    }));

    // Limpieza de cache para símbolos que ya no están en pantalla
    const active = new Set(bySym.keys());
    Array.from(_gravCache.keys()).forEach(k => {
      if (!active.has(k)) _gravCache.delete(k);
    });
  } finally {
    _gravBusy = false;
  }
}

setInterval(_gravTick, 2000);
// Primer tick rápido para que aparezca en cuanto se monten las cards
setTimeout(_gravTick, 400);
