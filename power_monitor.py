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
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread


# ---------------------------------------------------------------------------
# Configuration (tune these for your machine)
# ---------------------------------------------------------------------------
CONFIG = {
    "host": "0.0.0.0",
    "port": 5000,
    "cpu_tdp_w": 65,
    "idle_power_w": 15,
    "sample_interval_s": 2,
    "history_max": 180,
    "kwh_price": 0.65,
}

# Global state
history = []
last_cpu_times = None


# ---------------------------------------------------------------------------
# CPU utilities
# ---------------------------------------------------------------------------
def read_cpu_times() -> list[int] | None:
    try:
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.strip().split()
                    return [int(x) for x in parts[1:]]
    except Exception:
        return None


def get_cpu_load() -> float:
    global last_cpu_times
    times = read_cpu_times()
    if times is None or last_cpu_times is None:
        last_cpu_times = times
        return 0.0
    total_delta = sum(times) - sum(last_cpu_times)
    idle_delta = times[3] - last_cpu_times[3]
    last_cpu_times = times
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


def read_power_with_fallback() -> tuple[float, str]:
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
    return round(estimate_power_from_cpu(), 2), "estimated"


def estimate_power_from_cpu() -> float:
    load = get_cpu_load()
    est = CONFIG["idle_power_w"] + load * CONFIG["cpu_tdp_w"]
    return max(CONFIG["idle_power_w"], min(est, CONFIG["cpu_tdp_w"] * 1.3))


# ---------------------------------------------------------------------------
# Cost/energy
# ---------------------------------------------------------------------------
def estimate_cost_daily(power_w: float) -> float:
    return (power_w / 1000.0) * 24.0 * CONFIG["kwh_price"]


def estimate_cost_monthly(power_w: float) -> float:
    return estimate_cost_daily(power_w) * 30.0


# ---------------------------------------------------------------------------
# Background sampler
# ---------------------------------------------------------------------------
def sampler_loop():
    global history
    while True:
        watts, method = read_power_with_fallback()
        cpu_load = get_cpu_load()
        sample = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "watts": watts,
            "cpu_load": round(cpu_load * 100.0, 1),
            "method": method,
        }
        history.append(sample)
        if len(history) > CONFIG["history_max"]:
            history = history[-CONFIG["history_max"] :]
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
            return self.send_json(self.get_status())
        if self.path == "/api/history":
            return self.send_json({"history": history})
        if self.path == "/api/config":
            safe = {k: v for k, v in CONFIG.items() if k not in ("host", "port")}
            return self.send_json(safe)
        self.send_response(404)
        self.end_headers()

    def send_json(self, data):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def get_status(self) -> dict:
        watts = history[-1]["watts"] if history else 0.0
        cpu_load = history[-1]["cpu_load"] if history else 0.0
        method = history[-1]["method"] if history else "none"

        kwh = 0.0
        if len(history) >= 2:
            total_wh = 0.0
            for i in range(1, len(history)):
                t1 = datetime.fromisoformat(history[i - 1]["ts"].replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(history[i]["ts"].replace("Z", "+00:00"))
                dt = (t2 - t1).total_seconds()
                if dt > 0:
                    avg_w = (history[i]["watts"] + history[i - 1]["watts"]) / 2.0
                    total_wh += avg_w * dt / 3600.0
            kwh = round(total_wh, 5)

        if cpu_load > 80:
            status = "High"
        elif cpu_load > 40:
            status = "Medium"
        elif cpu_load > 10:
            status = "Low"
        else:
            status = "Idle"

        return {
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

    def send_html(self):
        html = HTML_TEMPLATE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


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
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
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
  .card .value { margin: 0; font-size: 26px; font-weight: 700; }
  .card .sub { margin-top: 4px; font-size: 12px; color: #6b7280; }
  .chart-wrap {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 14px 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }
  footer { margin-top: 12px; color: #6b7280; font-size: 12px; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
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
    <div class="sub">Watts</div>
  </article>
  <article class="card">
    <h2>Energy</h2>
    <p class="value" id="energy">—</p>
    <div class="sub">kWh</div>
  </article>
  <article class="card">
    <h2>CPU Load</h2>
    <p class="value" id="cpu">—</p>
    <div class="sub">%</div>
  </article>
  <article class="card">
    <h2>Status</h2>
    <p class="value" id="status">—</p>
    <div class="sub">Cores: <span id="cores">—</span></div>
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
  <canvas id="chart" aria-label="Power usage over time" role="img"></canvas>
</section>
<footer>
  Auto-refresh every <span id="interval">3</span>s · Source: <span id="src">—</span> · Updated: <span id="updated">—</span>
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
    document.getElementById('day').textContent = fmt(status.cost_daily, 2) + ' PLN';
    document.getElementById('month').textContent = fmt(status.cost_monthly, 2) + ' PLN';
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
    for (const s of h.history.slice(-120)) {
      const t = new Date(s.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      labels.push(t);
      data.push(s.watts);
    }
    chart.update('none');
  } catch (e) {
    console.warn('history failed', e);
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
