import argparse
import asyncio
import os
import re
from typing import Dict, List, Optional

import requests

PUBLIC_MASTER_MANIFEST = "https://hls.abdx.tv"


ATTR_RE = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]+))')


def parse_master_playlist(text: str, manifest_url: Optional[str] = None) -> List[Dict[str, str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    streams: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    base_url = manifest_url.rsplit("/", 1)[0] + "/" if manifest_url else ""

    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs: Dict[str, str] = {}
            for key, quoted, plain in ATTR_RE.findall(line):
                attrs[key] = quoted if quoted else plain
            current = {"attributes": attrs}
            continue

        if line.startswith("#"):
            continue

        if line and not line.startswith("#") and current:
            resolved_url = line
            if not line.startswith("http"):
                if base_url:
                    resolved_url = base_url + line.lstrip("/") if not line.startswith("/") else base_url.rstrip("/") + line
                else:
                    resolved_url = line
            current["url"] = resolved_url
            streams.append(current)
            current = {}

    return streams


def get_tmdb_movie(api_key: str, tmdb_id: int) -> Dict[str, object]:
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
    response = requests.get(url, params={"api_key": api_key, "language": "en-US"}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if "title" not in data:
        raise ValueError(f"TMDB movie {tmdb_id} not found or API key is invalid.")
    return data


def extract_manifest_candidates(text: str) -> List[str]:
    candidates: List[str] = []
    pattern = r'https?://[^\s\"\'<>]+\.m3u8(?:\?[^\s\"\'<>]*)?'
    for match in re.findall(pattern, text, flags=re.IGNORECASE):
        if match not in candidates:
            candidates.append(match)

    if not candidates:
        for match in re.findall(r'/(?:hls/)?[A-Za-z0-9_-]{30,}\.m3u8(?:\?[^\s\"\'<>]*)?', text, flags=re.IGNORECASE):
            if match not in candidates:
                candidates.append(match)

    return candidates


def discover_manifest_url(page_url: str) -> Optional[str]:
    response = requests.get(page_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    text = response.text

    candidates = extract_manifest_candidates(text)
    if candidates:
        return candidates[0]

    js_urls = re.findall(r'https?://[^\s\"\'<>]+(?:/tv|/cdn|/hls)[^\s\"\'<>]*', text, flags=re.IGNORECASE)
    for candidate in js_urls:
        if ".js" in candidate.lower() or ".css" in candidate.lower():
            try:
                js_response = requests.get(candidate, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                js_response.raise_for_status()
                js_text = js_response.text
                js_candidates = extract_manifest_candidates(js_text)
                if js_candidates:
                    return js_candidates[0]
            except Exception:
                continue

    return None


def fetch_manifest(manifest_url: str) -> str:
    response = requests.get(manifest_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    return response.text


async def test_abdx_direct(
    tmdb_api_key: Optional[str] = None,
    tmdb_id: Optional[int] = None,
    manifest_url: Optional[str] = None,
    page_url: Optional[str] = None,
) -> None:
    if tmdb_api_key and tmdb_id:
        movie = get_tmdb_movie(tmdb_api_key, tmdb_id)
        print(f"[*] TMDB movie: {movie.get('title')} (ID {tmdb_id})")

    resolved_manifest_url = manifest_url
    if not resolved_manifest_url and page_url:
        discovered = discover_manifest_url(page_url)
        if discovered:
            print(f"[*] Discovered manifest URL from page: {discovered}")
            resolved_manifest_url = discovered

    if not resolved_manifest_url:
        resolved_manifest_url = PUBLIC_MASTER_MANIFEST
        print(f"[*] No direct manifest URL provided; using public test manifest: {resolved_manifest_url}")

    playlist_text = fetch_manifest(resolved_manifest_url)
    streams = parse_master_playlist(playlist_text, manifest_url=resolved_manifest_url)

    if not streams:
        raise AssertionError("No HLS streams were parsed from the master manifest.")

    print("[*] Parsed HLS master playlist entries:")
    for index, stream in enumerate(streams, start=1):
        attrs = stream["attributes"]
        print(
            f"[{index}] BANDWIDTH={attrs.get('BANDWIDTH')} "
            f"RESOLUTION={attrs.get('RESOLUTION')} "
            f"URL={stream['url']}"
        )

    assert len(streams) >= 1
    assert all(".m3u8" in stream["url"] for stream in streams)
    print("[OK] HLS master manifest parsing passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parses ABDX HLS master playlists and optionally looks up a TMDB movie by ID.")
    parser.add_argument("--tmdb-id", type=int, help="TMDB movie ID to look up using the provided API key.")
    parser.add_argument("--tmdb-api-key", default=os.getenv("TMDB_API_KEY"), help="TMDB API key. Defaults to the TMDB_API_KEY environment variable.")
    parser.add_argument("--manifest-url", help="URL to a real .m3u8 master playlist to fetch and parse.")
    parser.add_argument("--page-url", help="Optional page URL used to discover a manifest from the page HTML or client script bundle.")
    args = parser.parse_args()

    if args.tmdb_api_key and not args.tmdb_id:
        parser.error("--tmdb-id is required when a TMDB API key is provided.")

    if args.tmdb_id and not args.tmdb_api_key:
        parser.error("A TMDB API key is required when using --tmdb-id. Set TMDB_API_KEY or pass --tmdb-api-key.")

    asyncio.run(test_abdx_direct(args.tmdb_api_key, args.tmdb_id, args.manifest_url, args.page_url))