#!/usr/bin/env python3
"""
PLDT H153-381 Modem WAN Traffic & Throttling Analyzer
======================================================
Connects directly to the Huawei H153-381 (PLDT Home WiFi) modem at 192.168.1.1
and polls its internal traffic_statistics API every 2 seconds.

Throttling Determination State Machine:
  - Active 6-thread HTTP download stress tests run every 15 minutes (or on-demand).
  - ONLY active speed stress tests determine if the system is "Unthrottled" or "Throttled".
  - If a test shows speed < 15.0 Mbps, a 2nd verification test is run 3 seconds later
    to confirm the throttle before declaring "Throttled".
  - Default status is "Unthrottled". If link is down, status is "No Internet".

Data Outputs:
  - Real-time Download & Upload Mbps (2s rate)
  - Today Download & Upload GB (resets at 12:00 AM midnight)
  - Lifetime Download & Upload GB (accumulates continuously, never resets)
  - Active Speed Test Logs & Countdown to next test
  - CSV Export with DateTime (Local), speeds, bytes, today GB, lifetime DL/UL GB, Signal, Network, Status

Serves dashboard at http://localhost:8080
REST APIs:
  GET /api/traffic           -- live current state, status badge, next test timestamp
  GET /api/history?range=... -- chart & daily history data
  GET /api/speed_probes      -- active speed test records
  GET /api/run_speed_test    -- trigger immediate on-demand speed probe
  GET /api/export/csv        -- download clean CSV export
  GET /api/reset_db          -- reset database
"""

import os
import sys
import csv
import time
import json
import sqlite3
import threading
import io
import urllib.request
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Manila")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=8))

def get_local_datetime(ts=None):
    """Return a timezone-aware datetime in Asia/Manila (UTC+8) timezone."""
    if ts is None:
        return datetime.now(LOCAL_TZ)
    return datetime.fromtimestamp(ts, tz=LOCAL_TZ)
from urllib.parse import parse_qs, urlparse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ─── Load .env Configuration ──────────────────────────────────────────────────
BASE_DIR              = os.path.dirname(os.path.abspath(__file__))
env_file = os.path.join(BASE_DIR, ".env")
if os.path.isfile(env_file):
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# ─── Config ───────────────────────────────────────────────────────────────────
MODEM_URL             = os.environ.get("MODEM_URL", "http://192.168.1.1/")
MODEM_USER            = os.environ.get("MODEM_USER", "admin")
MODEM_PASS            = os.environ.get("MODEM_PASS", "YOUR_MODEM_PASSWORD")
POLL_INTERVAL         = 2.0
TEST_INTERVAL_SECONDS = 900   # 15 minutes
DB_FILE               = "traffic_evidence.db"
ISP_THROTTLE_CAP_MBPS = 5.0
THROTTLE_THRESHOLD_MBPS = 15.0  # Speed test threshold (< 15.0 Mbps under load = Throttled)

SPEED_TEST_TARGET = "https://github.com/torvalds/linux/archive/refs/heads/master.zip"
SPEED_TEST_TARGET_2 = "https://speed.cloudflare.com/__down?bytes=50000000"

RANGE_SECONDS_MAP = {
    "1min":     60,
    "5m":       300,
    "30m":      1800,
    "1h":       3600,
    "6h":       21600,
    "12h":      43200,
    "1d":       86400,
    "today":    -1,   # special: from midnight today
    "3d":       259200,
    "1w":       604800,
    "1m":       2592000,
    "lifetime": 0,
}

NETWORK_TYPE_MAP = {
    "0": "No Service", "1": "GSM", "2": "GPRS", "3": "EDGE",
    "4": "WCDMA", "5": "HSDPA", "6": "HSUPA", "7": "HSPA+",
    "9": "HSPA+", "10": "EVDO", "11": "EVDO B", "12": "1xRTT",
    "13": "UMB", "17": "HSPA+ (DC)", "18": "TD-SCDMA",
    "19": "LTE", "41": "LTE CA", "1011": "5G NSA", "1001": "5G SA",
}

# ─── Shared State ─────────────────────────────────────────────────────────────
state_lock  = threading.Lock()
probe_lock  = threading.Lock()
probe_running = False  # simple flag — more reliable than probe_lock.locked()

current_state = {
    "status":                  "Unthrottled",  # "Unthrottled" | "Throttled" | "No Internet"
    "modem_status":            "connecting",   # "online" | "reconnecting"
    "modem_ip":                "192.168.1.1",
    "router_ip":               "192.168.0.1",
    "engine":                  "PLDT H153-381 Modem API",
    "dl_mbps":                 0.0,
    "ul_mbps":                 0.0,
    "session_download_bytes":  0,
    "session_upload_bytes":    0,
    "today_download_bytes":    0,
    "today_upload_bytes":      0,
    "lifetime_download_bytes": 0,
    "lifetime_upload_bytes":   0,
    "current_date":            get_local_datetime().strftime("%Y-%m-%d"),
    "throttle_cap_mbps":       ISP_THROTTLE_CAP_MBPS,
    "signal_icon":             "0",
    "network_type":            "",
    "connected_devices":      0,
    "last_updated":            time.time(),
    "next_test_ts":            int(time.time()) + TEST_INTERVAL_SECONDS,
    "db_total_samples":        0,
    "db_total_probes":         0,
    "last_probe_result":       None,
}

cache_lock   = threading.Lock()
sample_cache = []
daily_cache  = {}
_peak_override_cooldown_ts = 0  # Unix ts: do not insert another peak-override probe before this time

# ─── Poll Loop Accumulators (module-level so reset_database can zero them) ────
poll_day_stats        = {}   # keyed by date string
poll_lifetime_dl      = 0    # bytes, accumulates continuously
poll_lifetime_ul      = 0    # bytes, accumulates continuously
poll_last_session_dl  = None # last known modem session DL counter
poll_last_session_ul  = None # last known modem session UL counter


