/* ==========================================================================
   Market Report — shared renderer for the in-app Market Analysis panel and the
   public /desk page. MarketReport.mount(hostEl, opts) builds the full report
   (tabs + content), fetches /api/market-analysis, and handles tab switching.

   opts:
     onTicker(ticker)  optional — called when a stock row is clicked
     compact           optional — hides the big standalone header
   ========================================================================== */
window.MarketReport = (function () {
  function tabsFor(kind) {
    return [
      ['brief',   kind === 'post' ? 'Closing Brief' : 'Opening Brief'],
      ['indices', 'Markets'],
      ['tech',    'Technicals'],
      ['sectors', 'Sectors'],
      ['movers',  'Movers'],
      ['macro',   'Commodities & Rates'],
      ['news',    'News'],
    ];
  }

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
  function titleFor(kind) {
    return kind === 'pre' ? 'Pre-Market Report'
      : kind === 'post' ? 'Post-Market Report'
      : 'US pre &amp; post-market report';
  }

  function skeleton(compact, kind) {
    const sessionBadge = `<div class="ma-session" data-ma-session><span class="ma-dot"></span><span data-ma-session-label>Loading…</span></div>`;
    const head = compact
      ? `<div class="ma-head" style="justify-content:flex-end">${sessionBadge}</div>`
      : `<div class="ma-head"><div>` +
          `<div class="ma-eyebrow">MARKET ANALYSIS</div>` +
          `<div class="ma-title">${titleFor(kind)}</div></div>` +
        sessionBadge + `</div>`;
    const edition = `<div class="ma-edition" data-ma-edition hidden></div>`;
    const tabs = `<div class="ma-tabs" data-ma-tabs>` +
      tabsFor(kind).map((t, i) => `<button class="ma-tab${i === 0 ? ' on' : ''}" data-ma="${t[0]}">${t[1]}</button>`).join('') +
      `</div>`;
    return head + edition + tabs +
      `<div class="ma-content" data-ma-content><div class="ma-loading">📈 Fetching live market data…</div></div>` +
      `<div class="ma-foot" data-ma-foot></div>`;
  }

  function mount(host, opts) {
    opts = opts || {};
    host.classList.add('ma-report');
    host.innerHTML = skeleton(opts.compact, opts.kind);
    const st = host.__ma = {
      data: null, tab: 'brief', news: null, newsState: 'idle', opts,
      kind: opts.kind || null, endpoint: opts.endpoint || '/api/market-analysis', loading: false,
    };

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
    fetch(st.endpoint).then(r => r.json()).then(d => {
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
    const ed = host.querySelector('[data-ma-edition]');
    if (ed) {
      if (d.edition) {
        const e = d.edition;
        const tag = e.is_today
          ? `<span class="ma-ed-tag today">Today's edition</span>`
          : `<span class="ma-ed-tag prior">Latest edition</span>`;
        ed.hidden = false;
        const nextHint = (!e.is_today && e.next_label)
          ? `<span class="ma-ed-next">Next: ${e.next_label}</span>` : '';
        ed.innerHTML = `<span class="ma-ed-cal">🗓️</span><b>${e.title || 'Report'}</b>` +
          `<span class="ma-ed-date">${e.date_label}</span>` +
          (e.published_at ? `<span class="ma-ed-pub">· ${e.published_at}</span>` : '') +
          nextHint + tag;
      } else { ed.hidden = true; ed.innerHTML = ''; }
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
    const fn = { brief, indices, tech, sectors, movers, macro }[st.tab];
    c.innerHTML = fn ? fn(d) : '';
  }

  // ── Opening / Closing Brief — the editorial front page ───────────────────
  function statTile(label, sym, val, chg, dec) {
    return `<div class="ma-stat"><div class="s-l">${label}</div>` +
      `<div class="s-v">${val == null ? '—' : N(val, dec == null ? 2 : dec)}</div>` +
      `<div class="s-c ${cls(chg)}">${arr(chg)} ${pctTxt(chg)}</div></div>`;
  }
  function scorecard(d) {
    const idx = d.indices || [], tiles = [];
    ['SPY', 'QQQ', 'DIA', 'IWM'].forEach(s => {
      const r = find(idx, s);
      if (r.last != null) tiles.push(statTile(r.label, s, r.last, r.chg_pct));
    });
    const vix = find(d.rates_fx, '^VIX');
    if (vix.last != null) tiles.push(statTile('Volatility', 'VIX', vix.last, vix.chg_pct));
    return tiles.length ? `<div class="ma-scorecard">${tiles.join('')}</div>` : '';
  }
  function gapChip(d) {
    const gap = d.gap_pct;
    if (gap == null) return '';
    const dir = Math.abs(gap) < 0.15 ? 'flat' : gap > 0 ? 'up' : 'down';
    const word = d.kind === 'post' ? (dir === 'flat' ? 'Flat into tomorrow' : dir === 'up' ? 'Futures higher' : 'Futures lower')
      : (dir === 'flat' ? 'Flat open expected' : dir === 'up' ? 'Gap-up open' : 'Gap-down open');
    const c = dir === 'flat' ? 'ma-flat' : dir === 'up' ? 'ma-pos' : 'ma-neg';
    return `<div class="ma-gapchip ${c}"><span class="g-k">S&P FUTURES</span><span class="g-v">${word}</span><span class="g-n">${pctTxt(gap)}</span></div>`;
  }
  function brief(d) {
    const gap = d.gap_pct, spy = find(d.indices, 'SPY'), vix = find(d.rates_fx, '^VIX');
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
    let body;
    if (d.kind === 'post') {
      const ah = (gap != null && Math.abs(gap) >= 0.15)
        ? ` Futures already point <b>${gap > 0 ? 'higher' : 'lower'}</b> for tomorrow.` : '';
      body = `After the close. ${moodTxt} ${vixTxt}${ah}`;
    } else {
      const intro = d.kind === 'pre' ? 'Heading into the open. ' : 'For the current session. ';
      body = `${intro}${gapTxt} ${moodTxt} ${vixTxt}`;
    }
    const eyebrow = d.kind === 'post' ? 'THE CLOSE' : 'BEFORE THE BELL';
    const accent = spy.chg_pct == null ? 'flat' : spy.chg_pct > 0.05 ? 'pos' : spy.chg_pct < -0.05 ? 'neg' : 'flat';
    const narrative = (d.ai && d.ai.brief) ? `${aiTag()}${d.ai.brief}` : body;

    return `<div class="ma-hero ${accent}">` +
        `<div class="ma-hero-eyebrow">${eyebrow}</div>` +
        `<div class="ma-hero-text">${narrative}</div>` +
        gapChip(d) +
      `</div>` +
      scorecard(d) +
      events(d) +
      `<div class="ma-sec-h">${d.kind === 'post' ? 'The verdict · into tomorrow' : 'The verdict · the day ahead'}</div>` +
      verdict(d);
  }

  // Key macro events — a compact "what the market is waiting for" grid
  function events(d) {
    const all = d.events || [];
    if (!all.length) return '';
    // Prioritise high-impact, top up with medium, keep it short, then show soonest-first.
    const high = all.filter(e => e.impact === 'High');
    const med = all.filter(e => e.impact !== 'High');
    const evs = high.concat(med).slice(0, 8).sort((a, b) => (a.when_ts || 0) - (b.when_ts || 0));
    const note = (d.ai && d.ai.watching)
      ? `<div class="ma-watch-note">${aiTag()}${d.ai.watching}</div>` : '';
    const items = evs.map(e => {
      const when = (e.when || '').replace(' ET', '');
      const est = (e.estimate != null && e.estimate !== '') ? ` · est <b>${e.estimate}</b>` : '';
      return `<div class="ma-ev ${e.impact === 'High' ? 'high' : 'med'}">` +
        `<span class="ev-dot"></span>` +
        `<div class="ev-body"><div class="ev-name">${e.event} <span class="ev-flag">${e.flag || ''}</span></div>` +
        `<div class="ev-meta">${when}${est}</div></div>` +
      `</div>`;
    }).join('');
    return `<div class="ma-sec-h">📌 What the market's watching</div>` +
      note + `<div class="ma-events">${items}</div>`;
  }

  function indices(d) {
    return `<div class="ma-sec-h">Index ETFs</div>` +
      `<div class="ma-grid">${(d.indices || []).map(r => card(r)).join('')}</div>` +
      `<div class="ma-sec-h">Equity futures — overnight &amp; ${d.kind === 'post' ? 'after-hours' : 'pre-market'}</div>` +
      `<div class="ma-note">Futures trade nearly around the clock, so they show where the market is leaning ${d.kind === 'post' ? 'after the close' : 'before the 9:30 ET open'}.</div>` +
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
    // Post-market emphasises what just reported; pre/live emphasises what's on deck.
    const post = d.kind === 'post' || (!d.kind && d.session && (d.session.phase === 'post' || d.session.phase === 'closed'));
    const dteCell = x => x.dte == null ? '—' : (x.dte <= 0 ? 'today' : x.dte + 'd');
    const volCell = x => x.vol_ratio == null ? '—' : x.vol_ratio.toFixed(1) + '×';
    const highCell = x => x.from_high == null ? '—' : (x.from_high >= 0 ? 'new high' : x.from_high.toFixed(1) + '%');
    const usePost = post && (s.just_reported || []).length;

    const earningsList = usePost ? s.just_reported : s.earnings;
    const earningsHdr = usePost ? 'Reported' : 'Reports';
    const earningsCell = usePost ? (() => 'done') : dteCell;
    const gainersLbl = post ? "Today's biggest winners" : 'Top gainers';
    const losersLbl = post ? "Today's biggest losers" : 'Top losers';
    const earningsTitle = usePost ? 'Just reported' : 'Earnings on deck';

    const lead = (d.ai && d.ai.movers_note)
      ? `<div class="ma-lead">${aiTag()}${d.ai.movers_note}</div>`
      : `<div class="ma-lead">Stock-level detail across the <b>${s.count}-name</b> AlphaHunt universe — ${post ? "the day's tape" : "what's moving"}, the engine's top picks, earnings, and momentum flags.</div>`;

    return lead +
      `<div class="ma-two">` +
        `<div><div class="ma-mv-h"><span class="ic">📈</span> ${gainersLbl}</div>${table('Gainer', 'Score', s.gainers)}</div>` +
        `<div><div class="ma-mv-h"><span class="ic">📉</span> ${losersLbl}</div>${table('Loser', 'Score', s.losers)}</div>` +
      `</div>` +

      `<div class="ma-two">` +
        `<div><div class="ma-mv-h"><span class="ic">⭐</span> Highest Alpha Score</div>${table('Stock', 'Score', s.top_score)}</div>` +
        `<div><div class="ma-mv-h"><span class="ic">🗓️</span> ${earningsTitle}</div>${table('Stock', earningsHdr, earningsList, earningsCell)}</div>` +
      `</div>` +

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
    const conf = Math.abs(diff) >= 4 ? ['high', 'High'] : Math.abs(diff) >= 2 ? ['med', 'Medium'] : ['low', 'Low'];
    const keyLvl = t.sma20 != null ? t.sma20 : (t.sma50 != null ? t.sma50 : null);
    const biasCls = bias === 'bullish' ? 'ma-pos' : bias === 'bearish' ? 'ma-neg' : 'ma-flat';
    const openExp = gap == null ? 'open near flat' : gap > 0.15 ? 'open higher' : gap < -0.15 ? 'open lower' : 'open near flat';
    const spyChg = spy.chg_pct;
    const closeExp = spyChg == null ? 'finished mixed' : spyChg > 0.15 ? 'closed higher' : spyChg < -0.15 ? 'closed lower' : 'closed near flat';
    const headline = d.kind === 'post'
      ? `The US market <span class="${spyChg > 0.15 ? 'ma-pos' : spyChg < -0.15 ? 'ma-neg' : 'ma-flat'}">${closeExp}</span>, with a <span class="${biasCls}">${bias}</span> tone into tomorrow.`
      : `US market is set to <span class="${gap > 0.15 ? 'ma-pos' : gap < -0.15 ? 'ma-neg' : 'ma-flat'}">${openExp}</span>, with an overall <span class="${biasCls}">${bias}</span> bias.`;
    return `<div class="ma-verdict">` +
      `<div class="big">${headline}</div>` +
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
