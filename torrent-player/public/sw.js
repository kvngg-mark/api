/* KV client cache: caches HLS segments on the device, tied to home-screen install.
 * - Browser tab:        small budget, may be evicted by the browser.
 * - Installed + persistent: large budget, protected from eviction.
 * Eviction is per episode, least recently watched first. */
const VERSION = 'v1';
const CACHE = `kv-hls-${VERSION}`;
const SMALL_BUDGET = 150 * 1024 ** 2;   // plain browser tab
const BIG_BUDGET = 2 * 1024 ** 3;       // installed + persistent storage
const QUOTA_SHARE = 0.5;                // never use more than this share of the browser quota
const EXTRA_ORIGINS = [];               // e.g. ['https://cdn.example.com'] (must send CORS headers)
const HLS_RE = /\/hls\/([a-f0-9]{40})\/(\d+)\/([\w.-]+)$/;

/* ---------- tiny IndexedDB wrapper ---------- */
let dbp;
const db = () => (dbp ||= new Promise((resolve, reject) => {
  const r = indexedDB.open('kv-cache', 1);
  r.onupgradeneeded = () => {
    r.result.createObjectStore('eps', { keyPath: 'key' });
    r.result.createObjectStore('cfg', { keyPath: 'k' });
  };
  r.onsuccess = () => resolve(r.result);
  r.onerror = () => reject(r.error);
}));
async function run(store, mode, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const t = d.transaction(store, mode);
    const req = fn(t.objectStore(store));
    t.oncomplete = () => resolve(req && req.result);
    t.onerror = t.onabort = () => reject(t.error);
  });
}
const allRows = () => run('eps', 'readonly', (s) => s.getAll());
const getRow = (k) => run('eps', 'readonly', (s) => s.get(k));
const putRow = (row) => run('eps', 'readwrite', (s) => s.put(row));
const delRow = (k) => run('eps', 'readwrite', (s) => s.delete(k));
const getMode = async () => (await run('cfg', 'readonly', (s) => s.get('mode'))) || {};
const setMode = (m) => run('cfg', 'readwrite', (s) => s.put({ k: 'mode', installed: !!m.installed, persisted: !!m.persisted }));

// serialize accounting so concurrent segment stores don't race
let chain = Promise.resolve();
const serial = (fn) => { const p = chain.then(fn); chain = p.catch(() => {}); return p; };

/* ---------- budget + eviction ---------- */
async function getBudget() {
  const m = await getMode();
  let b = m.installed && m.persisted ? BIG_BUDGET : SMALL_BUDGET;
  try {
    const { quota } = await self.navigator.storage.estimate();
    if (quota) b = Math.min(b, quota * QUOTA_SHARE);
  } catch {}
  return b;
}
async function dropEpisode(key) {
  const [hash, idx] = key.split(':');
  const cache = await caches.open(CACHE);
  for (const req of await cache.keys()) {
    const m = HLS_RE.exec(new URL(req.url).pathname);
    if (m && m[1] === hash && m[2] === idx) await cache.delete(req);
  }
  await delRow(key);
}
const sum = (rows) => rows.reduce((a, r) => a + r.bytes, 0);

// Make room for `size` more bytes of episode `ep`; false = doesn't fit, so don't cache it.
function admit(ep, size) {
  return serial(async () => {
    const budget = await getBudget();
    const rows = await allRows();
    let total = sum(rows);
    if (total + size > budget) {
      const others = rows.filter((r) => r.key !== ep).sort((a, b) => a.last - b.last);
      for (const r of others) {
        if (total + size <= budget) break;
        await dropEpisode(r.key);
        total -= r.bytes;
      }
    }
    if (total + size > budget) return false;
    const cur = rows.find((r) => r.key === ep);
    await putRow({ key: ep, bytes: (cur ? cur.bytes : 0) + size, last: Date.now() });
    return true;
  });
}
const enforceAll = () => serial(async () => {
  const budget = await getBudget();
  const rows = (await allRows()).sort((a, b) => a.last - b.last);
  let total = sum(rows);
  for (const r of rows) {
    if (total <= budget) break;
    await dropEpisode(r.key);
    total -= r.bytes;
  }
});
const touch = (ep) => serial(async () => {
  const row = await getRow(ep);
  if (row) await putRow({ ...row, last: Date.now() });
});

/* ---------- fetch handling ---------- */
async function segment(e, req, ep) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(req);
  if (hit) { e.waitUntil(touch(ep)); return hit; }
  const res = await fetch(req);
  if (res.status === 200) {
    const copy = res.clone();
    e.waitUntil((async () => {
      try {
        const size = Number(copy.headers.get('content-length')) || (await copy.clone().blob()).size;
        if (await admit(ep, size)) await cache.put(req, copy);
      } catch {}
    })());
  }
  return res;
}

// Playlist: network first. Only keep a copy once the episode is complete (#EXT-X-ENDLIST),
// and fall back to it if the network fails or the server evicted the episode (404).
async function playlist(e, req, ep) {
  const cache = await caches.open(CACHE);
  try {
    const res = await fetch(req);
    if (res.status === 200) {
      const text = await res.clone().text();
      if (text.includes('#EXT-X-ENDLIST')) e.waitUntil(cache.put(req, res.clone()).then(() => touch(ep)).catch(() => {}));
      return res;
    }
    const cached = await cache.match(req);
    return cached || res;
  } catch (err) {
    const cached = await cache.match(req);
    if (cached) return cached;
    throw err;
  }
}

// Subtitles: cached only for episodes whose playlist is already cached (i.e. complete).
async function subs(e, req) {
  const cache = await caches.open(CACHE);
  try {
    const res = await fetch(req);
    if (res.ok) {
      if (await cache.match(new URL('index.m3u8', req.url))) e.waitUntil(cache.put(req, res.clone()).catch(() => {}));
      return res;
    }
    return (await cache.match(req)) || res;
  } catch (err) {
    const cached = await cache.match(req);
    if (cached) return cached;
    throw err;
  }
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET' || req.headers.has('range')) return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin && !EXTRA_ORIGINS.includes(url.origin)) return;
  const m = HLS_RE.exec(url.pathname);
  if (!m) return;
  const ep = `${m[1]}:${m[2]}`;
  if (m[3].endsWith('.ts')) e.respondWith(segment(e, req, ep));
  else if (m[3] === 'index.m3u8') e.respondWith(playlist(e, req, ep));
  else if (m[3] === 'subs.vtt') e.respondWith(subs(e, req));
});

/* ---------- messages from the page ---------- */
self.addEventListener('message', (e) => {
  const d = e.data || {};
  const reply = (m) => e.ports && e.ports[0] && e.ports[0].postMessage(m);
  if (d.type === 'mode') {
    e.waitUntil(setMode(d).then(enforceAll).then(() => reply({ ok: true })));
  } else if (d.type === 'usage') {
    e.waitUntil((async () => {
      const rows = await allRows();
      reply({ bytes: sum(rows), episodes: rows.length, budget: await getBudget(), ...(await getMode()) });
    })());
  } else if (d.type === 'clear') {
    e.waitUntil(serial(async () => {
      await caches.delete(CACHE);
      for (const r of await allRows()) await delRow(r.key);
      reply({ ok: true });
    }));
  }
});

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k.startsWith('kv-hls-') && k !== CACHE) await caches.delete(k);
  await self.clients.claim();
})()));
