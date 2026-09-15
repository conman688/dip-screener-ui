const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const lastCheck = document.getElementById('last-check');
const tokenRows = document.getElementById('token-rows');
const tokenCount = document.getElementById('token-count');
const alertsFeed = document.getElementById('alerts-feed');
const alertCount = document.getElementById('alert-count');
const toggleBtn = document.getElementById('toggle-btn');
const pollNowBtn = document.getElementById('poll-now-btn');
const settingsToggle = document.getElementById('settings-toggle');
const settingsChevron = document.getElementById('settings-chevron');
const settingsForm = document.getElementById('settings-form');
const saveNote = document.getElementById('save-note');

const wlChainSelect = document.getElementById('wl-chain');
const wlChainCustom = document.getElementById('wl-chain-custom');
const wlAddressInput = document.getElementById('wl-address');
const wlLabelInput = document.getElementById('wl-label');
const wlAddBtn = document.getElementById('wl-add-btn');
const watchlistItemsEl = document.getElementById('watchlist-items');
const watchlistCountEl = document.getElementById('watchlist-count');

let isRunning = true;
let currentWatchlist = []; // [[chain_id, address, label], ...] — mirrors server state
let latestTokens = [];     // last full, unfiltered token list from /api/status
let alertScoreThreshold = 70; // loaded from config, used only to badge rows - not a filter
let sortState = { column: 'score', direction: 'desc' };

// ---------------- Filters ----------------
// These are now PURE VIEW filters - they only change what's displayed from
// the already-scored token list, instantly, client-side. They do NOT get
// saved to the server and do NOT gate scoring or alerting - that's exactly
// the trap the old version fell into (individually-reasonable hard filters
// combining to eliminate almost everything). Eligibility (the real,
// deliberately loose floor that decides what gets scored at all) lives in
// config.json only - see the Settings hint text.

const filterInputs = {
  score: document.getElementById('f-score'),
  mcapMin: document.getElementById('f-mcap-min'),
  mcapMax: document.getElementById('f-mcap-max'),
  ageMin: document.getElementById('f-age-min'),
  ageMax: document.getElementById('f-age-max'),
  dipMin: document.getElementById('f-dip-min'),
  dipMax: document.getElementById('f-dip-max'),
  liquidity: document.getElementById('f-liquidity'),
  volume: document.getElementById('f-volume'),
};
const filterResetBtn = document.getElementById('filter-reset-btn');

const FILTER_DEFAULTS = {
  score: 0, mcapMin: 0, mcapMax: 0, ageMin: 0, ageMax: 0, dipMin: 0, dipMax: 0, liquidity: 0, volume: 0,
};

function currentFilters() {
  const n = (el, fallback) => {
    const v = Number(el.value);
    return el.value === '' || isNaN(v) ? fallback : v;
  };
  return {
    score: n(filterInputs.score, 0),
    mcapMin: n(filterInputs.mcapMin, 0),
    mcapMax: n(filterInputs.mcapMax, 0),
    ageMin: n(filterInputs.ageMin, 0),
    ageMax: n(filterInputs.ageMax, 0),
    dipMin: n(filterInputs.dipMin, 0),
    dipMax: n(filterInputs.dipMax, 0),
    liquidity: n(filterInputs.liquidity, 0),
    volume: n(filterInputs.volume, 0),
  };
}

function applyFiltersToInputs(f) {
  filterInputs.score.value = f.score || '';
  filterInputs.mcapMin.value = f.mcapMin || '';
  filterInputs.mcapMax.value = f.mcapMax || '';
  filterInputs.ageMin.value = f.ageMin || '';
  filterInputs.ageMax.value = f.ageMax || '';
  filterInputs.dipMin.value = f.dipMin || '';
  filterInputs.dipMax.value = f.dipMax || '';
  filterInputs.liquidity.value = f.liquidity || '';
  filterInputs.volume.value = f.volume || '';
}

