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

  // ── Fill activo ───────────────────────────────────────────────────────────
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

  const pctLbl = `${progress >= 0 ? '+' : ''}${fmt(progress, 1)}%`;
  const slLbl  = pos.sl > 0 ? fmtPrice(pos.sl) : '—';
  const tpLbl  = pos.tp > 0 ? fmtPrice(pos.tp) : '—';

  // ── Marcador de precio actual: SVG con anillo de momentum ─────────────────
  const mom     = calcMomentum(pos);
  const circ    = 37.7;   // 2π × r=6
  const offset  = 9.4;    // empieza desde arriba (circ/4)
  const filled  = ((0.15 + 0.85 * mom.strength) * circ).toFixed(1);

  // Label: PnL neto desde BE (positivo después del breakeven)
  const fnPct = pos.full_net_pct ?? 0;
  const fnUsd = pos.full_net_pnl ?? 0;
  const fnCls = fnPct >= 0 ? 'c-green' : 'c-red';
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
