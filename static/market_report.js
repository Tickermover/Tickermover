/* ==========================================================================
   Market Report — shared renderer for the in-app Market Analysis panel and the
   public /desk page. MarketReport.mount(hostEl, opts) builds the full report
   (tabs + content), fetches /api/market-analysis, and handles tab switching.

   opts:
     onTicker(ticker)  optional — called when a stock row is clicked
     compact           optional — hides the big standalone header
   ========================================================================== */
window.MarketReport = (function () {
  const TABS = [
    ['brief',   'Opening Brief'],
    ['indices', 'US Indices'],
    ['tech',    'Technicals'],
    ['sectors', 'Sectors'],
    ['movers',  'Movers'],
    ['macro',   'Commodities & Rates'],
    ['news',    'News'],
    ['verdict', 'Verdict'],
  ];

  // ── formatters ──────────────────────────────────────────────────────────
  function N(v, dec) {
    if (v == null || isNaN(v)) return '—';
    return Number(v).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }
  const cls = v => v == null ? 'ma-na' : (v > 0.0099 ? 'ma-pos' : v < -0.0099 ? 'ma-neg' : 'ma-flat');
  const arr = v => v == null ? '' : (v > 0.0099 ? '▲' : v < -0.0099 ? '▼' : '■');
  const aiTag = () => '<span class="ma-ai-tag">✦ AI</span> ';
  const pctTxt = v => v == null ? '—' : `${v >= 0 ? '+' : ''}${N(v, 2)}%`;
  const find = (a, s) => (a || []).find(x => x.symbol === s) || {};

  function price(r) {
    if (r.last == null) return '—';
    const n = N(r.last, r.decimals == null ? 2 : r.decimals);
    if (r.unit === '%') return n + '%';
    if (r.unit && r.unit.charAt(0) === '$') return '$' + n;
    return n;
  }
  function chgHtml(r) {
    if (r.chg_pct == null) return '<span class="chg ma-na">— no data</span>';
    const c = cls(r.chg_pct), dec = (r.decimals == null ? 2 : r.decimals);
    const a = (r.chg_abs >= 0 ? '+' : '') + N(r.chg_abs, dec);
    return `<div class="chg ${c}">${arr(r.chg_pct)} ${a} (${pctTxt(r.chg_pct)})</div>`;
  }
  function card(r, sub) {
    return `<div class="ma-card"><div class="lbl"><span>${r.label}</span><span class="sym">${r.symbol}</span></div>` +
      `<div class="val">${price(r)}</div>${chgHtml(r)}` + (sub ? `<div class="sub">${sub}</div>` : '') + `</div>`;
  }

  // ── skeleton + mount ────────────────────────────────────────────────────
  function skeleton(compact) {
    const sessionBadge = `<div class="ma-session" data-ma-session><span class="ma-dot"></span><span data-ma-session-label>Loading…</span></div>`;
    const head = compact
      ? `<div class="ma-head" style="justify-content:flex-end">${sessionBadge}</div>`
      : `<div class="ma-head"><div>` +
          `<div class="ma-eyebrow">MARKET ANALYSIS</div>` +
          `<div class="ma-title">US <em>pre &amp; post-market</em> report</div></div>` +
        sessionBadge + `</div>`;
    const tabs = `<div class="ma-tabs" data-ma-tabs>` +
      TABS.map((t, i) => `<button class="ma-tab${i === 0 ? ' on' : ''}" data-ma="${t[0]}">${t[1]}</button>`).join('') +
      `</div>`;
    return head + tabs +
      `<div class="ma-content" data-ma-content><div class="ma-loading">📈 Fetching live market data…</div></div>` +
      `<div class="ma-foot" data-ma-foot></div>`;
  }

  function mount(host, opts) {
    opts = opts || {};
    host.classList.add('ma-report');
    host.innerHTML = skeleton(opts.compact);
    const st = host.__ma = { data: null, tab: 'brief', news: null, newsState: 'idle', opts, loading: false };

    host.querySelector('[data-ma-tabs]').addEventListener('click', e => {
      const b = e.target.closest('.ma-tab'); if (!b) return;
      st.tab = b.getAttribute('data-ma');
      host.querySelectorAll('.ma-tab').forEach(x => x.classList.toggle('on', x === b));
      render(host, st);
    });
    host.querySelector('[data-ma-content]').addEventListener('click', e => {
      const row = e.target.closest('tr.click'); if (!row || !st.opts.onTicker) return;
      const tk = row.getAttribute('data-tk'); if (tk) st.opts.onTicker(tk);
    });

    load(host, st);
    return { reload: () => load(host, st, true) };
  }

  function load(host, st, force) {
    if (st.data && !force) { render(host, st); setSession(host, st); return; }
    if (st.loading) return;
    st.loading = true;
    const c = host.querySelector('[data-ma-content]');
    if (c && !st.data) c.innerHTML = '<div class="ma-loading">📈 Fetching live market data…</div>';
    fetch('/api/market-analysis').then(r => r.json()).then(d => {
      st.data = d; st.loading = false; setSession(host, st); render(host, st);
    }).catch(() => {
      st.loading = false;
      if (c) c.innerHTML = '<div class="ma-loading">Could not load market data. Please try again shortly.</div>';
    });
  }

  function setSession(host, st) {
    const badge = host.querySelector('[data-ma-session]');
    const lab = host.querySelector('[data-ma-session-label]');
    const d = st.data || {};
    if (badge && d.session) {
      badge.className = 'ma-session ' + (d.session.phase || '');
      if (lab) lab.textContent = (d.session.label || 'Market') + (d.session.et_time ? (' · ' + d.session.et_time) : '');
    }
    const foot = host.querySelector('[data-ma-foot]');
    if (foot) foot.innerHTML = 'Market data: ' + (d.source || 'Yahoo Finance') +
      ' · stock data: AlphaHunt universe. 15-min delayed quotes. Pre/post-market prints are indicative.';
  }

  // ── tab dispatch ────────────────────────────────────────────────────────
  function render(host, st) {
    const c = host.querySelector('[data-ma-content]'); if (!c) return;
    const d = st.data;
    if (!d || d.available === false) {
      c.innerHTML = '<div class="ma-loading">' + ((d && d.reason) || 'Market data is unavailable right now.') + '</div>';
      return;
    }
    if (st.tab === 'news') return renderNews(host, st, c);
    const fn = { brief, indices, tech, sectors, movers, macro, verdict }[st.tab];
    c.innerHTML = fn ? fn(d) : '';
  }

  // ── macro tabs (ported) ─────────────────────────────────────────────────
  function brief(d) {
    const ses = d.session || {}, gap = d.gap_pct;
    const spy = find(d.indices, 'SPY'), vix = find(d.rates_fx, '^VIX');
    const gapTxt = gap == null ? 'Equity futures data is unavailable right now.'
      : Math.abs(gap) < 0.15 ? 'S&P 500 futures are <b>roughly flat</b> — a quiet open is likely.'
      : `S&P 500 futures point to a <b>${gap > 0 ? 'higher' : 'lower'} open</b>, about ${Math.abs(gap).toFixed(2)}%.`;
    const moodTxt = spy.chg_pct == null ? 'Broad-market reading pending.'
      : `The S&P 500 (SPY) is <b class="${cls(spy.chg_pct)}">${spy.chg_pct >= 0 ? 'up' : 'down'} ${Math.abs(spy.chg_pct).toFixed(2)}%</b> versus its prior close.`;
    let vixTxt = '';
    if (vix.last != null) {
      const v = vix.last, w = v < 15 ? 'calm' : v < 20 ? 'a little nervous' : v < 25 ? 'anxious' : 'fearful';
      vixTxt = `Volatility (VIX) is <b>${v.toFixed(2)}</b> — the market looks <b>${w}</b>.`;
    }
    const lead = (d.ai && d.ai.brief)
      ? `<div class="ma-lead">${aiTag()}${d.ai.brief}</div>`
      : `<div class="ma-lead">It is <b>${ses.label || '—'}</b>. ${gapTxt} ${moodTxt} ${vixTxt}</div>`;
    return lead +
      `<div class="ma-sec-h">Headline indices</div>` +
      `<div class="ma-grid">${(d.indices || []).map(r => card(r)).join('')}</div>` +
      `<div class="ma-sec-h">Pre-market futures · gap read</div>` +
      `<div class="ma-grid">${(d.futures || []).map(r => card(r)).join('')}` +
        card(vix, vix.last == null ? '' : (vix.last < 15 ? 'Calm' : vix.last < 20 ? 'Some nervousness' : vix.last < 25 ? 'Anxious' : 'High fear')) + `</div>`;
  }

  function indices(d) {
    return `<div class="ma-sec-h">Index ETFs</div>` +
      `<div class="ma-grid">${(d.indices || []).map(r => card(r)).join('')}</div>` +
      `<div class="ma-sec-h">Equity futures — overnight &amp; pre-market</div>` +
      `<div class="ma-note">Futures trade nearly around the clock, so they show where the market is leaning before the 9:30&nbsp;ET open.</div>` +
      `<div class="ma-grid">${(d.futures || []).map(r => card(r)).join('')}</div>`;
  }

  function tech(d) {
    const t = d.technicals || {};
    if (t.price == null) return '<div class="ma-loading">Technical readings are unavailable right now.</div>';
    const sma = (lvl, name, note) => {
      if (lvl == null) return '';
      const above = t.price >= lvl;
      return `<div class="ma-card"><div class="lbl"><span>${name}</span><span class="sym">SPY</span></div>` +
        `<div class="val">$${N(lvl, 2)}</div>` +
        `<div class="chg ${above ? 'ma-pos' : 'ma-neg'}">Price is ${above ? 'above' : 'below'}</div>` +
        `<div class="sub">${note}</div></div>`;
    };
    let rsiNote = 'Neutral zone.', rsiCls = 'ma-flat';
    if (t.rsi14 != null) {
      if (t.rsi14 >= 60) { rsiNote = 'Overbought-leaning — may cool off.'; rsiCls = 'ma-neg'; }
      else if (t.rsi14 <= 40) { rsiNote = 'Oversold-leaning — may bounce.'; rsiCls = 'ma-pos'; }
    }
    const macdPos = (t.hist != null && t.hist >= 0);
    const macdNote = t.hist == null ? 'Not available.' : (macdPos ? 'Momentum building upward.' : 'Momentum fading / downward.');
    const techLead = (d.ai && d.ai.technicals_note)
      ? `<div class="ma-lead">${aiTag()}${d.ai.technicals_note}</div>`
      : `<div class="ma-lead">Where the <b>S&P 500 (SPY)</b> trades relative to its key moving averages, and what momentum says.</div>`;
    return techLead +
      `<div class="ma-tech">` +
        sma(t.sma20, '20-day average', 'Short-term trend.') +
        sma(t.sma50, '50-day average', 'Medium-term trend / support.') +
        sma(t.sma200, '200-day average', 'Long-term bull/bear line.') +
        `<div class="ma-card"><div class="lbl"><span>RSI (14)</span><span class="sym">SPY</span></div>` +
          `<div class="val">${t.rsi14 == null ? '—' : N(t.rsi14, 1)}</div>` +
          `<div class="chg ${rsiCls}">${rsiNote}</div>` +
          `<div class="sub">Below 40 oversold · 40–60 neutral · above 60 overbought.</div></div>` +
        `<div class="ma-card"><div class="lbl"><span>MACD (12,26,9)</span><span class="sym">SPY</span></div>` +
          `<div class="val">${t.macd == null ? '—' : N(t.macd, 2)}</div>` +
          `<div class="chg ${t.hist == null ? 'ma-na' : (macdPos ? 'ma-pos' : 'ma-neg')}">${macdNote}</div>` +
          `<div class="sub">Signal ${t.signal == null ? '—' : N(t.signal, 2)} · Histogram ${t.hist == null ? '—' : N(t.hist, 2)}.</div></div>` +
      `</div>`;
  }

  function sectors(d) {
    const secs = (d.sectors || []).slice();
    if (!secs.length) return '<div class="ma-loading">Sector data is unavailable right now.</div>';
    const maxAbs = Math.max(1, ...secs.map(s => Math.abs(s.chg_1d)));
    const rows = secs.map(s => {
      const w = Math.min(50, Math.abs(s.chg_1d) / maxAbs * 50);
      const pos = s.chg_1d >= 0;
      const fill = pos ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
      return `<div class="ma-bar-row"><div class="nm">${s.name}</div>` +
        `<div class="ma-bar-track"><div class="ma-bar-mid"></div>` +
          `<div class="ma-bar-fill ${pos ? 'pos' : 'neg'}" style="${fill}"></div></div>` +
        `<div class="pct ${cls(s.chg_1d)}">${pctTxt(s.chg_1d)}</div></div>`;
    }).join('');
    const top = secs[0], bot = secs[secs.length - 1];
    const wk = v => v == null ? '' : `, ${pctTxt(v)} on the week`;
    const lead = (d.ai && d.ai.sectors_note)
      ? `<div class="ma-lead">${aiTag()}${d.ai.sectors_note}</div>`
      : `<div class="ma-lead">SPDR sector ETFs ranked by <b>today's move</b>. This shows where money is rotating.</div>`;
    return lead + `<div class="ma-bars">${rows}</div>` +
      `<div class="ma-note"><b>Leading:</b> ${top.name} (${pctTxt(top.chg_1d)} today${wk(top.chg_5d)}). ` +
      `<b>Lagging:</b> ${bot.name} (${pctTxt(bot.chg_1d)} today).</div>`;
  }

  function macro(d) {
    return `<div class="ma-sec-h">Rates · volatility · dollar</div>` +
      `<div class="ma-grid">${(d.rates_fx || []).map(r => card(r)).join('')}</div>` +
      `<div class="ma-sec-h">Commodities</div>` +
      `<div class="ma-grid">${(d.commodities || []).map(r => card(r)).join('')}</div>`;
  }

  // ── Movers (stock-level) ────────────────────────────────────────────────
  function gradeCell(g) {
    if (!g) return '';
    const k = String(g).charAt(0).toUpperCase();
    return `<span class="ma-gd ${k}">${k}</span>`;
  }
  function stockRows(list, extraHdr, extraFn) {
    if (!list || !list.length) return '<tr><td colspan="5" style="color:#94a3b8">No names right now.</td></tr>';
    return list.map(s => {
      return `<tr class="click" data-tk="${s.ticker}">` +
        `<td><span class="ma-tk">${s.ticker}</span> <span class="ma-nm">${s.name || ''}</span></td>` +
        `<td class="r ma-px">${s.price == null ? '—' : '$' + N(s.price, 2)}</td>` +
        `<td class="r ${cls(s.change_pct)}" style="font-weight:800">${pctTxt(s.change_pct)}</td>` +
        `<td class="r">${gradeCell(s.grade)}</td>` +
        `<td class="r">${extraFn ? extraFn(s) : (s.score == null ? '—' : Math.round(s.score))}</td>` +
      `</tr>`;
    }).join('');
  }
  function table(title, hdr5, list, extraFn) {
    return `<table class="ma-tbl"><thead><tr>` +
      `<th>${title}</th><th class="r">Price</th><th class="r">Chg</th><th class="r">Gr</th><th class="r">${hdr5}</th>` +
      `</tr></thead><tbody>${stockRows(list, hdr5, extraFn)}</tbody></table>`;
  }

  function movers(d) {
    const s = d.stocks;
    if (!s || !s.count) return '<div class="ma-loading">Stock data is unavailable right now.</div>';
    const post = d.session && (d.session.phase === 'post' || d.session.phase === 'closed');
    const dteCell = x => x.dte == null ? '—' : (x.dte <= 0 ? 'today' : x.dte + 'd');
    const volCell = x => x.vol_ratio == null ? '—' : x.vol_ratio.toFixed(1) + '×';
    const highCell = x => x.from_high == null ? '—' : (x.from_high >= 0 ? 'new high' : x.from_high.toFixed(1) + '%');

    const earningsList = post && (s.just_reported || []).length ? s.just_reported : s.earnings;
    const earningsHdr = post && (s.just_reported || []).length ? 'Reported' : 'Reports';
    const earningsCell = post && (s.just_reported || []).length ? (() => 'done') : dteCell;

    return `<div class="ma-lead">Stock-level detail across the <b>${s.count}-name</b> AlphaHunt universe — today's movers, the engine's top picks, earnings on deck, and momentum flags.</div>` +

      `<div class="ma-two">` +
        `<div><div class="ma-mv-h"><span class="ic">📈</span> Top gainers today</div>${table('Gainer', 'Score', s.gainers)}</div>` +
        `<div><div class="ma-mv-h"><span class="ic">📉</span> Top losers today</div>${table('Loser', 'Score', s.losers)}</div>` +
      `</div>` +

      `<div class="ma-mv-h"><span class="ic">⭐</span> Highest Alpha Score right now</div>` +
      `<div style="margin-bottom:18px">${table('Stock', 'Score', s.top_score)}</div>` +

      `<div class="ma-mv-h"><span class="ic">🗓️</span> ${post && (s.just_reported || []).length ? 'Just reported earnings' : 'Earnings on deck (next 7 days)'}</div>` +
      `<div style="margin-bottom:18px">${table('Stock', earningsHdr, earningsList, earningsCell)}</div>` +

      `<div class="ma-two">` +
        `<div><div class="ma-mv-h"><span class="ic">🔊</span> Unusual volume</div>${table('Stock', 'Vol', s.unusual_volume, volCell)}</div>` +
        `<div><div class="ma-mv-h"><span class="ic">🚀</span> Near 52-week highs</div>${table('Stock', 'vs High', s.near_high, highCell)}</div>` +
      `</div>`;
  }

  // ── Verdict ─────────────────────────────────────────────────────────────
  function verdict(d) {
    const gap = d.gap_pct, t = d.technicals || {}, spy = find(d.indices, 'SPY'), vix = find(d.rates_fx, '^VIX');
    if (d.ai && d.ai.verdict && d.ai.verdict.headline) {
      const v = d.ai.verdict, cf = (v.confidence || '').toLowerCase();
      const cfCls = cf.indexOf('high') >= 0 ? 'high' : cf.indexOf('low') >= 0 ? 'low' : 'med';
      const keyLvl = t.sma20 != null ? t.sma20 : (t.sma50 != null ? t.sma50 : null);
      return `<div class="ma-verdict">` +
        `<div class="big">${aiTag()}${v.headline}</div>` +
        (v.what_it_means ? `<div class="kv">${String(v.what_it_means).replace(/\n/g, '<br>')}</div>` : '') +
        (keyLvl != null ? `<div class="kv" style="margin-top:8px"><b>Key level to watch:</b> SPY $${N(keyLvl, 2)} (20-day line).</div>` : '') +
        `<div class="ma-conf ${cfCls}">Confidence: ${v.confidence || 'Medium'}</div>` +
        `<div class="kv" style="margin-top:12px;color:#64748b">AI-written from live data — context only, no buy or sell calls.</div>` +
      `</div>`;
    }
    let bull = 0, bear = 0;
    if (gap != null) { if (gap > 0.1) bull++; else if (gap < -0.1) bear++; }
    if (spy.chg_pct != null) { if (spy.chg_pct > 0) bull++; else if (spy.chg_pct < 0) bear++; }
    if (t.price != null && t.sma50 != null) { t.price >= t.sma50 ? bull++ : bear++; }
    if (t.price != null && t.sma200 != null) { t.price >= t.sma200 ? bull++ : bear++; }
    if (t.rsi14 != null) { if (t.rsi14 > 60) bull++; else if (t.rsi14 < 40) bear++; }
    if (vix.last != null) { if (vix.last < 16) bull++; else if (vix.last > 24) bear++; }
    const diff = bull - bear;
    const bias = diff >= 2 ? 'bullish' : diff <= -2 ? 'bearish' : 'neutral';
    const openExp = gap == null ? 'open near flat' : gap > 0.15 ? 'open higher' : gap < -0.15 ? 'open lower' : 'open near flat';
    const conf = Math.abs(diff) >= 4 ? ['high', 'High'] : Math.abs(diff) >= 2 ? ['med', 'Medium'] : ['low', 'Low'];
    const keyLvl = t.sma20 != null ? t.sma20 : (t.sma50 != null ? t.sma50 : null);
    const biasCls = bias === 'bullish' ? 'ma-pos' : bias === 'bearish' ? 'ma-neg' : 'ma-flat';
    return `<div class="ma-verdict">` +
      `<div class="big">US market is set to <span class="${gap > 0.15 ? 'ma-pos' : gap < -0.15 ? 'ma-neg' : 'ma-flat'}">${openExp}</span>, ` +
        `with an overall <span class="${biasCls}">${bias}</span> bias.</div>` +
      `<div class="kv"><b>Read:</b> ${bull} bullish vs ${bear} bearish signals across futures, trend, RSI and volatility.</div>` +
      (keyLvl != null ? `<div class="kv"><b>Key level to watch:</b> SPY $${N(keyLvl, 2)} (20-day line).<br>If it holds, the up-trend stays intact. If it breaks, expect more two-way chop.</div>` : '') +
      `<div class="ma-conf ${conf[0]}">Confidence: ${conf[1]}</div>` +
      `<div class="kv" style="margin-top:12px;color:#64748b">For context only — no buy or sell calls. Levels are indicative and based on delayed data.</div>` +
    `</div>`;
  }

  // ── News (lazy) ─────────────────────────────────────────────────────────
  function renderNews(host, st, c) {
    if (st.newsState === 'loaded') { c.innerHTML = newsHtml(st); return; }
    c.innerHTML = '<div class="ma-loading">📰 Loading market news…</div>';
    if (st.newsState === 'loading') return;
    st.newsState = 'loading';
    fetch('/api/news/live').then(r => r.json()).then(j => {
      st.news = (j && j.news) || []; st.newsState = 'loaded';
      if (st.tab === 'news') c.innerHTML = newsHtml(st);
    }).catch(() => {
      st.newsState = 'loaded'; st.news = [];
      if (st.tab === 'news') c.innerHTML = newsHtml(st);
    });
  }
  function newsHtml(st) {
    if (!st.news || !st.news.length) return '<div class="ma-loading">No fresh market news available right now.</div>';
    const fmtT = ts => { if (!ts) return ''; try { return new Date(ts * 1000).toLocaleString('en-US', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short' }); } catch (e) { return ''; } };
    return st.news.slice(0, 12).map(n => {
      const sym = n.ticker ? `<span class="ma-tag">${n.ticker}</span>` : '';
      const link = (n.url && n.url !== '#') ? `<a href="${n.url}" target="_blank" rel="noopener">Read the full article →</a>` : '';
      return `<div class="ma-news-item">` +
        `<div class="hl">${sym} ${n.headline || ''}</div>` +
        (n.summary ? `<div class="mt">${(n.summary || '').slice(0, 180)}${(n.summary || '').length > 180 ? '…' : ''}</div>` : '') +
        `<div class="meta">${n.source ? ('Source: ' + n.source) : ''}${n.datetime ? (' · ' + fmtT(n.datetime)) : ''}${link ? (' · ' + link) : ''}</div>` +
      `</div>`;
    }).join('');
  }

  return { mount };
})();
