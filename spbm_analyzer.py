#!/usr/bin/env python3
"""
spbm_analyzer.py v2.0 — GB10 system behavior analyzer
Real-time power, thermal, and memory pressure correlation.

Signal stack:
  Behavioral probes  → bandwidth, contention, atomic latency (external tools)
  Power response     → spark_hwmon: instantaneous + energy accumulators
  Thermal response   → spark_hwmon: per-zone temperatures + tj_max_c
  Stall detection    → PSI /proc/pressure/memory
  Throttle cause     → prochot, pl_level, clock (nvidia-smi)

What we observe:    complete external observability
What we cannot see: UVM internal state, memory controller internals,
                    fabric coherence traffic — not exposed on GB10

Part of: gb10-uma-diagnostics
"""

import subprocess
import threading
import time
import json
import sys
import os
import re
from collections import deque

# ─── THRESHOLDS ───────────────────────────────────────────────────────────────
THROTTLE_CLOCK_MHZ      = 850     # below this under load = PROCHOT/power cap
POWER_SPIKE_W           = 15      # delta watts in one sample = spike
POWER_RISE_RATE_W_S     = 8       # watts/sec sustained rise = warning
GPU_UTIL_THROTTLE_PCT   = 80      # util above this + low clock = throttle
SAMPLE_HISTORY          = 20      # samples to keep for derivative

# Recovery model thresholds — must match run_correlated.sh
TEMP_MARGIN_C           = 2       # degrees above baseline = not recovered
POWER_MARGIN_W          = 2       # watts variance = not stable
STABILITY_SAMPLES       = 3       # consecutive samples for stability

# Thermal rise/fall profiling
THERMAL_RISE_RATE_C_S   = 0.5     # degrees/sec = significant thermal rise
TJ_MAX_C_WARNING        = 15      # thermal rise above ambient = warning
TJ_MAX_C_CRITICAL       = 25      # thermal rise above ambient = critical
# ──────────────────────────────────────────────────────────────────────────────

QUIET = not sys.stdout.isatty()

# ─── STATE ────────────────────────────────────────────────────────────────────
state = {
    # nvidia-smi
    "gpu_clock":    0,
    "gpu_util":     0,
    "gpu_temp":     0,
    "gpu_power_smi": 0.0,   # from nvidia-smi power.draw

    # spark_hwmon — instantaneous power (14 channels)
    "sys_total":    0.0,
    "dc_input":     0.0,
    "gpu_power":    0.0,
    "soc_pkg":      0.0,
    "cpu_gpu":      0.0,
    "cpu_p":        0.0,
    "cpu_e":        0.0,
    "vcore":        0.0,
    "prereg":       0.0,
    "dla":          0.0,
    "pl1":          0.0,
    "pl2":          0.0,
    "syspl1":       0.0,
    "syspl2":       0.0,

    # spark_hwmon — energy accumulators (millijoules, cumulative)
    # Use delta/time for true average power — avoids PID loop oscillation
    "energy_pkg":   0.0,
    "energy_gpu":   0.0,
    "energy_cpu_p": 0.0,
    "energy_cpu_e": 0.0,

    # spark_hwmon — temperature zones
    "temp_tj_max":      0.0,    # package Tj max
    "temp_gpu":         0.0,    # GPU zone
    "temp_soc":         0.0,    # SoC
    "temp_dla":         0.0,    # DLA
    "temp_cpu_p_clu0":  0.0,    # P-core cluster 0
    "temp_cpu_e_clu0":  0.0,    # E-core cluster 0
    "temp_cpu_p_clu1":  0.0,    # P-core cluster 1
    "temp_cpu_e_clu1":  0.0,    # E-core cluster 1
    "tj_max_c":         0.0,    # thermal rise above ambient (key health signal)

    # control
    "prochot":      0,
    "pl_level":     0,

    # PSI — ground truth for memory stall
    "uma_some":     0.0,
    "uma_full":     0.0,
    "mem_pct":      0.0,

    "ts":           0,
}

# Per-phase tracking
phase_state = {
    "name":             "idle",
    "start_ts":         0,
    "start_energy_pkg": 0.0,
    "start_energy_gpu": 0.0,
    "start_temp":       0.0,
    "peak_temp":        0.0,
    "peak_power":       0.0,
    "peak_tj_max_c":    0.0,
    "prochot_fired":    False,
}

