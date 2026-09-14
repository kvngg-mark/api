# HLS Resolver API - Integration & Prompt Guide for AI Agents

> **Note for AI Developers & Coding Assistants**: This document specifies the contract, request parameters, response schemas, and client implementation instructions for the HLS Resolver API. You do not need access to the source code to integrate this API into any web, mobile, or backend application.

---

## 1. Overview & Base Configuration

The **HLS Resolver API** resolves high-availability `.m3u8` HTTP Live Streaming (HLS) video sources for movies and TV shows using TMDB (The Movie Database) metadata. It executes dynamic interceptors across multiple provider fallback chains and outputs immediately streamable proxy URLs along with required HTTP playback headers.

* **Default Base URL**: `http://localhost:8000` (or your deployed API domain)
* **Protocol**: HTTP/HTTPS
* **Format**: JSON (`Content-Type: application/json`)

---

## 2. API Reference Specification

### Endpoint: `GET /api/v1/resolve`

Resolves playable video stream URLs for a given TMDB media entry.

#### Query Parameters

| Parameter | Type | Required | Default | Range / Allowed Values | Description |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `tmdb_id` | `integer` | **Yes** | - | `> 0` | The TMDB numeric ID (e.g., `27205` for *Inception*). |
| `type` | `string` | No | `movie` | `"movie" \| "tv"` | Media format. |
| `season` | `integer` | No | `1` | `>= 1` | Season number (evaluated when `type="tv"`). |
| `episode` | `integer` | No | `1` | `>= 1` | Episode number (evaluated when `type="tv"`). |

---

## 3. Response Contracts & Examples

### HTTP 200 - Success Response
Returned when a stream is successfully intercepted.

```json
{
  "status": "success",
  "title": "Inception",
  "tmdb_id": 27205,
  "media_type": "movie",
  "provider": "1Embed",
  "streams": [
    {
      "proxy_stream": "https://mp4-proxy-vps.decals-dda.workers.dev/m3u8-proxy.m3u8?url=https%3A%2F%2Fonlinevisibilitysystem.site%2FqHjc679HE%2Fpl%2F...%2Fmaster.m3u8",
      "direct_m3u8": "https://onlinevisibilitysystem.site/qHjc679HE/pl/.../master.m3u8",
      "type": "master",
      "headers": {
        "Referer": "https://nextgencloudfabric.com/",
        "Origin": "https://nextgencloudfabric.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
      }
    },
    {
      "proxy_stream": "https://mp4-proxy-vps.decals-dda.workers.dev/m3u8-proxy.m3u8?url=https%3A%2F%2Fonlinevisibilitysystem.site%2FqHjc679HE%2Fpl%2F...%2Findex.m3u8",
      "direct_m3u8": "https://onlinevisibilitysystem.site/qHjc679HE/pl/.../index.m3u8",
      "type": "index",
      "headers": {
        "Referer": "https://nextgencloudfabric.com/",
        "Origin": "https://nextgencloudfabric.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
      }
    }
  ]
}
```

#### Field Definitions
* `streams[].proxy_stream`: **PRIMARY LINK**. Use this link directly in HTML5 video players (`video.js`, `hls.js`, Expo AV, ExoPlayer). The Cloudflare proxy automatically injects required headers and handles CORS.
* `streams[].direct_m3u8`: The raw unproxied HLS stream URL.
* `streams[].type`: Stream quality specification (`master` = adaptive multi-bitrate manifest; `index` = direct resolution stream).
* `streams[].headers`: HTTP headers required if playing `direct_m3u8` directly without using `proxy_stream`.

### HTTP 404 - Stream Not Found
Returned when no active stream can be retrieved.

```json
{
  "status": "error",
  "message": "No active HLS streams found across available embed providers."
}
```

---

## 4. How to Consume This API in Application Code

### Playback Priority Rule
When implementing video players:
1. Always prefer the stream object with `"type": "master"` as it provides adaptive quality switching (1080p, 720p, 480p).
2. Pass `proxy_stream` directly to the player source without modifying headers.

