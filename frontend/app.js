/**
 * AlphaTrader Pro — Main Application JS
 * Powered by DeepSeek-R1 AI
 */

const API = '';  // Same origin
let ws = null;
let chart = null;
let candleSeries = null;
let currentChartSymbol = 'AAPL';
let currentRegion = 'Americas';
let marketData = {};
let portfolioData = {};
let priceCache = {};
let chatHistory = [];
let layoffCandidatesData = [];
let layoffEventsData = [];
let layoffResultsData = null;
let COLORS = ['#388bfd', '#3fb950', '#d4a820', '#f85149', '#a371f7', '#58a6ff', '#e3b341', '#f0883e'];

let authToken = localStorage.getItem('auth_token') || '';
let currentUser = null;

async function authFetch(url, options = {}) {
    if (!options.headers) options.headers = {};
    if (authToken) {
        options.headers['Authorization'] = `Bearer ${authToken}`;
    }
    if (options.method && options.method !== 'GET' && !options.headers['Content-Type']) {
        options.headers['Content-Type'] = 'application/json';
    }

    try {
        const res = await fetch(url, options);
        if (res.status === 401) {
            handleLogout();
            throw new Error('会话过期，请重新登录');
        }
        return res;
    } catch (e) {
        console.error('Fetch error:', e);
        throw e;
    }
}

// ─────────────────────────────────────────────
// Init
// ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    startClock();
    if (!authToken) {
        // Auto-login without requiring credentials
        try {
            const res = await fetch('/api/auth/auto-login');
            if (res.ok) {
                const data = await res.json();
                authToken = data.access_token;
                localStorage.setItem('auth_token', authToken);
            }
        } catch (e) {
            console.error('Auto-login failed:', e);
        }
    }
    if (authToken) {
        await initApp();
    } else {
        document.getElementById('authOverlay').style.display = 'flex';
    }

    // Time
    setInterval(() => {
        if (authToken) {
            loadPortfolio();
            updateSidebarEquity();
        }
    }, 60000);
});

async function initApp() {
    try {
        const res = await authFetch('/api/auth/me');
        if (!res.ok) throw new Error();
        currentUser = await res.json();
        updateUserUI();

        document.getElementById('authOverlay').style.display = 'none';
        connectWebSocket();
        loadMarkets();
        loadPortfolio();
        loadSettings();
        loadSignals();
        loadWatchlist();
    } catch (e) {
        handleLogout();
    }
}

function updateUserUI() {
    if (!currentUser) return;
    document.getElementById('userNameDisplay').textContent = currentUser.username;
    document.getElementById('userInitial').textContent = currentUser.username[0].toUpperCase();
}

function startClock() {
    function tick() {
        const now = new Date();
        document.getElementById('headerTime').textContent = now.toLocaleTimeString('zh-CN', { hour12: false });
    }
    tick();
    setInterval(tick, 1000);
}

// ─────────────────────────────────────────────
// WebSocket
// ─────────────────────────────────────────────
function connectWebSocket() {
    // Force WSS if we are on HTTPS, otherwise WS. Cloudflare/Proxies sometimes confuse location.protocol.
    const isSecure = window.location.protocol === 'https:' || window.location.hostname.includes('.sail.cloud.nesi.nz');
    const proto = isSecure ? 'wss:' : 'ws:';
    const host = location.host;

    console.log(`Initialising WebSocket connection to ${proto}//${host}/ws`);
    ws = new WebSocket(`${proto}//${host}/ws`);

    ws.onopen = () => {
        console.log("WebSocket connected cleanly!");
        document.getElementById('statusDot').style.background = 'var(--green)';
        document.getElementById('statusDot').style.boxShadow = '0 0 6px var(--green)';
        document.getElementById('statusLabel').textContent = '已连接';
        setInterval(() => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: 'ping' })), 30000);
    };

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'price_update') {
            Object.assign(priceCache, msg.prices);
            updateTicker();
            refreshPositionPrices();
        } else if (msg.type === 'trade_executed' || msg.type === 'auto_trade') {
            const t = msg.trade;
            if (t) showToast(`⚡ 自动${t.side === 'BUY' ? '买入' : '卖出'} ${t.symbol} × ${t.quantity} @ $${t.price}`, 'success');
            loadPortfolio();
            if (document.getElementById('page-trades').classList.contains('active')) loadTrades();
        }
    };

    ws.onclose = () => {
        document.getElementById('statusDot').style.background = 'var(--red)';
        document.getElementById('statusDot').style.boxShadow = '0 0 6px var(--red)';
        document.getElementById('statusLabel').textContent = '已断开';
        setTimeout(connectWebSocket, 5000);
    };

    ws.onerror = () => ws.close();
}

// ─────────────────────────────────────────────
// Navigation
// ─────────────────────────────────────────────
function showPage(name) {
    document.querySelectorAll('.main-content').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById(`page-${name}`).classList.add('active');
    document.getElementById(`nav-${name}`).classList.add('active');

    // Refresh on navigate
    if (name === 'portfolio') loadPortfolio();
    if (name === 'trades') loadTrades();
    if (name === 'signals') loadSignals();
    if (name === 'watchlist') loadWatchlist();
    if (name === 'settings') loadSettings();
    if (name === 'layoff') initLayoffPage();
}

// ─────────────────────────────────────────────
// Ticker
// ─────────────────────────────────────────────
function updateTicker() {
    const track = document.getElementById('tickerTrack');
    const symbols = Object.keys(priceCache);
    if (!symbols.length) return;

    // Duplicate for seamless scroll
    const makeItems = () => symbols.map(sym => {
        const p = priceCache[sym];
        const price = typeof p === 'object' ? p.current : p;
        const chg = typeof p === 'object' ? p.change_pct : 0;
        const cls = chg >= 0 ? 'up' : 'down';
        const arrow = chg >= 0 ? '▲' : '▼';
        return `<div class="ticker-item" onclick="loadChartFor('${sym}')">
      <span class="ticker-sym">${sym}</span>
      <span class="ticker-price">$${typeof price === 'number' ? price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : price}</span>
      <span class="ticker-chg ${cls}">${arrow}${Math.abs(chg).toFixed(2)}%</span>
    </div>`;
    }).join('');

    track.innerHTML = makeItems() + makeItems();
}

