/* AlphaHunt Service Worker - v2.0

   Strategy:
   - HTML pages: NETWORK-FIRST with cache fallback (so users always see latest UI
     when online, but can still open the app when offline).
   - Static assets (icons, manifest, fonts): CACHE-FIRST with network update.
   - API + WebSocket: always go to network, never cached.
   - Offline fallback: a tiny inline HTML page that says we are offline.

   Bumping CACHE_VERSION below forces all clients to re-fetch on next visit.
*/

const CACHE_VERSION = 'alphahunt-v2';
const CORE_ASSETS = [
  '/static/manifest.json',
  '/static/icons/favicon-32.png',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

const OFFLINE_HTML = '<!DOCTYPE html><html lang="en"><head>' +
'<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">' +
'<title>Offline - AlphaHunt</title>' +
'<meta name="theme-color" content="#15803d">' +
'<style>' +
'*{margin:0;padding:0;box-sizing:border-box}' +
'body{font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif;' +
'background:#fafbfc;color:#0a0a0a;min-height:100vh;' +
'display:flex;align-items:center;justify-content:center;padding:24px;line-height:1.55}' +
'.box{max-width:420px;text-align:center}' +
'.dot{width:14px;height:14px;background:#dc2626;border-radius:50%;display:inline-block;margin-right:8px;vertical-align:middle}' +
'h1{font-size:32px;font-weight:900;letter-spacing:-.02em;margin-bottom:12px;color:#0a0a0a}' +
'p{font-size:15.5px;color:#475569;margin-bottom:18px}' +
'button{background:#15803d;color:#fff;border:none;padding:12px 24px;border-radius:10px;' +
'font-size:14.5px;font-weight:700;cursor:pointer}' +
'button:hover{background:#0e6b32}' +
'.brand{display:inline-flex;align-items:center;gap:8px;margin-bottom:32px;font-size:16px;font-weight:800;color:#0a0a0a}' +
'.brand em{font-style:normal;color:#15803d}' +
'</style></head><body><div class="box">' +
'<div class="brand">AlphaHunt</div>' +
'<h1><span class="dot"></span>You are offline</h1>' +
'<p>AlphaHunt needs an internet connection to fetch live market data. ' +
'Once you are back online, tap the button below to reload.</p>' +
'<button onclick="location.reload()">Try again</button>' +
'</div></body></html>';

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then(cache => {
      return cache.addAll(CORE_ASSETS).then(() => {
        return cache.put(
          new Request('/__offline__'),
          new Response(OFFLINE_HTML, {
            headers: { 'Content-Type': 'text/html; charset=utf-8' }
          })
        );
      });
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('message', event => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) {
    return;
  }
  if (url.origin !== self.location.origin) return;

  const isHTML = req.mode === 'navigate' ||
                 (req.headers.get('accept') || '').includes('text/html');

  if (isHTML) {
    event.respondWith(
      fetch(req)
        .then(res => {
          if (res && res.status === 200) {
            const clone = res.clone();
            caches.open(CACHE_VERSION).then(c => c.put(req, clone));
          }
          return res;
        })
        .catch(() =>
          caches.match(req).then(cached => cached || caches.match('/__offline__'))
        )
    );
    return;
  }

  event.respondWith(
    caches.match(req).then(cached => {
      const networkPromise = fetch(req).then(res => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_VERSION).then(c => c.put(req, clone));
        }
        return res;
      }).catch(() => cached);
      return cached || networkPromise;
    })
  );
});