function passesFilters(t, f) {
  if (f.score && t.score < f.score) return false;
  if (f.mcapMin && t.market_cap_usd < f.mcapMin) return false;
  if (f.mcapMax && t.market_cap_usd > f.mcapMax) return false;
  if (f.ageMin && t.age_hours < f.ageMin) return false;
  if (f.ageMax && t.age_hours > f.ageMax) return false;
  if (f.dipMin && t.max_drawdown_pct < f.dipMin) return false;
  if (f.dipMax && t.max_drawdown_pct > f.dipMax) return false;
  if (f.liquidity && t.liquidity_usd < f.liquidity) return false;
  if (f.volume && t.volume24h_usd < f.volume) return false;
  return true;
}

Object.values(filterInputs).forEach(el => {
  el.addEventListener('input', renderTable);
});

filterResetBtn.addEventListener('click', () => {
  applyFiltersToInputs(FILTER_DEFAULTS);
  renderTable();
});

// ---------------- Sorting ----------------


document.querySelectorAll('th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    const col = th.dataset.sort;
    if (sortState.column === col) {
      sortState.direction = sortState.direction === 'asc' ? 'desc' : 'asc';
    } else {
      sortState = { column: col, direction: col === 'label' ? 'asc' : 'desc' };
    }
    updateSortIndicators();
    renderTable();
  });
});

function updateSortIndicators() {
  document.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sort === sortState.column) {
      th.classList.add(sortState.direction === 'asc' ? 'sort-asc' : 'sort-desc');
    }
  });
}

function sortTokens(tokens) {
  const { column, direction } = sortState;
  const dir = direction === 'asc' ? 1 : -1;
  return [...tokens].sort((a, b) => {
    let av = a[column], bv = b[column];
    if (typeof av === 'string') return av.localeCompare(bv) * dir;
    return ((av ?? 0) - (bv ?? 0)) * dir;
  });
}

// ---------------- Formatting ----------------

function fmtMoney(n) {
  if (n >= 1_000_000) return '$' + (n / 1_000_000).toFixed(2) + 'M';
  if (n >= 1_000) return '$' + (n / 1_000).toFixed(1) + 'K';
  return '$' + Math.round(n).toLocaleString();
}

function fmtPrice(n) {
  if (n === 0) return '$0';
  if (n < 0.01) return '$' + n.toFixed(8);
  return '$' + n.toFixed(4);
}

function fmtAge(hours) {
  if (hours < 48) return hours.toFixed(1) + 'h';
  return (hours / 24).toFixed(1) + 'd';
}

function fmtMoneySigned(n) {
  return (n >= 0 ? '+' : '-') + fmtMoney(Math.abs(n));
}

function fmtPctSigned(n) {
  return (n >= 0 ? '+' : '') + n.toFixed(1) + '%';
}

function timeAgo(ts) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (seconds < 60) return seconds + 's ago';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
  return Math.floor(seconds / 3600) + 'h ago';
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str ?? '';
  return div.innerHTML;
}

// ---------------- Toast (replaces window.alert, which isn't reliably
// available in every browser/embedded context) ----------------

const toastEl = document.getElementById('toast');
let toastTimer = null;

function showToast(message) {
  toastEl.textContent = message;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 3500);
}

// ---------------- Live status table ----------------

async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    isRunning = data.running;
    statusDot.className = 'dot ' + (isRunning ? 'live' : 'paused');
    statusText.textContent = isRunning ? 'Live' : 'Paused';
    toggleBtn.textContent = isRunning ? 'Pause' : 'Resume';
    lastCheck.textContent = data.last_cycle_ts
      ? 'checked ' + timeAgo(data.last_cycle_ts)
      : 'not checked yet';
    latestTokens = data.tokens;
    renderTable();
    renderFunnel(data.funnel);
  } catch (e) {
    statusText.textContent = 'Can\'t reach server';
    statusDot.className = 'dot paused';
  }
}

