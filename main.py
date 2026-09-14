import os
import asyncio
import urllib.parse
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx
from playwright.async_api import async_playwright
import uvicorn

app = FastAPI(
    title="HLS Resolver API",
    description="Multi-provider parallel HLS stream resolver with isolated Playwright contexts.",
    version="1.5.0"
)

# Enable CORS to allow direct requests from HTML/JS frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "faaa2adb2d69574b980d485c09b18298")
GO_CODESPACE_URL = "https://shiny-palm-tree-p7r7xq4rrx6rf6gqq-8080.app.github.dev/m3u8-proxy.m3u8"
LOCAL_GO_PROXY_BASE = os.getenv("GO_PROXY_URL", GO_CODESPACE_URL)
ANIME_IDS_URL = "https://raw.githubusercontent.com/Fribb/anime-ids/master/anime-ids.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}

_anime_id_cache = None

class StreamItem(BaseModel):
    proxy_stream: str
    direct_m3u8: Optional[str]
    type: str
    headers: Dict[str, Optional[str]]

class ProviderResult(BaseModel):
    provider: str
    streams: List[StreamItem]

class MultiStreamResponse(BaseModel):
    status: str = "success"
    title: str
    tmdb_id: int
    media_type: str
    providers_found: List[ProviderResult]

class ErrorResponse(BaseModel):
    status: str = "error"
    message: str

async def get_anime_id_map() -> List[Dict[str, Any]]:
    global _anime_id_cache
    if _anime_id_cache is None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(ANIME_IDS_URL)
                if res.status_code == 200:
                    _anime_id_cache = res.json()
        except Exception:
            _anime_id_cache = []
    return _anime_id_cache or []

async def map_anime_to_tmdb(source: str, source_id: int) -> Optional[Dict[str, Any]]:
    mappings = await get_anime_id_map()
    search_key = "mal_id" if source in ["mal", "jikan"] else "anilist_id"

    for entry in mappings:
        if entry.get(search_key) == source_id:
            tmdb_id = entry.get("themoviedb_id")
            if tmdb_id:
                return {
                    "tmdb_id": int(tmdb_id),
                    "type": "movie" if entry.get("type") == "MOVIE" else "tv"
                }

    if source == "anilist":
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"https://api.ani.zip/mappings?anilist_id={source_id}")
                if res.status_code == 200:
                    mappings_data = res.json().get("mappings", {})
                    tmdb_id = mappings_data.get("themoviedb_id") or mappings_data.get("tmdb_id")
                    if tmdb_id:
                        return {"tmdb_id": int(tmdb_id), "type": "tv"}
        except Exception:
            pass
    return None

async def get_tmdb_metadata(tmdb_id: int, media_type: str = "movie") -> Dict[str, Any]:
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=external_ids"
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=10.0) as client:
            res = await client.get(url)
            if res.status_code != 200:
                raise HTTPException(status_code=404, detail=f"TMDB media not found for ID {tmdb_id}")
            data = res.json()
            return {
                "tmdb_id": tmdb_id,
                "imdb_id": data.get("external_ids", {}).get("imdb_id"),
                "title": data.get("title") or data.get("name"),
                "type": media_type
            }
    except httpx.RequestError as e:
        raise HTTPException(status_code=500, detail=str(e))

def build_provider_urls(meta: Dict[str, Any], season: int = 1, episode: int = 1) -> List[Dict[str, str]]:
    tmdb_id = meta["tmdb_id"]
    imdb_id = meta["imdb_id"]
    is_movie = meta["type"] == "movie"

    providers = [
        {
            "name": "MegaPlay",
            "url": f"https://megaplay.buzz/embed/movie/{tmdb_id}" if is_movie else f"https://megaplay.buzz/embed/tv/{tmdb_id}/{season}/{episode}"
        },
        {
            "name": "1Embed",
            "url": f"https://1embed.cc/embed/movie/{tmdb_id}" if is_movie else f"https://1embed.cc/embed/tv/{tmdb_id}/{season}/{episode}"
        },
        {
            "name": "VidSrc.to",
            "url": f"https://vidsrc.to/embed/movie/{tmdb_id}" if is_movie else f"https://vidsrc.to/embed/tv/{tmdb_id}/{season}/{episode}"
        },
        {
            "name": "AutoEmbed",
            "url": f"https://player.autoembed.cc/embed/movie/{tmdb_id}" if is_movie else f"https://player.autoembed.cc/embed/tv/{tmdb_id}/{season}/{episode}"
        },
        {
            "name": "2Embed",
            "url": f"https://www.2embed.cc/embed/{tmdb_id}" if is_movie else f"https://www.2embed.cc/embedtv/{tmdb_id}&s={season}&e={episode}"
        }
    ]

    if imdb_id:
        providers.append({
            "name": "VidSrc.me",
            "url": f"https://vidsrc.me/embed/movie?imdb={imdb_id}" if is_movie else f"https://vidsrc.me/embed/tv?imdb={imdb_id}&s={season}&e={episode}"
        })

    return providers

