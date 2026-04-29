#!/usr/bin/env python3
"""
analyze_events.py — GB10 run health summary
Reads events.json from a correlated run and prints per-phase analysis.

Usage:
    python3 analyze_events.py correlated_<host>_<ts>/events.json
    python3 analyze_events.py correlated_<host>_<ts>.zip
"""

import json
import sys
import os
import zipfile

# ─── THRESHOLDS ───────────────────────────────────────────────────────────────
TJ_MAX_C_WARNING  = 15
TJ_MAX_C_CRITICAL = 25
PEAK_TEMP_WARNING = 75
PEAK_TEMP_CRITICAL= 85
PROCHOT_WARNING   = True
RECOVERY_SLOW_S   = 120   # seconds — recovery longer than this = slow cooling
# ──────────────────────────────────────────────────────────────────────────────

RESET  = "\033[0m"
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

def color(val, warn, crit, invert=False):
    """Color a value green/yellow/red based on thresholds."""
    if invert:
        if val <= warn:   return f"{GREEN}{val}{RESET}"
        if val <= crit:   return f"{YELLOW}{val}{RESET}"
        return f"{RED}{val}{RESET}"
    else:
        if val < warn:    return f"{GREEN}{val}{RESET}"
        if val < crit:    return f"{YELLOW}{val}{RESET}"
        return f"{RED}{val}{RESET}"

def load_events(path):
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith("events.json"):
                    with zf.open(name) as f:
                        return json.load(f)
        print(f"No events.json found in {path}")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)

def fmt_w(v):
    return f"{v:.1f}W"

def fmt_c(v):
    return f"{v:.1f}°C"