history         = deque(maxlen=SAMPLE_HISTORY)
temp_history    = deque(maxlen=10)
power_history   = deque(maxlen=10)
events          = []
lock            = threading.Lock()
running         = True
baseline_temp   = 0.0
baseline_power  = 0.0
# ──────────────────────────────────────────────────────────────────────────────

def ts_ms():
    return int(time.time() * 1000)

def read_psi():
    try:
        some = full = 0.0
        with open("/proc/pressure/memory") as f:
            for line in f:
                if line.startswith("some"):
                    some = float(line.split()[1].split("=")[1])
                elif line.startswith("full"):
                    full = float(line.split()[1].split("=")[1])
        return some, full
    except Exception:
        return 0.0, 0.0

def read_mem_pct():
    try:
        with open("/proc/meminfo") as f:
            lines = {l.split()[0]: int(l.split()[1])
                     for l in f if len(l.split()) >= 2}
        total = lines.get("MemTotal:", 0)
        avail = lines.get("MemAvailable:", 0)
        return (total - avail) / total * 100.0 if total > 0 else 0.0
    except Exception:
        return 0.0

def log_event(event_type, data):
    ev = {"ts": ts_ms(), "event": event_type, **data}
    with lock:
        events.append(ev)
    if not QUIET:
        print(f"\n🔴 EVENT [{event_type}] {json.dumps(data)}", flush=True)

def classify_root_cause(uma_some, uma_full, clock_mhz, prochot):
    memory = (uma_some > 0.5) or (uma_full > 0.1)
    power  = (0 < clock_mhz < THROTTLE_CLOCK_MHZ) or (prochot == 1)
    if memory and power: return "MEMORY+POWER"
    if memory:           return "MEMORY"
    if power:            return "POWER"
    return "UNKNOWN"

def classify_pressure(mem_pct, uma_some, uma_full):
    if uma_full > 0.15:                              return "CRITICAL"
    if mem_pct > 90 and uma_some > 0.7:              return "DANGER"
    if mem_pct > 85 and uma_some > 0.5:              return "WARNING"
    return "SAFE"

# ─── SENSOR PATTERNS ──────────────────────────────────────────────────────────
SENSOR_PATTERNS = {
    # instantaneous power
    "sys_total":    re.compile(r"sys_total.*?([0-9]+\.?[0-9]*)\s*W",    re.I),
    "dc_input":     re.compile(r"dc_input.*?([0-9]+\.?[0-9]*)\s*W",     re.I),
    "gpu_power":    re.compile(r"^\s*gpu\s.*?([0-9]+\.?[0-9]*)\s*W",    re.I | re.M),
    "soc_pkg":      re.compile(r"soc_pkg.*?([0-9]+\.?[0-9]*)\s*W",      re.I),
    "cpu_gpu":      re.compile(r"cpu_gpu.*?([0-9]+\.?[0-9]*)\s*W",      re.I),
    "cpu_p":        re.compile(r"cpu_p\b.*?([0-9]+\.?[0-9]*)\s*W",      re.I),
    "cpu_e":        re.compile(r"cpu_e\b.*?([0-9]+\.?[0-9]*)\s*W",      re.I),
    "vcore":        re.compile(r"vcore.*?([0-9]+\.?[0-9]*)\s*W",        re.I),
    "prereg":       re.compile(r"prereg.*?([0-9]+\.?[0-9]*)\s*W",       re.I),
    "dla":          re.compile(r"\bdla\b.*?([0-9]+\.?[0-9]*)\s*W",      re.I),
    "pl1":          re.compile(r"\bpl1\b.*?([0-9]+\.?[0-9]*)\s*W",      re.I),
    "pl2":          re.compile(r"\bpl2\b.*?([0-9]+\.?[0-9]*)\s*W",      re.I),
    "syspl1":       re.compile(r"syspl1.*?([0-9]+\.?[0-9]*)\s*W",       re.I),
    "syspl2":       re.compile(r"syspl2.*?([0-9]+\.?[0-9]*)\s*W",       re.I),
    # energy accumulators (millijoules)
    "energy_pkg":   re.compile(r"pkg.*?([0-9]+\.?[0-9]*)\s*mJ",         re.I),
    "energy_gpu":   re.compile(r"gpu.*?([0-9]+\.?[0-9]*)\s*mJ",         re.I),
    "energy_cpu_p": re.compile(r"cpu_p.*?([0-9]+\.?[0-9]*)\s*mJ",       re.I),
    "energy_cpu_e": re.compile(r"cpu_e.*?([0-9]+\.?[0-9]*)\s*mJ",       re.I),
    # temperature zones
    "temp_tj_max":      re.compile(r"tj_max\b.*?([0-9]+\.?[0-9]*)\s*[°C]",    re.I),
    "temp_gpu":         re.compile(r"^\s*gpu\s.*?([0-9]+\.?[0-9]*)\s*[°C]",   re.I | re.M),
    "temp_soc":         re.compile(r"\bsoc\b.*?([0-9]+\.?[0-9]*)\s*[°C]",     re.I),
    "temp_dla":         re.compile(r"\bdla\b.*?([0-9]+\.?[0-9]*)\s*[°C]",     re.I),
    "temp_cpu_p_clu0":  re.compile(r"cpu_p_clu0.*?([0-9]+\.?[0-9]*)\s*[°C]",  re.I),
    "temp_cpu_e_clu0":  re.compile(r"cpu_e_clu0.*?([0-9]+\.?[0-9]*)\s*[°C]",  re.I),
    "temp_cpu_p_clu1":  re.compile(r"cpu_p_clu1.*?([0-9]+\.?[0-9]*)\s*[°C]",  re.I),
    "temp_cpu_e_clu1":  re.compile(r"cpu_e_clu1.*?([0-9]+\.?[0-9]*)\s*[°C]",  re.I),
    "tj_max_c":         re.compile(r"tj_max_c.*?([0-9]+\.?[0-9]*)",             re.I),
    # control
    "prochot":      re.compile(r"prochot.*?([01])",         re.I),
    "pl_level":     re.compile(r"pl_level.*?([0-9]+)",      re.I),
}

