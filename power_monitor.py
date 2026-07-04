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
    "peaks_limit": 20,
    "energy_history_limit": 600,
    "alerts": [
        {"id": "high_watts", "label": "High power draw", "type": "watts", "threshold": 45, "sustain_s": 30},
        {"id": "high_cpu", "label": "High CPU load", "type": "cpu", "threshold": 85, "sustain_s": 15}
    ],
    "alert_cooldown_s": 120,
    "group_processes_by_basename": True,
}


class Alert:
    def __init__(self, id, label, active=False, since=None, value=None):
        self.id = id
        self.label = label
        self.active = active
        self.since = since
        self.value = value

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "active": self.active,
            "since": self.since,
            "value": self.value,
        }


def build_alerts() -> list[dict]:
    alerts_list = []
    alerts_config = CONFIG.get("alerts", [])
    with HISTORY_LOCK:
        local_history = list(history)

    for alert_conf in alerts_config:
        alert_id = alert_conf.get("id")
        label = alert_conf.get("label")
        alert_type = alert_conf.get("type")
        threshold = alert_conf.get("threshold", 0.0)
        sustain_s = alert_conf.get("sustain_s", 0)

        active = False
        since = None
        current_value = 0.0

        if local_history:
            exceeding = []
            for s in reversed(local_history):
                val = s.get("watts" if alert_type == "watts" else "cpu_load", 0.0)
                try:
                    val = float(val)
                except Exception:
                    val = 0.0

                if alert_type == "cpu":
                    val = min(100.0, max(0.0, val))
                elif alert_type == "watts":
                    val = max(0.0, val)

                if val > threshold:
                    exceeding.append((s, val))
                else:
                    break

            if exceeding:
                current_value = exceeding[0][1]
                newest_ts = _parse_ts(exceeding[0][0]["ts"])
                oldest_ts = _parse_ts(exceeding[-1][0]["ts"])
                if (newest_ts - oldest_ts) >= sustain_s:
                    active = True
                    since = exceeding[-1][0]["ts"]
            else:
                latest_s = local_history[-1]
                val = latest_s.get("watts" if alert_type == "watts" else "cpu_load", 0.0)
                try:
                    current_value = float(val)
                except Exception:
                    current_value = 0.0
                if alert_type == "cpu":
                    current_value = min(100.0, max(0.0, current_value))
                elif alert_type == "watts":
                    current_value = max(0.0, current_value)

        alert_obj = Alert(alert_id, label, active=active, since=since, value=round(current_value, 2))
        alerts_list.append(alert_obj.to_dict())

    return alerts_list


