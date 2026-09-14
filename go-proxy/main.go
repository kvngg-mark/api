package main

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"
)

// URI matching regex for tags like #EXT-X-KEY:METHOD=AES-128,URI="enc.key" or #EXT-X-MAP:URI="init.mp4"
var uriTagRegex = regexp.MustCompile(`(URI=["'])([^"']+)(["'])`)

var sharedTransport = &http.Transport{
	Proxy: http.ProxyFromEnvironment,
	DialContext: (&net.Dialer{
		Timeout:   10 * time.Second,
		KeepAlive: 30 * time.Second,
	}).DialContext,
	ForceAttemptHTTP2:     true,
	MaxIdleConns:          200,
	MaxIdleConnsPerHost:   50,
	IdleConnTimeout:       90 * time.Second,
	TLSHandshakeTimeout:   10 * time.Second,
	ExpectContinueTimeout: 1 * time.Second,
}

var httpClient = &http.Client{
	Transport: sharedTransport,
	Timeout:   45 * time.Second,
}

// corsMiddleware handles preflight and sets universal CORS headers
func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "*")
		w.Header().Set("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")

		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// isPrivateIP checks if an IP belongs to private, loopback, or cloud-metadata ranges (SSRF defense)
func isPrivateIP(ip net.IP) bool {
	if ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsPrivate() {
		return true
	}
	// Cloud metadata address: 169.254.169.254
	if ip.Equal(net.ParseIP("169.254.169.254")) {
		return true
	}
	return false
}

// validateTargetURL validates protocol and blocks internal network targets
func validateTargetURL(rawURL string) (*url.URL, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("malformed URL: %w", err)
	}

	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return nil, fmt.Errorf("unsupported scheme '%s'", parsed.Scheme)
	}

	hostname := parsed.Hostname()
	if hostname == "" {
		return nil, fmt.Errorf("empty hostname")
	}

	// Resolve IP to check for private / internal addresses
	ips, err := net.LookupIP(hostname)
	if err != nil {
		return nil, fmt.Errorf("failed to resolve host '%s': %w", hostname, err)
	}

	for _, ip := range ips {
		if isPrivateIP(ip) {
			return nil, fmt.Errorf("access to internal/private IP address is forbidden")
		}
	}

	return parsed, nil
}

// buildProxiedURL formats an absolute URL back into a proxy query URL
func buildProxiedURL(proxyPath string, target string, referer string, origin string) string {
	q := url.Values{}
	q.Set("url", target)
	if referer != "" {
		q.Set("referer", referer)
	}
	if origin != "" {
		q.Set("origin", origin)
	}
	return fmt.Sprintf("%s?%s", proxyPath, q.Encode())
}

// rewriteM3U8 parses and transforms all segment and child playlist URLs into proxied URLs
func rewriteM3U8(body []byte, baseURL *url.URL, proxyPath string, referer string, origin string) []byte {
	var output bytes.Buffer
	scanner := bufio.NewScanner(bytes.NewReader(body))

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())

		if line == "" {
			output.WriteString("\n")
			continue
		}

		if strings.HasPrefix(line, "#") {
			// Rewrite embedded tag URIs like #EXT-X-KEY:METHOD=...,URI="..." or #EXT-X-MAP:URI="..."
			if strings.Contains(line, "URI=") {
				line = uriTagRegex.ReplaceAllStringFunc(line, func(match string) string {
					sub := uriTagRegex.FindStringSubmatch(match)
					if len(sub) == 4 {
						rawTagURI := sub[2]
						resolved, err := baseURL.Parse(rawTagURI)
						if err == nil {
							proxied := buildProxiedURL(proxyPath, resolved.String(), referer, origin)
							return fmt.Sprintf("%s%s%s", sub[1], proxied, sub[3])
						}
					}
					return match
				})
			}
			output.WriteString(line)
			output.WriteString("\n")
			continue
		}

		// Line is a media chunk or sub-playlist URL
		resolved, err := baseURL.Parse(line)
		if err != nil {
			output.WriteString(line)
			output.WriteString("\n")
			continue
		}

		proxiedURL := buildProxiedURL(proxyPath, resolved.String(), referer, origin)
		output.WriteString(proxiedURL)
		output.WriteString("\n")
	}

	return output.Bytes()
}

func proxyHandler(w http.ResponseWriter, r *http.Request) {
	rawTargetURL := r.URL.Query().Get("url")
	referer := r.URL.Query().Get("referer")
	origin := r.URL.Query().Get("origin")

	if rawTargetURL == "" {
		http.Error(w, "Missing required 'url' query parameter", http.StatusBadRequest)
		return
	}

	targetURL, err := validateTargetURL(rawTargetURL)
	if err != nil {
		http.Error(w, fmt.Sprintf("Invalid target URL: %v", err), http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 35*time.Second)
	defer cancel()

	upstreamReq, err := http.NewRequestWithContext(ctx, r.Method, targetURL.String(), nil)
	if err != nil {
		http.Error(w, "Failed to create upstream request", http.StatusInternalServerError)
		return
	}

	// Attach spoofed headers to bypass provider hotlinking guards
	upstreamReq.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
	if referer != "" {
		upstreamReq.Header.Set("Referer", referer)
	}
	if origin != "" {
		upstreamReq.Header.Set("Origin", origin)
	}

	// Forward Range headers for seeking in media streams
	if rangeHeader := r.Header.Get("Range"); rangeHeader != "" {
		upstreamReq.Header.Set("Range", rangeHeader)
	}

	resp, err := httpClient.Do(upstreamReq)
	if err != nil {
		http.Error(w, fmt.Sprintf("Upstream request error: %v", err), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	contentType := resp.Header.Get("Content-Type")
	isM3U8 := strings.Contains(strings.ToLower(contentType), "mpegurl") ||
		strings.Contains(strings.ToLower(contentType), "m3u8") ||
		strings.HasSuffix(strings.ToLower(targetURL.Path), ".m3u8")

	// Forward critical streaming headers
	for _, key := range []string{"Accept-Ranges", "Content-Range", "Cache-Control"} {
		if val := resp.Header.Get(key); val != "" {
			w.Header().Set(key, val)
		}
	}

	if isM3U8 {
		// Read entire manifest to rewrite contained paths
		body, err := io.ReadAll(resp.Body)
		if err != nil {
			http.Error(w, "Failed to read upstream playlist", http.StatusBadGateway)
			return
		}

		rewritten := rewriteM3U8(body, targetURL, r.URL.Path, referer, origin)

		w.Header().Set("Content-Type", "application/vnd.apple.mpegurl")
		w.Header().Set("Content-Length", fmt.Sprintf("%d", len(rewritten)))
		w.WriteHeader(resp.StatusCode)
		w.Write(rewritten)
		return
	}

	// Media chunks (.ts, .m4s, keys): Stream directly without buffering entire payload
	if contentType != "" {
		w.Header().Set("Content-Type", contentType)
	}
	if cl := resp.Header.Get("Content-Length"); cl != "" {
		w.Header().Set("Content-Length", cl)
	}

	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/m3u8-proxy.m3u8", proxyHandler)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})

	handler := corsMiddleware(mux)

	server := &http.Server{
		Addr:         ":" + port,
		Handler:      handler,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 60 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	log.Printf("[Go Proxy] Listening on http://0.0.0.0:%s\n", port)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("[Go Proxy] Server fatal error: %v", err)
	}
}