def parse_sensors_block(block):
    parsed = {}

    # Detection layer — check if spark_hwmon (spbm) is present
    spbm_present = "spbm" in block.lower()

    if spbm_present:
        # Full spark_hwmon path — all channels available
        for field, pattern in SENSOR_PATTERNS.items():
            m = pattern.search(block)
            if m:
                parsed[field] = float(m.group(1))
    else:
        # Fallback path — acpitz only, no power channels
        # Collect all temp values from acpitz block
        acpitz_temps = []
        in_acpitz = False
        for line in block.splitlines():
            if "acpitz" in line.lower():
                in_acpitz = True
            elif in_acpitz and line.strip() == "":
                in_acpitz = False
            if in_acpitz:
                m = re.search(r'\+([0-9]+\.[0-9]+)', line)
                if m:
                    acpitz_temps.append(float(m.group(1)))

        if acpitz_temps:
            # Map generically — highest observed temp as platform thermal indicator
            parsed["_acpitz_max"]      = max(acpitz_temps)
            parsed["_acpitz_min"]      = min(acpitz_temps)
            parsed["_acpitz_mean"]     = round(sum(acpitz_temps) / len(acpitz_temps), 1)
            parsed["_acpitz_zones"]    = len(acpitz_temps)
            parsed["_power_available"] = 0.0
        else:
            parsed["_power_available"] = 0.0

    return parsed

# ──────────────────────────────────────────────────────────────────────────────

# ─── NVIDIA-SMI POLLER ────────────────────────────────────────────────────────
def nvidia_smi_thread():
    global running
    while running:
        try:
            out = subprocess.check_output([
                "nvidia-smi",
                "--query-gpu=clocks.gr,utilization.gpu,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits"
            ], timeout=2).decode().strip()
            parts = out.split(",")
            if len(parts) == 4:
                with lock:
                    state["gpu_clock"]    = int(parts[0].strip())
                    state["gpu_util"]     = int(parts[1].strip())
                    state["gpu_temp"]     = int(parts[2].strip())
                    try:
                        state["gpu_power_smi"] = float(parts[3].strip())
                    except ValueError:
                        pass  # N/A on GB10
        except Exception:
            pass
        time.sleep(0.5)
# ──────────────────────────────────────────────────────────────────────────────

# ─── THERMAL RISE/FALL PROFILING ─────────────────────────────────────────────
def is_cooling(th):
    """True if temp still on downward slope — requires 3 samples."""
    if len(th) < 3:
        return True
    return th[-1] <= th[-2] <= th[-3]

def power_stable(ph):
    """True if power variance across last STABILITY_SAMPLES is within margin."""
    if len(ph) < STABILITY_SAMPLES:
        return False
    window = list(ph)[-STABILITY_SAMPLES:]
    return (max(window) - min(window)) <= POWER_MARGIN_W

