import express from 'express';
import WebTorrent from 'webtorrent';
import Redis from 'ioredis';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ---------- config ----------
const PORT = Number(process.env.PORT || 3000);
const REDIS_URL = process.env.REDIS_URL || 'redis://127.0.0.1:6379';
const DATA = path.resolve(process.env.DATA_DIR || path.join(__dirname, 'data'));
const DL_DIR = path.join(DATA, 'downloads'); // raw torrent data (temporary)
const HLS_DIR = path.join(DATA, 'hls');      // transcoded cache (LRU managed via Redis)
const MAX_CACHE_BYTES = Number(process.env.MAX_CACHE_GB || 8) * 1024 ** 3;
const IDLE_MS = Number(process.env.TORRENT_IDLE_MIN || 5) * 60_000;
const UPLOAD_LIMIT = Number(process.env.UPLOAD_KBPS || 256) * 1024; // keep seeding modest
const META_TTL = 14 * 24 * 3600;
const POS_TTL = 90 * 24 * 3600;
const TOKEN = crypto.randomBytes(16).toString('hex'); // guards /raw so only our ffmpeg can use it
const VIDEO_EXT = ['.mkv', '.mp4', '.webm', '.m4v', '.mov', '.avi'];
const TEXT_SUBS = ['ass', 'ssa', 'subrip', 'webvtt', 'mov_text', 'text'];
const HASH_RE = /^[a-f0-9]{40}$/;

// ---------- redis ----------
const redis = new Redis(REDIS_URL, { maxRetriesPerRequest: 2 });
let redisWarned = false;
redis.on('error', (e) => { if (!redisWarned) { redisWarned = true; console.error('Redis error:', e.message); } });
try { await redis.ping(); } catch {
  console.error(`\nCannot reach Redis at ${REDIS_URL}. Run "npm start" (starts it for you) or "redis-server --daemonize yes".\n`);
  process.exit(1);
}

await fsp.rm(DL_DIR, { recursive: true, force: true }); // leftovers from a previous run
await fsp.mkdir(DL_DIR, { recursive: true });
await fsp.mkdir(HLS_DIR, { recursive: true });

const client = new WebTorrent({ uploadLimit: UPLOAD_LIMIT });
client.on('error', (e) => console.error('webtorrent:', e.message));

const hlsDir = (h, i) => path.join(HLS_DIR, h, String(i));
const keyOf = (h, i) => `${h}:${i}`;
const touched = new Map(); // infoHash -> last activity
const jobs = new Map();    // "hash:idx" -> job state
const pending = new Map(); // hash -> Promise<Torrent> (dedupe concurrent adds)

// ---------- smart cache: metadata ----------
async function getMeta(h) {
  const raw = await redis.get(`tp:meta:${h}`);
  return raw ? JSON.parse(raw) : null;
}
async function saveMeta(t) {
  const meta = {
    name: t.name,
    files: t.files.map((f, index) => ({
      index, name: f.name, length: f.length,
      playable: VIDEO_EXT.includes(path.extname(f.name).toLowerCase()),
    })),
    torrentB64: Buffer.from(t.torrentFile).toString('base64'), // lets us rejoin without a metadata lookup
  };
  await redis.set(`tp:meta:${t.infoHash}`, JSON.stringify(meta), 'EX', META_TTL);
  return meta;
}

// ---------- smart cache: transcoded output (LRU by last access) ----------
async function dropCache(k) {
  const [h, i] = k.split(':');
  await fsp.rm(hlsDir(h, i), { recursive: true, force: true });
  await redis.multi().zrem('tp:lru', k).hdel('tp:sizes', k).del(`tp:done:${k}`).exec();
}
async function isCached(h, i) {
  const k = keyOf(h, i);
  if (!(await redis.exists(`tp:done:${k}`))) return false;
  if (fs.existsSync(path.join(hlsDir(h, i), '.done'))) return true;
  await dropCache(k);
  return false;
}
async function cacheUsage() {
  const sizes = await redis.hgetall('tp:sizes');
  return Object.values(sizes).reduce((a, b) => a + Number(b), 0);
}
async function evict() {
  let total = await cacheUsage();
  if (total <= MAX_CACHE_BYTES) return;
  for (const k of await redis.zrange('tp:lru', 0, -1)) { // oldest first
    if (total <= MAX_CACHE_BYTES) break;
    const j = jobs.get(k);
    if (j && (j.state === 'probing' || j.state === 'running')) continue;
    total -= Number((await redis.hget('tp:sizes', k)) || 0);
    console.log('cache: evicting', k);
    await dropCache(k);
  }
}
async function dirSize(dir) {
  let n = 0;
  for (const f of await fsp.readdir(dir)) n += (await fsp.stat(path.join(dir, f))).size;
  return n;
}
async function reconcile() {
  for (const k of await redis.zrange('tp:lru', 0, -1)) {
    const [h, i] = k.split(':');
    if (!fs.existsSync(path.join(hlsDir(h, i), '.done'))) await dropCache(k);
  }
  for (const h of await fsp.readdir(HLS_DIR)) {
    for (const i of await fsp.readdir(path.join(HLS_DIR, h))) {
      if (!(await redis.exists(`tp:done:${h}:${i}`))) await fsp.rm(hlsDir(h, i), { recursive: true, force: true });
    }
  }
}
await reconcile();