const funnelListEl = document.getElementById('funnel-list');
const FUNNEL_LABELS = [
  ['watchlist', 'Your watchlist'],
  ['discovered_this_cycle', 'Discovered this cycle'],
  ['carried_forward_from_history', 'Carried forward from earlier finds'],
  ['after_dedup', 'Unique tokens in pool'],
  ['fetched_market_data', 'Market data fetched'],
  ['passed_eligibility', 'Passed eligibility floor'],
  ['scored', 'Scored and ranked'],
  ['alert_threshold_met', 'At/above alert threshold'],
];

function renderFunnel(funnel) {
  if (!funnel || Object.keys(funnel).length === 0) return;
  funnelListEl.innerHTML = FUNNEL_LABELS
    .filter(([key]) => funnel[key] !== undefined)
    .map(([key, label]) => `<li class="funnel-row"><span>${label}</span><span class="funnel-value">${funnel[key]}</span></li>`)
    .join('');
}

function renderTable() {
  const f = currentFilters();
  const filtered = sortTokens(latestTokens.filter(t => passesFilters(t, f)));

  tokenCount.textContent = filtered.length + (filtered.length === 1 ? ' token' : ' tokens')
    + (latestTokens.length !== filtered.length ? ` (of ${latestTokens.length})` : '');

  if (latestTokens.length === 0) {
    tokenRows.innerHTML = '<tr class="empty-row"><td colspan="12">No tokens tracked yet. Turn on auto-discover, or add one directly below.</td></tr>';
    return;
  }
  if (filtered.length === 0) {
    tokenRows.innerHTML = '<tr class="empty-row"><td colspan="12">No tracked tokens match the current filters. Try widening the market cap or age range, or lowering min score.</td></tr>';
    return;
  }

  tokenRows.innerHTML = filtered.map(t => {
    const bounce1hClass = t.bounce_1h_pct >= 0 ? 'pct-up' : 'pct-down';
    const starred = !!t.is_watchlisted;
    const isMatch = t.score >= alertScoreThreshold;
    const scoreClass = t.score >= 70 ? 'score-high' : t.score >= 40 ? 'score-mid' : 'score-low';
    const proxyNote = t.reference_high_is_proxy ? ' title="Recent-high estimated from 24h/6h/1h change - limited history so far"' : '';
    const breakdown = t.score_breakdown || {};
    const breakdownText = `Drawdown ${breakdown.drawdown_quality} · Short-term dip ${breakdown.short_term_dip} · Volume ${breakdown.volume_retention} · Liquidity ${breakdown.liquidity} · Momentum ${breakdown.buy_sell_momentum} · Activity ${breakdown.tx_activity} · Age ${breakdown.age}`;

    // Per-window (15m..24h) drawdown breakdown, shown as a hover tooltip on
    // the compact "Recent dip" cell - '*' flags a window our stored history
    // doesn't reach back far enough to fully cover yet.
    const windows = t.windows || {};
    const windowsTooltip = ['15m', '30m', '1h', '6h', '12h', '24h']
      .filter(w => windows[w])
      .map(w => `${w} -${windows[w].drawdown_pct}%${windows[w].has_full_coverage ? '' : '*'}`)
      .join(' · ');

    return `
      <tr class="${isMatch ? 'qualifies' : ''}">
        <td class="col-star">
          <button class="star-btn ${starred ? 'starred' : ''}"
                  data-chain="${escapeHtml(t.chain_id)}"
                  data-address="${escapeHtml(t.token_address)}"
                  data-label="${escapeHtml(t.label)}"
                  title="${starred ? 'Remove from watchlist' : 'Add to watchlist'}">${starred ? '★' : '☆'}</button>
          <button class="buy-btn"
                  data-chain="${escapeHtml(t.chain_id)}"
                  data-address="${escapeHtml(t.token_address)}"
                  data-label="${escapeHtml(t.label)}"
                  title="Paper buy">$</button>
        </td>
        <td>
          <div class="token-label">${escapeHtml(t.label)}</div>
          <span class="token-chain">${escapeHtml(t.chain_id)}${t.platform ? ' · ' + escapeHtml(t.platform) : ''}</span>
          ${t.url ? `<a class="token-link" href="${escapeHtml(t.url)}" target="_blank" rel="noopener">view chart</a>` : ''}
        </td>
        <td class="num">
          <span class="score-pill ${scoreClass}" title="${escapeHtml(breakdownText)}">${t.score}</span>
          ${isMatch ? '<span class="badge badge-match">Match</span>' : ''}
        </td>
        <td class="num">${fmtPrice(t.current_price)}</td>
        <td class="num">${fmtMoney(t.market_cap_usd)}</td>
        <td class="num pct-down"${proxyNote}>-${t.max_drawdown_pct}%${t.reference_high_is_proxy ? '*' : ''}</td>
        <td class="num pct-down" title="${escapeHtml(windowsTooltip)}">-${t.short_term_dip_pct}%${t.short_term_dip_has_coverage ? '' : '*'}</td>
        <td class="num ${bounce1hClass}">${t.bounce_1h_pct >= 0 ? '+' : ''}${t.bounce_1h_pct}%${t.bounce_1h_has_coverage ? '' : '*'}</td>
        <td class="num">${fmtAge(t.age_hours)}</td>
        <td class="num">${fmtMoney(t.liquidity_usd)}</td>
        <td class="num">${fmtMoney(t.volume_5m_usd)}</td>
        <td class="num">${fmtMoney(t.volume24h_usd)}</td>
      </tr>
    `;
  }).join('');
}