def compute_energy_avg_power(energy_start, energy_curr, ts_start, ts_curr):
    """True average power from energy accumulator delta. More accurate than
    instantaneous readings which oscillate due to 100ms PID loop."""
    dt_s = (ts_curr - ts_start) / 1000.0
    if dt_s <= 0:
        return 0.0
    delta_mj = energy_curr - energy_start
    return (delta_mj / 1000.0) / dt_s  # watts
# ──────────────────────────────────────────────────────────────────────────────

# ─── ANALYZER ─────────────────────────────────────────────────────────────────
def analyze():
    with lock:
        curr = dict(state)

    temp_history.append(curr["temp_tj_max"] or curr["gpu_temp"])
    power_history.append(curr["dc_input"] or curr["sys_total"])
    history.append(curr)

    if len(history) < 2:
        return

    prev = list(history)[-2]

    # ── 1. Power spike ────────────────────────────────────────────────────────
    dc_delta = curr["dc_input"] - prev["dc_input"]
    if dc_delta >= POWER_SPIKE_W:
        log_event("POWER_SPIKE", {
            "dc_prev_w":  prev["dc_input"],
            "dc_curr_w":  curr["dc_input"],
            "delta_w":    round(dc_delta, 1),
            "gpu_w":      curr["gpu_power"],
            "soc_pkg_w":  curr["soc_pkg"],
            "cpu_gpu_w":  curr["cpu_gpu"],
        })

    # ── 2. Sustained power rise ───────────────────────────────────────────────
    if len(history) >= 5:
        oldest = list(history)[-5]
        elapsed_s = (curr["ts"] - oldest["ts"]) / 1000.0
        if elapsed_s > 0:
            rise_rate = (curr["dc_input"] - oldest["dc_input"]) / elapsed_s
            if rise_rate >= POWER_RISE_RATE_W_S:
                log_event("POWER_RISING", {
                    "rate_w_per_s": round(rise_rate, 2),
                    "dc_input_w":   curr["dc_input"],
                    "gpu_w":        curr["gpu_power"],
                    "cpu_p_w":      curr["cpu_p"],
                    "cpu_e_w":      curr["cpu_e"],
                })

    # ── 3. Thermal rise ───────────────────────────────────────────────────────
    if len(history) >= 5:
        oldest = list(history)[-5]
        elapsed_s = (curr["ts"] - oldest["ts"]) / 1000.0
        t_curr = curr["temp_tj_max"] or curr["gpu_temp"]
        t_old  = oldest["temp_tj_max"] or oldest["gpu_temp"]
        if elapsed_s > 0:
            rise_rate_c = (t_curr - t_old) / elapsed_s
            if rise_rate_c >= THERMAL_RISE_RATE_C_S:
                log_event("THERMAL_RISE", {
                    "rate_c_per_s":  round(rise_rate_c, 2),
                    "temp_tj_max_c": curr["temp_tj_max"],
                    "temp_gpu_c":    curr["temp_gpu"] or curr["gpu_temp"],
                    "temp_soc_c":    curr["temp_soc"],
                    "dc_input_w":    curr["dc_input"],
                })

    # ── 4. tj_max_c threshold ─────────────────────────────────────────────────
    tj = curr["tj_max_c"]
    if tj > 0:
        if tj >= TJ_MAX_C_CRITICAL and prev["tj_max_c"] < TJ_MAX_C_CRITICAL:
            log_event("THERMAL_CRITICAL", {
                "tj_max_c":      tj,
                "temp_tj_max_c": curr["temp_tj_max"],
                "dc_input_w":    curr["dc_input"],
                "gpu_w":         curr["gpu_power"],
            })
        elif tj >= TJ_MAX_C_WARNING and prev["tj_max_c"] < TJ_MAX_C_WARNING:
            log_event("THERMAL_WARNING", {
                "tj_max_c":      tj,
                "temp_tj_max_c": curr["temp_tj_max"],
                "dc_input_w":    curr["dc_input"],
            })

    # ── 5. Throttle under load ────────────────────────────────────────────────
    if (curr["gpu_util"] >= GPU_UTIL_THROTTLE_PCT and
            0 < curr["gpu_clock"] < THROTTLE_CLOCK_MHZ):
        log_event("THROTTLE_UNDER_LOAD", {
            "gpu_clock_mhz": curr["gpu_clock"],
            "gpu_util_pct":  curr["gpu_util"],
            "dc_input_w":    curr["dc_input"],
            "prochot":       curr["prochot"],
        })

    # ── 6. PROCHOT fires ──────────────────────────────────────────────────────
    if curr["prochot"] == 1 and prev["prochot"] == 0:
        with lock:
            phase_state["prochot_fired"] = True
        log_event("PROCHOT_ACTIVE", {
            "gpu_clock_mhz": curr["gpu_clock"],
            "gpu_temp_c":    curr["gpu_temp"],
            "dc_input_w":    curr["dc_input"],
            "pl_level":      curr["pl_level"],
            "tj_max_c":      curr["tj_max_c"],
            # domain attribution — who caused it
            "gpu_w":         curr["gpu_power"],
            "soc_pkg_w":     curr["soc_pkg"],
            "cpu_p_w":       curr["cpu_p"],
            "cpu_e_w":       curr["cpu_e"],
            "vcore_w":       curr["vcore"],
        })

    # ── 7. Power limit level change ───────────────────────────────────────────
    if curr["pl_level"] != prev["pl_level"]:
        log_event("PL_LEVEL_CHANGE", {
            "pl_level_prev": prev["pl_level"],
            "pl_level_curr": curr["pl_level"],
            "dc_input_w":    curr["dc_input"],
            "gpu_clock_mhz": curr["gpu_clock"],
        })

    # ── 8. Clock collapse ─────────────────────────────────────────────────────
    if (prev["gpu_clock"] > THROTTLE_CLOCK_MHZ and
            0 < curr["gpu_clock"] < THROTTLE_CLOCK_MHZ):
        log_event("CLOCK_COLLAPSE", {
            "clock_prev_mhz": prev["gpu_clock"],
            "clock_curr_mhz": curr["gpu_clock"],
            "gpu_util_pct":   curr["gpu_util"],
            "dc_input_w":     curr["dc_input"],
            "prochot":        curr["prochot"],
        })

    # ── 9. Thermal recovery detected ─────────────────────────────────────────
    t_curr = curr["temp_tj_max"] or curr["gpu_temp"]
    if (t_curr <= baseline_temp + TEMP_MARGIN_C and
            not is_cooling(temp_history) and
            power_stable(power_history)):
        # Only emit once per phase — when coming down from a run
        if phase_state["peak_temp"] > baseline_temp + TEMP_MARGIN_C:
            avg_gpu_w = compute_energy_avg_power(
                phase_state["start_energy_gpu"],
                curr["energy_gpu"],
                phase_state["start_ts"],
                curr["ts"]
            )
            avg_pkg_w = compute_energy_avg_power(
                phase_state["start_energy_pkg"],
                curr["energy_pkg"],
                phase_state["start_ts"],
                curr["ts"]
            )
            log_event("RECOVERY_COMPLETE", {
                "phase":            phase_state["name"],
                "peak_temp_c":      phase_state["peak_temp"],
                "peak_power_w":     phase_state["peak_power"],
                "peak_tj_max_c":    phase_state["peak_tj_max_c"],
                "prochot_fired":    phase_state["prochot_fired"],
                "avg_gpu_w_energy": round(avg_gpu_w, 2),
                "avg_pkg_w_energy": round(avg_pkg_w, 2),
                "recovery_temp_c":  t_curr,
                "recovery_power_w": curr["dc_input"],
            })
            # Reset peak tracking after recovery
            with lock:
                phase_state["peak_temp"]     = 0.0
                phase_state["peak_power"]    = 0.0
                phase_state["peak_tj_max_c"] = 0.0
                phase_state["prochot_fired"] = False

    # ── 10. Update phase peaks ────────────────────────────────────────────────
    with lock:
        t = curr["temp_tj_max"] or curr["gpu_temp"]
        if t > phase_state["peak_temp"]:
            phase_state["peak_temp"] = t
        if curr["dc_input"] > phase_state["peak_power"]:
            phase_state["peak_power"] = curr["dc_input"]
        if curr["tj_max_c"] > phase_state["peak_tj_max_c"]:
            phase_state["peak_tj_max_c"] = curr["tj_max_c"]

    # ── 11. Root cause + pressure state change ────────────────────────────────
    root     = classify_root_cause(curr["uma_some"], curr["uma_full"],
                                   curr["gpu_clock"], curr["prochot"])
    pressure = classify_pressure(curr["mem_pct"], curr["uma_some"],
                                 curr["uma_full"])
    prev_root     = classify_root_cause(prev["uma_some"], prev["uma_full"],
                                        prev["gpu_clock"], prev["prochot"])
    prev_pressure = classify_pressure(prev["mem_pct"], prev["uma_some"],
                                      prev["uma_full"])

    if root != prev_root or pressure != prev_pressure:
        log_event("SYSTEM_STATE", {
            "root_cause":    root,
            "pressure":      pressure,
            "uma_some":      curr["uma_some"],
            "uma_full":      curr["uma_full"],
            "mem_pct":       round(curr["mem_pct"], 1),
            "gpu_clock_mhz": curr["gpu_clock"],
            "prochot":       curr["prochot"],
            "tj_max_c":      curr["tj_max_c"],
        })