// ─────────────────────────────────────────────
// Markets
// ─────────────────────────────────────────────
async function loadMarkets() {
    try {
        const res = await authFetch('/api/markets');
        const data = await res.json();
        marketData = data.data;
        showRegion(currentRegion);
        // Init ticker with first region
        const firstRegion = Object.values(marketData)[0] || [];
        firstRegion.forEach(idx => { if (idx.symbol) priceCache[idx.symbol] = idx.current; });
        // Load initial chart
        await loadChart();
    } catch (e) {
        document.getElementById('indicesGrid').innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div>市场数据加载失败</div><div style="font-size:12px;">${e.message}</div></div>`;
    }
}

async function refreshMarkets() {
    const icon = document.getElementById('refreshIcon');
    icon.style.animation = 'spin 0.6s linear infinite';
    await loadMarkets();
    await loadPortfolio();
    icon.style.animation = '';
    showToast('✅ 数据已刷新', 'success');
}

function showRegion(region) {
    currentRegion = region;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    [...document.querySelectorAll('.tab')].find(t => t.textContent.includes(
        region === 'Americas' ? '美洲' : region === 'Europe' ? '欧洲' : '亚太'
    ))?.classList.add('active');

    const grid = document.getElementById('indicesGrid');
    const indices = marketData[region] || [];
    if (!indices.length) {
        grid.innerHTML = '<div class="loading"><div class="spinner"></div> 加载中...</div>';
        return;
    }
    grid.innerHTML = indices.map(idx => {
        const up = idx.change_pct >= 0;
        return `<div class="index-card ${up ? 'up' : 'down'}" onclick="loadChartFor('${idx.symbol}')">
      <div class="index-name">${idx.name}</div>
      <div class="index-region">${idx.region || ''}</div>
      <div class="index-price" style="color:${up ? 'var(--green)' : 'var(--red)'}">${idx.current?.toLocaleString('en-US', { minimumFractionDigits: 2 }) || '--'}</div>
      <div class="index-change ${up ? 'text-green' : 'text-red'}">
        <span>${up ? '▲' : '▼'}</span>
        <span>${Math.abs(idx.change_pct).toFixed(2)}%</span>
        <span style="color:var(--text-muted)">${up ? '+' : ''}${idx.change?.toFixed(2) || '0.00'}</span>
      </div>
    </div>`;
    }).join('');
}

// ─────────────────────────────────────────────
// Chart
// ─────────────────────────────────────────────
function loadChartFor(symbol) {
    document.getElementById('chartSymbolInput').value = symbol;
    loadChart();
    // Switch to markets tab if not already there
    if (!document.getElementById('page-markets').classList.contains('active')) {
        showPage('markets');
    }
}

async function loadChart() {
    const symbol = document.getElementById('chartSymbolInput').value.trim().toUpperCase();
    const period = document.getElementById('chartPeriod').value;
    if (!symbol) return;
    currentChartSymbol = symbol;

    const container = document.getElementById('mainChart');
    container.innerHTML = '<div class="loading"><div class="spinner"></div> 加载图表...</div>';
    document.getElementById('chartMeta').innerHTML = '';

    try {
        const res = await authFetch(`/api/stock/${symbol}?period=${period}`);
        if (!res.ok) throw new Error(`股票 ${symbol} 未找到`);
        const data = await res.json();

        // Render meta
        const q = data.quote;
        const up = q.change_pct >= 0;
        document.getElementById('chartMeta').innerHTML = `
      <span style="font-size:16px;font-weight:700;">${q.name || symbol}</span>
      <span class="font-mono" style="font-size:20px;font-weight:700;color:${up ? 'var(--green)' : 'var(--red)'}">${q.currency === 'CNY' ? '¥' : '$'}${q.current?.toLocaleString('en-US', { minimumFractionDigits: 2 })}</span>
      <span class="${up ? 'text-green' : 'text-red'}" style="font-size:14px;">${up ? '▲' : '▼'}${Math.abs(q.change_pct).toFixed(2)}%</span>
      <span class="text-muted" style="font-size:12px;">P/E: ${q.pe_ratio?.toFixed(1) || 'N/A'}</span>
      <span class="text-muted" style="font-size:12px;">52W: ${q.currency === 'CNY' ? '¥' : '$'}${q.fifty_two_week_low?.toFixed(2) || '--'} - ${q.currency === 'CNY' ? '¥' : '$'}${q.fifty_two_week_high?.toFixed(2) || '--'}</span>
    `;

        // Clear & render chart
        container.innerHTML = '';
        chart = LightweightCharts.createChart(container, {
            width: container.clientWidth,
            height: 315,
            layout: { background: { color: '#ffffff' }, textColor: '#1f2328' },
            grid: { vertLines: { color: 'rgba(31,35,40,0.06)' }, horzLines: { color: 'rgba(31,35,40,0.06)' } },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            rightPriceScale: { borderColor: 'rgba(31,35,40,0.15)' },
            timeScale: { borderColor: 'rgba(31,35,40,0.15)', timeVisible: true },
        });

        candleSeries = chart.addCandlestickSeries({
            upColor: '#3fb950', downColor: '#f85149',
            borderUpColor: '#3fb950', borderDownColor: '#f85149',
            wickUpColor: '#3fb950', wickDownColor: '#f85149',
        });

        if (data.history?.length) {
            candleSeries.setData(data.history);
            chart.timeScale().fitContent();
        }

        // Indicators overlay
        const inds = data.indicators;
        if (inds?.ma20 && data.history?.length) {
            const ma20Series = chart.addLineSeries({ color: 'rgba(56,139,253,0.7)', lineWidth: 1, title: 'MA20' });
            const maData = data.history.slice(-data.history.length).map(d => ({ time: d.time, value: inds.ma20 }));
            // Use rolling MA data if we have more -- simplified approach
        }

    } catch (e) {
        container.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div>${e.message}</div></div>`;
    }
}

async function analyzeFromChart() {
    document.getElementById('analyzeSymbolInput').value = currentChartSymbol;
    showPage('signals');
    await analyzeStock();
}

function quickBuy() {
    openTradeModal(currentChartSymbol, 'BUY');
}
function quickSell() {
    openTradeModal(currentChartSymbol, 'SELL');
}

// ─────────────────────────────────────────────
// Portfolio
// ─────────────────────────────────────────────
async function loadPortfolio() {
    try {
        const res = await authFetch('/api/portfolio');
        portfolioData = await res.json();
        updatePortfolioUI();
        updateSidebarEquity();
    } catch (e) {
        console.error('Portfolio load error:', e);
    }
}