# ─── SQLite Database ──────────────────────────────────────────────────────────
def get_db():
    db_path = os.path.join(BASE_DIR, DB_FILE)
    conn = sqlite3.connect(db_path, timeout=15.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS traffic_samples (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                  INTEGER NOT NULL,
            dl_mbps             REAL    NOT NULL,
            ul_mbps             REAL    NOT NULL,
            dl_bytes_delta      INTEGER NOT NULL DEFAULT 0,
            ul_bytes_delta      INTEGER NOT NULL DEFAULT 0,
            today_dl_gb         REAL    NOT NULL DEFAULT 0,
            today_ul_gb         REAL    NOT NULL DEFAULT 0,
            lifetime_dl_gb      REAL    NOT NULL DEFAULT 0,
            lifetime_ul_gb      REAL    NOT NULL DEFAULT 0,
            date                TEXT    NOT NULL,
            signal_icon         TEXT    DEFAULT '',
            network_type        TEXT    DEFAULT '',
            status_label        TEXT    DEFAULT 'Unthrottled'
        );

        CREATE INDEX IF NOT EXISTS idx_samples_ts   ON traffic_samples(ts);
        CREATE INDEX IF NOT EXISTS idx_samples_date ON traffic_samples(date);

        CREATE TABLE IF NOT EXISTS daily_stats (
            date                TEXT    PRIMARY KEY,
            download_bytes      INTEGER DEFAULT 0,
            upload_bytes        INTEGER DEFAULT 0,
            peak_dl_mbps        REAL    DEFAULT 0,
            peak_ul_mbps        REAL    DEFAULT 0,
            avg_dl_mbps         REAL    DEFAULT 0,
            active_sample_count INTEGER DEFAULT 0,
            sample_count        INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS speed_probes (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                  INTEGER NOT NULL,
            date                TEXT    NOT NULL,
            dl_mbps             REAL    NOT NULL,
            ul_mbps             REAL    NOT NULL,
            today_dl_gb         REAL    NOT NULL DEFAULT 0,
            today_ul_gb         REAL    NOT NULL DEFAULT 0,
            lifetime_dl_gb      REAL    NOT NULL DEFAULT 0,
            lifetime_ul_gb      REAL    NOT NULL DEFAULT 0,
            is_throttled        INTEGER NOT NULL DEFAULT 0,
            provider            TEXT    DEFAULT '',
            status_desc         TEXT    DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_probes_ts ON speed_probes(ts);
    """)
    conn.commit()
    conn.close()
    print(f"[DB] Initialized SQLite at {os.path.join(BASE_DIR, DB_FILE)}")


def insert_sample(conn, ts, dl_mbps, ul_mbps, dl_delta, ul_delta,
                  today_dl_gb, today_ul_gb, lifetime_dl_gb, lifetime_ul_gb,
                  date, signal_icon, network_type, status_label):
    conn.execute("""
        INSERT INTO traffic_samples
          (ts, dl_mbps, ul_mbps, dl_bytes_delta, ul_bytes_delta,
           today_dl_gb, today_ul_gb, lifetime_dl_gb, lifetime_ul_gb,
           date, signal_icon, network_type, status_label)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (ts, dl_mbps, ul_mbps, dl_delta, ul_delta,
          today_dl_gb, today_ul_gb, lifetime_dl_gb, lifetime_ul_gb,
          date, signal_icon, network_type, status_label))


def upsert_daily(conn, date, dl_bytes, ul_bytes, peak_dl, peak_ul,
                 avg_dl, active_n, sample_n):
    conn.execute("""
        INSERT INTO daily_stats
          (date, download_bytes, upload_bytes, peak_dl_mbps, peak_ul_mbps,
           avg_dl_mbps, active_sample_count, sample_count)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
          download_bytes      = excluded.download_bytes,
          upload_bytes        = excluded.upload_bytes,
          peak_dl_mbps        = excluded.peak_dl_mbps,
          peak_ul_mbps        = excluded.peak_ul_mbps,
          avg_dl_mbps         = excluded.avg_dl_mbps,
          active_sample_count = excluded.active_sample_count,
          sample_count        = excluded.sample_count
    """, (date, dl_bytes, ul_bytes, peak_dl, peak_ul, avg_dl, active_n, sample_n))


def reset_database():
    """Clear all records from SQLite tables, reset in-memory caches, and zero all poll accumulators."""
    global sample_cache, daily_cache
    global poll_day_stats, poll_lifetime_dl, poll_lifetime_ul, poll_last_session_dl, poll_last_session_ul

    conn = get_db()
    conn.execute("DELETE FROM traffic_samples")
    conn.execute("DELETE FROM daily_stats")
    conn.execute("DELETE FROM speed_probes")
    conn.commit()
    conn.close()

    with cache_lock:
        sample_cache.clear()
        daily_cache.clear()

    # Zero the poll loop accumulators so the poller re-starts from scratch
    poll_day_stats       = {}
    poll_lifetime_dl     = 0
    poll_lifetime_ul     = 0
    poll_last_session_dl = None
    poll_last_session_ul = None

    with state_lock:
        current_state["today_download_bytes"]    = 0
        current_state["today_upload_bytes"]      = 0
        current_state["lifetime_download_bytes"] = 0
        current_state["lifetime_upload_bytes"]   = 0
        current_state["session_download_bytes"]  = 0
        current_state["session_upload_bytes"]    = 0
        current_state["db_total_samples"]        = 0
        current_state["db_total_probes"]         = 0
        current_state["status"]                  = "Unthrottled"
        current_state["next_test_ts"]            = int(time.time()) + TEST_INTERVAL_SECONDS

    print("[DB RESET] Database completely cleared — all counters zeroed!")


def load_db_stats():
    conn = get_db()
    n_samples = conn.execute("SELECT COUNT(*) FROM traffic_samples").fetchone()[0]
    n_probes  = conn.execute("SELECT COUNT(*) FROM speed_probes").fetchone()[0]
    conn.close()
    return n_samples, n_probes


def sync_sample_status_from_probes(conn=None):
    """
    Sync status_label in traffic_samples with recorded speed_probes.
    For any interval starting at a speed probe timestamp, update traffic_samples.status_label
    to match that probe's throttling status ("Throttled" vs "Unthrottled").
    """
    close_db = False
    if conn is None:
        conn = get_db()
        close_db = True
    try:
        probes = conn.execute("SELECT ts, is_throttled FROM speed_probes ORDER BY ts ASC").fetchall()
        if not probes:
            return
        for i in range(len(probes)):
            p = probes[i]
            p_ts = p["ts"]
            st = "Throttled" if p["is_throttled"] == 1 else "Unthrottled"
            if i < len(probes) - 1:
                next_ts = probes[i + 1]["ts"]
                conn.execute("UPDATE traffic_samples SET status_label = ? WHERE ts >= ? AND ts < ?", (st, p_ts, next_ts))
            else:
                conn.execute("UPDATE traffic_samples SET status_label = ? WHERE ts >= ?", (st, p_ts))
        conn.commit()
    except Exception as e:
        print(f"[STATUS SYNC ERROR] {e}")
    finally:
        if close_db:
            conn.close()


def load_cache_from_db():
    global sample_cache, daily_cache
    sync_sample_status_from_probes()
    cutoff = int(time.time()) - 86400
    conn = get_db()

    rows = conn.execute("""
        SELECT ts, dl_mbps, ul_mbps, dl_bytes_delta, ul_bytes_delta,
               today_dl_gb, today_ul_gb, lifetime_dl_gb, lifetime_ul_gb,
               date, signal_icon, network_type, status_label
        FROM traffic_samples WHERE ts >= ?
        ORDER BY ts ASC
    """, (cutoff,)).fetchall()

    daily_rows = conn.execute("SELECT * FROM daily_stats ORDER BY date ASC").fetchall()
    conn.close()

    with cache_lock:
        sample_cache = [dict(r) for r in rows]
        daily_cache  = {r["date"]: dict(r) for r in daily_rows}

    print(f"[CACHE] Loaded {len(sample_cache)} samples (last 24h), {len(daily_cache)} daily records from DB.")


def _insert_peak_override_probe(dl_mbps: float):
    """
    Insert a synthetic speed_probe record when real SNMP traffic exceeds the throttle
    threshold while the status is 'Throttled'. This auto-overrides the throttle label
    without requiring a formal speed test — any device on the network saturating the WAN
    at >=15 Mbps proves the line is not throttled at that moment.
    """
    global _peak_override_cooldown_ts
    now_ts  = int(time.time())
    dt_str  = get_local_datetime(now_ts).strftime('%Y-%m-%d')
    status_desc = (
        f"PEAK OVERRIDE: Real WAN traffic reached {dl_mbps:.2f} Mbps "
        f"\u2265 {THROTTLE_THRESHOLD_MBPS} Mbps \u2014 auto-marked Unthrottled"
    )

    with state_lock:
        t_dl_gb = round(current_state.get('today_download_bytes',    0) / (1024**3), 3)
        t_ul_gb = round(current_state.get('today_upload_bytes',      0) / (1024**3), 3)
        l_dl_gb = round(current_state.get('lifetime_download_bytes', 0) / (1024**3), 3)
        l_ul_gb = round(current_state.get('lifetime_upload_bytes',   0) / (1024**3), 3)
        current_state["status"] = "Unthrottled"

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO speed_probes
              (ts, date, dl_mbps, ul_mbps, today_dl_gb, today_ul_gb,
               lifetime_dl_gb, lifetime_ul_gb, is_throttled, provider, status_desc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (now_ts, dt_str, round(dl_mbps, 2), 0.0, t_dl_gb, t_ul_gb,
               l_dl_gb, l_ul_gb, 0, "peak-override", status_desc))
        conn.commit()
        print(f"[PEAK OVERRIDE] {status_desc}")
    except Exception as e:
        print(f"[PEAK OVERRIDE DB ERROR] {e}")
    finally:
        conn.close()

    sync_sample_status_from_probes()
    load_cache_from_db()


# ─── Active Speed Stress Test Prober ──────────────────────────────────────────
def _exec_dual_speed_test(duration_sec=6.0, n_threads=4):
    """
    Executes a speed test against both providers concurrently.
    """
    url1 = SPEED_TEST_TARGET
    url2 = SPEED_TEST_TARGET_2
    print(f"[SPEED TEST] Trying dual providers: Github and Cloudflare...")
    
    stop_event = threading.Event()
    
    def worker(url):
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                while not stop_event.is_set():
                    chunk = resp.read(65536)
                    if not chunk:
                        break
        except Exception:
            pass

    threads = []
    for _ in range(n_threads):
        threads.append(threading.Thread(target=worker, args=(url1,), daemon=True))
        threads.append(threading.Thread(target=worker, args=(url2,), daemon=True))
        
    for t in threads:
        t.start()
        
    time.sleep(duration_sec)
    stop_event.set()


def run_speed_test_probe():
    """
    Executes an active speed test with sequential provider fallback and
    double-verification retry logic. probe_running is already set True by
    the HTTP handler before this thread starts.
    """
    global probe_running
    # Guard against double-launch (e.g. from periodic loop)
    with probe_lock:
        if not probe_running:
            probe_running = True

    try:
        return _run_speed_test_probe_internal()
    finally:
        with probe_lock:
            probe_running = False


def launch_probe_if_free():
    """
    Atomically checks if a probe is running, sets the flag, and starts the thread.
    Returns (started: bool). Standalone function so probe_running assignment
    never happens inside do_GET (which would cause UnboundLocalError).
    """
    global probe_running
    with probe_lock:
        if probe_running:
            return False
        probe_running = True  # Set BEFORE thread starts — no race with JS poller
    t = threading.Thread(target=run_speed_test_probe, daemon=True)
    t.start()
    return True

def _run_speed_test_probe_internal():
    print("[ACTIVE PROBE] Starting active speed test (Dual Providers)...")
    
    test_start_ts = int(time.time())
    _exec_dual_speed_test(duration_sec=6.0, n_threads=4)
    test_end_ts = int(time.time())
    
    provider_used = "github+cloudflare"
    
    # Give the poller a small buffer to write the last sample
    time.sleep(1.0)
    
    # Query sample_cache for the peak dl_mbps during the test window
    with cache_lock:
        test_samples = [s for s in sample_cache if s["ts"] >= test_start_ts and s["ts"] <= test_end_ts + 1]
    
    if test_samples:
        peak_mbps = max(s["dl_mbps"] for s in test_samples)
    else:
        peak_mbps = 0.0
        
    mbps_1 = peak_mbps

    is_throttled = False
    verified_mbps = mbps_1
    status_desc = ""

    if mbps_1 < THROTTLE_THRESHOLD_MBPS:
        print(f"[ACTIVE PROBE] Test 1: Peak {mbps_1} Mbps. Running 2nd verification in 3s...")
        time.sleep(3.0)
        
        test_start_ts_2 = int(time.time())
        _exec_dual_speed_test(duration_sec=6.0, n_threads=4)
        test_end_ts_2 = int(time.time())
        
        time.sleep(1.0)
        
        with cache_lock:
            test_samples_2 = [s for s in sample_cache if s["ts"] >= test_start_ts_2 and s["ts"] <= test_end_ts_2 + 1]
            
        if test_samples_2:
            mbps_2 = max(s["dl_mbps"] for s in test_samples_2)
        else:
            mbps_2 = 0.0

        if mbps_1 == 0.0 and mbps_2 == 0.0:
            is_throttled = True
            verified_mbps = 0.0
            status_desc = "WAN OUTAGE: Failed to reach the internet (0.0 Mbps)"
        elif mbps_2 < THROTTLE_THRESHOLD_MBPS:
            is_throttled = True
            verified_mbps = round((mbps_1 + mbps_2) / 2.0, 2)
            status_desc = f"CONFIRMED THROTTLE: Test 1 ({mbps_1} Mbps) & Test 2 ({mbps_2} Mbps) < 15.0 Mbps under load"
        else:
            is_throttled = False
            verified_mbps = mbps_2
            status_desc = f"UNTHROTTLED (Re-test passed): Test 1 was {mbps_1} Mbps, Test 2 reached {mbps_2} Mbps"
    else:
        is_throttled = False
        status_desc = f"UNTHROTTLED: Reached {mbps_1} Mbps under active load"

    now_ts = int(time.time())
    dt_str = get_local_datetime(now_ts).strftime('%Y-%m-%d')

    with state_lock:
        today_dl_b = current_state.get('today_download_bytes', 0)
        today_ul_b = current_state.get('today_upload_bytes', 0)
        life_dl_b  = current_state.get('lifetime_download_bytes', 0)
        life_ul_b  = current_state.get('lifetime_upload_bytes', 0)

        t_dl_gb = round(today_dl_b / (1024**3), 3)
        t_ul_gb = round(today_ul_b / (1024**3), 3)
        l_dl_gb = round(life_dl_b  / (1024**3), 3)
        l_ul_gb = round(life_ul_b  / (1024**3), 3)
        cum_gb  = round((today_dl_b + today_ul_b) / (1024**3), 3)

        # Update system status state
        if current_state.get("modem_status") == "reconnecting":
            current_state["status"] = "No Internet"
        else:
            current_state["status"] = "Throttled" if is_throttled else "Unthrottled"
        
        current_state["next_test_ts"] = now_ts + TEST_INTERVAL_SECONDS
        
        res_obj = {
            "ts":             now_ts,
            "date":           dt_str,
            "dl_mbps":        verified_mbps,
            "ul_mbps":        0.0,
            "today_dl_gb":    t_dl_gb,
            "today_ul_gb":    t_ul_gb,
            "lifetime_dl_gb": l_dl_gb,
            "lifetime_ul_gb": l_ul_gb,
            "cumulative_gb":  cum_gb,
            "is_throttled":   1 if is_throttled else 0,
            "provider":       provider_used,
            "status":         current_state["status"],
            "status_desc":    status_desc,
        }
        current_state["last_probe_result"] = res_obj

    # Save probe to SQLite DB
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO speed_probes
              (ts, date, dl_mbps, ul_mbps, today_dl_gb, today_ul_gb,
               lifetime_dl_gb, lifetime_ul_gb, is_throttled, provider, status_desc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (now_ts, dt_str, verified_mbps, 0.0, t_dl_gb, t_ul_gb,
               l_dl_gb, l_ul_gb, 1 if is_throttled else 0, provider_used, status_desc))
        conn.commit()
    except Exception as db_err:
        # If DB schema doesn't have provider column yet (old DB), add it
        try:
            conn.execute("ALTER TABLE speed_probes ADD COLUMN provider TEXT DEFAULT ''")
            conn.execute("""
                INSERT INTO speed_probes
                  (ts, date, dl_mbps, ul_mbps, today_dl_gb, today_ul_gb,
                   lifetime_dl_gb, lifetime_ul_gb, is_throttled, provider, status_desc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (now_ts, dt_str, verified_mbps, 0.0, t_dl_gb, t_ul_gb,
                   l_dl_gb, l_ul_gb, 1 if is_throttled else 0, provider_used, status_desc))
            conn.commit()
        except Exception as e2:
            print(f"[DB] probe save error: {e2}")
    conn.close()
    sync_sample_status_from_probes()
    load_cache_from_db()

    print(f"[ACTIVE SPEED PROBE] Status: {current_state['status']} | {status_desc}")
    return res_obj


def record_diagnostic_log(provider, status_desc, is_error=True):
    """Inserts a diagnostic / auth error event into the speed_probes diagnostic log table."""
    try:
        now_ts = int(time.time())
        dt_str = get_local_datetime().strftime("%Y-%m-%d %H:%M:%S")

        with state_lock:
            t_dl_gb = round(current_state.get("today_download_bytes", 0) / 1024**3, 3)
            t_ul_gb = round(current_state.get("today_upload_bytes", 0) / 1024**3, 3)
            l_dl_gb = round(current_state.get("lifetime_download_bytes", 0) / 1024**3, 3)
            l_ul_gb = round(current_state.get("lifetime_upload_bytes", 0) / 1024**3, 3)

        conn = get_db()
        try:
            conn.execute("""
                INSERT INTO speed_probes
                  (ts, date, dl_mbps, ul_mbps, today_dl_gb, today_ul_gb,
                   lifetime_dl_gb, lifetime_ul_gb, is_throttled, provider, status_desc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (now_ts, dt_str, 0.0, 0.0, t_dl_gb, t_ul_gb,
                   l_dl_gb, l_ul_gb, 2 if is_error else 0, provider, status_desc))
            conn.commit()
        except Exception as db_err:
            try:
                conn.execute("ALTER TABLE speed_probes ADD COLUMN provider TEXT DEFAULT ''")
                conn.execute("""
                    INSERT INTO speed_probes
                      (ts, date, dl_mbps, ul_mbps, today_dl_gb, today_ul_gb,
                       lifetime_dl_gb, lifetime_ul_gb, is_throttled, provider, status_desc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (now_ts, dt_str, 0.0, 0.0, t_dl_gb, t_ul_gb,
                       l_dl_gb, l_ul_gb, 2 if is_error else 0, provider, status_desc))
                conn.commit()
            except Exception:
                pass
        conn.close()
        sync_sample_status_from_probes()
        load_cache_from_db()
    except Exception as e:
        print(f"[DIAGNOSTIC LOG ERROR] {e}")


def periodic_speed_probe_loop():
    """Background thread checking and executing active speed test every 15 minutes."""
    time.sleep(5)
    while True:
        try:
            now_ts = int(time.time())
            with state_lock:
                target_ts = current_state.get("next_test_ts", 0)
            if now_ts >= target_ts:
                run_speed_test_probe()
        except Exception as e:
            print(f"[SPEED PROBE ERROR] {e}")
        time.sleep(3)


def auto_prune_database_loop():
    """Daily background maintenance: prunes raw 1-second samples older than 6 months (180 days) while keeping daily stats & speed probes forever."""
    while True:
        try:
            time.sleep(3600)  # Check hourly
            now_ts = int(time.time())
            cutoff_180d = now_ts - (180 * 86400)
            conn = get_db()
            cursor = conn.execute("DELETE FROM traffic_samples WHERE ts < ?", (cutoff_180d,))
            deleted_cnt = cursor.rowcount
            if deleted_cnt > 0:
                print(f"[MAINTENANCE] Pruned {deleted_cnt:,} raw samples older than 6 months (180 days).")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception as e:
            print(f"[MAINTENANCE ERROR] {e}")


# ─── Modem Poller ─────────────────────────────────────────────────────────────
def poll_modem_loop():
    global sample_cache, daily_cache, current_state
    global poll_day_stats, poll_lifetime_dl, poll_lifetime_ul, poll_last_session_dl, poll_last_session_ul

    try:
        from huawei_lte_api.Client import Client
        from huawei_lte_api.Connection import Connection
    except ImportError:
        print("[ERROR] huawei-lte-api not installed. Run: pip install huawei-lte-api")
        sys.exit(1)

    # Initialize lifetime bytes from DB on startup
    if poll_lifetime_dl == 0 and poll_lifetime_ul == 0:
        try:
            conn = get_db()
            row = conn.execute("SELECT SUM(download_bytes), SUM(upload_bytes) FROM daily_stats").fetchone()
            if row and row[0] is not None:
                poll_lifetime_dl = row[0]
                poll_lifetime_ul = row[1]
            conn.close()
        except Exception as e:
            print(f"[DB INIT] Lifetime stats load error: {e}")

    day_stats        = poll_day_stats
    lifetime_dl_bytes = poll_lifetime_dl
    lifetime_ul_bytes = poll_lifetime_ul

    last_session_dl = poll_last_session_dl
    last_session_ul = poll_last_session_ul

    print(f"[MODEM] Connecting to PLDT H153-381 at {MODEM_URL} ...")

    consecutive_login_failures = 0

    while True:
        try:
            conn = get_db()
            with Connection(MODEM_URL, username=MODEM_USER, password=MODEM_PASS) as modem_conn:
                client = Client(modem_conn)
                print("[MODEM] Authenticated — polling started.")
                consecutive_login_failures = 0
                with state_lock:
                    current_state["modem_status"] = "online"

                write_counter = 0

                while True:
                    try:
                        # Sync from module globals — reset_database() may have zeroed them
                        if poll_lifetime_dl < lifetime_dl_bytes or poll_day_stats is not day_stats:
                            lifetime_dl_bytes = poll_lifetime_dl
                            lifetime_ul_bytes = poll_lifetime_ul
                            day_stats         = poll_day_stats
                            last_session_dl   = poll_last_session_dl
                            last_session_ul   = poll_last_session_ul

                        stats  = client.monitoring.traffic_statistics()
                        status = client.monitoring.status()

                        now    = time.time()
                        dt_obj = get_local_datetime()
                        today  = dt_obj.strftime("%Y-%m-%d")

                        dl_bytes_s = int(stats.get("CurrentDownloadRate", 0))
                        ul_bytes_s = int(stats.get("CurrentUploadRate",   0))
                        dl_mbps    = round(dl_bytes_s * 8 / 1_000_000, 3)
                        ul_mbps    = round(ul_bytes_s * 8 / 1_000_000, 3)

                        session_dl = int(stats.get("CurrentDownload", 0))
                        session_ul = int(stats.get("CurrentUpload",   0))

                        if last_session_dl is not None:
                            delta_dl = max(0, session_dl - last_session_dl)
                            delta_ul = max(0, session_ul - last_session_ul)
                        else:
                            delta_dl = 0
                            delta_ul = 0
                        last_session_dl = session_dl
                        last_session_ul = session_ul

                        lifetime_dl_bytes += delta_dl
                        lifetime_ul_bytes += delta_ul
                        # Write back to module globals so reset_database() sees current values
                        poll_lifetime_dl     = lifetime_dl_bytes
                        poll_lifetime_ul     = lifetime_ul_bytes
                        poll_day_stats       = day_stats
                        poll_last_session_dl = last_session_dl
                        poll_last_session_ul = last_session_ul

                        signal_icon  = status.get("SignalIcon", "0") or "0"
                        net_type_raw = status.get("CurrentNetworkTypeEx") or status.get("CurrentNetworkType", "0")
                        net_type     = NETWORK_TYPE_MAP.get(str(net_type_raw), f"Type {net_type_raw}")
                        connected_w  = int(status.get("CurrentWifiUser", 0) or 0)

                        if today not in day_stats:
                            existing = conn.execute(
                                "SELECT * FROM daily_stats WHERE date=?", (today,)
                            ).fetchone()
                            if existing:
                                day_stats[today] = dict(existing)
                            else:
                                day_stats[today] = {
                                    "date":                 today,
                                    "download_bytes":       0,
                                    "upload_bytes":         0,
                                    "peak_dl_mbps":         0.0,
                                    "peak_ul_mbps":         0.0,
                                    "avg_dl_mbps":          0.0,
                                    "active_sample_count":  0,
                                    "sample_count":         0,
                                }

                        d = day_stats[today]
                        d["download_bytes"] += delta_dl
                        d["upload_bytes"]   += delta_ul
                        d["sample_count"]   += 1
                        if dl_mbps > d["peak_dl_mbps"]: d["peak_dl_mbps"] = dl_mbps
                        if ul_mbps > d["peak_ul_mbps"]: d["peak_ul_mbps"] = ul_mbps
                        if dl_mbps >= 0.5:
                            n = d["active_sample_count"]
                            d["avg_dl_mbps"] = round(
                                (d["avg_dl_mbps"] * n + dl_mbps) / (n + 1), 3
                            )
                            d["active_sample_count"] = n + 1

                        today_dl_gb    = round(d["download_bytes"] / 1024**3, 6)
                        today_ul_gb    = round(d["upload_bytes"]   / 1024**3, 6)
                        lifetime_dl_gb = round(lifetime_dl_bytes   / 1024**3, 6)
                        lifetime_ul_gb = round(lifetime_ul_bytes   / 1024**3, 6)

                        with state_lock:
                            if current_state["current_date"] != today:
                                print(f"[MIDNIGHT] Date rollover -> {today}")
                                current_state["current_date"]        = today
                                current_state["today_download_bytes"] = 0
                                current_state["today_upload_bytes"]   = 0

                            current_state["modem_status"]            = "online"
                            current_state["dl_mbps"]               = dl_mbps
                            current_state["ul_mbps"]               = ul_mbps
                            current_state["session_download_bytes"] = session_dl
                            current_state["session_upload_bytes"]   = session_ul
                            current_state["today_download_bytes"]  = d["download_bytes"]
                            current_state["today_upload_bytes"]    = d["upload_bytes"]
                            current_state["lifetime_download_bytes"] = lifetime_dl_bytes
                            current_state["lifetime_upload_bytes"]   = lifetime_ul_bytes
                            current_state["signal_icon"]           = signal_icon
                            current_state["network_type"]          = net_type
                            current_state["connected_devices"]     = connected_w
                            current_state["last_updated"]          = now

                            curr_sys_status = current_state.get("status", "Unthrottled")

                        # ── Peak-Override: if currently Throttled but real traffic exceeds threshold,
                        #    auto-flip to Unthrottled so other-device saturation never falsely tags
                        #    normal traffic as throttled. Cooldown: 60 s between synthetic probes.
                        global _peak_override_cooldown_ts
                        if curr_sys_status == "Throttled" and dl_mbps >= THROTTLE_THRESHOLD_MBPS:
                            now_int = int(now)
                            if now_int >= _peak_override_cooldown_ts:
                                _peak_override_cooldown_ts = now_int + 60
                                curr_sys_status = "Unthrottled"
                                with state_lock:
                                    current_state["status"] = "Unthrottled"
                                threading.Thread(
                                    target=_insert_peak_override_probe,
                                    args=(dl_mbps,),
                                    daemon=True
                                ).start()

                        sample_entry = {
                            "ts":             int(now),
                            "dl_mbps":        dl_mbps,
                            "ul_mbps":        ul_mbps,
                            "dl_bytes_delta": delta_dl,
                            "ul_bytes_delta": delta_ul,
                            "today_dl_gb":    today_dl_gb,
                            "today_ul_gb":    today_ul_gb,
                            "lifetime_dl_gb": lifetime_dl_gb,
                            "lifetime_ul_gb": lifetime_ul_gb,
                            "date":           today,
                            "signal_icon":    signal_icon,
                            "network_type":   net_type,
                            "status_label":   curr_sys_status,
                        }

                        cutoff_24h = int(now) - 86400
                        with cache_lock:
                            sample_cache.append(sample_entry)
                            if len(sample_cache) > 46800:
                                sample_cache = [s for s in sample_cache if s["ts"] >= cutoff_24h]
                            daily_cache[today] = dict(d)

                        insert_sample(
                            conn, int(now), dl_mbps, ul_mbps,
                            delta_dl, delta_ul, today_dl_gb, today_ul_gb,
                            lifetime_dl_gb, lifetime_ul_gb, today,
                            signal_icon, net_type, curr_sys_status
                        )
                        write_counter += 1
                        if write_counter >= 5:
                            upsert_daily(
                                conn, today,
                                d["download_bytes"], d["upload_bytes"],
                                d["peak_dl_mbps"], d["peak_ul_mbps"],
                                d["avg_dl_mbps"], d["active_sample_count"],
                                d["sample_count"],
                            )
                            conn.commit()
                            write_counter = 0

                            n_samples, n_probes = load_db_stats()
                            with state_lock:
                                current_state["db_total_samples"] = n_samples
                                current_state["db_total_probes"]  = n_probes

                        time.sleep(POLL_INTERVAL)

                    except Exception as inner_e:
                        print(f"[POLL ERROR] {inner_e}")
                        with state_lock:
                            current_state["modem_status"] = "reconnecting"
                            current_state["status"]       = "No Internet"
                        break

            conn.commit()
            conn.close()

        except Exception as outer_e:
            err_msg = str(outer_e)
            consecutive_login_failures += 1
            backoff_sec = min(300, 5 * (2 ** min(consecutive_login_failures, 6)))

            log_desc = f"[MODEM AUTH ERROR] {err_msg}"
            if "108007" in err_msg or "overrun" in err_msg.lower():
                backoff_sec = max(backoff_sec, 60)
                log_desc = "[CREDENTIAL LOCKOUT] Error 108007: Modem login locked out due to multiple failed password attempts."
                print(f"[CONNECTION WARNING] Modem rate limit/lockout detected: {err_msg} — backing off for {backoff_sec}s...")
            else:
                print(f"[CONNECTION ERROR] {err_msg} — retrying in {backoff_sec}s...")

            record_diagnostic_log("Modem Auth", log_desc, is_error=True)

            with state_lock:
                current_state["modem_status"] = "reconnecting"
                current_state["status"]       = "No Internet"
            time.sleep(backoff_sec)


lifetime_cache_lock = threading.Lock()
lifetime_cache_data = {"ts": 0, "result": None}


# ─── Query Helpers ────────────────────────────────────────────────────────────
def query_samples(range_str, min_ts=None, max_ts=None):
    now_ts        = time.time()
    rng_lower     = range_str.lower()
    range_seconds = RANGE_SECONDS_MAP.get(rng_lower, 86400)

    # Custom time range explicit min_ts and max_ts
    if min_ts is not None and max_ts is not None:
        try:
            min_ts_val = float(min_ts)
            max_ts_val = float(max_ts)
            range_sec = max_ts_val - min_ts_val

            with cache_lock:
                filtered = [s for s in sample_cache if min_ts_val <= s["ts"] <= max_ts_val]
                daily    = dict(daily_cache)

            if not filtered or (sample_cache and (min_ts_val < sample_cache[0]["ts"] or max_ts_val > sample_cache[-1]["ts"])):
                conn = get_db()
                if range_sec > 86400:
                    bkt = max(1, int(range_sec // 400))
                    sql = f"""
                        SELECT 
                            (ts / {bkt}) * {bkt} AS ts,
                            MAX(dl_mbps) as dl_mbps, MAX(ul_mbps) as ul_mbps,
                            SUM(dl_bytes_delta) as dl_bytes_delta, SUM(ul_bytes_delta) as ul_bytes_delta,
                            MAX(today_dl_gb) as today_dl_gb, MAX(today_ul_gb) as today_ul_gb,
                            MAX(lifetime_dl_gb) as lifetime_dl_gb, MAX(lifetime_ul_gb) as lifetime_ul_gb,
                            MAX(date) as date,
                            CASE 
                                WHEN SUM(CASE WHEN status_label = 'Throttled' THEN 1 ELSE 0 END) > 0 THEN 'Throttled'
                                WHEN SUM(CASE WHEN status_label = 'No Internet' THEN 1 ELSE 0 END) > 0 THEN 'No Internet'
                                ELSE 'Unthrottled'
                            END as status_label
                        FROM traffic_samples
                        WHERE ts >= ? AND ts <= ?
                        GROUP BY (ts / {bkt}) ORDER BY ts ASC
                    """
                    rows = conn.execute(sql, (int(min_ts_val), int(max_ts_val))).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT ts, dl_mbps, ul_mbps, dl_bytes_delta, ul_bytes_delta,
                               today_dl_gb, today_ul_gb, lifetime_dl_gb, lifetime_ul_gb, date,
                               status_label
                        FROM traffic_samples WHERE ts >= ? AND ts <= ? ORDER BY ts ASC
                    """, (int(min_ts_val), int(max_ts_val))).fetchall()
                daily_rows = conn.execute("SELECT * FROM daily_stats ORDER BY date ASC").fetchall()
                conn.close()
                filtered = [dict(r) for r in rows]
                daily    = {r["date"]: dict(r) for r in daily_rows}
        except Exception as e:
            print(f"[QUERY SAMPLES CUSTOM ERROR] {e}")
            filtered = []
            daily = {}
    elif rng_lower == "lifetime":
        # Fast 10-second RAM cache for lifetime / all-data queries (0ms latency)
        with lifetime_cache_lock:
            if lifetime_cache_data["result"] is not None and (now_ts - lifetime_cache_data["ts"]) < 30:
                return lifetime_cache_data["result"]
        conn = get_db()
        row = conn.execute("SELECT MIN(ts), MAX(ts) FROM traffic_samples").fetchone()
        bkt = max(1, (row[1] - row[0]) // 400) if row and row[0] and row[1] else 1
        sql = f"""
            SELECT 
                (ts / {bkt}) * {bkt} AS ts,
                MAX(dl_mbps) as dl_mbps, MAX(ul_mbps) as ul_mbps,
                SUM(dl_bytes_delta) as dl_bytes_delta, SUM(ul_bytes_delta) as ul_bytes_delta,
                MAX(today_dl_gb) as today_dl_gb, MAX(today_ul_gb) as today_ul_gb,
                MAX(lifetime_dl_gb) as lifetime_dl_gb, MAX(lifetime_ul_gb) as lifetime_ul_gb,
                MAX(date) as date,
                CASE 
                    WHEN SUM(CASE WHEN status_label = 'Throttled' THEN 1 ELSE 0 END) > 0 THEN 'Throttled'
                    WHEN SUM(CASE WHEN status_label = 'No Internet' THEN 1 ELSE 0 END) > 0 THEN 'No Internet'
                    ELSE 'Unthrottled'
                END as status_label
            FROM traffic_samples
            GROUP BY (ts / {bkt}) ORDER BY ts ASC
        """
        rows = conn.execute(sql).fetchall()
        daily_rows = conn.execute("SELECT * FROM daily_stats ORDER BY date ASC").fetchall()
        conn.close()
        filtered = [dict(r) for r in rows]
        daily    = {r["date"]: dict(r) for r in daily_rows}
    elif rng_lower == "today":
        midnight = get_local_datetime().replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff_ts = midnight.timestamp()

        with cache_lock:
            filtered = [s for s in sample_cache if s["ts"] >= cutoff_ts]
            daily    = dict(daily_cache)
    elif range_seconds <= 86400 and range_seconds > 0:
        with cache_lock:
            cutoff   = now_ts - range_seconds
            filtered = [s for s in sample_cache if s["ts"] >= cutoff]
            daily    = dict(daily_cache)
    else:
        conn = get_db()
        cutoff = int(now_ts) - range_seconds
        bkt = max(1, int(range_seconds // 400))
        sql = f"""
            SELECT 
                (ts / {bkt}) * {bkt} AS ts,
                MAX(dl_mbps) as dl_mbps, MAX(ul_mbps) as ul_mbps,
                SUM(dl_bytes_delta) as dl_bytes_delta, SUM(ul_bytes_delta) as ul_bytes_delta,
                MAX(today_dl_gb) as today_dl_gb, MAX(today_ul_gb) as today_ul_gb,
                MAX(lifetime_dl_gb) as lifetime_dl_gb, MAX(lifetime_ul_gb) as lifetime_ul_gb,
                MAX(date) as date,
                CASE 
                    WHEN SUM(CASE WHEN status_label = 'Throttled' THEN 1 ELSE 0 END) > 0 THEN 'Throttled'
                    WHEN SUM(CASE WHEN status_label = 'No Internet' THEN 1 ELSE 0 END) > 0 THEN 'No Internet'
                    ELSE 'Unthrottled'
                END as status_label
            FROM traffic_samples
            WHERE ts >= ?
            GROUP BY (ts / {bkt}) ORDER BY ts ASC
        """
        rows = conn.execute(sql, (cutoff,)).fetchall()
        daily_rows = conn.execute("SELECT * FROM daily_stats ORDER BY date ASC").fetchall()
        conn.close()
        filtered = [dict(r) for r in rows]
        daily    = {r["date"]: dict(r) for r in daily_rows}

    if not filtered:
        return [], {}, daily

    stats = {
        "window_dl_bytes": sum(s.get("dl_bytes_delta", 0) for s in filtered),
        "window_ul_bytes": sum(s.get("ul_bytes_delta", 0) for s in filtered),
        "peak_dl_mbps":    round(max((s["dl_mbps"] for s in filtered), default=0.0), 2),
        "peak_ul_mbps":    round(max((s["ul_mbps"] for s in filtered), default=0.0), 2),
        "avg_dl_mbps":     round(sum(s["dl_mbps"] for s in filtered) / len(filtered), 2) if filtered else 0.0,
        "avg_ul_mbps":     round(sum(s["ul_mbps"] for s in filtered) / len(filtered), 2) if filtered else 0.0,
        "sample_count":    len(filtered),
    }

    target = 400
    if len(filtered) <= target:
        res_tuple = (filtered, stats, daily)
    else:
        bkt    = len(filtered) / target
        result = []
        for i in range(target):
            s = int(i * bkt); e = int((i + 1) * bkt)
            bucket = filtered[s:e]
            if not bucket: continue
            mid = bucket[len(bucket) // 2]

            if any(x.get("status_label") == "Throttled" for x in bucket):
                bkt_status = "Throttled"
            elif any(x.get("status_label") == "No Internet" for x in bucket):
                bkt_status = "No Internet"
            else:
                bkt_status = mid.get("status_label", "Unthrottled")

            result.append({
                "ts":             mid["ts"],
                "dl_mbps":        round(max(x["dl_mbps"] for x in bucket), 3),
                "ul_mbps":        round(max(x["ul_mbps"] for x in bucket), 3),
                "dl_bytes_delta": sum(x.get("dl_bytes_delta", 0) for x in bucket),
                "ul_bytes_delta": sum(x.get("ul_bytes_delta", 0) for x in bucket),
                "today_dl_gb":    bucket[-1].get("today_dl_gb", 0),
                "today_ul_gb":    bucket[-1].get("today_ul_gb", 0),
                "lifetime_dl_gb": bucket[-1].get("lifetime_dl_gb", 0),
                "lifetime_ul_gb": bucket[-1].get("lifetime_ul_gb", 0),
                "date":           mid.get("date", ""),
                "status_label":   bkt_status,
            })
        res_tuple = (result, stats, daily)

    if rng_lower == "lifetime" and min_ts is None:
        with lifetime_cache_lock:
            lifetime_cache_data["ts"] = now_ts
            lifetime_cache_data["result"] = res_tuple

    return res_tuple


def export_csv(days=30):
    """Generate a clean CSV string of all WAN traffic samples and active speed probes."""
    conn = get_db()
    if days > 0:
        cutoff = int(time.time()) - days * 86400
        rows = conn.execute("""
            SELECT ts, dl_mbps, ul_mbps, dl_bytes_delta, ul_bytes_delta,
                   today_dl_gb, today_ul_gb, lifetime_dl_gb, lifetime_ul_gb,
                   signal_icon, network_type, status_label
            FROM traffic_samples WHERE ts >= ? ORDER BY ts ASC
        """, (cutoff,)).fetchall()
        probes = conn.execute("""
            SELECT ts, dl_mbps, today_dl_gb, today_ul_gb, lifetime_dl_gb, lifetime_ul_gb,
                   is_throttled, status_desc
            FROM speed_probes WHERE ts >= ? ORDER BY ts ASC
        """, (cutoff,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT ts, dl_mbps, ul_mbps, dl_bytes_delta, ul_bytes_delta,
                   today_dl_gb, today_ul_gb, lifetime_dl_gb, lifetime_ul_gb,
                   signal_icon, network_type, status_label
            FROM traffic_samples ORDER BY ts ASC
        """).fetchall()
        probes = conn.execute("""
            SELECT ts, dl_mbps, today_dl_gb, today_ul_gb, lifetime_dl_gb, lifetime_ul_gb,
                   is_throttled, status_desc
            FROM speed_probes ORDER BY ts ASC
        """).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["=== PLDT H153-381 Modem WAN Traffic Data ==="])
    writer.writerow([f"Exported: {get_local_datetime().strftime('%Y-%m-%d %H:%M:%S')}",
                     f"Period: Last {days} days" if days else "Period: All time",
                     f"Samples: {len(rows)}"])
    writer.writerow([])
    writer.writerow(["DateTime (Local)", "Download Mbps", "Upload Mbps",
                     "DL Bytes (2s)", "UL Bytes (2s)",
                     "Today DL GB", "Today UL GB",
                     "Lifetime DL GB", "Lifetime UL GB",
                     "Signal", "Network", "Status"])
    for r in rows:
        dt = get_local_datetime(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        sig = r["signal_icon"] if "signal_icon" in r.keys() else ""
        net = r["network_type"] if "network_type" in r.keys() else ""
        status = r["status_label"] if "status_label" in r.keys() else "Unthrottled"
        writer.writerow([dt, r["dl_mbps"], r["ul_mbps"],
                         r["dl_bytes_delta"], r["ul_bytes_delta"],
                         r["today_dl_gb"], r["today_ul_gb"],
                         r["lifetime_dl_gb"], r["lifetime_ul_gb"],
                         sig, net, status])

    if probes:
        writer.writerow([])
        writer.writerow(["=== AUTOMATED ACTIVE SPEED PROBES (Every 15 mins) ==="])
        writer.writerow(["DateTime (Local)", "Tested Download Mbps", "Today DL GB", "Today UL GB",
                         "Lifetime DL GB", "Lifetime UL GB", "Throttled Status", "Description"])
        for p in probes:
            dt = get_local_datetime(p["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            st = "Throttled" if p["is_throttled"] else "Unthrottled"
            writer.writerow([dt, p["dl_mbps"], p["today_dl_gb"], p["today_ul_gb"],
                             p["lifetime_dl_gb"], p["lifetime_ul_gb"], st, p["status_desc"]])

    return buf.getvalue()


# ─── HTTP Handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        path   = parsed.path

        if path == "/api/traffic":
            with state_lock:
                snapshot = dict(current_state)
            with probe_lock:
                snapshot["probe_running"] = probe_running
            body = json.dumps(snapshot, separators=(',', ':')).encode()
            self._json(body)

        elif path == "/api/history":
            rng                   = qs.get("range", ["1d"])[0]
            min_ts                = qs.get("min_ts", [None])[0]
            max_ts                = qs.get("max_ts", [None])[0]
            samples, stats, daily = query_samples(rng, min_ts=min_ts, max_ts=max_ts)
            with state_lock:
                throttle_cap = current_state["throttle_cap_mbps"]
            body = json.dumps({
                "range":             rng,
                "samples":           samples,
                "stats":             stats,
                "daily":             daily,
                "throttle_cap_mbps": throttle_cap,
            }, separators=(',', ':')).encode()
            self._json(body)

        elif path == "/api/run_speed_test":
            try:
                started = launch_probe_if_free()
                if started:
                    body = json.dumps({"status": "started", "message": "Speed test probe launched!"}, separators=(',', ':')).encode()
                else:
                    body = json.dumps({"status": "running", "message": "Speed test probe is already in progress..."}, separators=(',', ':')).encode()
                self._json(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        elif path == "/api/speed_probes":
            conn = get_db()
            rows = conn.execute("SELECT * FROM speed_probes ORDER BY ts DESC LIMIT 100").fetchall()
            conn.close()
            probes = [dict(r) for r in rows]
            body   = json.dumps({"probes": probes}, separators=(',', ':')).encode()
            self._json(body)

        elif path == "/api/reset_db":
            try:
                reset_database()
                body = json.dumps({"status": "reset_success"}, separators=(',', ':')).encode()
                self._json(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        elif path == "/api/export/csv":
            days = int(qs.get("days", [30])[0])
            try:
                csv_data = export_csv(days)
                fname    = f"isp_evidence_{get_local_datetime().strftime('%Y%m%d')}_last{days}d.csv"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(csv_data.encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        else:
            fname = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            fpath = os.path.join(BASE_DIR, fname)
            if os.path.isfile(fpath):
                ct = ("text/html; charset=utf-8"              if fname.endswith(".html") else
                      "text/css; charset=utf-8"               if fname.endswith(".css")  else
                      "application/javascript; charset=utf-8" if fname.endswith(".js")   else
                      "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                with open(fpath, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()

    def _json(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return  # suppress access log noise


def run_server(port: int = 8085):
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[HTTP] Dashboard listening at http://0.0.0.0:{port}")
    srv.serve_forever()


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    load_cache_from_db()
    n_s, n_p = load_db_stats()
    with state_lock:
        current_state["db_total_samples"] = n_s
        current_state["db_total_probes"]  = n_p
    print(f"[DB] {n_s:,} total samples, {n_p} speed probes on record.")
    
    t_modem = threading.Thread(target=poll_modem_loop, daemon=True)
    t_modem.start()
    
    t_probe = threading.Thread(target=periodic_speed_probe_loop, daemon=True)
    t_probe.start()

    t_maint = threading.Thread(target=auto_prune_database_loop, daemon=True)
    t_maint.start()
    
    port_to_use = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8085))
    try:
        run_server(port_to_use)
    except KeyboardInterrupt:
        print("\n[EXIT] Shutting down.")
        sys.exit(0)