def group_processes_by_basename(procs: list[dict]) -> list[dict]:
    grouped = {}
    for p in procs:
        cmdline = p.get("cmdline", "")
        name = p.get("name", "")
        tokens = cmdline.split()
        argv0 = tokens[0] if tokens else name
        basename = os.path.basename(argv0) if argv0 else name

        if not basename:
            basename = "unknown"

        if basename not in grouped:
            grouped[basename] = {
                "name": basename,
                "cmdline": cmdline,
                "estimated_watts": 0.0,
                "cpu_pct": 0.0,
                "memory_mb": None,
            }

        g = grouped[basename]
        g["estimated_watts"] += p.get("estimated_watts", 0.0)
        g["cpu_pct"] += p.get("cpu_pct", 0.0)

        mem = p.get("memory_mb")
        if mem is not None:
            if g["memory_mb"] is None:
                g["memory_mb"] = mem
            else:
                g["memory_mb"] = max(g["memory_mb"], mem)

    result = []
    for basename, g in grouped.items():
        res = {
            "name": g["name"],
            "cmdline": g["cmdline"],
            "estimated_watts": round(g["estimated_watts"], 2),
            "cpu_pct": round(g["cpu_pct"], 1),
        }
        if g["memory_mb"] is not None:
            res["memory_mb"] = round(g["memory_mb"], 2)
        result.append(res)

    result.sort(key=lambda x: x.get("estimated_watts", 0.0), reverse=True)
    return result

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
    return estimated_procs


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
        path = self.path
        query_params = {}
        if "?" in path:
            path, query_str = path.split("?", 1)
            for pair in query_str.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    query_params[k] = v

        if path in ("/", "/index.html"):
            return self.send_html()
        if path == "/api/status":
            return self.send_json(status_payload())
        if path == "/api/history":
            limit = CONFIG.get("energy_history_limit", 600)
            if "limit" in query_params:
                try:
                    limit = int(query_params["limit"])
                except ValueError:
                    pass
            limit = min(limit, CONFIG.get("history_max", 1800))
            return self.send_json({"history": history_snapshot(limit)})
        if path == "/api/config":
            safe = {k: v for k, v in CONFIG.items() if k not in ("host", "port", "bind")}
            return self.send_json(safe)
        if path == "/api/alerts":
            return self.send_json({"alerts": build_alerts(), "generated_at": utc_now_iso()})
        if path.startswith("/api/stats/"):
            seconds = min(3600, max(10, int(path.rsplit("/", 1)[-1])))
            return self.send_json(window_stats(seconds))
        if path == "/api/stats":
            return self.send_json(window_stats(300))
        if path == "/api/export.csv":
            return self.send_csv()
        if path == "/api/uptime":
            return self.send_json({"uptime_s": round(time.time() - _start_time, 1)})
        if path == "/api/processes":
            limit = CONFIG.get("processes_top_n", 8)
            if "limit" in query_params:
                try:
                    limit = int(query_params["limit"])
                except ValueError:
                    pass
            limit = max(1, min(limit, 200))

            with PROCESSES_LOCK:
                gen_at = cached_processes_generated_at
                source = cached_processes_source
                procs = list(cached_processes)

            grouped = False
            if CONFIG.get("group_processes_by_basename", False):
                procs = group_processes_by_basename(procs)
                grouped = True
            else:
                procs = sorted(procs, key=lambda x: x.get("estimated_watts", 0.0), reverse=True)

            procs = procs[:limit]

            data = {
                "processes": procs,
                "generated_at": gen_at,
                "source": source
            }
            if grouped:
                data["grouped"] = True
            return self.send_json(data)
        if path == "/api/peaks":
            limit = CONFIG.get("peaks_limit", 20)
            if "limit" in query_params:
                try:
                    limit = int(query_params["limit"])
                except ValueError:
                    pass
            
            peaks = get_peaks(limit)
            return self.send_json({"peaks": peaks, "generated_at": utc_now_iso()})
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


def get_top_processes(limit: int) -> list[dict]:
    limit = min(limit, CONFIG.get("processes_top_n", 8))
    with PROCESSES_LOCK:
        sorted_procs = sorted(cached_processes, key=lambda x: x.get("estimated_watts", 0.0), reverse=True)
        return sorted_procs[:limit]


def get_top_peaks(limit: int) -> list[dict]:
    limit = min(limit, CONFIG.get("peaks_limit", 20))
    with HISTORY_LOCK:
        samples = []
        for s in history:
            samples.append({
                "ts": s["ts"],
                "watts": s["watts"],
                "cpu_load": s["cpu_load"],
                "method": s["method"]
            })
        samples.sort(key=lambda s: (s["watts"], _parse_ts(s["ts"])), reverse=True)
        return samples[:limit]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def top_by_estimated_watts(limit: int) -> list[dict]:
    limit = max(1, min(limit, 200))
    with PROCESSES_LOCK:
        sorted_procs = sorted(cached_processes, key=lambda x: x.get("estimated_watts", 0.0), reverse=True)
        return sorted_procs[:limit]


