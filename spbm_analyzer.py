#!/usr/bin/env python3
"""
spbm_analyzer.py — Real-time SPBM + nvidia-smi correlation analyzer
Reads sensors output and nvidia-smi live, emits classified events.
Part of: nvidia-uma-correlation
"""

import subprocess
import threading
import time
import json
import sys
import os
import re
from collections import deque
from datetime import datetime

# ─── THRESHOLDS ───────────────────────────────────────────────────────────────
THROTTLE_CLOCK_MHZ     = 850    # below this under load = PROCHOT/power cap
POWER_SPIKE_W          = 15     # delta watts in one sample = spike
POWER_RISE_RATE_W_S    = 8      # watts/sec sustained rise = warning
PROCHOT_FIELD          = "prochot"
PL_LEVEL_FIELD         = "pl_level"
GPU_UTIL_THROTTLE_PCT  = 80     # util above this + low clock = throttle
SAMPLE_HISTORY         = 20     # samples to keep for derivative
# ──────────────────────────────────────────────────────────────────────────────

# ─── STATE ────────────────────────────────────────────────────────────────────
# Auto-detect: silent when piped/scripted, stream when interactive
QUIET = not sys.stdout.isatty()

state = {
    "gpu_clock":    0,
    "gpu_util":     0,
    "gpu_temp":     0,
    "sys_total":    0.0,
    "dc_input":     0.0,
    "gpu_power":    0.0,
    "soc_pkg":      0.0,
    "cpu_gpu":      0.0,
    "pl1":          0.0,
    "pl2":          0.0,
    "syspl1":       0.0,
    "syspl2":       0.0,
    "prochot":      0,
    "pl_level":     0,
    "ts":           0,
    "uma_some":     0.0,
    "uma_full":     0.0,
    "mem_pct":      0.0,
}

history = deque(maxlen=SAMPLE_HISTORY)
events  = []
lock    = threading.Lock()
running = True
# ──────────────────────────────────────────────────────────────────────────────

def ts_ms():
    return int(time.time() * 1000)


def read_psi():
    """Read /proc/pressure/memory — ground truth for memory stall."""
    try:
        with open("/proc/pressure/memory") as f:
            for line in f:
                if line.startswith("some"):
                    some = float(line.split()[1].split("=")[1])
                if line.startswith("full"):
                    full = float(line.split()[1].split("=")[1])
        return some, full
    except Exception:
        return 0.0, 0.0

def read_mem_pct():
    """Read system memory usage percent."""
    try:
        with open("/proc/meminfo") as f:
            lines = {l.split()[0]: int(l.split()[1]) for l in f if len(l.split()) >= 2}
        total = lines.get("MemTotal:", 0)
        avail = lines.get("MemAvailable:", 0)
        if total > 0:
            return (total - avail) / total * 100.0
        return 0.0
    except Exception:
        return 0.0

def classify_root_cause(uma_some, uma_full, clock_mhz, prochot):
    """PSI is ground truth. GPU util is NOT a memory signal on GB10."""
    memory = (uma_some > 0.5) or (uma_full > 0.1)
    power  = (clock_mhz < THROTTLE_CLOCK_MHZ and clock_mhz > 0) or (prochot == 1)
    if memory and power:
        return "MEMORY+POWER"
    if memory:
        return "MEMORY"
    if power:
        return "POWER"
    return "UNKNOWN"

def classify_pressure(mem_pct, uma_some, uma_full):
    """Pressure level based on PSI — not memory capacity."""
    if uma_full > 0.15:
        return "CRITICAL"
    if mem_pct > 90 and uma_some > 0.7 and uma_full > 0.05:
        return "DANGER"
    if mem_pct > 85 and uma_some > 0.5:
        return "WARNING"
    return "SAFE"

def log_event(event_type, data):
    ev = {
        "ts":    ts_ms(),
        "event": event_type,
        **data
    }
    with lock:
        events.append(ev)
    print(f"\n🔴 EVENT [{event_type}] {json.dumps(data)}", flush=True)

def emit_status():
    if QUIET: return
    with lock:
        s = dict(state)
    print(
        f"\r⚡ {s['ts']} | "
        f"CLK:{s['gpu_clock']}MHz "
        f"UTIL:{s['gpu_util']}% "
        f"TEMP:{s['gpu_temp']}C | "
        f"GPU:{s['gpu_power']:.1f}W "
        f"DC:{s['dc_input']:.1f}W "
        f"SYS:{s['sys_total']:.1f}W | "
        f"PL1:{s['pl1']:.1f}W "
        f"PL2:{s['pl2']:.1f}W | "
        f"PROCHOT:{s['prochot']} | "
        f"PSI:{s['uma_some']:.2f}/{s['uma_full']:.2f} "
        f"MEM:{s['mem_pct']:.1f}% "
        f"[{classify_pressure(s['mem_pct'],s['uma_some'],s['uma_full'])}]",
        end="", flush=True
    )

