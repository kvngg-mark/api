import argparse
import os
import re
from typing import Dict, List, Optional

import requests

PUBLIC_MASTER_MANIFEST = "https://hls.abdx.tv"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
ATTR_RE = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]+))')
DEFAULT_TMDB_API_KEY = os.getenv("TMDB_API_KEY", "faaa2adb2d69574b980d485c09b18298")


def fetch_text(url: str, timeout: int = 8) -> str:
    print(f"[fetch] {url} ...")
    response = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return response.text


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

        if line and current:
            resolved_url = line
            if not line.startswith("http"):
                if base_url:
                    if line.startswith("/"):
                        resolved_url = base_url.rstrip("/") + line
                    else:
                        resolved_url = base_url + line
                else:
                    resolved_url = line
            current["url"] = resolved_url
            streams.append(current)
            current = {}

    return streams


def extract_manifest_candidates(text: str) -> List[str]:
    candidates: List[str] = []

    pattern = r'https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?'
    for match in re.findall(pattern, text, flags=re.IGNORECASE):
        if match not in candidates:
            candidates.append(match)

    if not candidates:
        pattern = r'/(?:hls/)?[A-Za-z0-9_-]{30,}\.m3u8(?:\?[^\s"\'<>]*)?'
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            if match not in candidates:
                candidates.append(match)

    return candidates


def discover_manifest_url(page_url: str) -> Optional[str]:
    try:
        page_text = fetch_text(page_url)
    except Exception:
        return None

    candidates = extract_manifest_candidates(page_text)
    if candidates:
        return candidates[0]

    js_pattern = r'https?://[^\s"\'<>]+(?:/tv|/cdn|/hls)[^\s"\'<>]*'
    for candidate in re.findall(js_pattern, page_text, flags=re.IGNORECASE):
        if ".js" in candidate.lower() or ".css" in candidate.lower():
            try:
                js_text = fetch_text(candidate)
                js_candidates = extract_manifest_candidates(js_text)
                if js_candidates:
                    return js_candidates[0]
            except Exception:
                continue

    return None


def get_tmdb_movie(api_key: str, tmdb_id: int) -> Dict[str, object]:
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
    response = requests.get(url, params={"api_key": api_key, "language": "en-US"}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if "title" not in data:
        raise ValueError(f"TMDB movie {tmdb_id} not found or API key is invalid.")
    return data


def normalize_title(title: str) -> str:
    title = title.lower().strip()
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return " ".join(title.split())


def find_abdx_search_candidates(title: str) -> List[str]:
    queries = [title, title.replace(" ", "+")]
    candidates: List[str] = []
    seen = set()

    for query in queries:
        for endpoint in [
            f"https://abdx.tv/search?q={query}",
            f"https://abdx.tv/search?query={query}",
        ]:
            if endpoint in seen:
                continue
            seen.add(endpoint)
            try:
                html = fetch_text(endpoint)
            except Exception:
                continue

            for href in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE):
                normalized = href.strip()
                if not normalized or normalized.startswith("http") and "abdx.tv" not in normalized:
                    continue
                lowered = normalized.lower()
                if any(token in lowered for token in ["/movie", "/tv", "/watch", "/search"]) and "?" not in normalized:
                    if normalized.startswith("/"):
                        normalized = "https://abdx.tv" + normalized
                    if normalized not in candidates:
                        candidates.append(normalized)

    return candidates


def resolve_tmdb_abdx_page(tmdb_id: int, api_key: str = DEFAULT_TMDB_API_KEY) -> Optional[str]:
    movie = get_tmdb_movie(api_key, tmdb_id)
    title = str(movie.get("title") or movie.get("name") or "")
    if not title:
        return None

    normalized_title = normalize_title(title)
    for candidate in find_abdx_search_candidates(title):
        page_text = fetch_text(candidate)
        if normalized_title in normalize_title(page_text):
            return candidate

    return None


def resolve_abdx_streams(manifest_url: Optional[str] = None, page_url: Optional[str] = None, tmdb_id: Optional[int] = None, tmdb_api_key: str = DEFAULT_TMDB_API_KEY) -> List[Dict[str, str]]:
    if manifest_url:
        resolved_manifest_url = manifest_url
    elif page_url:
        resolved_manifest_url = discover_manifest_url(page_url) or page_url
    elif tmdb_id:
        resolved_manifest_url = resolve_tmdb_abdx_page(tmdb_id, tmdb_api_key)
        if not resolved_manifest_url:
            raise ValueError(f"No ABDX page found for TMDB movie {tmdb_id}.")
    else:
        resolved_manifest_url = PUBLIC_MASTER_MANIFEST

    if resolved_manifest_url and resolved_manifest_url.endswith(".m3u8"):
        playlist_text = fetch_text(resolved_manifest_url)
        streams = parse_master_playlist(playlist_text, manifest_url=resolved_manifest_url)
    else:
        discovered = discover_manifest_url(resolved_manifest_url) if resolved_manifest_url else None
        playlist_url = discovered or (resolved_manifest_url if isinstance(resolved_manifest_url, str) and resolved_manifest_url.endswith(".m3u8") else None) or PUBLIC_MASTER_MANIFEST
        playlist_text = fetch_text(playlist_url)
        streams = parse_master_playlist(playlist_text, manifest_url=playlist_url)

    if not streams:
        raise ValueError(f"No HLS streams found in ABDX manifest: {resolved_manifest_url}")

    return streams


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve ABDX HLS streams from the public master manifest, a page URL, or a TMDB movie ID.")
    parser.add_argument("--manifest-url", help="Direct ABDX .m3u8 playlist URL to resolve.")
    parser.add_argument("--page-url", help="Page URL used to discover the ABDX manifest when no manifest URL is provided.")
    parser.add_argument("--tmdb-id", type=int, help="TMDB movie ID to look up and resolve against ABDX.")
    parser.add_argument("--tmdb-api-key", default=DEFAULT_TMDB_API_KEY, help="TMDB API key. Defaults to the TMDB_API_KEY environment variable.")
    args = parser.parse_args()

    try:
        streams = resolve_abdx_streams(args.manifest_url, args.page_url, args.tmdb_id, args.tmdb_api_key)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)

    print(f"Resolved {len(streams)} stream(s):")
    for index, stream in enumerate(streams, start=1):
        attrs = stream.get("attributes", {})
        print(f"[{index}] BANDWIDTH={attrs.get('BANDWIDTH')} RESOLUTION={attrs.get('RESOLUTION')} URL={stream['url']}")


if __name__ == "__main__":
    main()
