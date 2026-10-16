/* Ripster service worker — minimal "installable PWA" shell.
 *
 * We intentionally do NOT cache the HTML/JS bundle: it changes often and the
 * server already sends Cache-Control: no-store on /. Caching the SPA would
 * make updates invisible and confuse the user.
 *
 * We DO cache a tiny set of static assets — icons, logo, favicon — so the
 * home-screen icon survives offline and PWA install passes lighthouse.
 */
const CACHE = 'ripster-shell-v1';
const ASSETS = [
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png',
  '/static/favicon-32.png',
  '/static/raccoon.jpg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // Only serve from cache for our own static asset list — everything else
  // (HTML, API, WS, stream) passes through to the network untouched.
  if (event.request.method !== 'GET') return;
  if (!ASSETS.includes(url.pathname)) return;
  event.respondWith(
    caches.match(event.request).then((hit) => hit || fetch(event.request))
  );
});