# ─── SENSORS PARSER ───────────────────────────────────────────────────────────
SENSOR_PATTERNS = {
    "sys_total": re.compile(r"sys_total.*?([0-9]+\.?[0-9]*)\s*W", re.I),
    "dc_input":  re.compile(r"dc_input.*?([0-9]+\.?[0-9]*)\s*W",  re.I),
    "gpu_power": re.compile(r"^\s*gpu.*?([0-9]+\.?[0-9]*)\s*W",   re.I | re.M),
    "soc_pkg":   re.compile(r"soc_pkg.*?([0-9]+\.?[0-9]*)\s*W",   re.I),
    "cpu_gpu":   re.compile(r"cpu_gpu.*?([0-9]+\.?[0-9]*)\s*W",   re.I),
    "pl1":       re.compile(r"\bpl1\b.*?([0-9]+\.?[0-9]*)\s*W",   re.I),
    "pl2":       re.compile(r"\bpl2\b.*?([0-9]+\.?[0-9]*)\s*W",   re.I),
    "syspl1":    re.compile(r"syspl1.*?([0-9]+\.?[0-9]*)\s*W",    re.I),
    "syspl2":    re.compile(r"syspl2.*?([0-9]+\.?[0-9]*)\s*W",    re.I),
    "prochot":   re.compile(r"prochot.*?([01])",                    re.I),
    "pl_level":  re.compile(r"pl_level.*?([0-9]+)",                 re.I),
}

def parse_sensors_block(block):
    parsed = {}
    for field, pattern in SENSOR_PATTERNS.items():
        m = pattern.search(block)
        if m:
            parsed[field] = float(m.group(1))
    return parsed
# ──────────────────────────────────────────────────────────────────────────────

# ─── NVIDIA-SMI POLLER ────────────────────────────────────────────────────────
def nvidia_smi_thread():
    global running
    while running:
        try:
            out = subprocess.check_output([
                "nvidia-smi",
                "--query-gpu=clocks.gr,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits"
            ], timeout=2).decode().strip()
            parts = out.split(",")
            if len(parts) == 3:
                with lock:
                    state["gpu_clock"] = int(parts[0].strip())
                    state["gpu_util"]  = int(parts[1].strip())
                    state["gpu_temp"]  = int(parts[2].strip())
        except Exception:
            pass
        time.sleep(0.5)
# ──────────────────────────────────────────────────────────────────────────────

