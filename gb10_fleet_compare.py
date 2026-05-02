#!/usr/bin/env python3
"""
gb10_fleet_compare.py v1.1 — GB10 fleet health comparator
Reads all record_*.json files from a directory.
Outputs a comparison table across units, runs, driver versions.

Usage:
    python3 gb10_fleet_compare.py <records_dir>
    python3 gb10_fleet_compare.py <records_dir> --out fleet.json
    python3 gb10_fleet_compare.py <records_dir> --text

Part of: gb10-uma-diagnostics / nvidia-uma-fault-probe
"""

import sys
import os
import json
import re
import glob
import argparse
from datetime import datetime

# ─── FIELDS TO COMPARE ────────────────────────────────────────────────────────
SUMMARY_FIELDS = [
    ("unit",                      "Unit"),
    ("date",                      "Date"),
    ("driver",                    "Driver"),
    ("cuda",                      "CUDA"),
    ("kernel",                    "Kernel"),
    ("thermal_source",            "Thermal source"),
    ("power_available",           "Power avail"),

    ("probe_temp_baseline_c",     "Baseline temp °C"),
    ("probe_temp_max_c",          "Probe temp max °C"),

    ("clock_mhz",                 "Clock MHz"),
    ("clock_stable",              "Clock stable"),
    ("throttle_detected",         "Throttle"),
    ("all_cooldowns_timed_out",   "Cooldowns timed out"),

    ("pwr_mean_w",                "Power mean W"),

    ("gpu_read_gbs",              "GPU read GB/s"),
    ("gpu_write_gbs",             "GPU write GB/s"),
    ("empirical_peak_gbs",        "Empirical peak GB/s"),
    ("c2c_efficiency_pct",        "C2C efficiency %"),

    ("sparkview_peak_gpu_temp_c", "Inference GPU peak °C"),
    ("sparkview_peak_cpu_temp_c", "Inference CPU peak °C"),

    ("sparkview_trigger",         "Sparkview trigger"),
]

# ─── CONTENTION MODES ─────────────────────────────────────────────────────────
CONTENTION_MODES = [
    "gpu_read",
    "gpu_write",
    "cpu_write",
    "cpu_read_plus_gpu_read",
    "cpu_write_plus_gpu_read",
    "cpu_write_plus_gpu_write",
]

# ─── LOADER ───────────────────────────────────────────────────────────────────
def load_records(dirpath):

    pattern = os.path.join(dirpath, "record_*.json")

    paths = sorted(glob.glob(pattern))

    if not paths:
        print(f"No record_*.json found in {dirpath}", file=sys.stderr)
        sys.exit(1)

    records = []

    for path in paths:

        try:

            with open(path) as f:
                rec = json.load(f)

            rec["_path"] = path
            rec["_name"] = os.path.basename(path)

            records.append(rec)

        except Exception as e:

            print(f"Skip {path}: {e}", file=sys.stderr)

    return records

# ─── DELTA HELPERS ────────────────────────────────────────────────────────────
def delta(a, b):

    if a is None or b is None:
        return None

    try:
        return round(float(b) - float(a), 3)
    except Exception:
        return None


def pct_change(a, b):

    if a is None or b is None or float(a) == 0:
        return None

    try:
        return round(((float(b) - float(a)) / abs(float(a))) * 100, 1)
    except Exception:
        return None

# ─── COMPARISON BUILDERS ──────────────────────────────────────────────────────
def build_comparison(records):

    rows = []

    for key, label in SUMMARY_FIELDS:

        row = {
            "field":  key,
            "label":  label,
            "values": []
        }

        for rec in records:
            row["values"].append(
                rec.get("summary", {}).get(key)
            )

        rows.append(row)

    return rows


def build_contention_comparison(records):

    rows = []

    for mode in CONTENTION_MODES:

        row = {
            "mode":   mode,
            "values": []
        }

        for rec in records:

            ct = rec.get("contention", {}).get(mode, {})

            row["values"].append({
                "total_gbs":      ct.get("total_gbs"),
                "efficiency_pct": ct.get("efficiency_pct"),
                "gpu_drop_pct":   ct.get("gpu_drop_pct"),
            })

        rows.append(row)

    return rows


def build_deltas(records):

    if len(records) < 2:
        return []

    numeric_fields = [
        "probe_temp_baseline_c",
        "probe_temp_max_c",
        "clock_mhz",
        "pwr_mean_w",
        "gpu_read_gbs",
        "gpu_write_gbs",
        "empirical_peak_gbs",
        "c2c_efficiency_pct",
        "sparkview_peak_gpu_temp_c",
        "sparkview_peak_cpu_temp_c",
    ]

    deltas = []

    for i in range(len(records) - 1):

        a = records[i].get("summary", {})
        b = records[i + 1].get("summary", {})

        pair = {
            "from": records[i]["_name"],
            "to":   records[i + 1]["_name"],
            "fields": {}
        }

        for field in numeric_fields:

            d  = delta(a.get(field), b.get(field))
            pc = pct_change(a.get(field), b.get(field))

            if d is not None:

                pair["fields"][field] = {
                    "delta": d,
                    "pct_change": pc
                }

        deltas.append(pair)

    return deltas