def get_peaks(limit: int) -> list[dict]:
    limit = max(1, min(limit, 200))
    with HISTORY_LOCK:
        samples = []
        for s in history:
            samples.append({
                "ts": s["ts"],
                "watts": s["watts"],
                "cpu_load": s["cpu_load"],
                "method": s["method"]
            })
        samples.sort(key=lambda s: (s["watts"], s["ts"]), reverse=True)
        return samples[:limit]


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
    padding-top: 48px; /* space for fixed banner */
  }
  .alert-banner {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 9999;
    text-align: center;
    padding: 10px 16px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: background-color 0.3s, color 0.3s, border-color 0.3s;
    background: #ecfdf5; /* light green */
    color: #065f46;
    border-bottom: 1px solid #a7f3d0;
  }
  .alert-banner.active {
    background: #fee2e2; /* light red */
    color: #991b1b;
    border-bottom: 1px solid #fca5a5;
  }
  .seg-btn {
    background: none;
    border: none;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
    color: #4b5563;
    cursor: pointer;
    border-radius: 6px;
    transition: all 0.2s ease;
  }
  .seg-btn:hover {
    color: #111827;
  }
  .seg-btn.active {
    background: #ffffff;
    color: #2563eb;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
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

  /* Tabs Layout styling */
  .tabs-container {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
    border-bottom: 2px solid #e5e7eb;
    padding-bottom: 8px;
  }
  .tab-btn {
    background: none;
    border: none;
    padding: 8px 16px;
    font-size: 14px;
    font-weight: 600;
    color: #6b7280;
    cursor: pointer;
    border-radius: 6px;
    transition: all 0.2s ease;
  }
  .tab-btn:hover {
    color: #2563eb;
    background: rgba(37, 99, 235, 0.05);
  }
  .tab-btn.active {
    color: #2563eb;
    background: rgba(37, 99, 235, 0.1);
    box-shadow: inset 0 -2px 0 #2563eb;
  }
  .tab-content {
    display: none;
  }
  .tab-content.active {
    display: block;
    animation: fadeIn 0.3s ease;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  /* Overview chips row styling */
  .chips-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
  }
  .chip {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    transition: transform 0.2s, box-shadow 0.2s;
  }
  .chip:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -1px rgba(0,0,0,0.03);
    border-color: #cbd5e1;
  }
  .chip-label {
    font-size: 10px;
    text-transform: uppercase;
    color: #64748b;
    font-weight: 600;
    letter-spacing: 0.5px;
  }
  .chip-value {
    font-size: 15px;
    font-weight: 700;
    color: #0f172a;
  }

  /* Color accents for peak table values */
  .accent-high-cpu {
    color: #dc2626;
    font-weight: 600;
    background: #fee2e2;
    padding: 2px 6px;
    border-radius: 4px;
  }
  .accent-high-watts {
    color: #ea580c;
    font-weight: 600;
    background: #ffedd5;
    padding: 2px 6px;
    border-radius: 4px;
  }
  .sortable:hover {
    background: #f3f4f6;
  }
  .sort-icon {
    margin-left: 4px;
    font-size: 10px;
    color: #2563eb;
  }
</style>
</head>
<body>
<div id="alert-banner" class="alert-banner">System Status: Normal</div>
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

<!-- Time Range Segment Control -->
<div class="segment-wrap" style="display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-bottom: 12px; padding: 0 4px;">
  <span class="small" style="font-weight: 600; color: #4b5563;">Time Range:</span>
  <div class="segment-control" style="display: inline-flex; background: #e5e7eb; padding: 2px; border-radius: 8px;">
    <button class="seg-btn" data-seconds="60">1m</button>
    <button class="seg-btn active" data-seconds="600">10m</button>
    <button class="seg-btn" data-seconds="1800">30m</button>
    <button class="seg-btn" data-seconds="3600">1h</button>
  </div>
</div>

