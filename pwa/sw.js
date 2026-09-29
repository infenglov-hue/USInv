const CACHE_NAME = 'us-picker-shell-v1';
const APP_SHELL = [
  './',
  './index.html',
  './index.css?v=us1',
  './app.js?v=us1',
  './portfolio-costs.js?v=us1',
  './manifest.webmanifest?v=us1',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './vendor/pako.min.js?v=us1',
  './vendor/sql-wasm.js?v=us1',
  './vendor/sql-wasm.wasm?v=us1',
  './vendor/apexcharts.min.js?v=us1'
];

self.addEventListener('install', (e) => {
  console.log('[Service Worker] Install event');
  e.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      console.log('[Service Worker] Caching app shell assets');
      await cache.addAll(
        APP_SHELL.map(asset => new Request(asset, { cache: 'reload' }))
      );
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  console.log('[Service Worker] Activate event');
  e.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            console.log('[Service Worker] Removing old cache:', key);
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('message', (e) => {
  if (e.data?.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;

  const url = new URL(e.request.url);

  // Snapshot metadata and database content must always be checked online.
  // The application itself falls back to the last IndexedDB snapshot offline.
  if (url.pathname.includes('mobile_snapshot.db.gz') || url.pathname.includes('manifest.json')) {
    e.respondWith(fetch(e.request, { cache: 'no-store' }));
    return;
  }

  // Keep page navigations fresh, but preserve an offline app-shell fallback.
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put('./index.html', copy));
          }
          return response;
        })
        .catch(async () => (
          await caches.match('./index.html') ||
          await caches.match('./')
        ))
    );
    return;
  }

  // Network first makes installed PWAs receive new code immediately. Successful
  // responses are retained for offline startup, including optional web fonts.
  e.respondWith(
    fetch(e.request)
      .then((response) => {
        if (response && (response.ok || response.type === 'opaque')) {
          const responseToCache = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(e.request, responseToCache);
          });
        }
        return response;
      })
      .catch(() => {
        // Fallback to cache if network request fails (Offline support)
        return caches.match(e.request).then(
          cached => cached || caches.match(e.request, { ignoreSearch: true })
        );
      })
  );
});