// Delegated click handler for the paper-buy button on each row — opens the
// buy modal rather than using window.prompt(), which isn't reliably
// available in every browser/embedded context (e.g. it throws outright in
// this project's own preview harness).
tokenRows.addEventListener('click', (e) => {
  const btn = e.target.closest('.buy-btn');
  if (!btn) return;
  openBuyModal(btn.dataset.chain, btn.dataset.address, btn.dataset.label);
});

// Delegated click handler for star buttons — the table body is rebuilt on
// every render, so listeners are attached once here rather than per-row.
tokenRows.addEventListener('click', async (e) => {
  const btn = e.target.closest('.star-btn');
  if (!btn) return;
  const chain_id = btn.dataset.chain;
  const token_address = btn.dataset.address;
  const label = btn.dataset.label;
  const alreadyStarred = btn.classList.contains('starred');

  btn.disabled = true;
  const endpoint = alreadyStarred ? '/api/watchlist/remove' : '/api/watchlist/add';
  const res = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chain_id, token_address, label }),
  });
  const data = await res.json();
  if (data.ok) {
    currentWatchlist = data.watchlist;
    btn.classList.toggle('starred', !alreadyStarred);
    btn.textContent = alreadyStarred ? '☆' : '★';
    btn.title = alreadyStarred ? 'Add to watchlist' : 'Remove from watchlist';
    renderWatchlist();
  }
  btn.disabled = false;
});

// ---------------- Alerts feed ----------------

async function fetchAlerts() {
  try {
    const res = await fetch('/api/alerts');
    const data = await res.json();
    renderAlerts(data.alerts);
  } catch (e) { /* keep last known feed on transient errors */ }
}

function renderAlerts(alerts) {
  alertCount.textContent = alerts.length;
  if (alerts.length === 0) {
    alertsFeed.innerHTML = '<p class="empty-note">No matches yet. This fills in as tokens qualify.</p>';
    return;
  }
  alertsFeed.innerHTML = alerts.map(a => `
    <div class="alert-item">
      <div class="alert-label">${escapeHtml(a.label)}</div>
      <div class="alert-detail">Dip-and-recovery pattern confirmed</div>
      <span class="alert-time">${timeAgo(a.ts)}</span>
    </div>
  `).join('');
}

// ---------------- Paper trading ----------------
// Manual-only buy-the-dip trainer: the $ button on a token row opens a
// position at the current price, and Sell here closes it at whatever the
// current price is now — no auto-buy/auto-sell, this is for practicing the
// entry/exit judgment yourself. See app.py's /api/paper/* routes.

