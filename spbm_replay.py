#!/usr/bin/env python3
"""
spbm_replay.py v2.1 — correlated run forensic parser
Point at a correlated zip directory. Reads all available files.
Outputs one clean JSON forensic record per run.

Usage:
    python3 spbm_replay.py <correlated_run_dir>
    python3 spbm_replay.py <correlated_run_dir> --out record.json

No live polling. No threads. No nvidia-smi. Pure offline analysis.
Part of: gb10-uma-diagnostics / nvidia-uma-fault-probe
"""

import sys
import os
import re
import json
import glob
import argparse

PHASE_MARKERS    = ["START_RUN1", "START_RUN2", "START_CALIBRATION", "START_CONTENTION"]
COOLDOWN_MARKERS = ["pre-run", "post-run1", "post-run2", "post-calibration", "post-contention"]
TEMP_RE      = re.compile(r'\+(\d+\.\d+)')
CLOCK_RE     = re.compile(r'CLK=(\d+)MHz')
NOCLOCK_RE   = re.compile(r'No throttle:\s*(\d+)MHz', re.I)
TMP_RE       = re.compile(r'TMP=(\d+)C')
PWR_RE       = re.compile(r'PWR=(\d+)W')
TS_RE        = re.compile(r'^(\d{13})')
TIMEOUT_STR  = "Timeout"
NO_THROTTLE  = "No throttle"
BASELINE_STR = "Baseline clock"

def discover_files(dirpath):
    files = {}
    spbm = glob.glob(os.path.join(dirpath, "spbm_*.txt"))
    if spbm:
        files["spbm"] = spbm[0]
    for name in ["run_guard.log", "sparkview_summary.json",
                 "uma_bw_results.json", "uma_contention_results.json",
                 "peak_calibration.json", "timeline.json"]:
        path = os.path.join(dirpath, name)
        if os.path.exists(path):
            key = name.replace(".","_").replace("-","_")
            files[key] = path
    return files

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

def load_lines(path):
    try:
        with open(path) as f:
            return [l.rstrip() for l in f.readlines()]
    except Exception:
        return []

def extract_meta(dirpath, files):
    meta = {
        "unit": None, "date": None, "time": None,
        "driver": None, "cuda": None, "kernel": None,
        "platform": None, "power_available": False,
        "thermal_source": "UNKNOWN",
        "run_dir": os.path.basename(dirpath.rstrip("/"))
    }
    if "spbm" in files:
        m = re.search(r'spbm_([a-zA-Z0-9\-]+)_(\d{8})_(\d{6})', files["spbm"])
        if m:
            meta["unit"] = m.group(1)
            meta["date"] = m.group(2)
            meta["time"] = m.group(3)

    sv = load_json(files.get("sparkview_summary_json", ""))
    if sv:
        meta["driver"]                   = sv.get("driver")
        meta["cuda"]                     = sv.get("cuda")
        meta["kernel"]                   = sv.get("kernel")
        meta["sparkview_trigger"]        = sv.get("trigger")
        meta["sparkview_peak_gpu_temp_c"] = sv.get("peak_gpu_temp_c")
        meta["sparkview_peak_cpu_temp_c"] = sv.get("peak_cpu_temp_c")
        meta["sparkview_duration_s"]     = sv.get("duration_seconds")

    bw = load_json(files.get("uma_bw_results_json", ""))
    if bw:
        plat = bw.get("platform", {})
        meta["platform"] = plat.get("uma_type")
        meta["gpu_name"] = plat.get("gpu_name")
        meta["sm_major"] = plat.get("sm_major")
        meta["sm_minor"] = plat.get("sm_minor")

    if "spbm" in files:
        with open(files["spbm"]) as f:
            content = f.read(4096)
        if "spbm" in content.lower():
            meta["power_available"] = True
            meta["thermal_source"]  = "SPBM_HWMON"
        elif TEMP_RE.search(content):
            meta["thermal_source"]  = "ACPITZ_RAW"
        else:
            meta["thermal_source"]  = "UNKNOWN"
    return meta

