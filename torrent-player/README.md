# Web Torrent Player

Paste a magnet link, pick a file, watch it in the browser.

Torrent (TCP/UDP/DHT) -> ffmpeg (MKV -> HLS, H.264 is remuxed, anything else is transcoded) -> hls.js.
Redis is the smart cache layer.

## Run in GitHub Codespaces
1. Create a repo with these files (or upload the zip into a Codespace and `unzip` it).
2. If the Codespace already existed: `bash setup.sh` (installs ffmpeg, redis, npm deps).
   New Codespaces run it automatically via `.devcontainer`.
3. `npm start`, then open the forwarded port 3000.
4. Click **Try Sintel** to verify everything works with an open-licensed film.

## What Redis caches
| Key | Purpose |
|---|---|
| `tp:meta:<hash>` | File list + the .torrent itself (14d). Re-opening a torrent is instant and skips the metadata lookup. |
| `tp:done:<hash>:<idx>` | Marks a fully transcoded HLS copy on disk. Cached files play with zero torrent activity. |
| `tp:lru` / `tp:sizes` | Last-access time and bytes per cached file. Oldest entries are evicted when over `MAX_CACHE_GB`. |
| `tp:pos:<hash>:<idx>` | Resume position (90d). |
| `tp:recent` | Recently opened torrents. |

Idle torrents are released after `TORRENT_IDLE_MIN` and their raw download is deleted (the HLS copy stays cached).

## Env vars
`PORT` (3000) · `REDIS_URL` · `MAX_CACHE_GB` (8) · `TORRENT_IDLE_MIN` (5) · `UPLOAD_KBPS` (256) · `DATA_DIR` (./data)

## Device cache (PWA)
`public/sw.js` + `public/kv-cache.js` cache HLS segments in the browser (Cache API + IndexedDB bookkeeping).
Small budget in a tab, large + persistent once the site is added to the home screen. See `CHANIME.md` for the Flask drop-in.
Needs HTTPS (Codespaces forwarded ports are HTTPS; plain `localhost` also works).