// ---------- torrents ----------
const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
function b32ToHex(str) {
  let bits = '';
  for (const c of str.toUpperCase()) bits += B32.indexOf(c).toString(2).padStart(5, '0');
  let hex = '';
  for (let i = 0; i + 4 <= bits.length; i += 4) hex += parseInt(bits.slice(i, i + 4), 2).toString(16);
  return hex.slice(0, 40);
}
// Accepts 40-char hex or 32-char base32 info hashes (SubsPlease/Nyaa magnets use base32).
function parseHash(input) {
  const t = input.trim();
  const hex = /btih:([a-f0-9]{40})(?![a-z0-9])/i.exec(input) || /^([a-f0-9]{40})$/i.exec(t);
  if (hex) return hex[1].toLowerCase();
  const b32 = /btih:([a-z2-7]{32})(?![a-z0-9])/i.exec(input) || /^([a-z2-7]{32})$/i.exec(t);
  return b32 ? b32ToHex(b32[1]) : null;
}

async function ensureTorrent(h, magnet) {
  touched.set(h, Date.now());
  const live = await client.get(h);
  if (live?.ready) return live;
  if (pending.has(h)) return pending.get(h);

  const p = (async () => {
    let source = magnet;
    const cached = await getMeta(h);
    if (cached?.torrentB64) source = Buffer.from(cached.torrentB64, 'base64');
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        client.remove(h).catch(() => {});
        reject(new Error('Timed out waiting for torrent metadata (no peers found)'));
      }, 60_000);
      const t = client.add(source, { path: DL_DIR, deselect: true });
      t.once('error', (e) => { clearTimeout(timer); reject(e); });
      t.once('ready', async () => {
        clearTimeout(timer);
        try { await saveMeta(t); resolve(t); } catch (e) { reject(e); }
      });
    });
  })().finally(() => pending.delete(h));
  pending.set(h, p);
  return p;
}

// ---------- ffmpeg pipeline: torrent -> HLS ----------
function probe(url) {
  return new Promise((resolve, reject) => {
    const p = spawn('ffprobe', ['-v', 'error', '-show_streams', '-of', 'json', url]);
    let out = '', err = '';
    p.stdout.on('data', (d) => (out += d));
    p.stderr.on('data', (d) => (err += d));
    const to = setTimeout(() => p.kill(), 90_000);
    p.on('error', (e) => { clearTimeout(to); reject(new Error('ffprobe not found: ' + e.message)); });
    p.on('close', (c) => {
      clearTimeout(to);
      if (c !== 0) return reject(new Error('ffprobe failed: ' + err.slice(-300)));
      try { resolve(JSON.parse(out).streams || []); } catch (e) { reject(e); }
    });
  });
}