const paperCashEl = document.getElementById('paper-cash');
const paperTotalValueEl = document.getElementById('paper-total-value');
const paperWinRateEl = document.getElementById('paper-win-rate');
const paperPnlEl = document.getElementById('paper-total-pnl');
const paperPositionsEl = document.getElementById('paper-positions');
const paperClosedEl = document.getElementById('paper-closed');
const paperResetBtn = document.getElementById('paper-reset-btn');

async function fetchPaperPortfolio() {
  try {
    const res = await fetch('/api/paper/portfolio');
    const data = await res.json();
    renderPaperPortfolio(data);
  } catch (e) { /* keep last known panel state on transient errors */ }
}

function renderPaperPortfolio(data) {
  paperCashEl.textContent = fmtMoney(data.cash_usd);
  paperTotalValueEl.textContent = fmtMoney(data.total_value_usd);
  paperWinRateEl.textContent = data.trade_count
    ? `${data.win_rate_pct.toFixed(0)}% (${data.trade_count} trade${data.trade_count === 1 ? '' : 's'})`
    : '—';

  paperPnlEl.textContent = `${fmtMoneySigned(data.total_pnl_usd)} (${fmtPctSigned(data.total_pnl_pct)})`;
  paperPnlEl.className = 'count ' + (data.total_pnl_usd >= 0 ? 'pct-up' : 'pct-down');

  if (data.positions.length === 0) {
    paperPositionsEl.innerHTML = '<li class="empty-note">No open paper positions yet. Click the $ button on any token row to buy the dip with fake money.</li>';
  } else {
    paperPositionsEl.innerHTML = data.positions.map(p => `
      <li class="paper-position">
        <div class="paper-position-text">
          <span class="paper-position-label">${escapeHtml(p.label)}</span>
          <span class="paper-position-meta">${fmtMoney(p.amount_usd)} @ ${fmtPrice(p.entry_price)} · opened ${timeAgo(p.opened_at)}</span>
        </div>
        <span class="paper-position-pnl ${p.unrealized_pnl_usd >= 0 ? 'pct-up' : 'pct-down'}">${fmtMoneySigned(p.unrealized_pnl_usd)} (${fmtPctSigned(p.unrealized_pnl_pct)})</span>
        <button class="sell-btn" data-trade-id="${escapeHtml(p.trade_id)}" title="Sell at current price">Sell</button>
      </li>
    `).join('');
  }

  if (data.closed_trades.length === 0) {
    paperClosedEl.innerHTML = '';
  } else {
    paperClosedEl.innerHTML = '<h3 class="paper-closed-heading">Closed trades</h3>' + data.closed_trades.slice(0, 10).map(t => `
      <div class="paper-closed-row">
        <span>${escapeHtml(t.label)}</span>
        <span class="${t.pnl_usd >= 0 ? 'pct-up' : 'pct-down'}">${fmtMoneySigned(t.pnl_usd)} (${fmtPctSigned(t.pnl_pct)})</span>
      </div>
    `).join('');
  }
}

paperPositionsEl.addEventListener('click', async (e) => {
  const btn = e.target.closest('.sell-btn');
  if (!btn) return;
  btn.disabled = true;
  const res = await fetch('/api/paper/sell', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ trade_id: btn.dataset.tradeId }),
  });
  const data = await res.json();
  if (data.ok) {
    fetchPaperPortfolio();
  } else {
    showToast(data.error || 'Sell failed.');
    btn.disabled = false;
  }
});

// Two-click confirm instead of window.confirm(), for the same reason as
// the toast above - native blocking dialogs aren't reliable everywhere.
let resetArmed = false;
let resetArmTimer = null;