def summarize(events):
    # Separate by event type
    by_type = {}
    for ev in events:
        by_type.setdefault(ev["event"], []).append(ev)

    recoveries    = by_type.get("RECOVERY_COMPLETE", [])
    thermal_rises = by_type.get("THERMAL_RISE", [])
    prochots      = by_type.get("PROCHOT_ACTIVE", [])
    collapses     = by_type.get("CLOCK_COLLAPSE", [])
    spikes        = by_type.get("POWER_SPIKE", [])
    sys_states    = by_type.get("SYSTEM_STATE", [])
    th_warnings   = by_type.get("THERMAL_WARNING", [])
    th_criticals  = by_type.get("THERMAL_CRITICAL", [])

    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD} GB10 Run Health Summary{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    # ── Overall health verdict ────────────────────────────────────────────────
    issues = []
    if prochots:
        issues.append(f"{RED}PROCHOT fired {len(prochots)}x{RESET}")
    if collapses:
        issues.append(f"{RED}Clock collapse {len(collapses)}x{RESET}")
    if th_criticals:
        issues.append(f"{RED}Thermal CRITICAL {len(th_criticals)}x{RESET}")
    elif th_warnings:
        issues.append(f"{YELLOW}Thermal WARNING {len(th_warnings)}x{RESET}")

    if not issues:
        print(f"\n  Verdict : {GREEN}{BOLD}HEALTHY{RESET}")
    else:
        print(f"\n  Verdict : {RED}{BOLD}ISSUES DETECTED{RESET}")
        for i in issues:
            print(f"            {i}")

    # ── Per-phase recovery summary ────────────────────────────────────────────
    if recoveries:
        print(f"\n{BOLD}  Per-Phase Recovery{RESET}")
        print(f"  {'Phase':<20} {'Peak Temp':>10} {'Peak Pwr':>10} "
              f"{'tj_max_c':>10} {'Avg GPU W':>10} {'Avg Pkg W':>10} {'PROCHOT':>8}")
        print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*8}")

        for r in recoveries:
            phase       = r.get("phase", "unknown")
            peak_t      = r.get("peak_temp_c", 0)
            peak_p      = r.get("peak_power_w", 0)
            tj          = r.get("peak_tj_max_c", 0)
            avg_gpu     = r.get("avg_gpu_w_energy", 0)
            avg_pkg     = r.get("avg_pkg_w_energy", 0)
            prochot     = r.get("prochot_fired", False)
            rec_t       = r.get("recovery_temp_c", 0)

            t_str  = color(peak_t,  PEAK_TEMP_WARNING,  PEAK_TEMP_CRITICAL)
            tj_str = color(tj,      TJ_MAX_C_WARNING,   TJ_MAX_C_CRITICAL)
            p_str  = f"{peak_p:.1f}W"
            pg_str = f"{avg_gpu:.1f}W"
            pp_str = f"{avg_pkg:.1f}W"
            ph_str = f"{RED}YES{RESET}" if prochot else f"{GREEN}NO{RESET}"

            print(f"  {phase:<20} {t_str:>10} {p_str:>10} "
                  f"{tj_str:>10} {pg_str:>10} {pp_str:>10} {ph_str:>8}")

    # ── Thermal rise rates ────────────────────────────────────────────────────
    if thermal_rises:
        print(f"\n{BOLD}  Thermal Rise Events{RESET}")
        for tr in thermal_rises:
            rate  = tr.get("rate_c_per_s", 0)
            tj_c  = tr.get("temp_tj_max_c", 0)
            soc   = tr.get("temp_soc_c", 0)
            dc    = tr.get("dc_input_w", 0)
            r_str = color(rate, 1.0, 2.0)
            print(f"  {DIM}ts={tr['ts']}{RESET}  "
                  f"rate={r_str}°C/s  tj_max={fmt_c(tj_c)}  "
                  f"soc={fmt_c(soc)}  dc={fmt_w(dc)}")

    # ── PROCHOT events ────────────────────────────────────────────────────────
    if prochots:
        print(f"\n{BOLD}  PROCHOT Events — Domain Attribution{RESET}")
        for p in prochots:
            print(f"  {DIM}ts={p['ts']}{RESET}")
            print(f"    Clock  : {p.get('gpu_clock_mhz',0)} MHz  "
                  f"Temp: {p.get('gpu_temp_c',0)}°C  "
                  f"tj_max_c: {p.get('tj_max_c',0)}°C")
            print(f"    DC in  : {fmt_w(p.get('dc_input_w',0))}  "
                  f"PL level: {p.get('pl_level',0)}")
            print(f"    GPU    : {fmt_w(p.get('gpu_w',0))}  "
                  f"SOC pkg: {fmt_w(p.get('soc_pkg_w',0))}  "
                  f"cpu+gpu: {fmt_w(p.get('cpu_gpu_w',0))}")
            print(f"    cpu_p  : {fmt_w(p.get('cpu_p_w',0))}  "
                  f"cpu_e: {fmt_w(p.get('cpu_e_w',0))}  "
                  f"vcore: {fmt_w(p.get('vcore_w',0))}")

    # ── Clock collapses ───────────────────────────────────────────────────────
    if collapses:
        print(f"\n{BOLD}  Clock Collapses{RESET}")
        for c in collapses:
            print(f"  {DIM}ts={c['ts']}{RESET}  "
                  f"{c.get('clock_prev_mhz',0)} → "
                  f"{RED}{c.get('clock_curr_mhz',0)} MHz{RESET}  "
                  f"util={c.get('gpu_util_pct',0)}%  "
                  f"dc={fmt_w(c.get('dc_input_w',0))}  "
                  f"prochot={c.get('prochot',0)}")

    # ── Power spikes ──────────────────────────────────────────────────────────
    if spikes:
        print(f"\n{BOLD}  Power Spikes{RESET}")
        for s in spikes:
            delta = s.get("delta_w", 0)
            d_str = color(delta, 20, 35)
            print(f"  {DIM}ts={s['ts']}{RESET}  "
                  f"delta={d_str}W  "
                  f"{fmt_w(s.get('dc_prev_w',0))} → {fmt_w(s.get('dc_curr_w',0))}  "
                  f"gpu={fmt_w(s.get('gpu_w',0))}  "
                  f"soc={fmt_w(s.get('soc_pkg_w',0))}")

    # ── Memory pressure ───────────────────────────────────────────────────────
    critical_states = [s for s in sys_states
                       if s.get("pressure") in ("CRITICAL", "DANGER")]
    if critical_states:
        print(f"\n{BOLD}  Memory Pressure Events{RESET}")
        for s in critical_states:
            p_str = (f"{RED}{s['pressure']}{RESET}"
                     if s["pressure"] == "CRITICAL"
                     else f"{YELLOW}{s['pressure']}{RESET}")
            print(f"  {DIM}ts={s['ts']}{RESET}  "
                  f"pressure={p_str}  "
                  f"root={s.get('root_cause','?')}  "
                  f"psi={s.get('uma_some',0):.2f}/{s.get('uma_full',0):.2f}  "
                  f"mem={s.get('mem_pct',0):.1f}%")

    # ── Cooling efficiency note ───────────────────────────────────────────────
    if recoveries:
        max_tj = max(r.get("peak_tj_max_c", 0) for r in recoveries)
        print(f"\n{BOLD}  Cooling Efficiency{RESET}")
        if max_tj == 0:
            print(f"  tj_max_c not available — spark_hwmon may not be installed")
        elif max_tj < TJ_MAX_C_WARNING:
            print(f"  {GREEN}Good{RESET} — peak thermal rise {max_tj:.1f}°C above ambient")
            print(f"  Consistent with active external cooling")
        elif max_tj < TJ_MAX_C_CRITICAL:
            print(f"  {YELLOW}Moderate{RESET} — peak thermal rise {max_tj:.1f}°C above ambient")
            print(f"  Stock cooling — consider external fan for sustained workloads")
        else:
            print(f"  {RED}Poor{RESET} — peak thermal rise {max_tj:.1f}°C above ambient")
            print(f"  Thermal stress — check airflow and cooling setup")

    print(f"\n{BOLD}{'='*60}{RESET}\n")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_events.py <events.json or run.zip>")
        sys.exit(1)

    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"File not found: {path}")
        sys.exit(1)

    events = load_events(path)
    if not events:
        print("No events found — was the run on GB10 with spark_hwmon installed?")
        sys.exit(0)

    summarize(events)

if __name__ == "__main__":
    main()