async function runJob(job, h, i, file) {
  const k = keyOf(h, i);
  const dir = hlsDir(h, i);
  try {
    const url = `http://127.0.0.1:${PORT}/raw/${h}/${i}?t=${TOKEN}`;
    const streams = await probe(url);
    const v = streams.find((s) => s.codec_type === 'video');
    if (!v) throw new Error('No video stream found in this file');
    const audios = streams.filter((s) => s.codec_type === 'audio');
    const a = audios.find((s) => s.tags?.language === 'jpn') || audios[0];
    const subs = streams.filter((s) => s.codec_type === 'subtitle' && TEXT_SUBS.includes(s.codec_name));
    const sub = subs.find((s) => /^en/.test(s.tags?.language || '')) || subs[0];
    const copyVideo = v.codec_name === 'h264' && (!v.pix_fmt || v.pix_fmt === 'yuv420p');
    job.mode = copyVideo ? 'remux' : 'transcode';

    const args = ['-hide_banner', '-loglevel', 'error', '-y', '-i', url, '-map', `0:${v.index}`];
    if (a) args.push('-map', `0:${a.index}`);
    if (copyVideo) args.push('-c:v', 'copy');
    else args.push('-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-pix_fmt', 'yuv420p',
      '-force_key_frames', 'expr:gte(t,n_forced*6)');
    if (a) args.push('-c:a', 'aac', '-b:a', '160k', '-ac', '2');
    args.push('-f', 'hls', '-hls_time', '6', '-hls_playlist_type', 'event',
      '-hls_flags', 'independent_segments',
      '-hls_segment_filename', path.join(dir, 'seg_%05d.ts'), path.join(dir, 'index.m3u8'));
    if (sub) args.push('-map', `0:${sub.index}`, '-c:s', 'webvtt', '-f', 'webvtt', path.join(dir, 'subs.vtt'));

    file.select(); // keep downloading ahead of ffmpeg
    job.state = 'running';
    const proc = spawn('ffmpeg', args, { stdio: ['ignore', 'ignore', 'pipe'] });
    job.proc = proc;
    let tail = '';
    proc.stderr.on('data', (d) => { tail = (tail + d).slice(-1500); });
    proc.on('error', (e) => { job.state = 'failed'; job.error = 'ffmpeg not found: ' + e.message; });
    proc.on('close', async (code) => {
      if (job.state === 'failed' || job.cancelled) return;
      if (code !== 0) {
        job.state = 'failed'; job.error = tail.trim() || `ffmpeg exited with ${code}`;
        await fsp.rm(dir, { recursive: true, force: true });
        return;
      }
      try {
        const subPath = path.join(dir, 'subs.vtt');
        const hasSubs = fs.existsSync(subPath) && fs.statSync(subPath).size > 20;
        await fsp.writeFile(path.join(dir, '.done'), JSON.stringify({ hasSubs, mode: job.mode }));
        const bytes = await dirSize(dir);
        await redis.multi()
          .set(`tp:done:${k}`, '1').zadd('tp:lru', Date.now(), k).hset('tp:sizes', k, bytes).exec();
        job.hasSubs = hasSubs;
        job.state = 'done';
        await evict();
      } catch (e) { job.state = 'failed'; job.error = e.message; }
    });
  } catch (e) {
    job.state = 'failed'; job.error = e.message;
    await fsp.rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

async function startJob(h, i, magnet) {
  const k = keyOf(h, i);
  const existing = jobs.get(k);
  if (existing && (existing.state === 'probing' || existing.state === 'running')) return existing;
  if (await isCached(h, i)) { await redis.zadd('tp:lru', Date.now(), k); return { state: 'done' }; }

  const t = await ensureTorrent(h, magnet); // cache miss -> join the swarm
  const file = t.files[i];
  if (!file) throw new Error('No such file in torrent');
  const dir = hlsDir(h, i);
  await fsp.rm(dir, { recursive: true, force: true });
  await fsp.mkdir(dir, { recursive: true });
  const job = { state: 'probing', error: null, hasSubs: false };
  jobs.set(k, job);
  runJob(job, h, i, file);
  return job;
}

const hasActiveJob = (h) =>
  [...jobs].some(([k, j]) => k.startsWith(h) && (j.state === 'probing' || j.state === 'running'));

// drop idle torrents (and their raw data); the HLS cache + Redis metadata stay
setInterval(async () => {
  for (const t of client.torrents) {
    if (hasActiveJob(t.infoHash)) { touched.set(t.infoHash, Date.now()); continue; }
    if (Date.now() - (touched.get(t.infoHash) || 0) > IDLE_MS) {
      console.log('releasing idle torrent', t.infoHash);
      await client.remove(t.infoHash, { destroyStore: true }).catch(() => {});
    }
  }
}, 60_000).unref();

// ---------- http ----------
const app = express();
app.use(express.json());
const wrap = (fn) => (req, res) => fn(req, res).catch((e) => res.status(500).json({ error: e.message }));
const validKey = (h, i) => HASH_RE.test(h) && /^\d+$/.test(i);

// raw torrent bytes with Range support, for ffmpeg only
app.get('/raw/:h/:i', async (req, res) => {
  if (req.query.t !== TOKEN || !validKey(req.params.h, req.params.i)) return res.sendStatus(403);
  const t = await client.get(req.params.h);
  const f = t?.files?.[Number(req.params.i)];
  if (!f) return res.sendStatus(404);
  touched.set(t.infoHash, Date.now());
  const size = f.length;
  let start = 0, end = size - 1, status = 200;
  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || '');
  if (m && (m[1] || m[2])) {
    status = 206;
    if (m[1] === '') start = Math.max(0, size - Number(m[2]));
    else { start = Number(m[1]); if (m[2] !== '') end = Math.min(end, Number(m[2])); }
    if (start > end || start >= size) { res.set('Content-Range', `bytes */${size}`); return res.sendStatus(416); }
  }
  res.status(status).set({
    'Accept-Ranges': 'bytes', 'Content-Type': 'application/octet-stream', 'Content-Length': end - start + 1,
    ...(status === 206 ? { 'Content-Range': `bytes ${start}-${end}/${size}` } : {}),
  });
  const s = f.createReadStream({ start, end });
  s.on('error', () => res.destroy());
  res.on('close', () => s.destroy());
  s.pipe(res);
});

// HLS output: bump LRU score on access (throttled), then serve statically
const lastBump = new Map();
app.use('/hls/:h/:i', (req, res, next) => {
  const { h, i } = req.params;
  if (!validKey(h, i)) return res.sendStatus(404);
  const k = keyOf(h, i), now = Date.now();
  if (now - (lastBump.get(k) || 0) > 30_000) { lastBump.set(k, now); redis.zadd('tp:lru', now, k).catch(() => {}); }
  next();
});
app.use('/hls', express.static(HLS_DIR, {
  dotfiles: 'deny',
  setHeaders(res, p) {
    if (p.endsWith('.ts')) res.set('Cache-Control', 'public, max-age=3600');
    else res.set('Cache-Control', 'no-cache');
    if (p.endsWith('.vtt')) res.set('Content-Type', 'text/vtt; charset=utf-8');
  },
}));

app.post('/api/torrent', wrap(async (req, res) => {
  const input = String(req.body.magnet || '').trim();
  const h = parseHash(input);
  if (!h) return res.status(400).json({ error: 'Paste a magnet link (or info hash) that contains a btih hash.' });
  const magnet = input.startsWith('magnet:') ? input : `magnet:?xt=urn:btih:${h}`;
  let meta = await getMeta(h);
  const fromCache = !!meta;
  if (!meta) { await ensureTorrent(h, magnet); meta = await getMeta(h); }
  else await redis.expire(`tp:meta:${h}`, META_TTL);
  await redis.zadd('tp:recent', Date.now(), h);
  const files = await Promise.all(meta.files.map(async (f) => ({
    ...f,
    cached: await isCached(h, f.index),
    resume: Number(await redis.get(`tp:pos:${h}:${f.index}`)) || 0,
  })));
  res.json({ infoHash: h, name: meta.name, magnet, fromCache, files });
}));

app.post('/api/play', wrap(async (req, res) => {
  const h = String(req.body.infoHash || '');
  const i = String(req.body.index);
  if (!validKey(h, i)) return res.status(400).json({ error: 'Bad request' });
  const job = await startJob(h, i, req.body.magnet || `magnet:?xt=urn:btih:${h}`);
  res.json({ state: job.state, playlist: `/hls/${h}/${i}/index.m3u8` });
}));

app.get('/api/status/:h/:i', wrap(async (req, res) => {
  const { h, i } = req.params;
  if (!validKey(h, i)) return res.status(400).json({ error: 'Bad request' });
  const k = keyOf(h, i);
  const job = jobs.get(k);
  const ready = fs.existsSync(path.join(hlsDir(h, i), 'index.m3u8'));
  const t = await client.get(h);
  const net = t ? { progress: t.progress, speed: t.downloadSpeed, peers: t.numPeers } : null;
  if (job) return res.json({ state: job.state, mode: job.mode, ready, hasSubs: job.hasSubs, error: job.error, net });
  if (await isCached(h, i)) {
    const info = JSON.parse(await fsp.readFile(path.join(hlsDir(h, i), '.done'), 'utf8'));
    return res.json({ state: 'done', mode: info.mode, ready: true, hasSubs: info.hasSubs, net });
  }
  res.json({ state: 'idle', ready: false, net });
}));

app.post('/api/progress', wrap(async (req, res) => {
  const { infoHash: h, index, t } = req.body || {};
  if (!validKey(String(h), String(index)) || !(t >= 0)) return res.status(400).json({ error: 'Bad request' });
  await redis.set(`tp:pos:${h}:${index}`, Math.floor(t), 'EX', POS_TTL);
  res.json({ ok: true });
}));

app.get('/api/recent', wrap(async (_req, res) => {
  const hashes = await redis.zrevrange('tp:recent', 0, 9);
  const out = [];
  for (const h of hashes) {
    const m = await getMeta(h);
    if (m) out.push({ infoHash: h, name: m.name });
    else await redis.zrem('tp:recent', h);
  }
  res.json(out);
}));

app.get('/api/cache', wrap(async (_req, res) => {
  res.json({ usedBytes: await cacheUsage(), maxBytes: MAX_CACHE_BYTES, entries: await redis.zcard('tp:lru') });
}));

app.post('/api/cache/clear', wrap(async (_req, res) => {
  for (const k of await redis.zrange('tp:lru', 0, -1)) {
    const j = jobs.get(k);
    if (!j || (j.state !== 'probing' && j.state !== 'running')) await dropCache(k);
  }
  res.json({ ok: true });
}));

app.use(express.static(path.join(__dirname, 'public')));

const server = app.listen(PORT, () => console.log(`Web torrent player on http://localhost:${PORT}`));

function shutdown() {
  for (const j of jobs.values()) { j.cancelled = true; j.proc?.kill('SIGKILL'); }
  server.close();
  client.destroy(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);