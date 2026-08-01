/*!
 * TickerMover — cookie consent banner (UK GDPR / PECR)
 * Self-contained, no dependencies, no third-party CMP.
 *
 * WHY THIS EXISTS
 *   Under PECR you may only set non-essential cookies (analytics that store an
 *   identifier, and ALL advertising/tracking cookies such as Google Ads and the
 *   Meta pixel) AFTER the visitor has given consent. This banner blocks those
 *   tags until the user opts in, remembers the choice, and lets them change it.
 *
 * HOW TO ADD AN AD / MARKETING TAG (Google Ads, Meta pixel, etc.)
 *   Do NOT paste the raw <script>. Instead mark it so it only runs on consent:
 *
 *     <!-- Meta Pixel -->
 *     <script type="text/plain" data-cc="marketing">
 *       !function(f,b,e,v,n,t,s){ ...standard Meta pixel code... }();
 *       fbq('init','YOUR_PIXEL_ID'); fbq('track','PageView');
 *     </script>
 *
 *     <!-- Google Ads / GA4 (gtag) -->
 *     <script type="text/plain" data-cc="marketing"
 *             data-src="https://www.googletagmanager.com/gtag/js?id=AW-XXXX"></script>
 *     <script type="text/plain" data-cc="marketing">
 *       window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}
 *       gtag('js', new Date()); gtag('config','AW-XXXX');
 *     </script>
 *
 *   Use data-cc="analytics" for analytics-only tags. Tags auto-activate the
 *   moment the matching consent is granted (this load or a later visit).
 *
 * PROGRAMMATIC API
 *   window.tmConsent.enabled('marketing') -> boolean
 *   window.tmConsent.get()                -> {analytics, marketing, ts, v}
 *   window.tmConsent.openSettings()       -> reopen the preferences panel
 *   window.tmConsent.onChange(fn)         -> called with state on every change
 *   Any element with [data-cc-open] reopens the preferences panel on click.
 *
 * Set CONSENT_BANNER=off (server env) to stop the loader injecting this file.
 */