function updatePortfolioUI() {
    const d = portfolioData;
    if (!d.total_equity) return;

    const up = d.total_return >= 0;
    const fmtUSD = (n) => n?.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2 }) || '$0.00';

    // Markets page stats
    document.getElementById('totalEquity').textContent = fmtUSD(d.total_equity);
    document.getElementById('totalReturn').className = `stat-value ${up ? 'text-green' : 'text-red'}`;
    document.getElementById('totalReturn').textContent = fmtUSD(d.total_return);
    document.getElementById('totalReturnPct').textContent = `${up ? '+' : ''}${d.total_return_pct?.toFixed(2)}%`;
    document.getElementById('totalReturnPct').className = `stat-sub ${up ? 'text-green' : 'text-red'}`;
    document.getElementById('marketValue').textContent = fmtUSD(d.total_market_value);
    document.getElementById('unrealizedPnl').textContent = `未实现 ${fmtUSD(d.unrealized_pnl)}`;
    document.getElementById('cashBalance').textContent = fmtUSD(d.cash);

    // Portfolio page stats
    document.getElementById('pf-equity').textContent = fmtUSD(d.total_equity);
    document.getElementById('pf-return').className = `stat-value ${up ? 'text-green' : 'text-red'}`;
    document.getElementById('pf-return').textContent = fmtUSD(d.total_return);
    document.getElementById('pf-return-pct').textContent = `${up ? '+' : ''}${d.total_return_pct?.toFixed(2)}%`;
    document.getElementById('pf-unrealized').className = `stat-value ${d.unrealized_pnl >= 0 ? 'text-green' : 'text-red'}`;
    document.getElementById('pf-unrealized').textContent = fmtUSD(d.unrealized_pnl);
    document.getElementById('pf-trades').textContent = d.total_trades || 0;

    // Show provider tag
    let titleDiv = document.querySelector('.card-title.portfolio-title');
    if (!titleDiv) {
        titleDiv = document.createElement('div');
        titleDiv.className = 'card-title portfolio-title';
        titleDiv.style.display = 'flex';
        titleDiv.style.justifyContent = 'space-between';
        const oldTitle = document.querySelector('#portfolio .card-title');
        if (oldTitle) {
            oldTitle.replaceWith(titleDiv);
        }
    }
    const isLive = d.provider === 'Alpaca';
    titleDiv.innerHTML = `💼 投资组合概要 <span style="font-size:12px;padding:4px 8px;border-radius:12px;background:var(--bg-tertiary);color:var(--text-muted);">${isLive ? '🏦 Alpaca 真实通道' : '📝 本地模拟盘'}</span>`;

    // Dynamically update UI labels
    const providerText = isLive ? 'Alpaca 实盘通道' : '本地模拟模式 (Local Paper)';
    const providerHtml = isLive ? '🏆 真实资产通道' : '📝 本地虚拟账户';

    const sidebarEl = document.getElementById('sidebarModeLabel');
    if (sidebarEl) {
        sidebarEl.textContent = isLive ? '🔴 LIVE 运行中' : '模拟交易模式';
        if (isLive) sidebarEl.style.color = 'var(--red)';
    }
    const eqLabel = document.getElementById('equityProviderLabel');
    if (eqLabel) eqLabel.textContent = providerHtml;
    const cashLabel = document.getElementById('cashProviderLabel');
    if (cashLabel) cashLabel.textContent = '📊 ' + (isLive ? '真实通道资金' : '纸上交易');
    const setLabel = document.getElementById('settingsProviderLabel');
    if (setLabel) setLabel.textContent = providerText;


    // Positions table
    const positions = d.positions || [];
    const tbl = document.getElementById('positionsTable');
    if (!positions.length) {
        tbl.innerHTML = `<div class="empty-state"><div class="empty-icon">📭</div><div>暂无持仓</div></div>`;
        return;
    }
    tbl.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>股票</th><th>数量</th><th>均价</th><th>现价</th>
          <th>市值</th><th>盈亏</th><th>盈亏%</th><th>仓位%</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        ${positions.map(p => `
          <tr>
            <td><span style="font-weight:600;cursor:pointer;" onclick="loadChartFor('${p.symbol}')">${p.symbol}</span></td>
            <td>${p.quantity}</td>
            <td>$${p.avg_cost?.toFixed(2)}</td>
            <td>$${p.current_price?.toFixed(2)}</td>
            <td>$${p.market_value?.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
            <td class="${p.unrealized_pnl >= 0 ? 'text-green' : 'text-red'}">$${p.unrealized_pnl?.toFixed(2)}</td>
            <td class="${p.unrealized_pnl_pct >= 0 ? 'text-green' : 'text-red'}">${p.unrealized_pnl_pct?.toFixed(2)}%</td>
            <td>${p.weight_pct?.toFixed(1)}%</td>
            <td>
              <button class="btn btn-success btn-sm" onclick="openTradeModal('${p.symbol}','BUY')">买</button>
              <button class="btn btn-danger btn-sm" onclick="openTradeModal('${p.symbol}','SELL')">卖</button>
            </td>
          </tr>
        `).join('')}
      </tbody>
    </table>`;
}

function refreshPositionPrices() {
    // Update displayed prices if portfolio is visible
    if (!portfolioData?.positions) return;
    portfolioData.positions.forEach(p => {
        if (priceCache[p.symbol]) {
            const price = typeof priceCache[p.symbol] === 'object' ? priceCache[p.symbol].current : priceCache[p.symbol];
            p.current_price = price;
            p.unrealized_pnl = (price - p.avg_cost) * p.quantity;
            p.unrealized_pnl_pct = ((price - p.avg_cost) / p.avg_cost * 100);
            p.market_value = price * p.quantity;
        }
    });
}

function updateSidebarEquity() {
    const d = portfolioData;
    if (!d.total_equity) return;
    document.getElementById('sidebarEquity').textContent =
        d.total_equity?.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2 });
    const up = d.total_return >= 0;
    document.getElementById('sidebarPnl').textContent =
        `总收益 $${d.total_return?.toFixed(2)} (${up ? '+' : ''}${d.total_return_pct?.toFixed(2)}%)`;
    document.getElementById('sidebarPnl').className = up ? 'text-green' : 'text-red';
}

async function analyzePortfolio() {
    const card = document.getElementById('pfAnalysisCard');
    const content = document.getElementById('pfAnalysisContent');
    card.style.display = 'block';
    content.innerHTML = '<div class="loading"><div class="spinner"></div> DeepSeek-R1 正在分析您的投资组合...</div>';

    try {
        const res = await authFetch('/api/analyze-portfolio', { method: 'POST' });
        const data = await res.json();
        const score = data.portfolio_score || '--';
        const scoreColor = score >= 7 ? 'var(--green)' : score >= 5 ? 'var(--yellow)' : 'var(--red)';

        content.innerHTML = `
      <div style="display:flex;gap:20px;margin-bottom:16px;flex-wrap:wrap;">
        <div class="stat-card" style="flex:1;min-width:140px;">
          <div class="stat-label">综合评分</div>
          <div class="stat-value" style="color:${scoreColor}">${score}/10</div>
        </div>
        <div class="stat-card" style="flex:1;min-width:140px;">
          <div class="stat-label">分散度</div>
          <div class="stat-value text-accent" style="font-size:18px;">${data.diversification_rating || '--'}</div>
        </div>
        <div class="stat-card" style="flex:1;min-width:140px;">
          <div class="stat-label">风险等级</div>
          <div class="stat-value" style="font-size:18px;">${data.risk_level || '--'}</div>
        </div>
      </div>
      <div class="ai-analysis-box" style="margin-bottom:12px;max-height:none;">${data.overall_assessment || ''}</div>
      ${data.suggestions?.length ? `
        <div style="margin-top:12px;">
          <div style="font-size:12px;font-weight:600;color:var(--text-muted);margin-bottom:8px;">AI 建议</div>
          ${data.suggestions.map(s => `
            <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);">
              <span class="signal-badge signal-${s.action}">${s.action}</span>
              <span style="font-weight:600;">${s.symbol || ''}</span>
              <span style="font-size:12px;color:var(--text-secondary);flex:1;">${s.reason}</span>
              <span style="font-size:11px;color:var(--text-muted);">${s.urgency}</span>
            </div>
          `).join('')}
        </div>
      ` : ''}
    `;
    } catch (e) {
        content.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div>${e.message}</div></div>`;
    }
}

async function resetPortfolio() {
    await authFetch('/api/reset-portfolio', { method: 'POST' });
    portfolioData = {};
    await loadPortfolio();
    showToast('✅ 账户已重置为 $100,000', 'success');
}