# ──────────────────────────────────────────────────────────────────────────────

# ─── SENSORS THREAD ───────────────────────────────────────────────────────────
def read_hwmon_sysfs():
    """Read thermal zones directly from sysfs — no lm-sensors dependency.
    Returns dict with _acpitz_* fields populated from /sys/class/hwmon/.
    Works on any GB10 unit regardless of lm-sensors install state."""
    parsed = {}
    acpitz_temps = []
    try:
        hwmon_base = "/sys/class/hwmon"
        for hwmon_dir in sorted(os.listdir(hwmon_base)):
            hwmon_path = os.path.join(hwmon_base, hwmon_dir)
            name_file = os.path.join(hwmon_path, "name")
            if not os.path.exists(name_file):
                continue
            with open(name_file) as f:
                name = f.read().strip().lower()
            if "acpitz" not in name:
                continue
            for entry in sorted(os.listdir(hwmon_path)):
                if not entry.startswith("temp") or not entry.endswith("_input"):
                    continue
                try:
                    with open(os.path.join(hwmon_path, entry)) as f:
                        millideg = int(f.read().strip())
                    acpitz_temps.append(millideg / 1000.0)
                except Exception:
                    continue
    except Exception:
        pass
    if acpitz_temps:
        parsed["_acpitz_max"]      = max(acpitz_temps)
        parsed["_acpitz_min"]      = min(acpitz_temps)
        parsed["_acpitz_mean"]     = round(sum(acpitz_temps) / len(acpitz_temps), 1)
        parsed["_acpitz_zones"]    = len(acpitz_temps)
        parsed["_power_available"] = 0.0
    return parsed