<section class="chart-wrap">
  <div class="tabs-container">
    <button class="tab-btn active" data-tab="overview">Overview</button>
    <button class="tab-btn" data-tab="processes">Processes</button>
    <button class="tab-btn" data-tab="peaks">Peaks</button>
  </div>

  <!-- Overview Tab Content -->
  <div id="tab-overview" class="tab-content active">
    <div class="chips-grid">
      <div class="chip">
        <span class="chip-label">Source</span>
        <span class="chip-value" id="chip_source">—</span>
      </div>
      <div class="chip">
        <span class="chip-label">Samples</span>
        <span class="chip-value" id="chip_samples">—</span>
      </div>
      <div class="chip">
        <span class="chip-label">Avg Power</span>
        <span class="chip-value" id="chip_avg_w">— W</span>
      </div>
      <div class="chip">
        <span class="chip-label">Min Power</span>
        <span class="chip-value" id="chip_min_w">— W</span>
      </div>
      <div class="chip">
        <span class="chip-label">Max Power</span>
        <span class="chip-value" id="chip_max_w">— W</span>
      </div>
      <div class="chip">
        <span class="chip-label">Est. Daily</span>
        <span class="chip-value" id="chip_est_daily">—</span>
      </div>
      <div class="chip">
        <span class="chip-label">Est. Monthly</span>
        <span class="chip-value" id="chip_est_monthly">—</span>
      </div>
      <div class="chip">
        <span class="chip-label">Uptime</span>
        <span class="chip-value" id="chip_uptime">—</span>
      </div>
    </div>
  </div>

  <!-- Processes Tab Content -->
  <div id="tab-processes" class="tab-content">
    <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:8px; gap:8px;">
      <div>
        <strong>Top Processes Power (W)</strong>
        <span class="small" id="processes_source" style="margin-left: 8px;">source: estimated</span>
      </div>
      <div class="small" id="processes_updated">updated: —</div>
    </div>
    
    <div class="svg-chart-container" style="margin-bottom: 16px;">
      <svg id="processes_svg_chart" width="100%" style="background:#ffffff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px; margin-bottom: 16px;">
        <defs>
          <linearGradient id="barGradient" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stop-color="#3b82f6" />
            <stop offset="100%" stop-color="#2563eb" />
          </linearGradient>
        </defs>
      </svg>
    </div>

    <div style="overflow-x:auto;">
      <table class="proc-table" id="processes_table">
        <thead>
          <tr>
            <th style="width: 50px;">Rank</th>
            <th class="sortable" data-sort="name" style="cursor: pointer; user-select: none;">Name <span class="sort-icon"></span></th>
            <th class="sortable text-right" data-sort="watts" style="width: 100px; cursor: pointer; user-select: none;">Power <span class="sort-icon">↓</span></th>
            <th class="sortable text-right" data-sort="cpu" style="width: 80px; cursor: pointer; user-select: none;">CPU % <span class="sort-icon"></span></th>
            <th class="sortable text-right" data-sort="mem" style="width: 100px; cursor: pointer; user-select: none;">Memory <span class="sort-icon"></span></th>
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
  </div>

  <!-- Peaks Tab Content -->
  <div id="tab-peaks" class="tab-content">
    <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:8px;">
      <div><strong>Top High-Power Samples</strong></div>
    </div>
    <div style="overflow-x:auto;">
      <table class="proc-table" id="peaks_table">
        <thead>
          <tr>
            <th style="width: 50px;">#</th>
            <th>Time</th>
            <th class="text-right" style="width: 120px;">Power</th>
            <th class="text-right" style="width: 100px;">CPU %</th>
            <th>Method</th>
          </tr>
        </thead>
        <tbody id="peaks_table_body">
          <tr>
            <td colspan="5" style="text-align: center; color: #6b7280; padding: 16px;">Loading peaks...</td>
          </tr>
        </tbody>
      </table>
    </div>
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
let selectedWindowSeconds = 600;

function getRelativeTime(isoString) {
  if (!isoString) return '—';
  try {
    const d = new Date(isoString);
    const diffMs = Date.now() - d.getTime();
    const diffSec = Math.max(0, Math.floor(diffMs / 1000));
    if (diffSec < 5) return 'just now';
    if (diffSec < 60) return diffSec + 's ago';
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return diffMin + 'm ago';
    const diffHour = Math.floor(diffMin / 60);
    if (diffHour < 24) return diffHour + 'h ago';
    const diffDay = Math.floor(diffHour / 24);
    return diffDay + 'd ago';
  } catch (e) {
    return '—';
  }
}

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