// ─────────────────────────────────────────────
// Watchlist
// ─────────────────────────────────────────────
async function loadWatchlist() {
    try {
        const res = await authFetch('/api/watchlist');
        const data = await res.json();
        const symbols = data.symbols || [];
        const container = document.getElementById('watchlistContent');

        if (!symbols.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">⭐</div><div>自选股为空</div></div>';
            return;
        }

        // Fetch quotes in parallel (limited)
        const quotes = await Promise.all(
            symbols.slice(0, 30).map(sym =>
                authFetch(`/api/stock/${sym}`).then(r => r.json()).catch(() => null)
            )
        );

        container.innerHTML = `
      <table class="data-table">
        <thead><tr><th>股票</th><th>名称</th><th>现价</th><th>涨跌</th><th>涨跌%</th><th>成交量</th><th>操作</th></tr></thead>
        <tbody>${quotes.map((d, i) => {
            if (!d?.quote) return `<tr><td>${symbols[i]}</td><td colspan="5" style="color:var(--text-muted)">数据加载失败</td><td><button class="btn btn-ghost btn-sm" onclick="removeFromWatchlist('${symbols[i]}')">✕</button></td></tr>`;
            const q = d.quote;
            const up = q.change_pct >= 0;
            return `<tr>
            <td style="font-weight:600;cursor:pointer;" onclick="loadChartFor('${q.symbol}')">${q.symbol}</td>
            <td style="color:var(--text-secondary);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${q.name || ''}</td>
            <td>${q.currency === 'CNY' ? '¥' : '$'}${q.current?.toFixed(2)}</td>
            <td class="${up ? 'text-green' : 'text-red'}">${up ? '+' : ''}${q.change?.toFixed(2)}</td>
            <td class="${up ? 'text-green' : 'text-red'}">${up ? '+' : ''}${q.change_pct?.toFixed(2)}%</td>
            <td>${(q.volume / 1000000).toFixed(2)}M</td>
            <td style="display:flex;gap:4px;">
              <button class="btn btn-gold btn-sm" onclick="analyzeSymbol('${q.symbol}')">🤖</button>
              <button class="btn btn-success btn-sm" onclick="openTradeModal('${q.symbol}','BUY')">买</button>
              <button class="btn btn-ghost btn-sm" onclick="removeFromWatchlist('${q.symbol}')">✕</button>
            </td>
          </tr>`;
        }).join('')}</tbody>
      </table>`;
    } catch (e) {
        document.getElementById('watchlistContent').innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div>${e.message}</div></div>`;
    }
}

async function addToWatchlist() {
    const input = document.getElementById('watchlistSearch');
    const symbol = input.value.trim().toUpperCase();
    if (!symbol) return;
    try {
        const res = await authFetch('/api/watchlist', {
            method: 'POST',
            body: JSON.stringify({ symbol, action: 'add' })
        });
        input.value = '';
        showToast(`⭐ ${symbol} 已加入自选股`, 'success');
        loadWatchlist();
    } catch (e) {
        showToast(`添加失败: ${e.message}`, 'error');
    }
}

async function removeFromWatchlist(symbol) {
    await authFetch('/api/watchlist', {
        method: 'POST',
        body: JSON.stringify({ symbol, action: 'remove' })
    });
    showToast(`已移除 ${symbol}`, 'info');
    loadWatchlist();
}

// ─────────────────────────────────────────────
// Trades
// ─────────────────────────────────────────────
async function loadTrades() {
    try {
        const res = await authFetch('/api/trades?limit=100');
        const data = await res.json();
        const trades = data.trades || [];
        const tbl = document.getElementById('tradesTable');

        if (!trades.length) {
            tbl.innerHTML = '<div class="empty-state"><div class="empty-icon">📋</div><div>暂无交易记录</div></div>';
            return;
        }
        tbl.innerHTML = `
      <table class="data-table">
        <thead><tr><th>时间</th><th>股票</th><th>方向</th><th>数量</th><th>价格</th><th>总额</th><th>来源</th><th>置信度</th></tr></thead>
        <tbody>${trades.map(t => `<tr>
          <td style="color:var(--text-muted);font-size:11px;">${new Date(t.timestamp).toLocaleString('zh-CN')}</td>
          <td style="font-weight:600;cursor:pointer;" onclick="loadChartFor('${t.symbol}')">${t.symbol}</td>
          <td><span class="signal-badge signal-${t.side}">${t.side === 'BUY' ? '买入' : '卖出'}</span></td>
          <td>${t.quantity}</td>
          <td>$${t.price?.toFixed(2)}</td>
          <td>$${t.total_value?.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
          <td style="color:${t.ai_triggered ? 'var(--gold-light)' : 'var(--text-muted)'}">${t.ai_triggered ? '🤖 AI' : '手动'}</td>
          <td>${t.ai_confidence ? `${(t.ai_confidence * 100).toFixed(0)}%` : '--'}</td>
        </tr>`).join('')}</tbody>
      </table>`;
    } catch (e) {
        document.getElementById('tradesTable').innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div>${e.message}</div></div>`;
    }
}

// ─────────────────────────────────────────────
// Trade Modal
// ─────────────────────────────────────────────
function openTradeModal(symbol = '', side = 'BUY') {
    document.getElementById('tradeSymbol').value = symbol;
    document.getElementById('tradePrice').value = '';
    document.getElementById('tradeQty').value = '1';
    document.getElementById('tradeModalTitle').textContent = `${side === 'BUY' ? '买入' : '卖出'} ${symbol}`;
    document.getElementById('tradeCostPreview').textContent = '';
    document.getElementById('tradeModal').classList.add('open');
    updateTradeCostPreview();
}

function openAnalyzeModal() { showPage('signals'); }

function closeModal(id) {
    document.getElementById(id).classList.remove('open');
}

document.getElementById('tradeSymbol').addEventListener('input', updateTradeCostPreview);
document.getElementById('tradeQty').addEventListener('input', updateTradeCostPreview);
document.getElementById('tradePrice').addEventListener('input', updateTradeCostPreview);

function updateTradeCostPreview() {
    const sym = document.getElementById('tradeSymbol').value.toUpperCase();
    const qty = parseFloat(document.getElementById('tradeQty').value) || 0;
    const priceInput = parseFloat(document.getElementById('tradePrice').value);
    const livePrice = priceCache[sym] ? (typeof priceCache[sym] === 'object' ? priceCache[sym].current : priceCache[sym]) : null;
    const price = priceInput || livePrice || 0;
    const cost = qty * price;

    if (cost > 0) {
        document.getElementById('tradeCostPreview').innerHTML =
            `预计金额: <strong>$${cost.toLocaleString('en-US', { minimumFractionDigits: 2 })}</strong>
       ${price ? `| 参考价: $${price.toFixed(2)}` : ''}
       | 可用现金: $${(portfolioData.cash || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
    }
}

async function submitTrade(side) {
    const symbol = document.getElementById('tradeSymbol').value.trim().toUpperCase();
    const qty = parseFloat(document.getElementById('tradeQty').value);
    const priceInput = parseFloat(document.getElementById('tradePrice').value) || undefined;

    if (!symbol || !qty) { showToast('请输入股票代码和数量', 'error'); return; }

    try {
        const res = await authFetch('/api/trade', {
            method: 'POST',
            body: JSON.stringify({ symbol, side, quantity: qty, price: priceInput || null })
        });
        if (!res.ok) {
            const err = await res.json();
            showToast(`❌ ${err.detail}`, 'error'); return;
        }
        closeModal('tradeModal');
        showToast(`✅ ${side === 'BUY' ? '买入' : '卖出'} ${qty} ${symbol} 成功`, 'success');
        loadPortfolio();
        loadTrades();
    } catch (e) {
        showToast(`交易失败: ${e.message}`, 'error');
    }
}

// ─────────────────────────────────────────────
// AI Analysis
// ─────────────────────────────────────────────
function analyzeSymbol(symbol) {
    document.getElementById('analyzeSymbolInput').value = symbol;
    showPage('signals');
    analyzeStock();
}

async function analyzeStock() {
    const symbol = document.getElementById('analyzeSymbolInput').value.trim().toUpperCase();
    if (!symbol) { showToast('请输入股票代码', 'error'); return; }

    const btn = document.getElementById('analyzeBtn');
    btn.disabled = true;
    btn.innerHTML = '<div class="spinner" style="width:14px;height:14px;border-width:1.5px;"></div> 分析中...';

    const resultDiv = document.getElementById('quickAnalysisResult');
    resultDiv.innerHTML = `<div style="font-size:12px;color:var(--text-muted);padding:8px 0;">正在使用 DeepSeek-R1 深度分析 ${symbol}，请稍候...</div>`;

    try {
        const res = await authFetch('/api/analyze', {
            method: 'POST',
            body: JSON.stringify({ symbol })
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || '分析失败');
        }
        const data = await res.json();
        const s = data.signal;
        const conf = s.confidence || 0;
        const confPct = (conf * 100).toFixed(0);
        const confClass = conf >= 0.7 ? 'conf-high' : conf >= 0.5 ? 'conf-mid' : 'conf-low';

        resultDiv.innerHTML = `
      <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:8px;">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;">
          <span class="signal-badge signal-${s.signal}">${s.signal === 'BUY' ? '📈 买入' : s.signal === 'SELL' ? '📉 卖出' : '⏸️ 持有'}</span>
          <div style="flex:1;display:flex;align-items:center;gap:8px;">
            <div class="confidence-bar" style="flex:1;"><div class="confidence-fill ${confClass}" style="width:${confPct}%"></div></div>
            <span style="font-size:12px;font-weight:700;color:var(--text-primary);">${confPct}%</span>
          </div>
          ${s.target_price ? `<span style="font-size:12px;color:var(--green);">目标 $${s.target_price}</span>` : ''}
          ${s.stop_loss ? `<span style="font-size:12px;color:var(--red);">止损 $${s.stop_loss}</span>` : ''}
        </div>
        <div class="ai-analysis-box">${s.reasoning || ''}</div>
        ${s.key_factors?.length ? `<div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;">${s.key_factors.map(f => `<span style="background:var(--accent-glow);border:1px solid var(--border-light);border-radius:20px;padding:2px 10px;font-size:11px;color:var(--accent-bright);">${f}</span>`).join('')}</div>` : ''}
        ${data.auto_trade?.success ? `<div style="margin-top:10px;padding:8px;background:var(--green-dim);border:1px solid rgba(63,185,80,0.3);border-radius:8px;font-size:12px;color:var(--green);">⚡ 自动交易已执行</div>` : ''}
      </div>
    `;

        loadSignals();
    } catch (e) {
        resultDiv.innerHTML = `<div class="empty-state" style="padding:16px;"><div class="empty-icon">⚠️</div><div>${e.message}</div></div>`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '🤖 分析';
    }
}

async function loadSignals() {
    try {
        const res = await authFetch('/api/signals?limit=20');
        const data = await res.json();
        const signals = data.signals || [];

        // Auto trade status
        const settings = await authFetch('/api/settings').then(r => r.json());
        document.getElementById('autoTradeStatus').innerHTML = `
      <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:13px;">
        <div>自动交易: <strong style="color:${settings.auto_trade_enabled === 'true' ? 'var(--green)' : 'var(--text-muted)'}">${settings.auto_trade_enabled === 'true' ? '✅ 启用' : '❌ 停用'}</strong></div>
        <div>最低置信度: <strong>${(parseFloat(settings.auto_trade_min_confidence || 0.75) * 100).toFixed(0)}%</strong></div>
        <div>单笔风险: <strong>${settings.risk_per_trade_pct}%</strong></div>
      </div>
      <div style="margin-top:8px;font-size:12px;color:var(--text-muted);">在"设置"中配置自动交易参数</div>`;

        const container = document.getElementById('signalsList');
        if (!signals.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">🤖</div><div>暂无信号记录</div><div style="font-size:12px;">在上方输入股票代码进行分析</div></div>';
            return;
        }
        container.innerHTML = `
      <table class="data-table">
        <thead><tr><th>时间</th><th>股票</th><th>信号</th><th>置信度</th><th>目标价</th><th>止损价</th><th>模型</th><th>操作</th></tr></thead>
        <tbody>${signals.map(s => {
            const conf = s.confidence || 0;
            const confPct = (conf * 100).toFixed(0);
            const confClass = conf >= 0.7 ? 'conf-high' : conf >= 0.5 ? 'conf-mid' : 'conf-low';
            return `<tr>
            <td style="font-size:11px;color:var(--text-muted);">${new Date(s.timestamp).toLocaleString('zh-CN')}</td>
            <td style="font-weight:600;cursor:pointer;" onclick="loadChartFor('${s.symbol}')">${s.symbol}</td>
            <td><span class="signal-badge signal-${s.signal}">${s.signal}</span></td>
            <td>
              <div style="display:flex;align-items:center;gap:6px;">
                <div class="confidence-bar" style="width:60px;"><div class="confidence-fill ${confClass}" style="width:${confPct}%"></div></div>
                <span style="font-size:11px;">${confPct}%</span>
              </div>
            </td>
            <td>${s.target_price ? `$${s.target_price?.toFixed(2)}` : '--'}</td>
            <td>${s.stop_loss ? `$${s.stop_loss?.toFixed(2)}` : '--'}</td>
            <td style="font-size:10px;color:var(--gold-light);">${s.model || 'deepseek-reasoner'}</td>
            <td>
              <button class="btn btn-ghost btn-sm" onclick="showReasoning('${encodeURIComponent(s.reasoning || '')}', '${s.symbol}', '${s.signal}')">查看分析</button>
            </td>
          </tr>`;
        }).join('')}</tbody>
      </table>`;
    } catch (e) {
        document.getElementById('signalsList').innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div>${e.message}</div></div>`;
    }
}

