import asyncio
from playwright.async_api import async_playwright

TARGET_URL = "https://1embed.cc/embed/movie/1083381" 

m3u8_links = set()

def handle_request(request):
    url = request.url
    if ".m3u8" in url or "m3u8" in url:
        if url not in m3u8_links:
            print(f"\n[+] Found HLS Stream: {url}")
            m3u8_links.add(url)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={
                "Referer": "https://1embed.cc/"
            }
        )
        
        page = await context.new_page()
        page.on("request", handle_request)

        print(f"[*] Navigating to {TARGET_URL}...")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
            
            play_button = page.locator("button, .play-btn, #player").first
            if await play_button.is_visible():
                await play_button.click(force=True)
                await page.wait_for_timeout(5000)

        except Exception as e:
            print(f"[-] Navigation issue: {e}")

        await browser.close()

    if m3u8_links:
        with open("hls_streams.txt", "w") as f:
            for link in m3u8_links:
                f.write(f"{link}\n")
        print(f"\nSaved {len(m3u8_links)} HLS stream(s) to hls_streams.txt")
    else:
        print("\n[-] No .m3u8 links intercepted.")

if __name__ == "__main__":
    asyncio.run(main())