// Tab Switching
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    
    btn.classList.add('active');
    const tabId = 'tab-' + btn.getAttribute('data-tab');
    document.getElementById(tabId).classList.add('active');
  });
});

// Alert Banner Click Event
document.getElementById('alert-banner').addEventListener('click', () => {
  const tabBtn = document.querySelector('.tab-btn[data-tab="overview"]');
  if (tabBtn) tabBtn.click();
  const target = document.getElementById('tab-overview');
  if (target) {
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
});

// Segment Control Setup
document.querySelectorAll('.seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    selectedWindowSeconds = parseInt(btn.getAttribute('data-seconds'), 10);
    update();
  });
});

let procSortField = 'watts';
let procSortAsc = false;
let lastFetchedProcesses = [];
const HIGH_WATTS_THRESHOLD = 25;

function sortProcesses(processes) {
  if (!processes) return [];
  const sorted = [...processes];
  sorted.sort((a, b) => {
    let valA, valB;
    if (procSortField === 'name') {
      valA = (a.name || '').toLowerCase();
      valB = (b.name || '').toLowerCase();
    } else if (procSortField === 'watts') {
      valA = a.estimated_watts || 0;
      valB = b.estimated_watts || 0;
    } else if (procSortField === 'cpu') {
      valA = a.cpu_pct || 0;
      valB = b.cpu_pct || 0;
    } else if (procSortField === 'mem') {
      valA = a.memory_mb || 0;
      valB = b.memory_mb || 0;
    }
    
    if (valA < valB) return procSortAsc ? -1 : 1;
    if (valA > valB) return procSortAsc ? 1 : -1;
    return 0;
  });
  return sorted;
}

function updateSortHeaders() {
  document.querySelectorAll('#processes_table th.sortable').forEach(th => {
    const field = th.getAttribute('data-sort');
    const iconSpan = th.querySelector('.sort-icon');
    if (field === procSortField) {
      iconSpan.textContent = procSortAsc ? ' ↑' : ' ↓';
      th.style.color = '#2563eb';
    } else {
      iconSpan.textContent = '';
      th.style.color = '';
    }
  });
}

document.querySelectorAll('#processes_table th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    const field = th.getAttribute('data-sort');
    if (procSortField === field) {
      procSortAsc = !procSortAsc;
    } else {
      procSortField = field;
      procSortAsc = false;
    }
    updateSortHeaders();
    renderProcessesTable();
  });
});