def sensors_thread():
    global running
    while running:
        try:
            parsed = read_hwmon_sysfs()
            try:
                out = subprocess.check_output(
                    ["sensors"], timeout=2
                ).decode()
                sensors_parsed = parse_sensors_block(out)
                parsed.update(sensors_parsed)
            except Exception:
                pass
            with lock:
                state["ts"] = ts_ms()
                for k, v in parsed.items():
                    if k in state:
                        state[k] = v
            uma_some, uma_full = read_psi()
            mem_pct = read_mem_pct()
            with lock:
                state["uma_some"] = uma_some
                state["uma_full"] = uma_full
                state["mem_pct"]  = mem_pct
            analyze()
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
        f.seek(0, 2)
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
    global running, baseline_temp, baseline_power

    outdir  = sys.argv[1] if len(sys.argv) > 1 else "."
    sv_log  = sys.argv[2] if len(sys.argv) > 2 else None
    outfile = os.path.join(outdir, "events.json")

    if not QUIET:
        print("=== spbm_analyzer v2.0 — GB10 system behavior analyzer ===")
    print(f"Events → {outfile}")
    if sv_log:
        print(f"sparkview log → {sv_log}")

    # Start threads
    threading.Thread(target=nvidia_smi_thread, daemon=True).start()
    threading.Thread(target=sensors_thread,    daemon=True).start()
    if sv_log:
        threading.Thread(target=sparkview_thread,
                         args=(sv_log,), daemon=True).start()

    # Wait for first samples then capture baseline
    time.sleep(2)
    with lock:
        baseline_temp  = state["temp_tj_max"] or state["gpu_temp"] or 45.0
        baseline_power = state["dc_input"] or state["sys_total"] or 30.0
        phase_state["start_ts"]         = ts_ms()
        phase_state["start_energy_pkg"] = state["energy_pkg"]
        phase_state["start_energy_gpu"] = state["energy_gpu"]

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        running = False

    with open(outfile, "w") as f:
        json.dump(events, f, indent=2)
    print(f"\nEvents written: {len(events)} → {outfile}")

if __name__ == "__main__":
    main()
