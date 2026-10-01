/* SahayaLink service worker: keeps the app usable with weak or no signal.
   Pages and data are fetched from the network first; when there is no signal,
   the last saved copy is shown. Actions taken offline are queued by the page itself. */
const SHELL = 'sl-shell-v3';
const API = 'sl-api';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(['/'])).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(k => k.startsWith('sl-shell') && k !== SHELL).map(k => caches.delete(k))
  )).then(() => self.clients.claim()));
});

function apiKey(req) {
  // keep one saved copy per signed-in person so people never see each other's data
  const auth = req.headers.get('Authorization') || '';
  const u = new URL(req.url);
  u.searchParams.set('__who', auth.slice(-16));
  return u.toString();
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (url.pathname.startsWith('/api/')) {
    if (url.pathname.startsWith('/api/demo/')) return;
    const key = apiKey(req);
    e.respondWith(
      fetch(req).then(res => {
        if (res.ok && (res.headers.get('content-type') || '').includes('json')) {
          const copy = res.clone();
          caches.open(API).then(c => c.put(key, copy));
        }
        return res;
      }).catch(() => caches.match(key).then(m => m || new Response(
        JSON.stringify({ detail: 'You are offline and this page has not been saved on this phone yet.' }),
        { status: 503, headers: { 'Content-Type': 'application/json' } })))
    );
    return;
  }

  if (url.pathname === '/' || url.pathname === '/index.html') {
    e.respondWith(
      fetch(req).then(res => {
        const copy = res.clone();
        caches.open(SHELL).then(c => c.put('/', copy));
        return res;
      }).catch(() => caches.match('/'))
    );
  }
});