# ─── HEALTH FLAGS ─────────────────────────────────────────────────────────────
def fleet_health(records):

    flags = []

    summaries = [r.get("summary", {}) for r in records]

    throttled = [
        s for s in summaries
        if s.get("throttle_detected")
    ]

    if throttled:

        flags.append({
            "level": "WARN",
            "msg": f"{len(throttled)} run(s) with throttle detected"
        })

    by_unit = {}

    for s in summaries:

        unit = s.get("unit")

        if unit:
            by_unit.setdefault(unit, []).append(
                s.get("gpu_read_gbs")
            )

    for unit, vals in by_unit.items():

        vals = [v for v in vals if v is not None]

        if len(vals) > 1:

            spread = max(vals) - min(vals)

            if spread > 5:

                flags.append({
                    "level": "WARN",
                    "msg": (
                        f"{unit}: GPU read spread "
                        f"{spread:.1f} GB/s across runs"
                    )
                })

    low_c2c = [
        s for s in summaries
        if s.get("c2c_efficiency_pct")
        and s["c2c_efficiency_pct"] < 120
    ]

    if low_c2c:

        flags.append({
            "level": "WARN",
            "msg": (
                f"{len(low_c2c)} run(s) with "
                f"C2C efficiency below 120%"
            )
        })

    baselines = [
        s.get("probe_temp_baseline_c")
        for s in summaries
        if s.get("probe_temp_baseline_c") is not None
    ]

    if baselines:

        drift = max(baselines) - min(baselines)

        if drift > 3:

            flags.append({
                "level": "WARN",
                "msg": (
                    f"Baseline temp drift "
                    f"{drift:.1f}°C across runs"
                )
            })

    if not flags:

        flags.append({
            "level": "OK",
            "msg": "All runs within normal parameters"
        })

    return flags

# ─── TEXT OUTPUT ──────────────────────────────────────────────────────────────
def print_text(records, comparison, contention, deltas, health):

    names = []

    for r in records:

        m = re.search(r'_(\d{6})\.json$', r["_name"])
        names.append(m.group(1) if m else r["_name"])

    FIELD_W = 38
    col_w   = max(30, max(len(n) for n in names) + 4)

    divider = (
        f"  {'─' * FIELD_W}"
        + ("─" * col_w) * len(names)
    )

    print(f"\n{'─' * 120}")
    print(f"  GB10 Fleet Health Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Records: {len(records)}")
    print(f"{'─' * 120}\n")

    # ── SUMMARY TABLE ────────────────────────────────────────────────────────
    header = (
        f"  {'Field':<{FIELD_W}}"
        + "".join(f"{n:>{col_w}}" for n in names)
    )

    print(header)
    print(divider)

    for row in comparison:

        vals = []

        for v in row["values"]:

            if v is None:
                vals.append("—")

            elif isinstance(v, bool):
                vals.append(str(v))

            elif isinstance(v, float):
                vals.append(f"{v:.2f}")

            else:
                vals.append(str(v))

        line = (
            f"  {row['label']:<{FIELD_W}}"
            + "".join(f"{v:>{col_w}}" for v in vals)
        )

        print(line)

    # ── CONTENTION TABLE ─────────────────────────────────────────────────────
    print("\n")

    contention_header = (
        f"  {'Contention mode':<{FIELD_W}}"
        + "".join(f"{n:>{col_w}}" for n in names)
    )

    print(contention_header)
    print(divider)

    for row in contention:

        vals = []

        for v in row["values"]:

            eff = v.get("efficiency_pct")

            if eff is None:
                vals.append("—")
            else:
                vals.append(f"{eff:.1f}%")

        line = (
            f"  {row['mode']:<{FIELD_W}}"
            + "".join(f"{v:>{col_w}}" for v in vals)
        )

        print(line)

    # ── DELTAS ───────────────────────────────────────────────────────────────
    if deltas:

        print("\n")
        print("  Deltas")
        print(f"  {'─' * 100}")

        for pair in deltas:

            print(f"  {pair['from']}")
            print(f"    → {pair['to']}\n")

            for field, d in pair["fields"].items():

                sign = "+" if d["delta"] > 0 else ""

                pct = (
                    f"{sign}{d['pct_change']}%"
                    if d["pct_change"] is not None
                    else "—"
                )

                delta_str = f"{sign}{d['delta']}"

                print(
                    f"    {field:<42}"
                    f"{delta_str:>12}   "
                    f"({pct})"
                )

            print()

    # ── HEALTH ───────────────────────────────────────────────────────────────
    print("  Health")
    print(f"  {'─' * 100}")

    for flag in health:

        print(
            f"  [{flag['level']}] {flag['msg']}"
        )

    print()

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():

    parser = argparse.ArgumentParser(
        description="gb10_fleet_compare v1.1 — GB10 fleet health comparator"
    )

    parser.add_argument(
        "records_dir",
        help="Directory containing record_*.json files"
    )

    parser.add_argument(
        "--out",
        help="Write JSON output to file"
    )

    parser.add_argument(
        "--text",
        action="store_true",
        help="Print formatted human-readable report"
    )

    args = parser.parse_args()

    records    = load_records(args.records_dir)
    comparison = build_comparison(records)
    contention = build_contention_comparison(records)
    deltas     = build_deltas(records)
    health     = fleet_health(records)

    if args.text:

        print_text(
            records,
            comparison,
            contention,
            deltas,
            health
        )

        return

    out = {
        "generated": datetime.now().isoformat(),
        "record_count": len(records),
        "records": [r["_name"] for r in records],
        "comparison": comparison,
        "contention": contention,
        "deltas": deltas,
        "health": health,
    }

    result = json.dumps(out, indent=2)

    if args.out:

        with open(args.out, "w") as f:
            f.write(result)

        print(f"Written: {args.out}", file=sys.stderr)

    else:

        print(result)

if __name__ == "__main__":
    main()