# ─── ANALYZER ─────────────────────────────────────────────────────────────────
def analyze():
    prev = None

    with lock:
        snap = dict(state)

    history.append(snap)

    if len(history) < 2:
        return

    prev = history[-2]
    curr = history[-1]

    # 1. Power spike
    dc_delta = curr["dc_input"] - prev["dc_input"]
    if dc_delta >= POWER_SPIKE_W:
        log_event("POWER_SPIKE", {
            "dc_input_prev_w": prev["dc_input"],
            "dc_input_curr_w": curr["dc_input"],
            "delta_w":         dc_delta,
        })

    # 2. Sustained power rise
    if len(history) >= 5:
        oldest = history[-5]
        elapsed_s = (curr["ts"] - oldest["ts"]) / 1000.0
        if elapsed_s > 0:
            rise_rate = (curr["dc_input"] - oldest["dc_input"]) / elapsed_s
            if rise_rate >= POWER_RISE_RATE_W_S:
                log_event("POWER_RISING", {
                    "rate_w_per_s": round(rise_rate, 2),
                    "dc_input_w":  curr["dc_input"],
                })

    # 3. Throttle under load
    if (curr["gpu_util"] >= GPU_UTIL_THROTTLE_PCT and
            curr["gpu_clock"] < THROTTLE_CLOCK_MHZ and
            curr["gpu_clock"] > 0):
        log_event("THROTTLE_UNDER_LOAD", {
            "gpu_clock_mhz": curr["gpu_clock"],
            "gpu_util_pct":  curr["gpu_util"],
            "dc_input_w":    curr["dc_input"],
        })

    # 4. PROCHOT
    if curr["prochot"] == 1 and prev["prochot"] == 0:
        log_event("PROCHOT_ACTIVE", {
            "gpu_clock_mhz": curr["gpu_clock"],
            "gpu_temp_c":    curr["gpu_temp"],
            "dc_input_w":    curr["dc_input"],
            "pl_level":      curr["pl_level"],
        })

    # 5. Power limit engagement
    if curr["pl_level"] != prev["pl_level"]:
        log_event("PL_LEVEL_CHANGE", {
            "pl_level_prev": prev["pl_level"],
            "pl_level_curr": curr["pl_level"],
            "dc_input_w":    curr["dc_input"],
            "gpu_clock_mhz": curr["gpu_clock"],
        })

    # 7. Root cause + pressure classification
    root    = classify_root_cause(
                curr["uma_some"], curr["uma_full"],
                curr["gpu_clock"], curr["prochot"])
    pressure = classify_pressure(
                curr["mem_pct"], curr["uma_some"], curr["uma_full"])

    # Emit on state change or DANGER/CRITICAL
    prev_root = classify_root_cause(
                prev["uma_some"], prev["uma_full"],
                prev["gpu_clock"], prev["prochot"])
    prev_pressure = classify_pressure(
                prev["mem_pct"], prev["uma_some"], prev["uma_full"])

    if root != prev_root or pressure != prev_pressure:
        log_event("SYSTEM_STATE", {
            "root_cause":    root,
            "pressure":      pressure,
            "uma_some":      curr["uma_some"],
            "uma_full":      curr["uma_full"],
            "mem_pct":       round(curr["mem_pct"], 1),
            "gpu_clock_mhz": curr["gpu_clock"],
            "prochot":       curr["prochot"],
        })

    # 6. Clock collapse
    if (prev["gpu_clock"] > THROTTLE_CLOCK_MHZ and
            curr["gpu_clock"] < THROTTLE_CLOCK_MHZ and
            curr["gpu_clock"] > 0):
        log_event("CLOCK_COLLAPSE", {
            "clock_prev_mhz": prev["gpu_clock"],
            "clock_curr_mhz": curr["gpu_clock"],
            "gpu_util_pct":   curr["gpu_util"],
            "dc_input_w":     curr["dc_input"],
        })
# ──────────────────────────────────────────────────────────────────────────────

# ─── SENSORS STREAM READER ────────────────────────────────────────────────────
def sensors_thread():
    global running
    block = []

    proc = subprocess.Popen(
        ["sensors", "-A"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True
    )

    # Continuous polling loop
    while running:
        try:
            out = subprocess.check_output(
                ["sensors"], timeout=2
            ).decode()

            parsed = parse_sensors_block(out)

            with lock:
                state["ts"] = ts_ms()
                for k, v in parsed.items():
                    if k in state:
                        state[k] = v

            # Read PSI and memory — ground truth signals
            uma_some, uma_full = read_psi()
            mem_pct = read_mem_pct()
            with lock:
                state["uma_some"] = uma_some
                state["uma_full"] = uma_full
                state["mem_pct"]  = mem_pct
            analyze()
            emit_status()

        except Exception:
            pass

        time.sleep(0.2)
# ──────────────────────────────────────────────────────────────────────────────

# ─── SPARKVIEW LOG WATCHER ────────────────────────────────────────────────────
def sparkview_thread(log_path):
    global running
    if not log_path or not os.path.exists(log_path):
        return

    with open(log_path, "r") as f:
        f.seek(0, 2)  # seek to end — only watch new lines
        while running:
            line = f.readline()
            if line:
                line = line.strip()
                if any(kw in line for kw in
                       ["PROCHOT", "CRITICAL", "THROTTLED", "Trigger"]):
                    log_event("SPARKVIEW_SIGNAL", {"line": line})
            else:
                time.sleep(0.1)
# ──────────────────────────────────────────────────────────────────────────────

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    global running

    outdir  = sys.argv[1] if len(sys.argv) > 1 else "."
    sv_log  = sys.argv[2] if len(sys.argv) > 2 else None
    outfile = os.path.join(outdir, "events.json")

    if not QUIET: print("=== spbm_analyzer — real-time system behavior interpreter ===")
    print(f"Events → {outfile}")
    if sv_log:
        print(f"sparkview log → {sv_log}")
    print("")

    # Start threads
    t_smi = threading.Thread(target=nvidia_smi_thread, daemon=True)
    t_sen = threading.Thread(target=sensors_thread,    daemon=True)
    t_smi.start()
    t_sen.start()

    if sv_log:
        t_sv = threading.Thread(
            target=sparkview_thread, args=(sv_log,), daemon=True
        )
        t_sv.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        running = False
        print("\n\nStopping analyzer...")

    # Write events
    with open(outfile, "w") as f:
        json.dump(events, f, indent=2)

    print(f"\nEvents written: {len(events)} → {outfile}")

if __name__ == "__main__":
    main()
