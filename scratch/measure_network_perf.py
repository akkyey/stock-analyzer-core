"""ネットワーク基本性能 網羅的計測スクリプト
1. DNS名前解決レイテンシ (DNS Resolution Latency)
2. TCP接続時間・TLSハンドシェイク・TTFB (Time To First Byte)
3. 主要APIホストへのRTT遅延
4. 実効ダウンロード帯域幅・スループット (Throughput)
"""

import time
import socket
import ssl
import requests
import statistics

hosts = [
    ("Yahoo! Finance", "query1.finance.yahoo.com", "https://query1.finance.yahoo.com"),
    ("EDINET (金融庁)", "api.edinet-fsa.go.jp", "https://api.edinet-fsa.go.jp"),
    ("JPX (日本取引所)", "www.jpx.co.jp", "https://www.jpx.co.jp"),
    ("Cloudflare (1.1.1.1)", "1.1.1.1", "https://1.1.1.1"),
    ("Google DNS (8.8.8.8)", "8.8.8.8", "https://8.8.8.8"),
]

print("=" * 75)
print("🌐 【ネットワーク基本性能ベンチマーク計測】")
print("=" * 75)

# 1. DNS Resolution
print("\n--- 1. DNS名前解決時間 (DNS Resolution Latency) ---")
for name, host, _ in hosts:
    if host in ["1.1.1.1", "8.8.8.8"]:
        continue
    latencies = []
    ip = "Unknown"
    for _ in range(5):
        t0 = time.perf_counter()
        try:
            ip = socket.gethostbyname(host)
            latencies.append((time.perf_counter() - t0) * 1000)
        except Exception:
            pass
    if latencies:
        avg_l = statistics.mean(latencies)
        min_l = min(latencies)
        print(f"  * {name:<18} ({host:<26}): IP={ip:<15} 平均: {avg_l:6.2f} ms (最速: {min_l:6.2f} ms)")

# 2. HTTPS Connection & TTFB
print("\n--- 2. HTTPS 接続確立・TTFB (Time To First Byte) ---")
for name, host, url in hosts:
    ttfb_list = []
    status = 0
    for _ in range(3):
        t0 = time.perf_counter()
        try:
            r = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            ttfb_list.append((time.perf_counter() - t0) * 1000)
            status = r.status_code
        except Exception:
            pass
    if ttfb_list:
        avg_t = statistics.mean(ttfb_list)
        min_t = min(ttfb_list)
        max_t = max(ttfb_list)
        print(f"  * {name:<18}: 平均: {avg_t:6.2f} ms (最速: {min_t:6.2f} ms, 最遅: {max_t:6.2f} ms, HTTP {status})")

# 3. Throughput Test
print("\n--- 3. 実効ダウンロード帯域幅・スループット (Throughput) ---")
test_urls = [
    ("Cloudflare CDN (10MB)", "https://speed.cloudflare.com/__down?bytes=10000000"),
    ("Cloudflare CDN (25MB)", "https://speed.cloudflare.com/__down?bytes=25000000"),
    ("JPX 公式データ (223KB)", "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"),
]

for label, url in test_urls:
    try:
        t0 = time.perf_counter()
        r = requests.get(url, timeout=15)
        elapsed = time.perf_counter() - t0
        size_bytes = len(r.content)
        size_mb = size_bytes / (1024 * 1024)
        mbps = (size_bytes * 8) / (elapsed * 1_000_000)
        mb_per_sec = size_mb / elapsed
        print(f"  * {label:<22}: {size_mb:5.2f} MB in {elapsed:5.2f} 秒 ➔ {mbps:6.2f} Mbps ({mb_per_sec:5.2f} MB/s)")
    except Exception as e:
        print(f"  * {label:<22}: 測定失敗 ({e})")

print("=" * 75)
