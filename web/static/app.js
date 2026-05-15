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

// ── Barra de progreso SL→TP ───────────────────────────────────────────────────

function buildProgressBar(pos) {
  const isLong   = pos.direction === 'LONG';
  const entryPct = pos.entry_pct_bar;
  const markPct  = pos.mark_pct_bar;
  const progress = pos.progress_pct;
  const bePct    = pos.be_pct_bar ?? entryPct;

  // ── Zonas de fondo ────────────────────────────────────────────────────────
  // Pérdida: SL → entrada
  const lossStyle = isLong
    ? `left:0%;width:${entryPct.toFixed(1)}%`
    : `left:${entryPct.toFixed(1)}%;width:${(100 - entryPct).toFixed(1)}%`;

  // Zona amarilla: entrada → BE (recuperación de fees)
  let beZoneStyle = '';
  if (pos.be_pct_bar != null) {
    if (isLong) {
      const w = Math.max(0, bePct - entryPct);
      beZoneStyle = `left:${entryPct.toFixed(1)}%;width:${w.toFixed(1)}%`;
    } else {
      const w = Math.max(0, entryPct - bePct);
      beZoneStyle = `left:${bePct.toFixed(1)}%;width:${w.toFixed(1)}%`;
    }
  }

  // Zona verde: BE → TP
  const profitStyle = isLong
    ? `left:${bePct.toFixed(1)}%;width:${(100 - bePct).toFixed(1)}%`
    : `left:0%;width:${bePct.toFixed(1)}%`;

  // ── Fill activo — tricolor: rojo / naranja(BE) / verde ───────────────────
  // Estado según posición del mark respecto a entry y breakeven
  const mark  = pos.mark;
  const bePx  = pos.breakeven_price || pos.entry;
  let fillState;
  if (isLong) {
    if (mark >= bePx)        fillState = 'profit';  // verde: pasó el BE
    else if (mark >= pos.entry) fillState = 'be';   // naranja: entre entry y BE
    else                     fillState = 'loss';    // rojo: debajo de entry
  } else {
    if (mark <= bePx)        fillState = 'profit';
    else if (mark <= pos.entry) fillState = 'be';
    else                     fillState = 'loss';
  }

  let fillLeft, fillWidth;
  if (fillState === 'loss') {
    // Relleno va del mark hacia la entrada (zona de pérdida)
    if (isLong) { fillLeft = Math.max(0, markPct); fillWidth = Math.max(0, entryPct - Math.max(0, markPct)); }
    else        { fillLeft = entryPct; fillWidth = Math.max(0, Math.min(100, markPct) - entryPct); }
  } else {
    // Relleno va de la entrada hacia el mark (zona BE o profit)
    if (isLong) { fillLeft = entryPct; fillWidth = Math.max(0, markPct - entryPct); }
    else        { fillLeft = Math.max(0, markPct); fillWidth = Math.max(0, entryPct - Math.max(0, markPct)); }
  }
  const fillClass = fillState === 'profit' ? 'prog-fill-profit'
                  : fillState === 'be'     ? 'prog-fill-be'
                  :                          'prog-fill-loss';

  const pctLbl = `${progress >= 0 ? '+' : ''}${fmt(progress, 1)}%`;
  const slLbl  = pos.sl > 0 ? fmtPrice(pos.sl) : '—';
  const tpLbl  = pos.tp > 0 ? fmtPrice(pos.tp) : '—';

  // ── Marcador de precio actual: SVG con anillo de momentum ─────────────────
  const mom     = calcMomentum(pos);
  const circ    = 37.7;   // 2π × r=6
  const offset  = 9.4;    // empieza desde arriba (circ/4)
  const filled  = ((0.15 + 0.85 * mom.strength) * circ).toFixed(1);

  // Label: PnL neto desde BE — color sigue el estado del fill
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

  // ── Órdenes límite pendientes ─────────────────────────────────────────────
  const orderMarkers = (pos.orders || []).map(o => {
    if (!o.price || !pos.sl || !pos.tp || pos.tp === pos.sl) return '';
    const pct = (o.price - pos.sl) / (pos.tp - pos.sl) * 100;
    const cls = o.side === 'Buy' ? 'prog-order-buy' : 'prog-order-sell';
    const tip = `${o.side} ${o.qty} @ ${fmtPrice(o.price)} (${o.status})`;
    return `<div class="prog-order-marker ${cls}" style="left:${Math.max(0,Math.min(100,pct)).toFixed(1)}%" title="${esc(tip)}"></div>`;
  }).join('');

  // ── Breakeven marker (naranja) ────────────────────────────────────────────
  const beMarker = (pos.be_pct_bar != null && pos.breakeven_price)
    ? `<div class="prog-be-marker" style="left:${pos.be_pct_bar.toFixed(1)}%" title="Breakeven: ${esc(fmtPrice(pos.breakeven_price))}"></div>
       <div class="prog-be-label"  style="left:${pos.be_pct_bar.toFixed(1)}%">BE</div>`
    : '';

  // ── Hitos 25/50/75 (verde) ────────────────────────────────────────────────
  const milestoneMarkers = (pos.milestones || []).map(m => {
    const tip      = `${m.pct}% → ${fmtPrice(m.price)} | ROI ${m.roi >= 0 ? '+' : ''}${fmt(m.roi, 2)}%`;
    const grossStr = m.gross != null ? ` (${fmtMoneyAbs(m.gross)})` : '';
    return `
      <div class="prog-milestone" style="left:${m.bar_pct.toFixed(1)}%" title="${esc(tip)}"></div>
      <div class="prog-milestone-label" style="left:${m.bar_pct.toFixed(1)}%">${m.pct}% ${fmtPrice(m.price)}${grossStr}</div>`;
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
      ${beZoneStyle ? `<div class="prog-zone-be" style="${beZoneStyle}"></div>` : ''}
      <div class="prog-zone-profit" style="${profitStyle}"></div>
      <div class="${fillClass}"     style="left:${fillLeft.toFixed(1)}%;width:${fillWidth.toFixed(1)}%"></div>
      ${orderMarkers}
      ${beMarker}
      ${milestoneMarkers}
      <div class="prog-entry-line"  style="left:${entryPct.toFixed(1)}%"></div>
      <div class="prog-entry-label" style="left:${entryPct.toFixed(1)}%">Entrada ${esc(fmtPrice(pos.entry))}</div>
      <div class="prog-mark-wrap" style="left:${markPct.toFixed(1)}%" title="Mark: ${esc(fmtPrice(pos.mark))}">
        <div class="prog-mark-label ${fnCls}">${esc(markLbl)}</div>
        ${markSvg}
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
    </div>
  </div>`;
}

// ── Render posiciones ─────────────────────────────────────────────────────────

function renderPositions(positions) {
  const n     = positions ? positions.length : 0;
  const html  = n ? positions.map(buildPositionCard).join('') : '<div class="empty-state">Sin posiciones abiertas</div>';
  const label = n ? `${n} activa${n > 1 ? 's' : ''}` : '';

  const c  = document.getElementById('positions-container');
  const pc = document.getElementById('pos-count');
  if (c)  c.innerHTML    = html;
  if (pc) pc.textContent = label;

  // Badge en side-tab "Trades"
  const sb = document.getElementById('side-pos-count');
  if (sb) sb.textContent = n > 0 ? String(n) : '';
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

function buildHistoryCard(t) {
  const isLong  = (t.side || '').toLowerCase() === 'buy';
  const dirCls  = isLong ? 'long' : 'short';
  const dirLbl  = isLong ? 'LONG' : 'SHORT';
  const pnlCls  = pnlClass(t.closed_pnl);
  const roi     = t.entry_price > 0
    ? ((t.exit_price - t.entry_price) / t.entry_price * 100 * (isLong ? 1 : -1) * t.leverage)
    : 0;

  return `
  <div class="hist-card">
    <div class="hist-header">
      <span class="pos-symbol">${esc(t.symbol)}</span>
      <span class="pos-dir ${dirCls}">${dirLbl}</span>
      <span class="pos-leverage">${t.leverage}x</span>
      <span class="hist-pnl ${pnlCls}">${fmtMoney(t.closed_pnl)}</span>
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
      <span>${t.qty} contratos</span>
    </div>
  </div>`;
}

async function loadHistory() {
  const c = document.getElementById('history-container');
  c.innerHTML = '<div class="empty-state">Cargando…</div>';
  try {
    const res  = await fetch('/api/history');
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    if (!data.history || data.history.length === 0) {
      c.innerHTML = '<div class="empty-state">Sin trades cerrados recientes</div>';
      return;
    }
    c.innerHTML = data.history.map(buildHistoryCard).join('');
    _historyLoaded = true;
  } catch (e) {
    c.innerHTML = `<div class="empty-state c-red">Error: ${esc(String(e))}</div>`;
  }
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

const _lsKey = 'qts_analyses';

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
  _renderSavedAnalyses();
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
    if (_tpMode === 'analysis') { _renderSavedAnalyses(); _updateAnalysisPreview(); }
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

// ── Analysis: Save to localStorage ───────────────────────────────────────────

function _loadAnalyses() {
  try { return JSON.parse(localStorage.getItem(_lsKey) || '[]'); } catch { return []; }
}

function _saveAnalyses(list) {
  localStorage.setItem(_lsKey, JSON.stringify(list));
}

document.getElementById('ta-btn-save')?.addEventListener('click', () => {
  const sym   = document.getElementById('ta-symbol')?.value?.trim() || '';
  const entry = parseFloat(document.getElementById('ta-entry')?.value) || 0;
  const slv   = parseFloat(document.getElementById('ta-sl')?.value) || 0;
  const tpv   = parseFloat(document.getElementById('ta-tp')?.value) || 0;
  const size  = parseFloat(document.getElementById('ta-size')?.value) || 0;
  const notes = document.getElementById('ta-notes')?.value?.trim() || '';

  if (!sym || !entry || !slv || !tpv) { alert('Completa símbolo, entrada, SL y TP.'); return; }

  const list = _loadAnalyses();
  list.unshift({
    id:         Date.now().toString(36),
    symbol:     _symFull(sym),
    label:      sym.toUpperCase().replace('USDT',''),
    direction:  _taDir,
    entry, sl: slv, tp: tpv, size, notes,
    created_at: Date.now(),
  });
  _saveAnalyses(list);
  _renderSavedAnalyses();

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
});

function _renderSavedAnalyses() {
  const list = _loadAnalyses();
  const c    = document.getElementById('ta-saved-list');
  if (!c) return;
  if (!list.length) {
    c.innerHTML = '<div class="c-dim" style="font-size:10px;text-align:center;padding:12px 0">Sin análisis guardados</div>';
    return;
  }
  c.innerHTML = list.map(a => {
    const isLong = a.direction === 'Buy';
    const dirCls = isLong ? 'c-green' : 'c-red';
    const dirLbl = isLong ? '▲ LONG' : '▼ SHORT';
    const mark   = _liveMarks[a.symbol] || 0;
    const qty    = a.size > 0 && a.entry > 0 ? a.size / a.entry : 0;
    const virPnl = mark > 0 && qty > 0
      ? ((isLong ? mark - a.entry : a.entry - mark) * qty)
      : null;
    const pnlHtml = virPnl != null
      ? `<span class="ta-saved-pnl ${virPnl >= 0 ? 'c-green' : 'c-red'}">${virPnl >= 0 ? '+' : ''}$${virPnl.toFixed(2)}</span>`
      : '';
    const markHtml = mark > 0 ? `<span class="c-dim" style="font-size:9px">Mark ${fmtPrice(mark)}</span>` : '';
    const date  = new Date(a.created_at);
    const p = n => String(n).padStart(2,'0');
    const dateStr = `${date.getMonth()+1}/${date.getDate()} ${p(date.getHours())}:${p(date.getMinutes())}`;
    const rr = (a.tp - a.entry) !== 0 && (a.entry - a.sl) !== 0
      ? Math.abs((a.tp - a.entry) / (a.entry - a.sl)).toFixed(2)
      : '—';
    return `
    <div class="ta-saved-card" data-id="${esc(a.id)}">
      <div class="ta-saved-hdr">
        <span class="ta-saved-sym">${esc(a.label)}</span>
        <span class="${dirCls}" style="font-size:10px;font-weight:600">${dirLbl}</span>
        <span class="ta-saved-meta">${esc(dateStr)} · R:R ${rr} ${markHtml}</span>
        ${pnlHtml}
      </div>
      ${a.notes ? `<div class="ta-saved-notes">${esc(a.notes)}</div>` : ''}
      <div style="font-size:9px;color:var(--text-dim);margin-bottom:6px">
        Entrada ${fmtPrice(a.entry)} · SL ${fmtPrice(a.sl)} · TP ${fmtPrice(a.tp)}
        ${a.size ? ` · $${a.size}` : ''}
      </div>
      <div class="ta-saved-actions">
        <button class="ta-saved-btn danger" data-del="${esc(a.id)}">Eliminar</button>
        <button class="ta-saved-btn promote" data-promote="${esc(a.id)}">→ Real</button>
      </div>
    </div>`;
  }).join('');

  // Wire delete
  c.querySelectorAll('[data-del]').forEach(btn => {
    btn.addEventListener('click', () => {
      const newList = _loadAnalyses().filter(a => a.id !== btn.dataset.del);
      _saveAnalyses(newList);
      _renderSavedAnalyses();
    });
  });

  // Wire promote to manual
  c.querySelectorAll('[data-promote]').forEach(btn => {
    btn.addEventListener('click', () => {
      const a = _loadAnalyses().find(x => x.id === btn.dataset.promote);
      if (!a) return;
      // Switch to manual mode
      _tpMode = 'manual';
      document.querySelectorAll('.trade-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === 'manual'));
      document.getElementById('trade-manual').style.display   = 'flex';
      document.getElementById('trade-analysis').style.display = 'none';
      // Populate fields
      const lbl = a.symbol.replace('USDT','');
      const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v ?? ''; };
      setVal('tm-symbol', lbl);
      setVal('tm-sl',     a.sl);
      setVal('tm-tp',     a.tp);
      setVal('tm-size',   a.size || '');
      // Direction
      _tmDir = a.direction;
      document.querySelectorAll('#trade-manual .trade-dir-btn').forEach(b => b.classList.toggle('active', b.dataset.dir === _tmDir));
      _updateTradePreview();
    });
  });
}

// Sync trade panel marks and analysis PnL every second from lastSnap
setInterval(() => {
  if (!_tpOpen || !lastSnap) return;
  _updateMarkFromSnap(lastSnap);
  if (_tpMode === 'analysis') { _updateAnalysisPreview(); _renderSavedAnalyses(); }
}, 1000);