def parse_stream_url(url: str, default_referer: str) -> StreamItem:
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    
    direct_url = params.get("url", [url])[0]
    referer = params.get("referer", [None])[0] or default_referer
    origin = params.get("origin", [None])[0] or (urllib.parse.urlparse(default_referer).scheme + "://" + urllib.parse.urlparse(default_referer).netloc)
    
    encoded_direct = urllib.parse.quote(direct_url, safe="")
    encoded_referer = urllib.parse.quote(referer, safe="")
    encoded_origin = urllib.parse.quote(origin, safe="")
    
    go_proxy_url = f"{LOCAL_GO_PROXY_BASE}?url={encoded_direct}&referer={encoded_referer}&origin={encoded_origin}"
    stream_type = "master" if "master.m3u8" in direct_url else ("index" if "index.m3u8" in direct_url else "variant")

    return StreamItem(
        proxy_stream=go_proxy_url,
        direct_m3u8=direct_url,
        type=stream_type,
        headers={
            "Referer": referer,
            "Origin": origin,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/127.0.0.0 Safari/537.36"
        }
    )

async def test_provider(provider: Dict[str, str], browser) -> Optional[ProviderResult]:
    captured_urls = set()

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 720},
        extra_http_headers={"Referer": "https://google.com/"}
    )

    try:
        page = await context.new_page()

        def handle_request(request):
            u = request.url
            if ".m3u8" in u or "m3u8" in u:
                captured_urls.add(u)

        page.on("request", handle_request)

        try:
            await page.goto(provider["url"], wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(3000)

            # Force click triggers across frames to start stream loading
            for frame in page.frames:
                try:
                    targets = [
                        "button", ".play-btn", "#player", ".play", "#play-button",
                        ".jwplayer", ".vidsrc-player", "div[id*='player']", "iframe",
                        ".megaplay-player", "#jwplayer"
                    ]
                    for target in targets:
                        elem = frame.locator(target).first
                        if await elem.is_visible():
                            await elem.click(force=True, timeout=1000)
                            await page.wait_for_timeout(1000)
                except Exception:
                    pass

            await page.wait_for_timeout(4000)
        except Exception:
            pass

    finally:
        await context.close()

    if captured_urls:
        parsed_streams = [parse_stream_url(u, provider["url"]) for u in captured_urls]
        return ProviderResult(
            provider=provider["name"],
            streams=parsed_streams
        )
    return None

@app.get("/api/v1/resolve", response_model=MultiStreamResponse)
async def resolve_stream(
    tmdb_id: int = Query(...),
    type: str = Query("movie", pattern="^(movie|tv)$"),
    season: Optional[int] = Query(1, ge=1),
    episode: Optional[int] = Query(1, ge=1)
):
    meta = await get_tmdb_metadata(tmdb_id, type)
    providers = build_provider_urls(meta, season, episode)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        tasks = [test_provider(prov, browser) for prov in providers]
        results = await asyncio.gather(*tasks)

        await browser.close()

    found_providers = [r for r in results if r is not None]

    if not found_providers:
        raise HTTPException(status_code=404, detail="No active HLS streams found across any providers.")

    return MultiStreamResponse(
        status="success",
        title=meta["title"],
        tmdb_id=tmdb_id,
        media_type=type,
        providers_found=found_providers
    )

@app.get("/api/v1/resolve/anime", response_model=MultiStreamResponse)
async def resolve_anime_stream(
    id: int = Query(...),
    source: str = Query("anilist", pattern="^(anilist|mal|jikan)$"),
    season: Optional[int] = Query(1, ge=1),
    episode: Optional[int] = Query(1, ge=1)
):
    mapped = await map_anime_to_tmdb(source, id)
    if not mapped or not mapped.get("tmdb_id"):
        raise HTTPException(status_code=404, detail=f"No TMDB mapping found for {source.upper()} ID {id}")

    return await resolve_stream(
        tmdb_id=mapped["tmdb_id"],
        type=mapped["type"],
        season=season,
        episode=episode
    )

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)