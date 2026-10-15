# Dropping the device cache into Chanime 2.1 (Flask/Jinja2)

The service worker caches HLS segments transparently, so **the player code does not change**.
It matches any same-origin URL ending in `/hls/<40-hex>/<n>/<file>`.

## 1. Copy files into your static folder
`sw.js`, `kv-cache.js`, `manifest.webmanifest`, `icons/*` (swap in the real Chanime icons: 192, 512, 512 maskable, 180 for Apple).

## 2. Serve the service worker and manifest from the site root
```python
from flask import send_from_directory

@app.route('/sw.js')
def sw():
    r = send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')
    r.headers['Cache-Control'] = 'no-cache'
    r.headers['Service-Worker-Allowed'] = '/'
    return r

@app.route('/manifest.webmanifest')
def manifest():
    return send_from_directory(app.static_folder, 'manifest.webmanifest', mimetype='application/manifest+json')
```
If your icons are under `/static/icons/`, update the `src` paths in the manifest.

## 3. In your base Jinja template `<head>`
```html
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#050505">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="apple-touch-icon" href="{{ url_for('static', filename='icons/apple-touch-icon.png') }}">
<script src="{{ url_for('static', filename='kv-cache.js') }}" defer></script>
```
and before `</body>`:
```html
<button data-kv-install hidden>Add to Home Screen</button>
<script>addEventListener('load', () => KVCache.init());</script>
```

## 4. How it behaves
| Situation | Budget | Notes |
|---|---|---|
| Normal browser tab | 150 MiB | browser may evict it |
| Installed to home screen + persistent storage granted | 2 GiB | `navigator.storage.persist()` is only requested after install |
| Never more than | 50% of the browser's quota | |

Eviction is per episode, least recently watched first. An episode that doesn't fit isn't cached (it just streams).
Offline replay works for episodes that were fully processed on the server and fully watched on the device.
To cache from a CDN origin later, add it to `EXTRA_ORIGINS` in `sw.js` (the CDN must send CORS headers).