def parse_spbm(path):
    lines = load_lines(path)
    phases = {}
    current = None
    current_lines = []
    for line in lines:
        for marker in PHASE_MARKERS:
            if line.strip().startswith(marker):
                if current and current_lines:
                    phases[current] = current_lines
                current = marker
                current_lines = []
                break
        if current:
            current_lines.append(line)
    if current and current_lines:
        phases[current] = current_lines

    result = {}
    for phase, plines in phases.items():
        temps = []
        ts_vals = []
        for line in plines:
            m = TS_RE.match(line.strip())
            if m:
                ts_vals.append(int(m.group(1)))
            for tm in TEMP_RE.finditer(line):
                v = float(tm.group(1))
                if 0 < v < 120:
                    temps.append(v)
        result[phase] = {
            "ts_start":   ts_vals[0] if ts_vals else None,
            "ts_end":     ts_vals[-1] if ts_vals else None,
            "duration_s": round((ts_vals[-1]-ts_vals[0])/1000.0,1) if len(ts_vals)>1 else None,
            "temp_max":   round(max(temps),1) if temps else None,
            "temp_min":   round(min(temps),1) if temps else None,
            "temp_mean":  round(sum(temps)/len(temps),1) if temps else None,
            "samples":    len(temps),
        }
    return result

def parse_run_guard(path):
    lines = load_lines(path)
    sections = {}
    current = None
    current_lines = []
    for line in lines:
        for marker in COOLDOWN_MARKERS:
            if f"[{marker}]" in line:
                if current and current_lines:
                    sections[current] = current_lines
                current = marker
                current_lines = []
                break
        if current:
            current_lines.append(line)
    if current and current_lines:
        sections[current] = current_lines

    result = {}
    for section, slines in sections.items():
        clocks = []
        temps  = []
        pwrs   = []
        ts_vals = []
        timeout = False
        no_throttle = False
        throttle = False
        baseline_clock = None
        baseline_temp  = None

        for line in slines:
            m = TS_RE.match(line.strip())
            if m:
                ts_vals.append(int(m.group(1)))

            # Clock — standard CLK= format
            cm = CLOCK_RE.search(line)
            if cm:
                clocks.append(int(cm.group(1)))

            # Clock — "No throttle: 2405MHz" format
            ncm = NOCLOCK_RE.search(line)
            if ncm:
                clocks.append(int(ncm.group(1)))

            tm = TMP_RE.search(line)
            if tm:
                temps.append(int(tm.group(1)))

            pm = PWR_RE.search(line)
            if pm:
                pwrs.append(int(pm.group(1)))

            if TIMEOUT_STR in line:
                timeout = True

            if NO_THROTTLE in line:
                no_throttle = True

            # Fix: exclude "Checking for throttle" and "No throttle" lines
            if ("throttle" in line.lower()
                    and NO_THROTTLE not in line
                    and "checking for" not in line.lower()):
                throttle = True

            if BASELINE_STR in line:
                bcm = CLOCK_RE.search(line)
                btm = re.search(r'temp:\s*(\d+)', line, re.I)
                if bcm:
                    baseline_clock = int(bcm.group(1))
                if btm:
                    baseline_temp = int(btm.group(1))

        result[section] = {
            "ts_start":           ts_vals[0] if ts_vals else None,
            "ts_end":             ts_vals[-1] if ts_vals else None,
            "duration_s":         round((ts_vals[-1]-ts_vals[0])/1000.0,1) if len(ts_vals)>1 else None,
            "clock_mhz":          clocks[0] if clocks else None,
            "clock_stable":       len(set(clocks))==1 if clocks else None,
            "temp_max_c":         max(temps) if temps else None,
            "temp_min_c":         min(temps) if temps else None,
            "temp_mean_c":        round(sum(temps)/len(temps),1) if temps else None,
            "pwr_max_w":          max(pwrs) if pwrs else None,
            "pwr_min_w":          min(pwrs) if pwrs else None,
            "pwr_mean_w":         round(sum(pwrs)/len(pwrs),1) if pwrs else None,
            "timeout":            timeout,
            "throttle":           throttle,
            "no_throttle":        no_throttle,
            "baseline_clock_mhz": baseline_clock,
            "baseline_temp_c":    baseline_temp,
            "reached_baseline":   not timeout,
        }
    return result

def extract_bandwidth(files):
    bw = load_json(files.get("uma_bw_results_json", ""))
    if not bw:
        return {}
    r  = bw.get("results", {})
    pc = load_json(files.get("peak_calibration_json", ""))
    return {
        "gpu_read_gbs":         r.get("gpu_read_gbs"),
        "gpu_read_stddev":      r.get("gpu_read_stddev"),
        "gpu_write_gbs":        r.get("gpu_write_gbs"),
        "gpu_write_stddev":     r.get("gpu_write_stddev"),
        "cpu_read_gbs":         r.get("cpu_read_gbs"),
        "cpu_write_gbs":        r.get("cpu_write_gbs"),
        "concurrent_total_gbs": r.get("concurrent_total_gbs"),
        "empirical_peak_gbs":   pc.get("empirical_peak_bw_gbps") if pc else None,
        "raw_gpu_read_runs":    bw.get("raw_runs", {}).get("gpu_read"),
    }