paperResetBtn.addEventListener('click', async () => {
  if (!resetArmed) {
    resetArmed = true;
    paperResetBtn.textContent = 'Click again to confirm';
    clearTimeout(resetArmTimer);
    resetArmTimer = setTimeout(() => {
      resetArmed = false;
      paperResetBtn.textContent = 'Reset portfolio';
    }, 4000);
    return;
  }
  clearTimeout(resetArmTimer);
  resetArmed = false;
  paperResetBtn.textContent = 'Reset portfolio';
  await fetch('/api/paper/reset', { method: 'POST' });
  fetchPaperPortfolio();
});

// ---------------- Buy modal ----------------

const buyModalOverlay = document.getElementById('buy-modal-overlay');
const buyModalTitle = document.getElementById('buy-modal-title');
const buyModalAmount = document.getElementById('buy-modal-amount');
const buyModalError = document.getElementById('buy-modal-error');
const buyModalCancel = document.getElementById('buy-modal-cancel');
const buyModalConfirm = document.getElementById('buy-modal-confirm');

let pendingBuy = null; // { chain_id, token_address, label }

function openBuyModal(chain_id, token_address, label) {
  pendingBuy = { chain_id, token_address, label };
  buyModalTitle.textContent = `Paper buy ${label}`;
  buyModalAmount.value = '100';
  buyModalError.hidden = true;
  buyModalOverlay.classList.remove('hidden');
  buyModalAmount.focus();
  buyModalAmount.select();
}

function closeBuyModal() {
  buyModalOverlay.classList.add('hidden');
  pendingBuy = null;
}

buyModalCancel.addEventListener('click', closeBuyModal);
buyModalOverlay.addEventListener('click', (e) => {
  if (e.target === buyModalOverlay) closeBuyModal();
});

async function submitBuy() {
  if (!pendingBuy) return;
  const amount_usd = Number(buyModalAmount.value);
  if (!amount_usd || amount_usd <= 0) {
    buyModalError.textContent = 'Enter a valid dollar amount.';
    buyModalError.hidden = false;
    return;
  }
  buyModalConfirm.disabled = true;
  const res = await fetch('/api/paper/buy', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...pendingBuy, amount_usd }),
  });
  const data = await res.json();
  buyModalConfirm.disabled = false;
  if (data.ok) {
    closeBuyModal();
    fetchPaperPortfolio();
  } else {
    buyModalError.textContent = data.error || 'Buy failed.';
    buyModalError.hidden = false;
  }
}

buyModalConfirm.addEventListener('click', submitBuy);
buyModalAmount.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') submitBuy();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !buyModalOverlay.classList.contains('hidden')) closeBuyModal();
});

// ---------------- Watchlist panel ----------------

async function fetchWatchlist() {
  const res = await fetch('/api/watchlist');
  const data = await res.json();
  currentWatchlist = data.watchlist || [];
  renderWatchlist();
}

function renderWatchlist() {
  watchlistCountEl.textContent = currentWatchlist.length;
  if (currentWatchlist.length === 0) {
    watchlistItemsEl.innerHTML = '<li class="empty-note">Nothing watched yet — everything below comes from auto-discover only.</li>';
    return;
  }
  watchlistItemsEl.innerHTML = currentWatchlist.map(([chain, address, label]) => `
    <li class="watchlist-item">
      <div class="wl-item-text">
        <span class="wl-item-label">${escapeHtml(label || address.slice(0, 8))}</span>
        <span class="wl-item-meta">${escapeHtml(chain)} · ${escapeHtml(address.slice(0, 4))}…${escapeHtml(address.slice(-4))}</span>
      </div>
      <button class="wl-remove-btn" data-chain="${escapeHtml(chain)}" data-address="${escapeHtml(address)}" title="Remove">✕</button>
    </li>
  `).join('');
}

watchlistItemsEl.addEventListener('click', async (e) => {
  const btn = e.target.closest('.wl-remove-btn');
  if (!btn) return;
  btn.disabled = true;
  const res = await fetch('/api/watchlist/remove', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chain_id: btn.dataset.chain, token_address: btn.dataset.address }),
  });
  const data = await res.json();
  if (data.ok) {
    currentWatchlist = data.watchlist;
    renderWatchlist();
    fetchStatus(); // a starred row in the table may need to un-star
  }
  btn.disabled = false;
});

