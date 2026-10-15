from main import read_fallback_hls_streams, is_valid_hls_manifest_text


def test_is_valid_hls_manifest_text_rejects_proxy_status_page():
    assert not is_valid_hls_manifest_text("Stream Proxy Active (Open Access)\n")
    assert not is_valid_hls_manifest_text("<!doctype html><html>bad</html>")
    assert is_valid_hls_manifest_text("#EXTM3U\n#EXTINF:10,Example\nhttps://cdn.example.com/segment.ts")


def test_read_fallback_hls_streams_uses_known_live_manifests(monkeypatch, tmp_path):
    manifest_path = tmp_path / "hls_streams.txt"
    manifest_path.write_text(
        "https://example.com/valid.m3u8\n"
        "https://example.com/not-hls.txt\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("main.FALLBACK_HLS_PATH", str(manifest_path))

    class FakeResponse:
        def __init__(self, body: str, status_code: int = 200):
            self.text = body
            self.status_code = status_code

    def fake_get(url, headers=None, timeout=None):
        if "valid" in url:
            return FakeResponse("#EXTM3U\n#EXTINF:10,Example\nhttps://cdn.example.com/segment.ts\n")
        return FakeResponse("Stream Proxy Active (Open Access)\n")

    monkeypatch.setattr("main.httpx.get", fake_get)

    streams = read_fallback_hls_streams()

    assert len(streams) == 1
    assert streams[0].direct_m3u8 == "https://example.com/valid.m3u8"
    assert streams[0].proxy_stream.startswith("http://localhost:8080/m3u8-proxy.m3u8?")