def extract_contention(files):
    ct = load_json(files.get("uma_contention_results_json", ""))
    if not ct:
        return {}
    out = {}
    for r in ct.get("results", []):
        mode = r.get("mode","").replace("+","_plus_").replace("-","_")
        out[mode] = {
            "total_gbs":      r.get("total_bw_gbs"),
            "efficiency_pct": r.get("efficiency_pct"),
            "gpu_drop_pct":   r.get("gpu_drop_pct"),
        }
    return out

def build_summary(meta, spbm_phases, rg_sections, bandwidth, contention):
    probe_temps    = [p["temp_max"] for p in spbm_phases.values() if p.get("temp_max")]
    baseline_temps = [s["baseline_temp_c"] for s in rg_sections.values() if s.get("baseline_temp_c")]
    clocks         = [s["clock_mhz"] for s in rg_sections.values() if s.get("clock_mhz")]
    timeouts       = [s["timeout"] for s in rg_sections.values()]
    pwrs           = [s["pwr_mean_w"] for s in rg_sections.values() if s.get("pwr_mean_w")]
    c2c_eff = None
    if "cpu_write_plus_gpu_read" in contention:
        c2c_eff = contention["cpu_write_plus_gpu_read"].get("efficiency_pct")
    return {
        "unit":                    meta.get("unit"),
        "date":                    meta.get("date"),
        "driver":                  meta.get("driver"),
        "cuda":                    meta.get("cuda"),
        "kernel":                  meta.get("kernel"),
        "platform":                meta.get("platform"),
        "thermal_source":          meta.get("thermal_source"),
        "power_available":         meta.get("power_available"),
        "gpu_name":                meta.get("gpu_name"),
        "probe_temp_max_c":        max(probe_temps) if probe_temps else None,
        "probe_temp_baseline_c":   min(baseline_temps) if baseline_temps else None,
        "clock_mhz":               clocks[0] if clocks else None,
        "clock_stable":            len(set(clocks))==1 if clocks else None,
        "throttle_detected":       any(s.get("throttle") for s in rg_sections.values()),
        "all_cooldowns_timed_out": all(timeouts) if timeouts else None,
        "pwr_mean_w":              round(sum(pwrs)/len(pwrs),1) if pwrs else None,
        "gpu_read_gbs":            bandwidth.get("gpu_read_gbs"),
        "gpu_write_gbs":           bandwidth.get("gpu_write_gbs"),
        "empirical_peak_gbs":      bandwidth.get("empirical_peak_gbs"),
        "c2c_efficiency_pct":      c2c_eff,
        "sparkview_peak_gpu_temp_c": meta.get("sparkview_peak_gpu_temp_c"),
        "sparkview_peak_cpu_temp_c": meta.get("sparkview_peak_cpu_temp_c"),
        "sparkview_trigger":         meta.get("sparkview_trigger"),
    }

def replay(dirpath, out_path=None):
    if not os.path.isdir(dirpath):
        print(f"Error: not a directory: {dirpath}", file=sys.stderr)
        sys.exit(1)
    files       = discover_files(dirpath)
    meta        = extract_meta(dirpath, files)
    spbm_phases = parse_spbm(files["spbm"]) if "spbm" in files else {}
    rg_sections = parse_run_guard(files["run_guard_log"]) if "run_guard_log" in files else {}
    bandwidth   = extract_bandwidth(files)
    contention  = extract_contention(files)
    summary     = build_summary(meta, spbm_phases, rg_sections, bandwidth, contention)
    record = {
        "summary":    summary,
        "meta":       meta,
        "thermal":    spbm_phases,
        "cooldown":   rg_sections,
        "bandwidth":  bandwidth,
        "contention": contention,
    }
    out = json.dumps(record, indent=2)
    if out_path:
        with open(out_path, "w") as f:
            f.write(out)
        print(f"Written: {out_path}", file=sys.stderr)
    else:
        print(out)

def main():
    parser = argparse.ArgumentParser(description="spbm_replay v2.1 — correlated run forensic parser")
    parser.add_argument("run_dir", help="Path to correlated run directory")
    parser.add_argument("--out", help="Write JSON to file instead of stdout")
    args = parser.parse_args()
    replay(args.run_dir, out_path=args.out)

if __name__ == "__main__":
    main()