wlChainSelect.addEventListener('change', () => {
  const custom = wlChainSelect.value === '__custom';
  wlChainCustom.hidden = !custom;
  if (custom) wlChainCustom.focus();
});

wlAddBtn.addEventListener('click', async () => {
  const chain_id = wlChainSelect.value === '__custom'
    ? wlChainCustom.value.trim()
    : wlChainSelect.value;
  const token_address = wlAddressInput.value.trim();
  const label = wlLabelInput.value.trim();

  if (!chain_id || !token_address) {
    wlAddressInput.focus();
    return;
  }

  wlAddBtn.disabled = true;
  const res = await fetch('/api/watchlist/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chain_id, token_address, label }),
  });
  const data = await res.json();
  wlAddBtn.disabled = false;
  if (data.ok) {
    currentWatchlist = data.watchlist;
    renderWatchlist();
    wlAddressInput.value = '';
    wlLabelInput.value = '';
    fetchStatus();
  }
});

// ---------------- Controls ----------------

toggleBtn.addEventListener('click', async () => {
  const action = isRunning ? 'pause' : 'start';
  await fetch('/api/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  });
  fetchStatus();
});

pollNowBtn.addEventListener('click', async () => {
  pollNowBtn.disabled = true;
  pollNowBtn.textContent = 'Checking…';
  await fetch('/api/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'poll_now' }),
  });
  setTimeout(() => {
    pollNowBtn.disabled = false;
    pollNowBtn.textContent = 'Check now';
    fetchStatus();
    fetchAlerts();
  }, 4000);
});

settingsToggle.addEventListener('click', () => {
  settingsForm.classList.toggle('collapsed');
  settingsChevron.classList.toggle('collapsed');
});

// ---------------- Settings (operational only — thresholds live in the filter bar) ----------------

async function loadConfig() {
  const res = await fetch('/api/config');
  const cfg = await res.json();
  document.getElementById('cfg-auto-discover').checked = !!cfg.auto_discover;
  document.getElementById('cfg-poll-interval').value = cfg.poll_interval_seconds;
  document.getElementById('cfg-alert-score').value = cfg.alert_score_threshold;
  document.getElementById('cfg-confirm-polls').value = cfg.confirm_polls;
  document.getElementById('cfg-retention-days').value = cfg.known_universe_retention_days;
  document.getElementById('cfg-telegram-token').value = cfg.telegram_bot_token;
  document.getElementById('cfg-telegram-chat').value = cfg.telegram_chat_id;
  alertScoreThreshold = Number(cfg.alert_score_threshold) || 70;

  applyFiltersToInputs(FILTER_DEFAULTS);
}

settingsForm.addEventListener('submit', async (e) => {
  e.preventDefault();

  const payload = {
    auto_discover: document.getElementById('cfg-auto-discover').checked,
    poll_interval_seconds: Number(document.getElementById('cfg-poll-interval').value),
    alert_score_threshold: Number(document.getElementById('cfg-alert-score').value),
    confirm_polls: Number(document.getElementById('cfg-confirm-polls').value),
    known_universe_retention_days: Number(document.getElementById('cfg-retention-days').value),
    telegram_bot_token: document.getElementById('cfg-telegram-token').value,
    telegram_chat_id: document.getElementById('cfg-telegram-chat').value,
  };

  await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  alertScoreThreshold = payload.alert_score_threshold;
  renderTable();
  saveNote.textContent = 'Saved';
  saveNote.classList.add('show');
  setTimeout(() => saveNote.classList.remove('show'), 2000);
});

updateSortIndicators();
loadConfig();
fetchWatchlist();
fetchStatus();
fetchAlerts();
fetchPaperPortfolio();
setInterval(fetchStatus, 5000);
setInterval(fetchAlerts, 10000);
setInterval(fetchPaperPortfolio, 5000);
