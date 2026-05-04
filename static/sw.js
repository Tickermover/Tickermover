/* AlphaHunt Service Worker — v1.0
   Caches the shell (HTML/CSS/JS) for offline resilience.
   API calls always go to network — never serve stale data.
*/

const CACHE   = 'alphahunt-v1';
const SHELL   = ['/'];   // cache the main page shell

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API calls + WebSocket — always network, never cache
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) {
    return;   // let browser handle normally
  }

  // Shell: cache-first, fallback to network
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request).then(res => {
      if (res && res.status === 200 && e.request.method === 'GET') {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
      }
      return res;
    }))
  );
});
