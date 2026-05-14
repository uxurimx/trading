/* QTS Dashboard — WebSocket client */
'use strict';

// ── Estado persistido ─────────────────────────────────────────────────────────
let _theme      = localStorage.getItem('qts_theme')  || 'dark';
let _showMxn    = localStorage.getItem('qts_mxn')    === 'true';
let _mxnRate    = parseFloat(localStorage.getItem('qts_mxn_rate') || '17.5');
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

// ── Reloj ─────────────────────────────────────────────────────────────────────
function tickClock() {
  const d  = new Date();
  const p  = n => String(n).padStart(2, '0');
  document.getElementById('clock').textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
setInterval(tickClock, 1000);
tickClock();

// ── Controles de tema y moneda ────────────────────────────────────────────────

function applyTheme() {
  document.documentElement.setAttribute('data-theme', _theme);
  document.getElementById('btn-theme').textContent = _theme === 'dark' ? '☀' : '☾';
  localStorage.setItem('qts_theme', _theme);
}

function applyCurrency() {
  const btn = document.getElementById('btn-currency');
  const lbl = document.getElementById('mxn-rate-lbl');
  btn.textContent = _showMxn ? 'MXN' : 'USD';
  btn.classList.toggle('active', _showMxn);
  if (lbl) lbl.style.display = _showMxn ? 'inline' : 'none';
  localStorage.setItem('qts_mxn', _showMxn);
  if (lastSnap) render(lastSnap);
}

document.getElementById('btn-theme').addEventListener('click', () => {
  _theme = _theme === 'dark' ? 'light' : 'dark';
  applyTheme();
});

document.getElementById('btn-currency').addEventListener('click', () => {
  _showMxn = !_showMxn;
  applyCurrency();
});

applyTheme();
applyCurrency();

// ── Render account ────────────────────────────────────────────────────────────

function renderAccount(a) {
  const badge = document.getElementById('conn-badge');
  if (a.error) {
    badge.className   = 'badge badge-error';
    badge.textContent = `● ${a.error}`;
  } else if (a.connected) {
    badge.className   = 'badge badge-ok';
    badge.textContent = '● EN VIVO';
  } else {
    badge.className   = 'badge badge-connecting';
    badge.textContent = '● CONECTANDO';
  }

  document.getElementById('equity').textContent      = fmtMoneyAbs(a.equity);
  document.getElementById('available').textContent   = fmtMoneyAbs(a.available);
  document.getElementById('used-margin').textContent = fmtMoneyAbs(a.used_margin);
  document.getElementById('margin-pct').textContent  = `${fmt(a.margin_pct)}% del equity`;
  document.getElementById('open-count').textContent  = a.open_count;

  const upnl = document.getElementById('unrealized-pnl');
  upnl.textContent = fmtMoney(a.unrealized_pnl);
  upnl.className   = `metric-value ${pnlClass(a.unrealized_pnl)}`;

  const dpnl = document.getElementById('daily-pnl');
  dpnl.textContent = fmtMoney(a.daily_pnl);
  dpnl.className   = `metric-value ${pnlClass(a.daily_pnl)}`;

  const pct  = Math.min(100, Math.max(0, a.margin_pct || 0));
  const fill = document.getElementById('margin-bar-fill');
  const lbl  = document.getElementById('margin-bar-label');
  fill.style.width      = `${pct}%`;
  fill.style.background = pct > 80 ? 'var(--red)' : pct > 60 ? 'var(--orange)' : 'var(--green)';
  lbl.textContent       = `Margen ${fmt(pct, 1)}%`;
}

// ── Barra de progreso SL→TP ───────────────────────────────────────────────────

function buildProgressBar(pos) {
  const isLong    = pos.direction === 'LONG';
  const entryPct  = pos.entry_pct_bar;
  const markPct   = pos.mark_pct_bar;
  const progress  = pos.progress_pct;

  // Zonas de fondo
  const lossStyle   = isLong
    ? `left:0%;width:${entryPct}%`
    : `left:${entryPct}%;width:${100 - entryPct}%`;
  const profitStyle = isLong
    ? `left:${entryPct}%;width:${100 - entryPct}%`
    : `left:0%;width:${entryPct}%`;

  // Fill activo
  const inProfit = progress >= 0;
  let fillLeft, fillWidth, fillClass;
  if (isLong) {
    if (inProfit) { fillLeft = entryPct; fillWidth = Math.max(0, markPct - entryPct); }
    else          { fillLeft = Math.max(0, markPct); fillWidth = Math.max(0, entryPct - Math.max(0, markPct)); }
  } else {
    if (inProfit) { fillLeft = Math.max(0, markPct); fillWidth = Math.max(0, entryPct - Math.max(0, markPct)); }
    else          { fillLeft = entryPct; fillWidth = Math.max(0, Math.min(100, markPct) - entryPct); }
  }
  fillClass = inProfit ? 'prog-fill-profit' : 'prog-fill-loss';

  const pctLbl  = `${progress >= 0 ? '+' : ''}${fmt(progress, 1)}%`;
  const slLbl   = pos.sl > 0 ? fmtPrice(pos.sl) : '—';
  const tpLbl   = pos.tp > 0 ? fmtPrice(pos.tp) : '—';
  const markLbl = fmtPrice(pos.mark);

  // Marcadores de órdenes límite pendientes
  const orderMarkers = (pos.orders || []).map(o => {
    if (!o.price || !pos.sl || !pos.tp || pos.tp === pos.sl) return '';
    const pct     = (o.price - pos.sl) / (pos.tp - pos.sl) * 100;
    const clamped = Math.max(0, Math.min(100, pct));
    const cls     = o.side === 'Buy' ? 'prog-order-buy' : 'prog-order-sell';
    const tip     = `${o.side} ${o.qty} @ ${fmtPrice(o.price)} (${o.status})`;
    return `<div class="prog-order-marker ${cls}" style="left:${clamped.toFixed(1)}%" title="${esc(tip)}"></div>`;
  }).join('');

  return `
  <div class="prog-wrap">
    <div class="prog-labels">
      <span>SL <strong>${esc(slLbl)}</strong></span>
      <span class="prog-pct">${esc(pctLbl)} hacia TP</span>
      <span>TP <strong>${esc(tpLbl)}</strong></span>
    </div>
    <div class="prog-track">
      <div class="prog-zone-loss"   style="${lossStyle}"></div>
      <div class="prog-zone-profit" style="${profitStyle}"></div>
      <div class="${fillClass}"     style="left:${fillLeft.toFixed(1)}%;width:${fillWidth.toFixed(1)}%"></div>
      ${orderMarkers}
      <div class="prog-entry-line"  style="left:${entryPct.toFixed(1)}%"></div>
      <div class="prog-entry-label" style="left:${entryPct.toFixed(1)}%">Entrada ${esc(fmtPrice(pos.entry))}</div>
      <div class="prog-mark-wrap"   style="left:${markPct.toFixed(1)}%">
        <div class="prog-mark-label">${esc(markLbl)}</div>
        <div class="prog-mark-dot"  title="Mark: ${esc(markLbl)}"></div>
      </div>
    </div>
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

// ── Tarjeta de posición ───────────────────────────────────────────────────────

function buildPositionCard(pos) {
  const isLong   = pos.direction === 'LONG';
  const dirClass = isLong ? 'long' : 'short';

  const netNow  = pos.net_pnl_now;
  const roiStr  = fmtPct(pos.roi_pct);
  const chgStr  = fmtPct(pos.roi_entry_pct);

  // Absoluto total en SL/TP (directo desde el servidor)
  const netAtSL = pos.net_at_sl;
  const netAtTP = pos.net_at_tp;

  return `
  <div class="pos-card">
    <div class="pos-header">
      <span class="pos-symbol">${esc(pos.symbol)}</span>
      <span class="pos-dir ${dirClass}">${pos.direction}</span>
      <span class="pos-leverage">${pos.leverage}x</span>
      <span class="pos-time">⏱ ${pos.elapsed_fmt}</span>
    </div>

    <div class="pos-body">

      <div class="pos-prices">
        <div class="price-cell entry">
          <div class="lbl">ENTRADA</div>
          <div class="val">${fmtPrice(pos.entry)}</div>
        </div>
        <div class="price-cell mark">
          <div class="lbl">MARK &nbsp;<span class="${pnlClass(pos.roi_entry_pct)}">${esc(chgStr)}</span></div>
          <div class="val">${fmtPrice(pos.mark)}</div>
        </div>
        <div class="price-cell sl">
          <div class="lbl">STOP LOSS</div>
          <div class="val">${fmtPrice(pos.sl) || '—'}</div>
        </div>
        <div class="price-cell tp">
          <div class="lbl">TAKE PROFIT</div>
          <div class="val">${fmtPrice(pos.tp) || '—'}</div>
        </div>
      </div>

      ${buildProgressBar(pos)}

      <div class="pos-metrics">
        <div class="pnl-cell">
          <div class="lbl">UNREALIZED PnL</div>
          <div class="val ${pnlClass(pos.gross_pnl)}">${fmtMoney(pos.gross_pnl)}</div>
          <div class="sub">ROI <span class="${pnlClass(pos.roi_pct)}">${esc(roiStr)}</span> · −fee cierre ${fmtMoneyAbs(pos.exit_fee_now, 4)}</div>
        </div>
        <div class="pnl-cell">
          <div class="lbl">EN SL <span style="color:var(--text-sub);font-size:8px">(${fmtPrice(pos.sl)})</span></div>
          <div class="val ${pnlClass(netAtSL)}">${fmtMoney(netAtSL)}</div>
          <div class="sub">bruto · −fee ${fmtMoneyAbs(pos.exit_fee_sl, 4)}</div>
        </div>
        <div class="pnl-cell">
          <div class="lbl">EN TP <span style="color:var(--text-sub);font-size:8px">(${fmtPrice(pos.tp)})</span></div>
          <div class="val ${pnlClass(netAtTP)}">${fmtMoney(netAtTP)}</div>
          <div class="sub">bruto · −fee ${fmtMoneyAbs(pos.exit_fee_tp, 4)}</div>
        </div>
      </div>

      ${buildOrdersRow(pos.orders)}
      ${buildSignalChips(pos)}

      <div class="pos-fees">
        <span><span class="key">R:R</span> ${fmt(pos.rr_ratio)}</span>
        <span><span class="key">Fee entrada (pagado):</span> ${fmtMoneyAbs(pos.entry_fee, 4)}</span>
        <span><span class="key">Fee salida est:</span> ${fmtMoneyAbs(pos.exit_fee_now, 4)}</span>
        <span><span class="key">Funding est:</span> ${fmtMoneyAbs(pos.funding_est, 4)}</span>
        <span><span class="key">Notional:</span> ${fmtMoneyAbs(pos.notional, 2)}</span>
        <span><span class="key">Margen:</span> ${fmtMoneyAbs(pos.margin, 2)}</span>
        <span><span class="key">Liq:</span> ${fmtPrice(pos.liq)}</span>
        <span><span class="key">ATR:</span> ${fmtPrice(pos.atr)}</span>
      </div>
    </div>
  </div>`;
}

// ── Render posiciones ─────────────────────────────────────────────────────────

function renderPositions(positions) {
  const container = document.getElementById('positions-container');
  const count     = document.getElementById('pos-count');
  if (!positions || positions.length === 0) {
    container.innerHTML = '<div class="empty-state">Sin posiciones abiertas</div>';
    count.textContent   = '';
    return;
  }
  count.textContent   = `${positions.length} activa${positions.length > 1 ? 's' : ''}`;
  container.innerHTML = positions.map(buildPositionCard).join('');
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
  const el = document.getElementById('mxn-rate-lbl');
  if (el) el.textContent = `1 USD = ${rate.toFixed(2)} MXN`;
}

// ── Render completo ───────────────────────────────────────────────────────────

function render(data) {
  renderAccount(data.account);
  renderPositions(data.positions);
  renderSymbolPnl(data.symbol_pnl);

  if (data.mxn_rate && data.mxn_rate > 1) {
    _mxnRate = data.mxn_rate;
    localStorage.setItem('qts_mxn_rate', _mxnRate);
    renderMxnLabel(_mxnRate);
  }

  const ts = new Date(data.ts);
  const p  = n => String(n).padStart(2, '0');
  document.getElementById('last-update').textContent =
    `actualizado ${p(ts.getHours())}:${p(ts.getMinutes())}:${p(ts.getSeconds())}`;
}

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
    b.className   = 'badge badge-connecting';
    b.textContent = '● RECONECTANDO';
    reconnectTimer = setTimeout(connect, 2000);
  };
}

setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send('ping');
}, 15000);

connect();
