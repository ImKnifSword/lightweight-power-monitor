#!/usr/bin/env python3
"""
Lightweight Power Monitor - minimal resource usage power monitoring web app.
Backend: built-in http.server + simple JSON API.
"""

import os
import time
import json
import subprocess
import re
import threading
from threading import Thread
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler


CONFIG = {
    "host": "0.0.0.0",
    "port": 5000,
    "bind": "0.0.0.0:5000",
    "cpu_tdp_w": 65,
    "idle_power_w": 15,
    "sample_interval_s": 2,
    "history_max": 1800,
    "kwh_price": 0.65,
    "processes_top_n": 8,
}

HISTORY_LOCK = threading.Lock()
history = []

_start_time = time.time()
_last_cpu_times = read_cpu_times_helper() if False else None


def read_cpu_times_helper() -> list[int] | None:
    try:
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.strip().split()
                    return [int(x) for x in parts[1:]]
    except Exception:
        return None


def get_cpu_load() -> float:
    global _last_cpu_times
    times = read_cpu_times_helper()
    if times is None or _last_cpu_times is None:
        _last_cpu_times = times
        return 0.0
    total_delta = sum(times) - sum(_last_cpu_times)
    idle_delta = times[3] - _last_cpu_times[3]
    _last_cpu_times = times
    if total_delta <= 0:
        return 0.0
    idle_ratio = idle_delta / total_delta
    return max(0.0, min(1.0, 1.0 - idle_ratio))


def get_cpu_count() -> int:
    return os.cpu_count() or 1


# ---------------------------------------------------------------------------
# Power reading with fallbacks
# ---------------------------------------------------------------------------
def read_powercap_intel_rapl() -> float | None:
    base_path = "/sys/class/powercap/intel-rapl"
    if not os.path.isdir(base_path):
        return None
    total = 0.0
    found = False
    try:
        for entry in sorted(os.listdir(base_path)):
            pkg = os.path.join(base_path, entry)
            power_file = os.path.join(pkg, "power_uw")
            if os.path.isfile(power_file):
                try:
                    with open(power_file, "r") as f:
                        total += int(f.read().strip()) / 1_000_000.0
                        found = True
                except Exception:
                    pass
    except Exception:
        return None
    return total if found else None


def read_power_supply() -> float | None:
    base_path = "/sys/class/power_supply/"
    if not os.path.isdir(base_path):
        return None
    try:
        for entry in sorted(os.listdir(base_path)):
            p = os.path.join(base_path, entry)
            type_file = os.path.join(p, "type")
            if not os.path.isfile(type_file):
                continue
            with open(type_file, "r") as f:
                t = f.read().strip()
            if t in ("Mains", "USB", "USBPD"):
                power_file = os.path.join(p, "power_now")
                if os.path.isfile(power_file):
                    with open(power_file, "r") as f:
                        val = f.read().strip()
                    if val.isdigit():
                        return int(val) / 1_000_000.0
    except Exception:
        pass
    return None