---

### Implementation Snippets

#### React / Next.js + `hls.js` Implementation
```tsx
import React, { useEffect, useRef } from 'react';
import Hls from 'hls.js';

interface VideoPlayerProps {
  tmdbId: number;
  type?: 'movie' | 'tv';
  season?: number;
  episode?: number;
}

export const HlsVideoPlayer: React.FC<VideoPlayerProps> = ({ tmdbId, type = 'movie', season = 1, episode = 1 }) => {
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    const fetchAndPlayStream = async () => {
      try {
        const query = new URLSearchParams({
          tmdb_id: tmdbId.toString(),
          type,
          season: season.toString(),
          episode: episode.toString(),
        });

        const res = await fetch(`http://localhost:8000/api/v1/resolve?${query}`);
        const data = await res.json();

        if (data.status === 'success' && data.streams.length > 0) {
          // Select master playlist or fallback to the first stream
          const masterStream = data.streams.find((s: any) => s.type === 'master') || data.streams[0];
          const streamUrl = masterStream.proxy_stream;

          if (videoRef.current) {
            if (Hls.isSupported()) {
              const hls = new Hls();
              hls.loadSource(streamUrl);
              hls.attachMedia(videoRef.current);
            } else if (videoRef.current.canPlayType('application/vnd.apple.mpegurl')) {
              // Safari native HLS support
              videoRef.current.src = streamUrl;
            }
          }
        }
      } catch (err) {
        console.error('Failed to resolve or play stream:', err);
      }
    };

    fetchAndPlayStream();
  }, [tmdbId, type, season, episode]);

  return <video ref={videoRef} controls style={{ width: '100%', height: 'auto' }} />;
};
```

#### Node.js / Express Proxy Route
```javascript
const express = require('express');
const axios = require('axios');
const app = express();

app.get('/watch/:tmdbId', async (req, res) => {
  try {
    const { tmdbId } = req.params;
    const apiRes = await axios.get('http://localhost:8000/api/v1/resolve', {
      params: { tmdb_id: tmdbId, type: 'movie' }
    });

    const streams = apiRes.data.streams;
    const selectedStream = streams.find(s => s.type === 'master') || streams[0];

    res.json({
      title: apiRes.data.title,
      playUrl: selectedStream.proxy_stream
    });
  } catch (error) {
    res.status(404).json({ error: 'Stream unavailable' });
  }
});
```

#### Python (`requests` + `yt-dlp` CLI command generator)
```python
import requests

def get_download_command(tmdb_id: int) -> str:
    res = requests.get("http://localhost:8000/api/v1/resolve", params={"tmdb_id": tmdb_id, "type": "movie"})
    data = res.json()
    
    if data.get("status") == "success":
        stream = next((s for s in data["streams"] if s["type"] == "master"), data["streams"][0])
        proxy_url = stream["proxy_stream"]
        return f'yt-dlp "{proxy_url}" -o "{data["title"]}.mp4"'
    
    raise ValueError("Stream resolution failed")
```

---

## 5. Direct Prompt for AI Coding Assistants

Copy and paste the following snippet to any AI assistant (ChatGPT, Claude, Cursor, Copilot) to generate code without providing source code:

```text
I am connecting to an external HLS Stream Resolver API endpoint with the following specification:

Endpoint: GET http://localhost:8000/api/v1/resolve
Query Params: tmdb_id (int), type ("movie"|"tv"), season (int, optional), episode (int, optional)

Response Schema:
{
  "status": "success",
  "title": "string",
  "tmdb_id": number,
  "media_type": "movie" | "tv",
  "provider": "string",
  "streams": [
    {
      "proxy_stream": "string (URL to directly assign to player)",
      "direct_m3u8": "string",
      "type": "master" | "index",
      "headers": { "Referer": "string", "Origin": "string", "User-Agent": "string" }
    }
  ]
}

Task: Please write a video player component that accepts a `tmdbId` prop, fetches the stream from this endpoint, selects the stream where `type === 'master'`, and loads `proxy_stream` into the player.
```