function renderProcessesTable() {
  const tbody = document.getElementById('processes_table_body');
  tbody.innerHTML = '';
  
  const sorted = sortProcesses(lastFetchedProcesses);
  
  if (sorted.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #6b7280; padding: 16px;">No active processes found</td></tr>';
    return;
  }
  
  sorted.forEach((p, index) => {
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

function renderProcessesSvg(processes) {
  const svg = document.getElementById('processes_svg_chart');
  svg.innerHTML = '';
  if (!processes || processes.length === 0) {
    svg.style.display = 'none';
    return;
  }
  svg.style.display = 'block';
  
  // Create linear gradient
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  const gradient = document.createElementNS('http://www.w3.org/2000/svg', 'linearGradient');
  gradient.setAttribute('id', 'barGradient');
  gradient.setAttribute('x1', '0%');
  gradient.setAttribute('y1', '0%');
  gradient.setAttribute('x2', '100%');
  gradient.setAttribute('y2', '0%');
  
  const stop1 = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
  stop1.setAttribute('offset', '0%');
  stop1.setAttribute('stop-color', '#3b82f6');
  
  const stop2 = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
  stop2.setAttribute('offset', '100%');
  stop2.setAttribute('stop-color', '#2563eb');
  
  gradient.appendChild(stop1);
  gradient.appendChild(stop2);
  defs.appendChild(gradient);
  svg.appendChild(defs);
  
  const maxW = Math.max(...processes.map(p => p.estimated_watts || 0), 1);
  const rowHeight = 22;
  const gap = 8;
  const labelWidth = 130;
  const maxBarWidth = 380;
  const valueOffset = 520;
  
  const totalHeight = processes.length * (rowHeight + gap) + 10;
  svg.setAttribute('viewBox', `0 0 600 ${totalHeight}`);
  
  processes.forEach((p, i) => {
    const y = 5 + i * (rowHeight + gap);
    const wVal = p.estimated_watts || 0;
    const pct = wVal / maxW;
    const barWidth = Math.max(2, pct * maxBarWidth);
    
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', '10');
    text.setAttribute('y', y + 15);
    text.setAttribute('fill', '#4b5563');
    text.setAttribute('font-size', '12px');
    text.setAttribute('font-weight', '600');
    let displayName = p.name || '—';
    if (displayName.length > 15) displayName = displayName.slice(0, 14) + '…';
    text.textContent = displayName;
    svg.appendChild(text);
    
    const bgRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    bgRect.setAttribute('x', labelWidth);
    bgRect.setAttribute('y', y);
    bgRect.setAttribute('width', maxBarWidth);
    bgRect.setAttribute('height', rowHeight);
    bgRect.setAttribute('fill', '#f3f4f6');
    bgRect.setAttribute('rx', '4');
    svg.appendChild(bgRect);
    
    const activeRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    activeRect.setAttribute('x', labelWidth);
    activeRect.setAttribute('y', y);
    activeRect.setAttribute('width', barWidth);
    activeRect.setAttribute('height', rowHeight);
    activeRect.setAttribute('fill', 'url(#barGradient)');
    activeRect.setAttribute('rx', '4');
    svg.appendChild(activeRect);
    
    const valText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    valText.setAttribute('x', valueOffset);
    valText.setAttribute('y', y + 15);
    valText.setAttribute('fill', '#111827');
    valText.setAttribute('font-size', '12px');
    valText.setAttribute('font-weight', '700');
    valText.textContent = fmt(wVal, 2) + ' W';
    svg.appendChild(valText);
  });
}

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

async function update() {
  try {
    const alertsData = await fetchJSON('/api/alerts');
    const banner = document.getElementById('alert-banner');
    const activeAlerts = (alertsData.alerts || []).filter(a => a.active);
    if (activeAlerts.length > 0) {
      banner.classList.add('active');
      const alertTexts = activeAlerts.map(a => `${a.label} (${getRelativeTime(a.since)})`);
      banner.textContent = '⚠️ Active Alerts: ' + alertTexts.join(', ');
    } else {
      banner.classList.remove('active');
      banner.textContent = 'System Status: Normal';
    }
  } catch (e) {
    console.warn('alerts failed', e);
  }
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
    
    const updatedTime = status.updated ? new Date(status.updated).toLocaleTimeString() : '—';
    const updatedRel = status.updated ? getRelativeTime(status.updated) : '';
    document.getElementById('updated').textContent = status.updated ? `${updatedTime} (${updatedRel})` : '—';
    
    document.getElementById('chip_source').textContent = status.method ?? '—';
    document.getElementById('chip_est_daily').textContent = fmt(status.cost_daily, 2) + ' PLN';
    document.getElementById('chip_est_monthly').textContent = fmt(status.cost_monthly, 2) + ' PLN';
  } catch (e) {
    console.warn('status failed', e);
  }
  try {
    const h = await fetchJSON(`/api/history?limit=${Math.floor(selectedWindowSeconds / 2)}`);
    labels.length = 0;
    data.length = 0;
    cpuLabels.length = 0;
    cpuData.length = 0;
    for (const s of h.history) {
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
    const stats = await fetchJSON(`/api/stats/${selectedWindowSeconds}`);
    document.getElementById('stats_samples').textContent = 'samples: ' + (stats.samples ?? 0);
    document.getElementById('stats_avg').textContent = fmt(stats.avg_w, 1);
    document.getElementById('stats_min').textContent = fmt(stats.min_w, 1);
    document.getElementById('stats_max').textContent = fmt(stats.max_w, 1);
    document.getElementById('cpu_avg').textContent = fmt(stats.avg_cpu, 0);
    document.getElementById('power_window').textContent = stats.window_s ? 'window: last ' + Math.floor(stats.window_s/60) + ' min' : '';
    document.getElementById('cpu_window').textContent = stats.window_s ? 'window: last ' + Math.floor(stats.window_s/60) + ' min' : '';
    
    document.getElementById('chip_samples').textContent = stats.samples ?? 0;
    document.getElementById('chip_avg_w').textContent = fmt(stats.avg_w, 1) + ' W';
    document.getElementById('chip_min_w').textContent = fmt(stats.min_w, 1) + ' W';
    document.getElementById('chip_max_w').textContent = fmt(stats.max_w, 1) + ' W';
  } catch (e) {
    console.warn('stats failed', e);
  }
  try {
    const u = await fetchJSON('/api/uptime');
    const s = Math.floor((u.uptime_s || 0));
    const mm = String(Math.floor(s/60)).padStart(2,'0');
    const ss = String(s%60).padStart(2,'0');
    const timeStr = mm + ':' + ss;
    document.getElementById('uptime').textContent = timeStr;
    document.getElementById('chip_uptime').textContent = timeStr;
  } catch (e) {
    console.warn('uptime failed', e);
  }
  try {
    const procData = await fetchJSON('/api/processes');
    const sourceEl = document.getElementById('processes_source');
    if (sourceEl) sourceEl.textContent = 'source: ' + (procData.source ?? 'estimated');
    document.getElementById('processes_updated').textContent = procData.generated_at ? 'updated: ' + getRelativeTime(procData.generated_at) : 'updated: —';
    
    lastFetchedProcesses = procData.processes || [];
    renderProcessesTable();
    updateSortHeaders();
    
    const topByPower = [...lastFetchedProcesses].sort((a, b) => (b.estimated_watts || 0) - (a.estimated_watts || 0)).slice(0, 8);
    renderProcessesSvg(topByPower);
  } catch (e) {
    console.warn('processes failed', e);
  }
  try {
    const peaksData = await fetchJSON('/api/peaks');
    const tbody = document.getElementById('peaks_table_body');
    tbody.innerHTML = '';
    
    const peaks = peaksData.peaks || [];
    if (peaks.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #6b7280; padding: 16px;">No peak data available</td></tr>';
    } else {
      peaks.forEach((s, index) => {
        const tr = document.createElement('tr');
        
        const tdIndex = document.createElement('td');
        tdIndex.textContent = index + 1;
        
        const tdTime = document.createElement('td');
        tdTime.textContent = s.ts ? getRelativeTime(s.ts) : '—';
        tdTime.title = s.ts ? new Date(s.ts).toLocaleString() : '';
        
        const tdWatts = document.createElement('td');
        tdWatts.className = 'text-right';
        if (s.watts > HIGH_WATTS_THRESHOLD) {
          const span = document.createElement('span');
          span.className = 'accent-high-watts';
          span.textContent = fmt(s.watts, 2) + ' W';
          tdWatts.appendChild(span);
        } else {
          tdWatts.textContent = fmt(s.watts, 2) + ' W';
        }
        
        const tdCpu = document.createElement('td');
        tdCpu.className = 'text-right';
        if (s.cpu_load > 75) {
          const span = document.createElement('span');
          span.className = 'accent-high-cpu';
          span.textContent = fmt(s.cpu_load, 1) + ' %';
          tdCpu.appendChild(span);
        } else {
          tdCpu.textContent = fmt(s.cpu_load, 1) + ' %';
        }
        
        const tdMethod = document.createElement('td');
        tdMethod.textContent = s.method || '—';
        
        tr.appendChild(tdIndex);
        tr.appendChild(tdTime);
        tr.appendChild(tdWatts);
        tr.appendChild(tdCpu);
        tr.appendChild(tdMethod);
        
        tbody.appendChild(tr);
      });
    }
  } catch (e) {
    console.warn('peaks failed', e);
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