def read_lm_sensors() -> float | None:
    total = 0.0
    try:
        r = subprocess.run(["sensors"], capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            return None
        matches = re.findall(r"([0-9]+\.[0-9]+)\s*(m?W)", r.stdout, re.IGNORECASE)
        for val, unit in matches:
            v = float(val)
            if unit.lower().startswith("m"):
                v /= 1000.0
            total += v
    except Exception:
        return None
    return total if total > 0 else None


def estimate_power_from_cpu(load01: float) -> float:
    est = CONFIG["idle_power_w"] + load01 * CONFIG["cpu_tdp_w"]
    return max(CONFIG["idle_power_w"], min(est, CONFIG["cpu_tdp_w"] * 1.3))


def read_power_with_fallback(load01: float) -> tuple[float, str]:
    v = read_powercap_intel_rapl()
    if v is not None and v > 0:
        return round(v, 2), "Intel RAPL"
    v = read_power_supply()
    if v is not None and v > 0:
        return round(v, 2), "AC adapter"
    try:
        r = subprocess.run(["which", "sensors"], capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            v = read_lm_sensors()
            if v is not None and v > 0:
                return round(v, 2), "lm-sensors"
    except Exception:
        pass
    return round(estimate_power_from_cpu(load01), 2), "estimated"


# ---------------------------------------------------------------------------
# Process tracking & estimation
# ---------------------------------------------------------------------------
PROCESSES_LOCK = threading.Lock()
cached_processes = []
cached_processes_generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
cached_processes_source = "estimated"

_prev_procs = {}
_prev_sys_cpu = None


def read_process_list() -> list[dict]:
    processes = []
    try:
        pids = [int(x) for x in os.listdir("/proc") if x.isdigit()]
    except Exception:
        return []

    for pid in pids:
        # Read stat
        try:
            with open(f"/proc/{pid}/stat", "r") as f:
                stat_line = f.read().strip()
        except (FileNotFoundError, PermissionError):
            continue

        rparen = stat_line.rfind(")")
        if rparen == -1:
            continue
        name = stat_line[stat_line.find("(") + 1 : rparen]
        rest = stat_line[rparen + 2 :].split()
        if len(rest) < 13:
            continue
        try:
            cpu_time = int(rest[11]) + int(rest[12])
        except ValueError:
            continue

        # Read cmdline
        try:
            with open(f"/proc/{pid}/cmdline", "r", errors="replace") as f:
                cmdline = f.read().replace("\x00", " ").strip()
        except (FileNotFoundError, PermissionError):
            cmdline = ""

        if not cmdline:
            continue
        if name == "kthreadd" or (name.startswith("[") and name.endswith("]")):
            continue

        # Read status
        memory_mb = None
        try:
            with open(f"/proc/{pid}/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            try:
                                memory_mb = round(int(parts[1]) / 1024.0, 2)
                            except ValueError:
                                pass
                        break
        except (FileNotFoundError, PermissionError):
            pass

        # Read io
        rchar = None
        wchar = None
        try:
            with open(f"/proc/{pid}/io", "r") as f:
                for line in f:
                    if line.startswith("rchar:"):
                        try:
                            rchar = int(line.split()[1])
                        except (ValueError, IndexError):
                            pass
                    elif line.startswith("wchar:"):
                        try:
                            wchar = int(line.split()[1])
                        except (ValueError, IndexError):
                            pass
        except (FileNotFoundError, PermissionError):
            pass

        proc = {
            "pid": pid,
            "name": name,
            "cmdline": cmdline,
            "cpu_time": cpu_time,
        }
        if memory_mb is not None:
            proc["memory_mb"] = memory_mb
        if rchar is not None:
            proc["rchar"] = rchar
        if wchar is not None:
            proc["wchar"] = wchar

        processes.append(proc)
    return processes


def estimate_process_power() -> list[dict]:
    global _prev_procs, _prev_sys_cpu
    
    sys_cpu_2 = read_cpu_times_helper()
    procs_2 = {p["pid"]: p for p in read_process_list()}
    
    if sys_cpu_2 is None:
        return []
        
    if _prev_sys_cpu is None or not _prev_procs:
        _prev_sys_cpu = sys_cpu_2
        _prev_procs = {pid: p["cpu_time"] for pid, p in procs_2.items()}
        time.sleep(0.5)
        sys_cpu_2 = read_cpu_times_helper()
        procs_2 = {p["pid"]: p for p in read_process_list()}
        if sys_cpu_2 is None:
            return []
            
    sys_delta = sum(sys_cpu_2) - sum(_prev_sys_cpu)
    
    idle_delta = sys_cpu_2[3] - _prev_sys_cpu[3]
    if sys_delta > 0:
        load01 = max(0.0, min(1.0, 1.0 - (idle_delta / sys_delta)))
    else:
        load01 = 0.0
        
    system_watts, _ = read_power_with_fallback(load01)
    num_cores = get_cpu_count()
    
    estimated_procs = []
    for pid, p2 in procs_2.items():
        p1_cpu = _prev_procs.get(pid)
        if p1_cpu is not None:
            cpu_delta = p2["cpu_time"] - p1_cpu
        else:
            cpu_delta = 0
            
        if cpu_delta < 0:
            cpu_delta = 0
            
        if sys_delta > 0:
            process_cpu_ratio_of_total = cpu_delta / sys_delta
        else:
            process_cpu_ratio_of_total = 0.0
            
        est_watts = process_cpu_ratio_of_total * system_watts
        cpu_pct = process_cpu_ratio_of_total * 100.0 * num_cores
        
        d = {
            "pid": pid,
            "name": p2["name"],
            "cmdline": p2["cmdline"],
            "estimated_watts": round(est_watts, 2),
            "cpu_pct": round(cpu_pct, 1),
        }
        if "memory_mb" in p2:
            d["memory_mb"] = p2["memory_mb"]
        if "rchar" in p2:
            d["rchar"] = p2["rchar"]
        if "wchar" in p2:
            d["wchar"] = p2["wchar"]
            
        estimated_procs.append(d)
        
    # Update global previous state
    _prev_sys_cpu = sys_cpu_2
    _prev_procs = {pid: p["cpu_time"] for pid, p in procs_2.items()}
    
    estimated_procs.sort(key=lambda x: x["estimated_watts"], reverse=True)
    top_n = CONFIG.get("processes_top_n", 8)
    return estimated_procs[:top_n]


# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------
def estimate_cost_daily(power_w: float) -> float:
    return (power_w / 1000.0) * 24.0 * CONFIG["kwh_price"]


def estimate_cost_monthly(power_w: float) -> float:
    return estimate_cost_daily(power_w) * 30.0


# ---------------------------------------------------------------------------
# Background sampler
# ---------------------------------------------------------------------------
def sampler_loop():
    global history, _last_cpu_times
    prev_cpu = read_cpu_times_helper()
    last_ts = time.time()
    cum_kwh = 0.0
    while True:
        now = time.time()
        cur_cpu = read_cpu_times_helper()
        load01 = 0.0
        if prev_cpu is not None and cur_cpu is not None:
            total_delta = sum(cur_cpu) - sum(prev_cpu)
            idle_delta = cur_cpu[3] - prev_cpu[3]
            prev_cpu = cur_cpu
            if total_delta > 0:
                load01 = max(0.0, min(1.0, 1.0 - (idle_delta / total_delta)))
        watts, method = read_power_with_fallback(load01)
        cpu_load_pct = round(load01 * 100.0, 1)

        dt_hours = max(0.0, (now - last_ts)) / 3600.0
        cum_kwh += (watts / 1000.0) * dt_hours
        last_ts = now

        if cpu_load_pct > 80:
            status = "High"
        elif cpu_load_pct > 40:
            status = "Medium"
        elif cpu_load_pct > 10:
            status = "Low"
        else:
            status = "Idle"

        sample = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "watts": round(watts, 2),
            "cpu_load": cpu_load_pct,
            "method": method,
        }
        with HISTORY_LOCK:
            history.append(sample)
            if len(history) > CONFIG["history_max"]:
                history = history[-CONFIG["history_max"] :]
        
        try:
            procs = estimate_process_power()
            source = "rapl" if (read_powercap_intel_rapl() or 0) > 0 else "estimated"
            with PROCESSES_LOCK:
                global cached_processes, cached_processes_generated_at, cached_processes_source
                cached_processes = procs
                cached_processes_generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                cached_processes_source = source
        except Exception:
            pass

        time.sleep(CONFIG["sample_interval_s"])


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class PowerMonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self.send_html()
        if self.path == "/api/status":
            return self.send_json(status_payload())
        if self.path == "/api/history":
            return self.send_json({"history": history_snapshot(400)})
        if self.path == "/api/config":
            safe = {k: v for k, v in CONFIG.items() if k not in ("host", "port", "bind")}
            return self.send_json(safe)
        if self.path.startswith("/api/stats/"):
            seconds = min(900, max(10, int(self.path.rsplit("/", 1)[-1])))
            return self.send_json(window_stats(seconds))
        if self.path == "/api/stats":
            return self.send_json(window_stats(300))
        if self.path == "/api/export.csv":
            return self.send_csv()
        if self.path == "/api/uptime":
            return self.send_json({"uptime_s": round(time.time() - _start_time, 1)})
        if self.path == "/api/processes":
            with PROCESSES_LOCK:
                data = {
                    "processes": cached_processes,
                    "generated_at": cached_processes_generated_at,
                    "source": cached_processes_source
                }
            return self.send_json(data)
        self.send_response(404)
        self.end_headers()

    def send_json(self, data):
        payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self):
        html = HTML_TEMPLATE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def send_csv(self):
        with HISTORY_LOCK:
            rows = list(history)
        lines = ["ts,watts,cpu_load,method"]
        for s in rows:
            lines.append(f"{s['ts']},{s['watts']},{s['cpu_load']},{s['method']}")
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


# ---------------------------------------------------------------------------
# Derived snapshots
# ---------------------------------------------------------------------------
def _parse_ts(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def history_snapshot(limit: int) -> list[dict]:
    with HISTORY_LOCK:
        return history[-limit:]


def window_stats(seconds: int) -> dict:
    cutoff = time.time() - seconds
    subset = []
    with HISTORY_LOCK:
        subset = [s for s in history if _parse_ts(s["ts"]) >= cutoff][:4000]
    if not subset:
        return {"window_s": seconds, "samples": 0, "avg_w": 0.0, "min_w": 0.0, "max_w": 0.0, "avg_cpu": 0.0}
    ws = [s["watts"] for s in subset]
    cs = [s["cpu_load"] for s in subset]
    return {
        "window_s": seconds,
        "samples": len(ws),
        "avg_w": round(sum(ws) / len(ws), 2),
        "min_w": round(min(ws), 2),
        "max_w": round(max(ws), 2),
        "avg_cpu": round(sum(cs) / len(cs), 1),
    }


_last_status = {
    "watts": 0.0,
    "kwh": 0.0,
    "cpu_load": 0.0,
    "cpu_cores": get_cpu_count(),
    "status": "Idle",
    "method": "none",
    "cost_daily": 0.0,
    "cost_monthly": 0.0,
    "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}


def status_payload() -> dict:
    global _last_status
    if not history:
        return dict(_last_status)
    with HISTORY_LOCK:
        last = history[-1]
    watts = last["watts"]
    cpu_load = last["cpu_load"]
    method = last["method"]

    cum_kwh = 0.0
    if len(history) >= 2:
        with HISTORY_LOCK:
            subset = history[-400:]
        for i in range(1, len(subset)):
            dt = (_parse_ts(subset[i]["ts"]) - _parse_ts(subset[i - 1]["ts"]))
            if 0 < dt < 600:
                avg_w = (subset[i]["watts"] + subset[i - 1]["watts"]) / 2.0
                cum_kwh += avg_w * dt / 3600.0
    kwh = round(cum_kwh, 5)

    if cpu_load > 80:
        status = "High"
    elif cpu_load > 40:
        status = "Medium"
    elif cpu_load > 10:
        status = "Low"
    else:
        status = "Idle"

    payload = {
        "watts": round(watts, 2),
        "kwh": kwh,
        "cpu_load": round(cpu_load, 1),
        "cpu_cores": get_cpu_count(),
        "status": status,
        "method": method,
        "cost_daily": round(estimate_cost_daily(watts), 2),
        "cost_monthly": round(estimate_cost_monthly(watts), 2),
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _last_status = payload
    return payload


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Lightweight Power Monitor</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, Segoe UI, Arial, sans-serif;
    background: #f5f7fa;
    color: #1f2937;
    margin: 0;
    padding: 16px;
  }
  .proc-table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 8px;
  }
  .proc-table th, .proc-table td {
    padding: 8px;
    text-align: left;
    border-bottom: 1px solid #e5e7eb;
  }
  .proc-table th {
    font-weight: 600;
    color: #6b7280;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .proc-table tbody tr:hover {
    background: #f9fafb;
  }
  .text-right { text-align: right; }
  .cmd-excerpt {
    font-family: monospace;
    color: #4b5563;
    max-width: 250px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }
  h1 { margin: 0; font-size: 20px; letter-spacing: 0.2px; }
  .badge {
    font-size: 12px;
    color: #6b7280;
    background: #eef2ff;
    border: 1px solid #e5e7eb;
    padding: 4px 8px;
    border-radius: 999px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
    margin-bottom: 14px;
  }
  .card {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 14px 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }
  .card h2 {
    margin: 0 0 6px;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: #6b7280;
  }
  .card .value { margin: 0; font-size: 24px; font-weight: 700; }
  .card .sub { margin-top: 4px; font-size: 12px; color: #6b7280; }
  .chart-wrap {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 14px 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    margin-bottom: 12px;
  }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  footer { margin-top: 12px; color: #6b7280; font-size: 12px; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .small { font-size: 12px; color: #6b7280; }
</style>
</head>
<body>
<header>
  <h1>⚡ Lightweight Power Monitor</h1>
  <span class="badge" id="method">loading method…</span>
</header>
<section class="grid">
  <article class="card">
    <h2>Power</h2>
    <p class="value" id="power">—</p>
    <div class="sub">W</div>
  </article>
  <article class="card">
    <h2>Energy</h2>
    <p class="value" id="energy">—</p>
    <div class="sub">kWh</div>
  </article>
  <article class="card">
    <h2>CPU Load</h2>
    <p class="value" id="cpu">—</p>
    <div class="sub">Cores: <span id="cores">—</span></div>
  </article>
  <article class="card">
    <h2>Status</h2>
    <p class="value" id="status">—</p>
    <div class="sub">Sample method</div>
  </article>
  <article class="card">
    <h2>Est. Daily</h2>
    <p class="value" id="day">—</p>
    <div class="sub">PLN/day</div>
  </article>
  <article class="card">
    <h2>Est. Monthly</h2>
    <p class="value" id="month">—</p>
    <div class="sub">PLN/month</div>
  </article>
</section>

<section class="chart-wrap">
  <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:8px;">
    <div>
      <strong>Power</strong>
      <span class="small" id="power_window">window: last 10 min</span>
    </div>
    <div class="small">
      <span id="stats_samples">samples: 0</span> ·
      avg <span id="stats_avg">—</span> ·
      min <span id="stats_min">—</span> ·
      max <span id="stats_max">—</span>
    </div>
  </div>
  <canvas id="chart" aria-label="Power usage over time" role="img"></canvas>
</section>

<section class="chart-wrap">
  <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:8px;">
    <div><strong>CPU load</strong><span class="small" id="cpu_window">window: last 10 min</span></div>
    <div class="small">avg <span id="cpu_avg">—</span> %</div>
  </div>
  <canvas id="cpu_chart" aria-label="CPU load over time" role="img"></canvas>
</section>

<section class="chart-wrap">
  <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:8px;">
    <div><strong>Top processes</strong><span class="small" id="processes_source" style="margin-left: 8px;">source: estimated</span></div>
    <div class="small" id="processes_updated">updated: —</div>
  </div>
  <div style="overflow-x:auto;">
    <table class="proc-table">
      <thead>
        <tr>
          <th style="width: 50px;">Rank</th>
          <th>Name</th>
          <th class="text-right" style="width: 100px;">Power</th>
          <th class="text-right" style="width: 80px;">CPU %</th>
          <th class="text-right" style="width: 100px;">Memory</th>
          <th>Command Excerpt</th>
        </tr>
      </thead>
      <tbody id="processes_table_body">
        <tr>
          <td colspan="6" style="text-align: center; color: #6b7280; padding: 16px;">Loading processes...</td>
        </tr>
      </tbody>
    </table>
  </div>
</section>

<footer>
  <div class="row" style="justify-content:space-between;align-items:center;">
    <div>
      Auto-refresh every 3s · Source: <span id="src">—</span> · Updated: <span id="updated">—</span>
    </div>
    <div>
      <a href="/api/export.csv">CSV export</a>
      <span class="small"> · uptime <span id="uptime">—</span></span>
    </div>
  </div>
</footer>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js">
</script>
<script>
const REFRESH_MS = 3000;
function fmt(n, d=2) {
  if (n == null || isNaN(n)) return '—';
  if (Math.abs(n) < 1 && d < 3) d = 3;
  return Number(n).toFixed(d);
}
const labels = [];
const data = [];
const ctx = document.getElementById('chart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: { labels, datasets: [{ label: 'Power (W)', data, fill: true, tension: 0.25, borderWidth: 2, pointRadius: 0, backgroundColor: 'rgba(37,99,235,0.12)', borderColor: '#2563eb' }] },
  options: {
    animation: false,
    plugins: { legend: { display: false } },
    scales: { x: { display: true, ticks: { maxTicksLimit: 6 } }, y: { beginAtZero: false, suggestedMin: 0 } },
    maintainAspectRatio: true
  }
});

const cpuLabels = [];
const cpuData = [];
const cpuCtx = document.getElementById('cpu_chart').getContext('2d');
const cpuChart = new Chart(cpuCtx, {
  type: 'line',
  data: { labels: cpuLabels, datasets: [{ label: 'CPU %', data: cpuData, fill: true, tension: 0.2, borderWidth: 2, pointRadius: 0, backgroundColor: 'rgba(16,185,129,0.12)', borderColor: '#10b981' }] },
  options: {
    animation: false,
    plugins: { legend: { display: false } },
    scales: { x: { display: true, ticks: { maxTicksLimit: 6 } }, y: { min: 0, max: 100 } },
    maintainAspectRatio: true
  }
});

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

async function update() {
  try {
    const status = await fetchJSON('/api/status');
    document.getElementById('power').textContent = fmt(status.watts, 1) + ' W';
    document.getElementById('energy').textContent = fmt(status.kwh, 3);
    document.getElementById('cpu').textContent = fmt(status.cpu_load, 0);
    document.getElementById('cores').textContent = status.cpu_cores ?? '—';
    document.getElementById('status').textContent = status.status ?? 'Idle';
    document.getElementById('day').textContent = fmt(status.cost_daily, 2);
    document.getElementById('month').textContent = fmt(status.cost_monthly, 2);
    document.getElementById('src').textContent = status.method ?? '—';
    document.getElementById('method').textContent = status.method ?? '—';
    document.getElementById('updated').textContent = status.updated ? new Date(status.updated).toLocaleString() : '—';
  } catch (e) {
    console.warn('status failed', e);
  }
  try {
    const h = await fetchJSON('/api/history');
    labels.length = 0;
    data.length = 0;
    cpuLabels.length = 0;
    cpuData.length = 0;
    for (const s of h.history.slice(-120)) {
      const t = new Date(s.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      labels.push(t);
      data.push(s.watts);
      cpuLabels.push(t);
      cpuData.push(s.cpu_load);
    }
    chart.update('none');
    cpuChart.update('none');
  } catch (e) {
    console.warn('history failed', e);
  }
  try {
    const stats = await fetchJSON('/api/stats/600');
    document.getElementById('stats_samples').textContent = 'samples: ' + (stats.samples ?? 0);
    document.getElementById('stats_avg').textContent = fmt(stats.avg_w, 1);
    document.getElementById('stats_min').textContent = fmt(stats.min_w, 1);
    document.getElementById('stats_max').textContent = fmt(stats.max_w, 1);
    document.getElementById('cpu_avg').textContent = fmt(stats.avg_cpu, 0);
    document.getElementById('power_window').textContent = stats.window_s ? 'window: last ' + Math.floor(stats.window_s/60) + ' min' : '';
    document.getElementById('cpu_window').textContent = stats.window_s ? 'window: last ' + Math.floor(stats.window_s/60) + ' min' : '';
  } catch (e) {
    console.warn('stats failed', e);
  }
  try {
    const u = await fetchJSON('/api/uptime');
    const s = Math.floor((u.uptime_s || 0));
    const mm = String(Math.floor(s/60)).padStart(2,'0');
    const ss = String(s%60).padStart(2,'0');
    document.getElementById('uptime').textContent = mm + ':' + ss;
  } catch (e) {
    console.warn('uptime failed', e);
  }
  try {
    const procData = await fetchJSON('/api/processes');
    document.getElementById('processes_source').textContent = 'source: ' + (procData.source ?? 'estimated');
    document.getElementById('processes_updated').textContent = procData.generated_at ? 'updated: ' + new Date(procData.generated_at).toLocaleTimeString() : 'updated: —';
    
    const tbody = document.getElementById('processes_table_body');
    tbody.innerHTML = '';
    
    if (!procData.processes || procData.processes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #6b7280; padding: 16px;">No active processes found</td></tr>';
    } else {
      procData.processes.forEach((p, index) => {
        const tr = document.createElement('tr');
        
        const tdRank = document.createElement('td');
        tdRank.textContent = index + 1;
        
        const tdName = document.createElement('td');
        tdName.style.fontWeight = '500';
        tdName.textContent = p.name || '—';
        
        const tdWatts = document.createElement('td');
        tdWatts.className = 'text-right';
        tdWatts.textContent = fmt(p.estimated_watts, 2) + ' W';
        
        const tdCpu = document.createElement('td');
        tdCpu.className = 'text-right';
        tdCpu.textContent = fmt(p.cpu_pct, 1) + ' %';
        
        const tdMem = document.createElement('td');
        tdMem.className = 'text-right';
        tdMem.textContent = p.memory_mb != null ? fmt(p.memory_mb, 1) + ' MB' : '—';
        
        const tdCmd = document.createElement('td');
        const cmdDiv = document.createElement('div');
        cmdDiv.className = 'cmd-excerpt';
        cmdDiv.title = p.cmdline || '';
        cmdDiv.textContent = p.cmdline || '—';
        tdCmd.appendChild(cmdDiv);
        
        tr.appendChild(tdRank);
        tr.appendChild(tdName);
        tr.appendChild(tdWatts);
        tr.appendChild(tdCpu);
        tr.appendChild(tdMem);
        tr.appendChild(tdCmd);
        
        tbody.appendChild(tr);
      });
    }
  } catch (e) {
    console.warn('processes failed', e);
  }
}
update();
setInterval(update, REFRESH_MS);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sampler = Thread(target=sampler_loop, daemon=True)
    sampler.start()
    server = HTTPServer((CONFIG["host"], CONFIG["port"]), PowerMonitorHandler)
    print(f"PowerMonitor running on http://{CONFIG['host']}:{CONFIG['port']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