function showReasoning(encoded, symbol, signal) {
    const text = decodeURIComponent(encoded);
    // Show in a simple popup
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay open';
    overlay.innerHTML = `<div class="modal">
    <div class="modal-header">
      <div class="modal-title">🤖 ${symbol} — DeepSeek-R1 分析
        <span class="signal-badge signal-${signal}" style="margin-left:8px;">${signal}</span>
      </div>
      <button class="modal-close" onclick="this.closest('.modal-overlay').remove()">✕</button>
    </div>
    <div class="ai-analysis-box" style="max-height:60vh;">${text}</div>
  </div>`;
    document.body.appendChild(overlay);
}

// ─────────────────────────────────────────────
// Layoff Framework
// ─────────────────────────────────────────────
function initLayoffPage() {
    renderLayoffCandidates();
    renderLayoffEventEditor();
    renderLayoffSummary(null);
    renderLayoffPathChart([]);
    renderLayoffResultTable([]);
}

function escapeHTML(text) {
    return (text || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

async function discoverLayoffCandidates() {
    const hoursBack = parseInt(document.getElementById('layoffHoursBack').value || '168', 10);
    const extra = (document.getElementById('layoffExtraSymbols').value || '')
        .split(',')
        .map(s => s.trim().toUpperCase())
        .filter(Boolean);

    const panel = document.getElementById('layoffCandidates');
    panel.innerHTML = '<div class="loading"><div class="spinner"></div>抓取中...</div>';

    try {
        const res = await authFetch('/api/layoff-framework/discover', {
            method: 'POST',
            body: JSON.stringify({
                use_watchlist: true,
                symbols: extra,
                hours_back: hoursBack,
                max_items: 100
            })
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || '抓取失败');
        }
        const data = await res.json();
        layoffCandidatesData = data.candidates || [];
        if (!layoffEventsData.length && layoffCandidatesData.length) {
            layoffEventsData = layoffCandidatesData.slice(0, 20).map(c => ({
                symbol: c.symbol,
                announcement_date: c.announcement_date,
                layoff_percentage: c.layoff_percentage ?? '',
                layoff_employees: c.layoff_employees ?? '',
                guidance_change: c.guidance_change || '',
            }));
        }
        renderLayoffCandidates();
        renderLayoffEventEditor();
        showToast(`✅ 已发现 ${layoffCandidatesData.length} 条候选事件`, 'success');
    } catch (e) {
        panel.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div>${e.message}</div></div>`;
    }
}

function importSelectedCandidates() {
    const selected = layoffCandidatesData.filter((_, idx) => {
        const el = document.getElementById(`layoff-candidate-${idx}`);
        return el && el.checked;
    });
    if (!selected.length) {
        showToast('请至少勾选一条候选事件', 'error');
        return;
    }

    const exists = new Set(layoffEventsData.map(e => `${e.symbol}|${e.announcement_date}`));
    selected.forEach(c => {
        const key = `${c.symbol}|${c.announcement_date}`;
        if (!exists.has(key)) {
            layoffEventsData.push({
                symbol: c.symbol,
                announcement_date: c.announcement_date,
                layoff_percentage: c.layoff_percentage ?? '',
                layoff_employees: c.layoff_employees ?? '',
                guidance_change: c.guidance_change || '',
            });
        }
    });
    renderLayoffEventEditor();
    showToast(`✅ 已导入 ${selected.length} 条事件`, 'success');
}

function addManualLayoffEvent() {
    layoffEventsData.push({
        symbol: '',
        announcement_date: '',
        layoff_percentage: '',
        layoff_employees: '',
        guidance_change: '',
    });
    renderLayoffEventEditor();
}

function removeLayoffEvent(idx) {
    layoffEventsData.splice(idx, 1);
    renderLayoffEventEditor();
}

function syncLayoffEventsFromEditor() {
    layoffEventsData = layoffEventsData.map((_, idx) => ({
        symbol: (document.getElementById(`layoff-symbol-${idx}`)?.value || '').trim().toUpperCase(),
        announcement_date: (document.getElementById(`layoff-date-${idx}`)?.value || '').trim(),
        layoff_percentage: (document.getElementById(`layoff-pct-${idx}`)?.value || '').trim(),
        layoff_employees: (document.getElementById(`layoff-emp-${idx}`)?.value || '').trim(),
        guidance_change: (document.getElementById(`layoff-guide-${idx}`)?.value || '').trim().toLowerCase(),
    })).filter(e => e.symbol && e.announcement_date);
}

function renderLayoffCandidates() {
    const container = document.getElementById('layoffCandidates');
    if (!layoffCandidatesData.length) {
        container.innerHTML = '<div class="empty-state" style="padding:20px;"><div class="empty-icon">📰</div><div>暂无候选事件，点击“抓取候选”开始扫描</div></div>';
        return;
    }

    container.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:10px;flex-wrap:wrap;">
      <div style="font-size:12px;color:var(--text-muted);">共 ${layoffCandidatesData.length} 条匹配关键词新闻</div>
      <button class="btn btn-success btn-sm" onclick="importSelectedCandidates()">导入勾选项</button>
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr><th></th><th>股票</th><th>公告日</th><th>标题</th><th>线索</th></tr></thead>
        <tbody>
          ${layoffCandidatesData.map((c, idx) => `
            <tr>
              <td><input type="checkbox" id="layoff-candidate-${idx}" checked /></td>
              <td>${c.symbol}</td>
              <td>${c.announcement_date}</td>
              <td style="max-width:420px;white-space:normal;font-family:'Inter',sans-serif;">
                ${c.link ? `<a href="${escapeHTML(c.link)}" target="_blank" style="color:var(--accent-bright);">${escapeHTML(c.headline)}</a>` : escapeHTML(c.headline)}
              </td>
              <td style="white-space:normal;font-family:'Inter',sans-serif;color:var(--text-secondary);">
                ${c.layoff_percentage ? `裁员比例 ${c.layoff_percentage}% ` : ''}
                ${c.layoff_employees ? `人数 ${c.layoff_employees} ` : ''}
                ${c.guidance_change ? `指引 ${c.guidance_change}` : ''}
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>`;
}

function renderLayoffEventEditor() {
    const container = document.getElementById('layoffEventEditor');
    if (!layoffEventsData.length) {
        container.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
          <div class="text-muted" style="font-size:12px;">还没有事件，先导入候选或手动新增</div>
          <button class="btn btn-ghost btn-sm" onclick="addManualLayoffEvent()">+ 手动新增</button>
        </div>`;
        return;
    }
    container.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
      <div style="font-size:12px;color:var(--text-muted);">待评估事件: ${layoffEventsData.length}</div>
      <button class="btn btn-ghost btn-sm" onclick="addManualLayoffEvent()">+ 手动新增</button>
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr><th>股票</th><th>公告日</th><th>裁员%</th><th>裁员人数</th><th>指引</th><th></th></tr></thead>
        <tbody>
          ${layoffEventsData.map((e, idx) => `
            <tr>
              <td><input id="layoff-symbol-${idx}" class="input layoff-edit-input" value="${escapeHTML(e.symbol)}" /></td>
              <td><input id="layoff-date-${idx}" class="input layoff-edit-input" value="${escapeHTML(e.announcement_date)}" placeholder="YYYY-MM-DD" /></td>
              <td><input id="layoff-pct-${idx}" class="input layoff-edit-input" value="${escapeHTML(String(e.layoff_percentage ?? ''))}" /></td>
              <td><input id="layoff-emp-${idx}" class="input layoff-edit-input" value="${escapeHTML(String(e.layoff_employees ?? ''))}" /></td>
              <td><input id="layoff-guide-${idx}" class="input layoff-edit-input" value="${escapeHTML(e.guidance_change || '')}" placeholder="up/down/none" /></td>
              <td><button class="btn btn-ghost btn-sm" onclick="removeLayoffEvent(${idx})">移除</button></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>`;
}

async function evaluateLayoffEvents() {
    syncLayoffEventsFromEditor();
    if (!layoffEventsData.length) {
        showToast('没有可评估事件', 'error');
        return;
    }

    const events = layoffEventsData.map(e => ({
        symbol: e.symbol,
        announcement_date: e.announcement_date,
        layoff_percentage: e.layoff_percentage === '' ? null : parseFloat(e.layoff_percentage),
        layoff_employees: e.layoff_employees === '' ? null : parseInt(e.layoff_employees, 10),
        guidance_change: e.guidance_change || null,
    }));
    const benchmark = (document.getElementById('layoffBenchmark').value || 'SPY').trim().toUpperCase();
    const lookaheadDays = parseInt(document.getElementById('layoffLookahead').value || '20', 10);

    document.getElementById('layoffResultTable').innerHTML = '<div class="loading"><div class="spinner"></div>评估中...</div>';
    try {
        const res = await authFetch('/api/layoff-framework/evaluate', {
            method: 'POST',
            body: JSON.stringify({
                events,
                benchmark_symbol: benchmark,
                lookahead_days: lookaheadDays
            })
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || '评估失败');
        }
        layoffResultsData = await res.json();
        renderLayoffSummary(layoffResultsData);
        renderLayoffResultTable(layoffResultsData.results || []);
        const avgPath = buildAveragePath(layoffResultsData.results || []);
        renderLayoffPathChart(avgPath);
        showToast('✅ 裁员事件框架评估完成', 'success');
    } catch (e) {
        document.getElementById('layoffResultTable').innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div>${e.message}</div></div>`;
    }
}

function renderLayoffSummary(data) {
    const root = document.getElementById('layoffSummaryStats');
    if (!data) {
        root.innerHTML = `
        <div class="stat-card"><div class="stat-label">平均 Day+1</div><div class="stat-value">--</div></div>
        <div class="stat-card"><div class="stat-label">平均反应持续天数</div><div class="stat-value">--</div></div>
        <div class="stat-card"><div class="stat-label">平均强度分数</div><div class="stat-value">--</div></div>`;
        return;
    }
    const d1 = data.avg_day1_return_pct;
    const d1Cls = (d1 || 0) >= 0 ? 'text-green' : 'text-red';
    root.innerHTML = `
      <div class="stat-card">
        <div class="stat-label">平均 Day+1</div>
        <div class="stat-value ${d1Cls}">${d1 == null ? '--' : `${d1 >= 0 ? '+' : ''}${d1.toFixed(2)}%`}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">平均反应持续天数</div>
        <div class="stat-value">${data.avg_reaction_duration_days == null ? '--' : data.avg_reaction_duration_days}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">平均强度分数</div>
        <div class="stat-value">${data.avg_event_strength_score == null ? '--' : data.avg_event_strength_score}</div>
      </div>`;
}

function renderLayoffResultTable(results) {
    const container = document.getElementById('layoffResultTable');
    if (!results.length) {
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">📉</div><div>暂无结果</div></div>';
        return;
    }
    container.innerHTML = `
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr><th>股票</th><th>公告日</th><th>Day+1</th><th>Day+5</th><th>Day+20</th><th>超额Day+5</th><th>持续天数</th><th>强度</th></tr></thead>
        <tbody>
          ${results.map(r => {
              if (r.error) {
                  return `<tr><td>${r.symbol || '--'}</td><td>${r.announcement_date || '--'}</td><td colspan="6" style="color:var(--red);font-family:'Inter',sans-serif;">${escapeHTML(r.error)}</td></tr>`;
              }
              const ret = r.event_window_returns_pct || {};
              const abn = r.abnormal_returns_pct || {};
              const d1 = ret.day_1;
              const d5 = ret.day_5;
              const d20 = ret.day_20;
              const a5 = abn.day_5;
              return `<tr>
                <td>${r.symbol}</td>
                <td>${r.announcement_date}</td>
                <td class="${(d1 || 0) >= 0 ? 'text-green' : 'text-red'}">${d1 == null ? '--' : `${d1 >= 0 ? '+' : ''}${d1.toFixed(2)}%`}</td>
                <td class="${(d5 || 0) >= 0 ? 'text-green' : 'text-red'}">${d5 == null ? '--' : `${d5 >= 0 ? '+' : ''}${d5.toFixed(2)}%`}</td>
                <td class="${(d20 || 0) >= 0 ? 'text-green' : 'text-red'}">${d20 == null ? '--' : `${d20 >= 0 ? '+' : ''}${d20.toFixed(2)}%`}</td>
                <td class="${(a5 || 0) >= 0 ? 'text-green' : 'text-red'}">${a5 == null ? '--' : `${a5 >= 0 ? '+' : ''}${a5.toFixed(2)}%`}</td>
                <td>${r.reaction_duration_days}</td>
                <td>${r.event_strength_score}</td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>`;
}

function buildAveragePath(results) {
    const valid = results.filter(r => !r.error && Array.isArray(r.path_returns_pct) && r.path_returns_pct.length);
    if (!valid.length) return [];
    const byDay = {};
    valid.forEach(r => {
        r.path_returns_pct.forEach(p => {
            if (!byDay[p.day]) byDay[p.day] = [];
            byDay[p.day].push(p.return_pct);
        });
    });
    return Object.keys(byDay)
        .map(k => Number(k))
        .sort((a, b) => a - b)
        .map(day => {
            const vals = byDay[day];
            const avg = vals.reduce((s, v) => s + v, 0) / vals.length;
            return { day, return_pct: avg };
        });
}

function renderLayoffPathChart(path) {
    const container = document.getElementById('layoffPathChart');
    if (!path.length) {
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">📈</div><div>评估后显示平均路径</div></div>';
        return;
    }

    const width = Math.max(680, container.clientWidth || 680);
    const height = 280;
    const pad = { l: 42, r: 18, t: 18, b: 28 };
    const xs = path.map(p => p.day);
    const ys = path.map(p => p.return_pct);
    const maxX = Math.max(...xs);
    const minY = Math.min(0, ...ys);
    const maxY = Math.max(0, ...ys);
    const yRange = (maxY - minY) || 1;

    const xTo = (x) => pad.l + (x / (maxX || 1)) * (width - pad.l - pad.r);
    const yTo = (y) => pad.t + (1 - ((y - minY) / yRange)) * (height - pad.t - pad.b);
    const points = path.map(p => `${xTo(p.day).toFixed(2)},${yTo(p.return_pct).toFixed(2)}`).join(' ');
    const zeroY = yTo(0);
    const last = path[path.length - 1];
    const lineColor = last.return_pct >= 0 ? '#1a7f37' : '#d1242f';

    container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" class="layoff-svg">
      <line x1="${pad.l}" y1="${zeroY}" x2="${width - pad.r}" y2="${zeroY}" stroke="rgba(31,35,40,0.22)" stroke-dasharray="4 4"/>
      <line x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${height - pad.b}" stroke="rgba(31,35,40,0.22)"/>
      <line x1="${pad.l}" y1="${height - pad.b}" x2="${width - pad.r}" y2="${height - pad.b}" stroke="rgba(31,35,40,0.22)"/>
      <polyline fill="none" stroke="${lineColor}" stroke-width="2.5" points="${points}" />
      ${path.map(p => `<circle cx="${xTo(p.day)}" cy="${yTo(p.return_pct)}" r="2.5" fill="${lineColor}" />`).join('')}
      <text x="${pad.l}" y="${height - 8}" fill="#59636e" font-size="11">Day 0</text>
      <text x="${width - 62}" y="${height - 8}" fill="#59636e" font-size="11">Day ${maxX}</text>
      <text x="8" y="${zeroY - 4}" fill="#59636e" font-size="11">0%</text>
      <text x="${width - 136}" y="${pad.t + 10}" fill="${lineColor}" font-size="12">Avg: ${last.return_pct >= 0 ? '+' : ''}${last.return_pct.toFixed(2)}%</text>
    </svg>`;
}

// ─────────────────────────────────────────────
// AI Chat
// ─────────────────────────────────────────────
async function sendChat() {
    const input = document.getElementById('chatInput');
    const text = input.value.trim();
    if (!text) return;

    input.value = '';
    chatHistory.push({ role: 'user', content: text });
    appendChatMsg('user', text);

    const btn = document.getElementById('chatSendBtn');
    btn.disabled = true;
    btn.textContent = '...';

    appendChatMsg('ai', '<div class="spinner" style="width:14px;height:14px;border-width:1.5px;"></div>', true);

    try {
        const res = await authFetch('/api/chat', {
            method: 'POST',
            body: JSON.stringify({ messages: chatHistory })
        });
        const data = await res.json();
        const aiMsg = data.response || '出错了，请重试';
        chatHistory.push({ role: 'assistant', content: aiMsg });
        // Replace loading msg
        const msgs = document.querySelectorAll('.chat-msg.ai');
        const last = msgs[msgs.length - 1];
        if (last) last.innerHTML = aiMsg.replace(/\n/g, '<br>');
    } catch (e) {
        chatHistory.push({ role: 'assistant', content: `Error: ${e.message}` });
        const msgs = document.querySelectorAll('.chat-msg.ai');
        const last = msgs[msgs.length - 1];
        if (last) last.textContent = e.message;
    } finally {
        btn.disabled = false;
        btn.textContent = '发送';
    }
}

function appendChatMsg(role, html, loading = false) {
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = `chat-msg ${role}`;
    div.innerHTML = html;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function quickChat(text) {
    document.getElementById('chatInput').value = text;
    sendChat();
}

// ─────────────────────────────────────────────
// Settings
// ─────────────────────────────────────────────
async function loadSettings() {
    try {
        const res = await authFetch('/api/settings');
        const settings = await res.json();
        document.getElementById('autoTradeToggle').checked = settings.auto_trade_enabled === 'true';
        document.getElementById('minConfidence').value = settings.auto_trade_min_confidence || 0.75;
        document.getElementById('riskPct').value = settings.risk_per_trade_pct || 2.0;
        document.getElementById('refreshInterval').value = settings.refresh_interval_seconds || 30;

        const providerSel = document.getElementById('aiProviderSelect');
        if (providerSel) {
            providerSel.value = settings.ai_provider || 'deepseek_api';
            toggleApiKeyVisibility();
        }

        const modelDisplay = document.getElementById('currentModelDisplay');
        if (modelDisplay) {
            modelDisplay.textContent = (settings.ai_provider === 'ollama') ? 'deepseek-r1:14b (本地大模型)' : 'deepseek-reasoner (DeepSeek-R1)';
        }

        if (settings.deepseek_api_key_set) {
            document.getElementById('apiKeyStatus').innerHTML =
                `<span style="color:var(--green);">✅ API Key 已配置</span> (${settings.deepseek_api_key_preview})`;
        } else {
            document.getElementById('apiKeyStatus').innerHTML =
                `<span style="color:var(--red);">❌ 未配置 API Key — AI 功能不可用</span>`;
        }

        // Alpaca Settings
        document.getElementById('alpacaPaperModeToggle').checked = settings.alpaca_paper_mode !== 'false';
        if (settings.alpaca_api_key_set) {
            document.getElementById('alpacaApiKeyInput').placeholder = `已配置: ${settings.alpaca_api_key_preview}`;
        }
        if (settings.alpaca_secret_key_set) {
            document.getElementById('alpacaSecretKeyInput').placeholder = `已配置 (隐藏显示)`;
        }

    } catch (e) { console.error(e); }
}

async function saveApiKey() {
    const key = document.getElementById('apiKeyInput').value.trim();
    if (!key) { showToast('请输入 API Key', 'error'); return; }
    await authFetch('/api/settings', {
        method: 'POST',
        body: JSON.stringify({ key: 'deepseek_api_key', value: key })
    });
    document.getElementById('apiKeyInput').value = '';
    showToast('✅ DeepSeek API Key 已保存', 'success');
    loadSettings();
}

async function saveAiProvider() {
    const provider = document.getElementById('aiProviderSelect').value;
    const key = document.getElementById('apiKeyInput').value.trim();

    await authFetch('/api/settings', {
        method: 'POST',
        body: JSON.stringify({ key: 'ai_provider', value: provider })
    });

    if (key) {
        await authFetch('/api/settings', {
            method: 'POST',
            body: JSON.stringify({ key: 'deepseek_api_key', value: key })
        });
        document.getElementById('apiKeyInput').value = '';
    }

    showToast('✅ AI 配置已保存', 'success');
    loadSettings();
}

async function saveAlpacaSettings() {
    const apiKey = document.getElementById('alpacaApiKeyInput').value.trim();
    const secretKey = document.getElementById('alpacaSecretKeyInput').value.trim();
    const paperMode = document.getElementById('alpacaPaperModeToggle').checked.toString();

    if (apiKey) {
        await authFetch('/api/settings', {
            method: 'POST',
            body: JSON.stringify({ key: 'alpaca_api_key', value: apiKey })
        });
        document.getElementById('alpacaApiKeyInput').value = '';
    }
    if (secretKey) {
        await authFetch('/api/settings', {
            method: 'POST',
            body: JSON.stringify({ key: 'alpaca_secret_key', value: secretKey })
        });
        document.getElementById('alpacaSecretKeyInput').value = '';
    }

    await authFetch('/api/settings', {
        method: 'POST',
        body: JSON.stringify({ key: 'alpaca_paper_mode', value: paperMode })
    });

    showToast('✅ 券商配置已保存', 'success');
    loadSettings();
}

async function saveSettings() {
    const settings = [
        { key: 'auto_trade_enabled', value: document.getElementById('autoTradeToggle').checked.toString() },
        { key: 'auto_trade_min_confidence', value: document.getElementById('minConfidence').value },
        { key: 'risk_per_trade_pct', value: document.getElementById('riskPct').value },
    ];
    for (const s of settings) {
        await authFetch('/api/settings', { method: 'POST', body: JSON.stringify(s) });
    }
}

async function saveRefreshInterval() {
    const val = document.getElementById('refreshInterval').value;
    await authFetch('/api/settings', { method: 'POST', body: JSON.stringify({ key: 'refresh_interval_seconds', value: val }) });
    showToast('✅ 刷新间隔已保存', 'success');
}

// ─────────────────────────────────────────────
// Toast Notifications
// ─────────────────────────────────────────────
function showToast(msg, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = msg;
    container.appendChild(toast);
    setTimeout(() => { toast.style.opacity = '0'; toast.style.transform = 'translateX(40px)'; toast.style.transition = '0.3s'; setTimeout(() => toast.remove(), 300); }, 4000);
}

// Click outside modal to close
document.getElementById('tradeModal').addEventListener('click', function (e) {
    if (e.target === this) closeModal('tradeModal');
});

// Dynamic API Key field visibility
window.toggleApiKeyVisibility = function () {
    const provider = document.getElementById('aiProviderSelect')?.value;
    const apiKeyGroup = document.getElementById('apiKeyGroup');
    if (apiKeyGroup) {
        if (provider === 'deepseek_api') {
            apiKeyGroup.style.display = 'flex';
        } else {
            apiKeyGroup.style.display = 'none';
        }
    }
};

window.saveAiProvider = saveAiProvider;

// ─────────────────────────────────────────────
// Auth Handlers
// ─────────────────────────────────────────────
function toggleAuthMode(mode) {
    const loginForm = document.getElementById('loginForm');
    const registerForm = document.getElementById('registerForm');
    const subtitle = document.getElementById('authSubtitle');

    if (mode === 'register') {
        loginForm.style.display = 'none';
        registerForm.style.display = 'block';
        subtitle.textContent = '创建您的交易账户';
    } else {
        loginForm.style.display = 'block';
        registerForm.style.display = 'none';
        subtitle.textContent = '请登录您的交易账户';
    }
}

async function handleLogin() {
    const username = document.getElementById('loginUsername').value.trim();
    const password = document.getElementById('loginPassword').value.trim();

    if (!username || !password) {
        showToast('请输入用户名和密码', 'error');
        return;
    }

    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });

        if (!res.ok) {
            const err = await res.json();
            showToast(err.detail || '登录失败', 'error');
            return;
        }

        const data = await res.json();
        authToken = data.access_token;
        localStorage.setItem('auth_token', authToken);
        showToast('🔓 登录成功', 'success');
        await initApp();
    } catch (e) {
        showToast('登录出错', 'error');
    }
}

async function handleRegister() {
    const username = document.getElementById('regUsername').value.trim();
    const email = document.getElementById('regEmail').value.trim();
    const password = document.getElementById('regPassword').value.trim();

    if (!username || password.length < 6) {
        showToast('用户名和密码（至少6位）是必填的', 'error');
        return;
    }

    try {
        const res = await fetch('/api/auth/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, email, password })
        });

        if (!res.ok) {
            const err = await res.json();
            showToast(err.detail || '注册失败', 'error');
            return;
        }

        showToast('✅ 注册成功，请登录', 'success');
        toggleAuthMode('login');
    } catch (e) {
        showToast('注册出错', 'error');
    }
}

function handleLogout() {
    authToken = '';
    currentUser = null;
    localStorage.removeItem('auth_token');
    document.getElementById('authOverlay').style.display = 'flex';
    if (ws) ws.close();
    showToast('已安全退出', 'info');
}

// ─────────────────────────────────────────────
// Transfer Handlers
// ─────────────────────────────────────────────
async function handleTransfer(type) {
    const input = type === 'DEPOSIT' ? 'depositAmount' : 'withdrawAmount';
    const amount = parseFloat(document.getElementById(input).value);

    if (isNaN(amount) || amount <= 0) {
        showToast('请输入有效金额', 'error');
        return;
    }

    try {
        const res = await authFetch('/api/transfer', {
            method: 'POST',
            body: JSON.stringify({ amount, type })
        });

        if (!res.ok) {
            const err = await res.json();
            showToast(err.detail || '操作失败', 'error');
            return;
        }

        const data = await res.json();
        currentUser.balance = data.balance;
        showToast(`✅ ${type === 'DEPOSIT' ? '充值' : '提现'}成功`, 'success');
        loadPortfolio();
    } catch (e) {
        showToast('资金划转失败', 'error');
    }
}

window.handleLogin = handleLogin;
window.handleRegister = handleRegister;
window.handleLogout = handleLogout;
window.toggleAuthMode = toggleAuthMode;
window.handleTransfer = handleTransfer;

// ─────────────────────────────────────────────
// Modal Helpers
// ─────────────────────────────────────────────
function openModal(id) {
    document.getElementById(id).classList.add('open');
}

async function handleQuickRecharge() {
    const input = document.getElementById('quickDepositAmount');
    const amount = parseFloat(input.value);

    if (isNaN(amount) || amount <= 0) {
        showToast('请输入有效金额', 'error');
        return;
    }

    try {
        const res = await authFetch('/api/transfer', {
            method: 'POST',
            body: JSON.stringify({ amount, type: 'DEPOSIT' })
        });

        if (!res.ok) {
            const err = await res.json();
            showToast(err.detail || '操作失败', 'error');
            return;
        }

        const data = await res.json();
        currentUser.balance = data.balance;
        showToast(`✅ 成功充值 $${amount.toLocaleString()}`, 'success');
        closeModal('rechargeModal');
        loadPortfolio();
    } catch (e) {
        showToast('充值失败', 'error');
    }
}

window.openModal = openModal;
window.closeModal = closeModal;
window.handleQuickRecharge = handleQuickRecharge;
window.discoverLayoffCandidates = discoverLayoffCandidates;
window.importSelectedCandidates = importSelectedCandidates;
window.addManualLayoffEvent = addManualLayoffEvent;
window.removeLayoffEvent = removeLayoffEvent;
window.evaluateLayoffEvents = evaluateLayoffEvents;