(function () {
  "use strict";
  var VERSION = 1;
  var LS_KEY = "tm_consent_v" + VERSION;
  var COOKIE = "tm_consent";
  var MAX_AGE = 60 * 60 * 24 * 182; // ~6 months, then re-ask (ICO guidance)
  var listeners = [];

  // ---- storage --------------------------------------------------------
  function readStore() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (raw) return JSON.parse(raw);
    } catch (e) {}
    // fall back to cookie (survives localStorage clears / lets the server read it)
    var m = document.cookie.match(/(?:^|;\s*)tm_consent=([^;]+)/);
    if (m) { try { return JSON.parse(decodeURIComponent(m[1])); } catch (e) {} }
    return null;
  }
  function writeStore(state) {
    var payload = JSON.stringify(state);
    try { localStorage.setItem(LS_KEY, payload); } catch (e) {}
    var secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie = COOKIE + "=" + encodeURIComponent(payload) +
      "; Max-Age=" + MAX_AGE + "; Path=/; SameSite=Lax" + secure;
  }

  var state = readStore(); // null => not yet decided

  // ---- activate gated tags on consent --------------------------------
  function activate(category) {
    var sel = 'script[type="text/plain"][data-cc~="' + category + '"]';
    var nodes = document.querySelectorAll(sel);
    for (var i = 0; i < nodes.length; i++) {
      var old = nodes[i];
      if (old.dataset.ccActivated) continue;
      var s = document.createElement("script");
      // copy every attribute except the gating ones
      for (var a = 0; a < old.attributes.length; a++) {
        var at = old.attributes[a];
        if (at.name === "type" || at.name === "data-cc") continue;
        if (at.name === "data-src") { s.src = at.value; continue; }
        s.setAttribute(at.name, at.value);
      }
      if (old.textContent) s.text = old.textContent;
      old.dataset.ccActivated = "1";
      old.parentNode.insertBefore(s, old.nextSibling);
    }
  }
  function applyState(st) {
    if (st && st.analytics) activate("analytics");
    if (st && st.marketing) activate("marketing");
    document.documentElement.setAttribute(
      "data-consent",
      st ? ((st.analytics ? "a" : "") + (st.marketing ? "m" : "")) || "none" : "pending"
    );
    for (var i = 0; i < listeners.length; i++) { try { listeners[i](st); } catch (e) {} }
    try {
      document.dispatchEvent(new CustomEvent("tm:consent", { detail: st }));
    } catch (e) {}
  }

  function save(analytics, marketing) {
    state = { analytics: !!analytics, marketing: !!marketing, ts: Date.now(), v: VERSION };
    writeStore(state);
    applyState(state);
    close();
    renderReopen();
  }

  // ---- UI -------------------------------------------------------------
  var host, banner, modal;
  var CSS =
    '.tmcc,.tmcc *{box-sizing:border-box}' +
    '.tmcc-banner{position:fixed;left:16px;right:16px;bottom:16px;z-index:2147483000;max-width:560px;margin:0 auto;' +
    'background:#fff;color:#0f172a;border:1px solid rgba(9,20,60,.12);border-radius:16px;' +
    'box-shadow:0 18px 50px rgba(9,20,60,.28);padding:20px 22px;font:400 14.5px/1.55 "Instrument Sans",system-ui,-apple-system,Segoe UI,sans-serif}' +
    '.tmcc-banner h2{font:700 16px/1.3 "Instrument Sans",system-ui,sans-serif;margin:0 0 6px;color:#0a0e22}' +
    '.tmcc-banner p{margin:0 0 14px;color:#334155}' +
    '.tmcc-banner a{color:#0040c1;font-weight:600;text-decoration:underline}' +
    '.tmcc-row{display:flex;gap:10px;flex-wrap:wrap}' +
    '.tmcc-btn{cursor:pointer;border:1px solid transparent;border-radius:10px;font:600 14px/1 "Instrument Sans",system-ui,sans-serif;padding:11px 16px}' +
    '.tmcc-primary{background:#0040c1;color:#fff}' +
    '.tmcc-primary:hover{background:#0a0e22}' +
    '.tmcc-ghost{background:#fff;color:#0f172a;border-color:rgba(9,20,60,.18)}' +
    '.tmcc-ghost:hover{border-color:#0040c1;color:#0040c1}' +
    '.tmcc-link{background:none;border:none;color:#475569;text-decoration:underline;cursor:pointer;font:600 13.5px/1 "Instrument Sans",system-ui,sans-serif;padding:11px 6px}' +
    '.tmcc-overlay{position:fixed;inset:0;z-index:2147483001;background:rgba(9,15,35,.5);display:flex;align-items:center;justify-content:center;padding:16px}' +
    '.tmcc-modal{background:#fff;color:#0f172a;max-width:520px;width:100%;max-height:88vh;overflow:auto;border-radius:18px;padding:26px 26px 20px;' +
    'font:400 14.5px/1.55 "Instrument Sans",system-ui,sans-serif;box-shadow:0 24px 70px rgba(9,20,60,.4)}' +
    '.tmcc-modal h2{font:700 20px/1.25 "Instrument Sans",system-ui,sans-serif;margin:0 0 4px;color:#0a0e22}' +
    '.tmcc-modal>p{color:#475569;margin:0 0 18px}' +
    '.tmcc-cat{border:1px solid rgba(9,20,60,.1);border-radius:12px;padding:14px 16px;margin:0 0 12px}' +
    '.tmcc-cat-head{display:flex;justify-content:space-between;align-items:center;gap:12px}' +
    '.tmcc-cat-head strong{font-size:15px;color:#0a0e22}' +
    '.tmcc-cat p{margin:6px 0 0;color:#475569;font-size:13.5px}' +
    '.tmcc-fixed{font-size:12.5px;color:#15803d;font-weight:700}' +
    '.tmcc-sw{position:relative;width:42px;height:24px;flex:0 0 42px}' +
    '.tmcc-sw input{opacity:0;width:100%;height:100%;margin:0;cursor:pointer}' +
    '.tmcc-sw span{position:absolute;inset:0;background:#cbd5e1;border-radius:99px;transition:.2s;pointer-events:none}' +
    '.tmcc-sw span:before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}' +
    '.tmcc-sw input:checked+span{background:#0040c1}' +
    '.tmcc-sw input:checked+span:before{transform:translateX(18px)}' +
    '.tmcc-modal .tmcc-row{margin-top:16px;justify-content:flex-end}' +
    '.tmcc-reopen{position:fixed;left:16px;bottom:16px;z-index:2147482000;background:#fff;color:#334155;border:1px solid rgba(9,20,60,.14);' +
    'border-radius:99px;padding:8px 14px;font:600 12.5px/1 "Instrument Sans",system-ui,sans-serif;cursor:pointer;box-shadow:0 6px 18px rgba(9,20,60,.14)}' +
    '.tmcc-reopen:hover{color:#0040c1;border-color:#0040c1}' +
    '@media (prefers-reduced-motion:reduce){.tmcc-sw span,.tmcc-sw span:before{transition:none}}';

  function injectCSS() {
    if (document.getElementById("tmcc-style")) return;
    var st = document.createElement("style");
    st.id = "tmcc-style";
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);
  }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function showBanner() {
    if (banner) return;
    banner = el("div", "tmcc tmcc-banner");
    banner.setAttribute("role", "dialog");
    banner.setAttribute("aria-live", "polite");
    banner.setAttribute("aria-label", "Cookie consent");
    banner.innerHTML =
      '<h2>Your privacy choices</h2>' +
      '<p>We use essential cookies to run TickerMover. With your permission we ' +
      'also use analytics and advertising cookies to measure and promote the ' +
      'site. You can accept all, reject non-essential, or choose. See our ' +
      '<a href="/privacy">Privacy&nbsp;Policy</a>.</p>' +
      '<div class="tmcc-row">' +
      '<button class="tmcc-btn tmcc-primary" data-act="all">Accept all</button>' +
      '<button class="tmcc-btn tmcc-ghost" data-act="none">Reject non-essential</button>' +
      '<button class="tmcc-link" data-act="prefs">Manage preferences</button>' +
      '</div>';
    banner.addEventListener("click", function (e) {
      var a = e.target.getAttribute && e.target.getAttribute("data-act");
      if (a === "all") save(true, true);
      else if (a === "none") save(false, false);
      else if (a === "prefs") openSettings();
    });
    document.body.appendChild(banner);
  }

  function openSettings() {
    var cur = state || { analytics: false, marketing: false };
    var ov = el("div", "tmcc tmcc-overlay");
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    ov.setAttribute("aria-label", "Cookie preferences");
    var m = el("div", "tmcc-modal");
    m.innerHTML =
      '<h2>Cookie preferences</h2>' +
      '<p>Choose which cookies TickerMover may use. Essential cookies are always ' +
      'on because the site cannot work without them.</p>' +
      cat("Essential", null, "Sign-in, security and remembering your privacy choice. Always active.", true, true) +
      cat("Analytics", "analytics", "Helps us understand how the site is used so we can improve it.", cur.analytics, false) +
      cat("Advertising", "marketing", "Lets us measure and target our ads (e.g. Google Ads, Meta). Only set if you allow it.", cur.marketing, false) +
      '<div class="tmcc-row">' +
      '<button class="tmcc-btn tmcc-ghost" data-act="none">Reject non-essential</button>' +
      '<button class="tmcc-btn tmcc-primary" data-act="save">Save choices</button>' +
      '</div>';
    ov.appendChild(m);
    ov.addEventListener("click", function (e) {
      if (e.target === ov) closeModal(ov);
      var a = e.target.getAttribute && e.target.getAttribute("data-act");
      if (a === "none") { closeModal(ov); save(false, false); }
      else if (a === "save") {
        var an = m.querySelector('input[data-cat="analytics"]');
        var mk = m.querySelector('input[data-cat="marketing"]');
        closeModal(ov);
        save(an && an.checked, mk && mk.checked);
      }
    });
    document.addEventListener("keydown", escClose);
    modal = ov;
    document.body.appendChild(ov);
    var first = m.querySelector("input:not([disabled])");
    if (first) first.focus();
  }
  function cat(name, key, desc, on, fixed) {
    return '<div class="tmcc-cat"><div class="tmcc-cat-head"><strong>' + name + '</strong>' +
      (fixed
        ? '<span class="tmcc-fixed">Always on</span>'
        : '<label class="tmcc-sw"><input type="checkbox" data-cat="' + key + '"' + (on ? " checked" : "") +
          ' aria-label="' + name + ' cookies"><span></span></label>') +
      '</div><p>' + desc + '</p></div>';
  }
  function escClose(e) { if (e.key === "Escape" && modal) closeModal(modal); }
  function closeModal(ov) {
    if (ov && ov.parentNode) ov.parentNode.removeChild(ov);
    if (modal === ov) modal = null;
    document.removeEventListener("keydown", escClose);
  }
  function close() {
    if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
    banner = null;
  }

  function renderReopen() {
    // Persistent way to change/withdraw consent, as required. Skipped if the
    // page already provides its own [data-cc-open] trigger (e.g. a footer link).
    if (document.querySelector("[data-cc-open]")) return;
    if (document.getElementById("tmcc-reopen")) return;
    if (!state) return; // only show once a choice has been made
    var b = el("button", "tmcc tmcc-reopen", "Cookie settings");
    b.id = "tmcc-reopen";
    b.type = "button";
    b.addEventListener("click", openSettings);
    document.body.appendChild(b);
  }

  // ---- public API -----------------------------------------------------
  window.tmConsent = {
    get: function () { return state; },
    enabled: function (cat) { return !!(state && state[cat]); },
    openSettings: openSettings,
    onChange: function (fn) { if (typeof fn === "function") { listeners.push(fn); if (state) fn(state); } }
  };

  // ---- boot -----------------------------------------------------------
  function boot() {
    injectCSS();
    document.addEventListener("click", function (e) {
      var t = e.target;
      while (t && t !== document) {
        if (t.hasAttribute && t.hasAttribute("data-cc-open")) { e.preventDefault(); openSettings(); return; }
        t = t.parentNode;
      }
    });
    if (state) { applyState(state); renderReopen(); }
    else { applyState(null); showBanner(); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
