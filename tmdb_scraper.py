import asyncio
import csv
import json
import requests
from playwright.async_api import async_playwright

# Configuration
TMDB_API_KEY = "faaa2adb2d69574b980d485c09b18298"  # Replace with your TMDB v3 Key
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}

# 1. Fetch metadata & IMDb ID from TMDB
def get_tmdb_metadata(tmdb_id, media_type="movie"):
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=external_ids"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200:
        raise ValueError(f"Failed to fetch metadata from TMDB for ID {tmdb_id}")
    
    data = res.json()
    title = data.get("title") or data.get("name")
    imdb_id = data.get("external_ids", {}).get("imdb_id")
    
    return {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": title,
        "type": media_type
    }

# 2. Build provider fallback list
def build_provider_urls(meta, season=1, episode=1):
    tmdb_id = meta["tmdb_id"]
    imdb_id = meta["imdb_id"]
    is_movie = meta["type"] == "movie"

    providers = []
    
    # 1Embed
    providers.append({
        "name": "1Embed",
        "url": f"https://1embed.cc/embed/movie/{tmdb_id}" if is_movie else f"https://1embed.cc/embed/tv/{tmdb_id}/{season}/{episode}"
    })
    
    # VidSrc.to
    providers.append({
        "name": "VidSrc.to",
        "url": f"https://vidsrc.to/embed/movie/{tmdb_id}" if is_movie else f"https://vidsrc.to/embed/tv/{tmdb_id}/{season}/{episode}"
    })

    # AutoEmbed
    providers.append({
        "name": "AutoEmbed",
        "url": f"https://player.autoembed.cc/embed/movie/{tmdb_id}" if is_movie else f"https://player.autoembed.cc/embed/tv/{tmdb_id}/{season}/{episode}"
    })

    # 2Embed
    providers.append({
        "name": "2Embed",
        "url": f"https://www.2embed.cc/embed/{tmdb_id}" if is_movie else f"https://www.2embed.cc/embedtv/{tmdb_id}&s={season}&e={episode}"
    })

    # VidSrc.me (Uses IMDb ID if available, fallback to TMDB)
    if imdb_id:
        providers.append({
            "name": "VidSrc.me (IMDb)",
            "url": f"https://vidsrc.me/embed/movie?imdb={imdb_id}" if is_movie else f"https://vidsrc.me/embed/tv?imdb={imdb_id}&s={season}&e={episode}"
        })

    return providers

# 3. Intercept HLS URL for a provider URL
async def test_provider(provider, context):
    print(f"[*] Testing provider: {provider['name']} -> {provider['url']}")
    m3u8_found = set()

    def handle_request(request):
        url = request.url
        if ".m3u8" in url or "m3u8" in url:
            m3u8_found.add(url)

    page = await context.new_page()
    page.on("request", handle_request)

    try:
        await page.goto(provider["url"], wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(4000)

        # Trigger interaction on player overlay if present
        play_btn = page.locator("button, .play-btn, #player, .play").first
        if await play_btn.is_visible():
            await play_btn.click(force=True)
            await page.wait_for_timeout(3000)

    except Exception as e:
        print(f"[-] {provider['name']} timed out or failed: {e}")
    finally:
        await page.close()

    return list(m3u8_found)

# 4. Main fallback loop
async def resolve_hls_stream(tmdb_id, media_type="movie", season=1, episode=1):
    meta = get_tmdb_metadata(tmdb_id, media_type)
    print(f"\n[+] Processing: {meta['title']} ({meta['type'].upper()})")

    providers = build_provider_urls(meta, season, episode)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={"Referer": "https://google.com/"}
        )

        for provider in providers:
            streams = await test_provider(provider, context)
            if streams:
                print(f"\n[SUCCESS] Found {len(streams)} HLS link(s) via {provider['name']}!")
                await browser.close()
                return {
                    "title": meta["title"],
                    "tmdb_id": tmdb_id,
                    "provider": provider["name"],
                    "streams": streams
                }

        await browser.close()

    print("\n[-] All providers failed to return an HLS stream.")
    return None

if __name__ == "__main__":
    # Example: Inception (TMDB ID: 27205)
    result = asyncio.run(resolve_hls_stream(tmdb_id=27205, media_type="movie"))
    
    if result:
        print("\n--- Final Output ---")
        print(json.dumps(result, indent=2))