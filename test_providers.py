import urllib.request
import threading
import time

SPEED_TEST_TARGETS = [
    "https://github.com/torvalds/linux/archive/refs/heads/master.zip",
    "https://download.jetbrains.com/idea/ideaIC-2023.3.4.exe",
    "https://proof.ovh.net/files/100Mb.dat",
    "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.7.4.tar.xz",
]

def _try_single_provider(url, duration_sec=4.0, n_threads=6):
    stop_event = threading.Event()
    downloaded_bytes = 0
    bytes_lock = threading.Lock()

    def worker():
        nonlocal downloaded_bytes
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                while not stop_event.is_set():
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    with bytes_lock:
                        downloaded_bytes += len(chunk)
        except Exception:
            pass

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n_threads)]
    t_start = time.time()
    for t in threads:
        t.start()

    time.sleep(duration_sec)
    stop_event.set()
    t_elapsed = max(0.1, time.time() - t_start)
    mbps = round((downloaded_bytes * 8 / t_elapsed) / 1_000_000, 2)
    return mbps

print("Starting provider speed benchmark (testing each 3 times for 5 seconds)...")
for url in SPEED_TEST_TARGETS:
    provider_name = url.split('/')[2]
    print(f"\n--- Testing {provider_name} ---")
    speeds = []
    for i in range(3):
        print(f"  Run {i+1}...")
        mbps = _try_single_provider(url, duration_sec=5.0, n_threads=6)
        speeds.append(mbps)
        print(f"  Result: {mbps} Mbps")
        time.sleep(1)
    
    avg_speed = round(sum(speeds) / len(speeds), 2)
    print(f"  AVERAGE for {provider_name}: {avg_speed} Mbps")
