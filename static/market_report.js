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
    // 'post' is now the single daily edition (close summary + next-day read).
    return kind === 'pre' ? 'Pre-Market Report'
      : kind === 'post' ? 'Daily Market Report'
      : 'Daily Market Report';
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
    // No tabs — the report is now one editorial scroll (masthead → infographics →
    // technicals → commodities/rates → what's watching → the verdict).
    return head + edition +
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

    const tabsEl = host.querySelector('[data-ma-tabs]');
    if (tabsEl) tabsEl.addEventListener('click', e => {
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
      if (d.edition && d.edition.live) {
        // Live mode — tracks the market intraday, labelled with the "as of" time.
        const e = d.edition;
        ed.hidden = false;
        ed.innerHTML = `<span class="ma-ed-cal">🗓️</span><b>${e.title || 'Report'}</b>` +
          (e.as_of ? `<span class="ma-ed-date">${e.as_of}</span>` : '') +
          `<span class="ma-ed-tag today">● Live</span>`;
      } else if (d.edition) {
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
      ' · stock data: TickerMover universe. 15-min delayed quotes. Pre/post-market prints are indicative.';
  }

  // ── tab dispatch ────────────────────────────────────────────────────────
  function render(host, st) {
    const c = host.querySelector('[data-ma-content]'); if (!c) return;
    const d = st.data;
    if (!d || d.available === false) {
      c.innerHTML = '<div class="ma-loading">' + ((d && d.reason) || 'Market data is unavailable right now.') + '</div>';
      return;
    }
    c.innerHTML = brief(d);   // single editorial report — no tabs
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
  // ── Editorial / infographic front page ──────────────────────────────────
  function injectStyles() {
    if (document.getElementById('mr-ed-styles')) return;
    const s = document.createElement('style');
    s.id = 'mr-ed-styles';
    s.textContent =
      ".ed-mast{margin:2px 0 8px}" +
      ".ed-kicker{font-size:11px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#14587D}" +
      ".ed-headline{font-family:'Fraunces',Georgia,serif;font-weight:600;font-size:clamp(22px,3.3vw,31px);line-height:1.13;letter-spacing:-.01em;color:#0A2F46;margin:7px 0 7px}" +
      ".ed-dek{font-size:15px;line-height:1.55;color:#475569;max-width:780px}" +
      ".ed-sec-h{display:flex;align-items:center;gap:8px;font-size:11.5px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;color:#64748b;margin:24px 0 11px;padding-top:15px;border-top:1px solid rgba(10,47,70,.08)}" +
      ".ed-bullets{display:grid;gap:9px;margin:4px 0 2px}" +
      ".ed-bullet{display:flex;gap:11px;font-size:14.5px;line-height:1.5;color:#1e293b}" +
      ".ed-bullet::before{content:'';flex:none;width:7px;height:7px;border-radius:50%;background:#14587D;margin-top:7px}" +
      ".ed-chart{display:grid;gap:7px;margin:4px 0 2px}" +
      ".ed-row{display:grid;grid-template-columns:104px 1fr 62px;align-items:center;gap:11px}" +
      ".ed-row .lbl{font-size:13px;font-weight:600;color:#334155;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
      ".ed-track{position:relative;height:18px;border-radius:6px;background:rgba(10,47,70,.05)}" +
      ".ed-track .mid{position:absolute;left:50%;top:0;bottom:0;width:1px;background:rgba(10,47,70,.18)}" +
      ".ed-fill{position:absolute;top:2px;bottom:2px;border-radius:5px;min-width:2px}" +
      ".ed-fill.pos{background:linear-gradient(90deg,#34d399,#15803d)}" +
      ".ed-fill.neg{background:linear-gradient(90deg,#f87171,#dc2626)}" +
      ".ed-row .v{font-size:13px;font-weight:800;text-align:right}" +
      ".ed-vix{display:flex;align-items:center;gap:16px;margin:8px 0 2px}" +
      ".ed-gauge{position:relative;flex:1;height:14px;border-radius:8px;background:linear-gradient(90deg,#15803d 0%,#16a34a 22%,#FF6100 50%,#ea7317 72%,#dc2626 100%)}" +
      ".ed-gauge .pin{position:absolute;top:-5px;width:3px;height:24px;background:#0A2F46;border-radius:2px;transform:translateX(-50%);box-shadow:0 0 0 2px #fff}" +
      ".ed-vix .read{flex:none;text-align:right;min-width:120px}" +
      ".ed-vix .read .n{font-size:23px;font-weight:800;color:#0A2F46;line-height:1}" +
      ".ed-vix .read .w{font-size:11.5px;font-weight:600;color:#64748b;margin-top:2px}" +
      ".ed-tom{background:rgba(20,88,125,.06);border:1px solid rgba(20,88,125,.18);border-radius:14px;padding:15px 18px;margin:4px 0 2px}" +
      ".ed-tom .h{font-size:15.5px;font-weight:700;color:#0A2F46;margin-bottom:6px}" +
      ".ed-tom .t{font-size:14.5px;line-height:1.6;color:#334155}" +
      ".ed-regime{background:linear-gradient(180deg,rgba(20,88,125,.05),rgba(20,88,125,.02));border:1px solid rgba(20,88,125,.16);border-radius:14px;padding:16px 18px;margin:2px 0}" +
      ".ed-rg-top{display:flex;align-items:baseline;justify-content:space-between;gap:12px}" +
      ".ed-rg-lab{font-family:'Fraunces',Georgia,serif;font-size:22px;font-weight:600;color:#0A2F46}" +
      ".ed-rg-score{font-size:22px;font-weight:800;color:#14587D}.ed-rg-score span{font-size:13px;font-weight:600;color:#94a3b8}" +
      ".ed-rg-bar{display:flex;align-items:center;gap:9px;margin:10px 0 4px}" +
      ".ed-rg-bar .lo,.ed-rg-bar .hi{font-size:11px;font-weight:600;color:#64748b;white-space:nowrap}" +
      ".ed-rg-track{position:relative;flex:1;height:12px;border-radius:7px;background:linear-gradient(90deg,#dc2626,#FF6100 50%,#15803d)}" +
      ".ed-rg-track .pin{position:absolute;top:-4px;width:3px;height:20px;background:#0A2F46;border-radius:2px;transform:translateX(-50%);box-shadow:0 0 0 2px #fff}" +
      ".ed-rg-read{font-size:14.5px;line-height:1.55;color:#334155;margin:10px 0 4px}" +
      ".ed-tf-h,.ed-tf{display:grid;grid-template-columns:96px 1fr 1fr 1fr;gap:6px;align-items:center}" +
      ".ed-tf-h{font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#94a3b8;margin-top:8px;padding-bottom:4px;border-bottom:1px solid rgba(10,47,70,.07)}" +
      ".ed-tf-h span,.ed-tf span:not(.nm){text-align:right}" +
      ".ed-tf{font-size:13px;padding:5px 0;border-bottom:1px solid rgba(10,47,70,.05)}" +
      ".ed-tf .nm{font-weight:600;color:#334155}" +
      ".ed-track-stat{font-size:13.5px;line-height:1.5;color:#334155;background:rgba(21,128,61,.07);border:1px solid rgba(21,128,61,.18);border-radius:11px;padding:11px 14px;margin-top:10px}" +
      // White floating-card wrapper for the chart/narrative sections so they
      // read as panels instead of sitting loose on the panel background.
      ".ed-card{background:#fff;border:1px solid rgba(10,47,70,.07);border-radius:16px;padding:2px 20px 18px;margin:14px 0;box-shadow:0 2px 14px rgba(10,47,70,.06)}" +
      ".ed-card>.ed-sec-h:first-child{border-top:0;margin-top:14px;padding-top:0}" +
      // Inside a white card the regime gauge drops its own tinted box (no card-in-card).
      ".ed-card .ed-regime{background:none;border:0;padding:0;margin:6px 0 0}";
    document.head.appendChild(s);
  }

  // Diverging horizontal bar chart from [{label, pct}].
  function diverBars(items) {
    items = (items || []).filter(i => i.pct != null);
    if (!items.length) return '';
    const maxAbs = Math.max(0.5, ...items.map(i => Math.abs(i.pct)));
    return '<div class="ed-chart">' + items.map(i => {
      const pos = i.pct >= 0, w = Math.min(49, Math.abs(i.pct) / maxAbs * 49);
      const fill = pos ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
      return `<div class="ed-row"><div class="lbl">${i.label}</div>` +
        `<div class="ed-track"><div class="mid"></div><div class="ed-fill ${pos ? 'pos' : 'neg'}" style="${fill}"></div></div>` +
        `<div class="v ${cls(i.pct)}">${pctTxt(i.pct)}</div></div>`;
    }).join('') + '</div>';
  }

  function sectorChart(d) {
    const secs = (d.sectors || []).slice();
    if (!secs.length) return '';
    secs.sort((a, b) => (b.chg_1d || 0) - (a.chg_1d || 0));
    const note = (d.ai && d.ai.sectors_note) ? `<div class="ma-note">${aiTag()}${d.ai.sectors_note}</div>` : '';
    return `<div class="ed-sec-h">🔁 Sector rotation — today</div>` + note +
      diverBars(secs.map(s => ({ label: s.name, pct: s.chg_1d })));
  }

  // 📡 The Big Picture — our proprietary regime gauge (the IBD-style market pulse).
  // The per-index table was removed: its 1-day column duplicated "Markets at a
  // glance" and the multi-timeframe context now lives in the AI regime read.
  function regimeSection(d) {
    const r = d.regime || {};
    if (r.regime_label == null || r.regime_score == null) return '';
    const pin = Math.max(2, Math.min(98, r.regime_score));
    const read = (d.ai && d.ai.regime_read) ? `<div class="ed-rg-read">${aiTag()}${d.ai.regime_read}</div>` : '';
    return `<div class="ed-sec-h">📡 The Big Picture · our regime model</div>` +
      `<div class="ed-regime">` +
        `<div class="ed-rg-top"><div class="ed-rg-lab">${r.regime_label}</div><div class="ed-rg-score">${N(r.regime_score, 0)}<span>/100</span></div></div>` +
        `<div class="ed-rg-bar"><span class="lo">Defensive</span><div class="ed-rg-track"><div class="pin" style="left:${pin}%"></div></div><span class="hi">Risk-on</span></div>` +
        read +
      `</div>`;
  }

  // 🎯 Through our lens — what today did to our top-rated names + the track record.
  function houseSection(d) {
    const hv = (d.ai && d.ai.house_view) ? `<div class="ed-dek">${aiTag()}${d.ai.house_view}</div>` : '';
    const top = ((d.stocks || {}).top_score || []).slice(0, 5);
    const tr = (d.house && d.house.track) || {};
    if (!hv && !top.length) return '';
    const bars = top.length ? diverBars(top.map(x => ({ label: x.ticker, pct: x.change_pct }))) : '';
    const stat = (tr && tr.n) ? `<div class="ed-track-stat"><b>Our track record:</b> ${tr.hit_rate}% hit rate across ${tr.n} closed picks · avg move ${pctTxt(tr.avg)} · avg winner ${pctTxt(tr.avg_win)}.</div>` : '';
    return `<div class="ed-sec-h">🎯 Through our lens · top-rated names today</div>` + hv + bars + stat;
  }

  // Wrap a section's HTML in a white floating card (skips empty sections).
  const sect = h => h ? `<div class="ed-card">${h}</div>` : '';

  function brief(d) {
    injectStyles();
    const v = (d.ai && d.ai.verdict) || {};
    const headline = (d.ai && d.ai.headline) || v.headline ||
      (d.kind === 'post' ? 'The day on Wall Street' : 'Where the market stands');
    const dek = (d.ai && d.ai.dek) || '';

    return `<div class="ed-mast">` +
        `<div class="ed-kicker">From the desk · Daily market report</div>` +
        `<div class="ed-headline">${headline}</div>` +
        (dek ? `<div class="ed-dek">${dek}</div>` : '') +
      `</div>` +
      `<div class="ed-sec-h">${d.kind === 'post' ? '⚖️ The verdict · into tomorrow' : '⚖️ The verdict · the day ahead'}</div>` +
      verdict(d) +
      events(d) +
      sect(regimeSection(d)) +
      sect(sectorChart(d)) +
      sect(houseSection(d)) +
      `<div class="ed-sec-h">📐 Technicals · S&amp;P 500</div>` + tech(d);
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
        dist52Card(t) + ret1mCard(t) + volCard(t) +
      `</div>`;
  }

  // ── extra technical KPIs (52-wk positioning, momentum, realised vol) ──────
  function dist52Card(t) {
    const d = t.dist_52w_high;
    const note = d == null ? 'Not available.'
      : d >= -0.05 ? 'At / near the 52-week high.'
      : d >= -5 ? 'Just shy of the yearly high.'
      : d >= -10 ? 'In a mild pullback from the high.'
      : 'Well off the yearly high.';
    const c = d == null ? 'ma-na' : d >= -3 ? 'ma-pos' : d <= -10 ? 'ma-neg' : 'ma-flat';
    const sub = (t.hi_52w != null && t.lo_52w != null)
      ? `52-wk range $${N(t.lo_52w, 2)} – $${N(t.hi_52w, 2)}.` : '52-week positioning.';
    return `<div class="ma-card"><div class="lbl"><span>From 52-wk high</span><span class="sym">SPY</span></div>` +
      `<div class="val">${d == null ? '—' : pctTxt(d)}</div>` +
      `<div class="chg ${c}">${note}</div><div class="sub">${sub}</div></div>`;
  }
  function ret1mCard(t) {
    const r = t.ret_1m;
    const note = r == null ? 'Not available.'
      : r >= 2 ? 'Solid one-month uptrend.'
      : r >= 0 ? 'Modestly higher over the month.'
      : r >= -2 ? 'Slightly lower over the month.'
      : 'Down over the past month.';
    return `<div class="ma-card"><div class="lbl"><span>1-month return</span><span class="sym">SPY</span></div>` +
      `<div class="val">${r == null ? '—' : pctTxt(r)}</div>` +
      `<div class="chg ${cls(r)}">${note}</div>` +
      `<div class="sub">Price vs ~21 trading days ago.</div></div>`;
  }
  function volCard(t) {
    const v = t.vol_20d;
    let note = 'Not available.', c = 'ma-na';
    if (v != null) {
      if (v < 12) { note = 'Calm — low realised volatility.'; c = 'ma-pos'; }
      else if (v < 20) { note = 'Normal volatility regime.'; c = 'ma-flat'; }
      else { note = 'Elevated — choppier tape.'; c = 'ma-neg'; }
    }
    return `<div class="ma-card"><div class="lbl"><span>Volatility (20d)</span><span class="sym">SPY</span></div>` +
      `<div class="val">${v == null ? '—' : N(v, 1) + '%'}</div>` +
      `<div class="chg ${c}">${note}</div>` +
      `<div class="sub">Annualised, from 20-day daily returns.</div></div>`;
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
      : `<div class="ma-lead">Stock-level detail across the <b>${s.count}-name</b> TickerMover universe — ${post ? "the day's tape" : "what's moving"}, the engine's top picks, earnings, and momentum flags.</div>`;

    return lead +
      `<div class="ma-two">` +
        `<div><div class="ma-mv-h"><span class="ic">📈</span> ${gainersLbl}</div>${table('Gainer', 'Score', s.gainers)}</div>` +
        `<div><div class="ma-mv-h"><span class="ic">📉</span> ${losersLbl}</div>${table('Loser', 'Score', s.losers)}</div>` +
      `</div>` +

      `<div class="ma-two">` +
        `<div><div class="ma-mv-h"><span class="ic">⭐</span> Highest Quant Score</div>${table('Stock', 'Score', s.top_score)}</div>` +
        `<div><div class="ma-mv-h"><span class="ic">🗓️</span> ${earningsTitle}</div>${table('Stock', earningsHdr, earningsList, earningsCell)}</div>` +
      `</div>` +

      `<div class="ma-two">` +
        `<div><div class="ma-mv-h"><span class="ic">🔊</span> Unusual volume</div>${table('Stock', 'Vol', s.unusual_volume, volCell)}</div>` +
        `<div><div class="ma-mv-h"><span class="ic">🚀</span> Near 52-week highs</div>${table('Stock', 'vs High', s.near_high, highCell)}</div>` +
      `</div>`;
  }

  // ── Verdict ─────────────────────────────────────────────────────────────
  /* Where the WHY came from. Our own feeds say what the tape did; the reason
     is the one thing no feed we hold carries, so the narrative is grounded in
     the day's reporting. Anything sourced that way has to be checkable — an
     unciteable claim about causation is just a guess in a confident voice. */
  function srcLine(ai) {
    const s = (ai && ai.sources) || [];
    if (!s.length) return '';
    const links = s.slice(0, 5).map(function (x, i) {
      const host = (function () { try { return new URL(x.url).hostname.replace(/^www\./, ''); }
                                  catch (e) { return x.url || ''; } })();
      return `<a class="ma-src" href="${x.url}" target="_blank" rel="noopener nofollow"
                 title="${(x.title || '').replace(/"/g, '&quot;')}">[W${i + 1}] ${host}</a>`;
    }).join('');
    return `<div class="ma-srcs"><span class="ma-srcs-k">Grounded in</span>${links}</div>`;
  }

  function verdict(d) {
    const gap = d.gap_pct, t = d.technicals || {}, spy = find(d.indices, 'SPY'), vix = find(d.rates_fx, '^VIX');
    if (d.ai && d.ai.verdict && d.ai.verdict.headline) {
      const v = d.ai.verdict, cf = (v.confidence || '').toLowerCase();
      const cfCls = cf.indexOf('high') >= 0 ? 'high' : cf.indexOf('low') >= 0 ? 'low' : 'med';
      const keyLvl = t.sma20 != null ? t.sma20 : (t.sma50 != null ? t.sma50 : null);
      return `<div class="ma-verdict">` +
        `<div class="big">${aiTag()}${v.headline}</div>` +
        (v.what_it_means ? `<div class="kv">${String(v.what_it_means).replace(/\n/g, '<br>')}</div>` : '') +
        (Array.isArray(v.evidence) && v.evidence.length
          ? `<div class="ma-ev"><div class="ma-ev-k">The evidence</div><ul>` +
            v.evidence.slice(0, 4).map(function (e) { return `<li>${e}</li>`; }).join('') +
            `</ul></div>` : '') +
        (v.counter_case ? `<div class="ma-ctr"><span class="ma-ctr-k">The other side</span> ${v.counter_case}</div>` : '') +
        (v.what_changes_view ? `<div class="kv" style="margin-top:8px"><b>What would change our view:</b> ${v.what_changes_view}</div>` : '') +
        (keyLvl != null ? `<div class="kv" style="margin-top:8px"><b>Key level to watch:</b> SPY $${N(keyLvl, 2)} (20-day line).</div>` : '') +
        `<div class="ma-conf ${cfCls}">Confidence: ${v.confidence || 'Medium'}</div>` +
        srcLine(d.ai) +
        `<div class="kv" style="margin-top:12px;color:#64748b">${(d.ai.sources||[]).length
          ? 'AI-written from live data and today’s reporting — context only, no buy or sell calls.'
          : 'AI-written from live data — context only, no buy or sell calls.'}</div>` +
      `</div>`;
    }
    // ── Deterministic fallback: a labelled read of the live signals (no AI). ──
    // Each signal carries a direction (+1 bull / -1 bear / 0 neutral) so the
    // "Read" line can NAME what leans which way instead of a bare tally, and an
    // interpretive line is derived from the trend-vs-momentum configuration.
    const sigs = [];
    const push = (label, state) => { if (state !== null) sigs.push({ label, state }); };
    push('the futures setup', gap == null ? null : gap > 0.1 ? 1 : gap < -0.1 ? -1 : 0);
    push('the cash close', spy.chg_pct == null ? null : spy.chg_pct > 0 ? 1 : spy.chg_pct < 0 ? -1 : 0);
    push('the short-term trend', (t.price != null && t.sma50 != null) ? (t.price >= t.sma50 ? 1 : -1) : null);
    push('the long-term trend', (t.price != null && t.sma200 != null) ? (t.price >= t.sma200 ? 1 : -1) : null);
    push('momentum', t.rsi14 == null ? null : t.rsi14 > 60 ? 1 : t.rsi14 < 40 ? -1 : 0);
    push('volatility', vix.last == null ? null : vix.last < 16 ? 1 : vix.last > 24 ? -1 : 0);

    const bull = sigs.filter(s => s.state > 0).length;
    const bear = sigs.filter(s => s.state < 0).length;
    const diff = bull - bear;
    const bias = diff >= 2 ? 'bullish' : diff <= -2 ? 'bearish' : 'neutral';
    const conf = Math.abs(diff) >= 4 ? ['high', 'High'] : Math.abs(diff) >= 2 ? ['med', 'Medium'] : ['low', 'Low'];
    const keyLvl = t.sma20 != null ? t.sma20 : (t.sma50 != null ? t.sma50 : null);
    const biasCls = bias === 'bullish' ? 'ma-pos' : bias === 'bearish' ? 'ma-neg' : 'ma-flat';
    const spyChg = spy.chg_pct;
    const openExp = gap == null ? 'open near flat' : gap > 0.15 ? 'open higher' : gap < -0.15 ? 'open lower' : 'open near flat';
    const closeExp = spyChg == null ? 'finished mixed' : spyChg > 0.15 ? 'closed higher' : spyChg < -0.15 ? 'closed lower' : 'closed near flat';
    const headline = d.kind === 'post'
      ? `The US market <span class="${spyChg > 0.15 ? 'ma-pos' : spyChg < -0.15 ? 'ma-neg' : 'ma-flat'}">${closeExp}</span>, with a <span class="${biasCls}">${bias}</span> tone into tomorrow.`
      : `US market is set to <span class="${gap > 0.15 ? 'ma-pos' : gap < -0.15 ? 'ma-neg' : 'ma-flat'}">${openExp}</span>, with an overall <span class="${biasCls}">${bias}</span> bias.`;

    // Name the signals on each side instead of a bare count.
    const nameList = arr => arr.length <= 1 ? (arr[0] || '')
      : arr.slice(0, -1).join(', ') + ' and ' + arr[arr.length - 1];
    const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
    const bulls = sigs.filter(s => s.state > 0).map(s => s.label);
    const bears = sigs.filter(s => s.state < 0).map(s => s.label);
    let read;
    if (bulls.length && bears.length) read = `${cap(nameList(bulls))} lean constructive, while ${nameList(bears)} lean cautious.`;
    else if (bulls.length) read = `${cap(nameList(bulls))} lean constructive, with nothing flashing a clear warning.`;
    else if (bears.length) read = `${cap(nameList(bears))} lean cautious, with nothing offering a clear all-clear.`;
    else read = `The signals we track are sitting on the fence — no clear edge either way.`;

    // One interpretive line from the trend-vs-momentum configuration.
    const trendBull = (t.price != null && t.sma200 != null) ? t.price >= t.sma200 : null;
    const momoBull = t.rsi14 == null ? null : t.rsi14 > 60;
    const momoBear = t.rsi14 == null ? null : t.rsi14 < 40;
    let means;
    if (trendBull === true && momoBear === true) means = 'The primary uptrend is intact, but short-term momentum is cooling — a pause within a larger advance rather than a reversal.';
    else if (trendBull === true && momoBull === true) means = 'Trend and momentum are aligned higher — buyers have been stepping in on dips.';
    else if (trendBull === false && momoBear === true) means = 'Both trend and momentum have rolled over — the burden of proof is on the bulls until the tape stabilises.';
    else if (trendBull === false) means = 'Price is working below its long-term average — a market on the back foot until it can reclaim its trend.';
    else means = 'Trend and momentum are pulling in different directions — a market in balance, waiting on its next catalyst.';

    // Price-aware key level: how far SPY sits from its 20-day line, and what each side means.
    let kvLine = '';
    if (keyLvl != null && t.price != null) {
      const distPct = (t.price - keyLvl) / keyLvl * 100, above = distPct >= 0;
      kvLine = `SPY sits <b>${Math.abs(distPct).toFixed(1)}% ${above ? 'above' : 'below'}</b> its 20-day line ($${N(keyLvl, 2)}). ` +
        (above ? 'Holding above it keeps the short-term uptrend intact; a daily close below would open the door to more two-way chop.'
               : 'Reclaiming it would steady the tape; until then, expect more two-way chop.');
    } else if (keyLvl != null) {
      kvLine = `SPY $${N(keyLvl, 2)} (20-day line). If it holds, the up-trend stays intact; if it breaks, expect more two-way chop.`;
    }

    return `<div class="ma-verdict">` +
      `<div class="big">${headline}</div>` +
      `<div class="kv"><b>Read:</b> ${read}</div>` +
      `<div class="kv" style="margin-top:8px"><b>What it means:</b> ${means}</div>` +
      (kvLine ? `<div class="kv" style="margin-top:8px"><b>Key level to watch:</b> ${kvLine}</div>` : '') +
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
