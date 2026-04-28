# gb10-uma-diagnostics

**Unified memory diagnostic suite for NVIDIA GB10 (DGX Spark)**
Controlled measurement and system-level behavior classification

---

## Overview

`gb10-uma-diagnostics` is a controlled measurement suite for analyzing unified memory behavior on GB10 systems.

It performs targeted experiments to measure:

- Memory bandwidth (CPU and GPU)
- CPU–GPU contention on shared memory
- Atomic coherence cost (system vs GPU scope)
- Memory stall behavior under pressure
- Power and clock response during load

Measured signals are interpreted into actionable system states:
ROOT_CAUSE: MEMORY | POWER | MEMORY+POWER | UNKNOWN
PRESSURE:   SAFE | WARNING | CRITICAL

---

## Why This Exists

On GB10 (DGX Spark), some key unified memory signals are not fully exposed through current APIs:

- CUPTI UVM event collection — unavailable (CUPTI_ERROR_NOT_READY, CUDA 13.0, driver 580.142)
- NVML memory clock — not exposed (returns N/A)
- Nsight Systems UVM tracing — unsupported

System-level telemetry does exist (power, clocks, memory usage, PSI), but fine-grained unified memory behavior must be inferred from controlled experiments.

---

## Approach
controlled experiment → measured response → classification

Rather than relying on internal driver state, behavior is derived from:

- bandwidth response
- contention patterns
- latency
- PSI (stall signal)
- power and clock changes

---

## Diagnostic Model

### Methodology

**1. Controlled Experiment**
- Defined memory access patterns
- CPU/GPU concurrency models
- Repeatable workload conditions

**2. Measured Response**
- Bandwidth (GB/s)
- Latency (ns)
- PSI (`/proc/pressure/memory`)
- Power / clocks
- System behavior under load

**3. Classification**
- Convert signals into system-level interpretation

### Signal Interpretation

Memory Contention:
symptom:      bandwidth drop under concurrent access
interpretation: shared memory fabric contention

Memory Stall:
symptom:      PSI (memory) rising — especially "full"
interpretation: scheduler blocked on memory

Power Limiting:
symptom:      clock reduction under load, power plateau
interpretation: power or thermal constraint

Combined Effects:
symptom:      bandwidth drop + PSI rise + power increase
interpretation: contention driving both memory stall and power response

### Key Principle

PSI (`/proc/pressure/memory`) is the most reliable observable indicator of memory stall on GB10 systems where direct UVM telemetry is unavailable. PSI reflects time stalled, not allocation size — making it suitable for detecting failure conditions before they become unrecoverable.

### Constraints

- No direct UVM fault stream
- No memory clock via NVML
- Partial profiler support

Classification is based on externally observable behavior, not internal driver state.

---

## Tools

### uma_bw — Bandwidth Probe

Measures CPU and GPU bandwidth using PTX-level cache operators for true DRAM measurement.
PTX read : ld.global.cg  (L1 bypass)
PTX write: st.global.cs  (L2 bypass — true DRAM write)

Flags:
--calibrate-peak                    empirical peak BW, no hardcoded spec
--peak-from peak_calibration.json   load peak, compute efficiency%
--json-only

Build:
```bash
nvcc -O2 -std=c++17 -I./include uma_bandwidth_test.cu -o uma_bw -lcudart -lpthread
```

### uma_contention — Contention Probe

Measures bandwidth degradation under CPU/GPU simultaneous memory access.

Modes:
--mode gpu-read
--mode gpu-write
--mode cpu-read
--mode cpu-write
--mode cpu-read-gpu-read      split buffer — parallel bandwidth
--mode cpu-write-gpu-read     same buffer — maximum contention
--mode cpu-write-gpu-write    same buffer — both writing
--mode sweep                  all modes (default)
--peak-from peak_calibration.json

Build:
```bash
nvcc -O2 -std=c++17 -I./include uma_contention.cu -o uma_contention -lcudart -lpthread
```

### uma_atomic — Coherence Probe

Measures atomic coherence cost on hardware-coherent UMA.
atom.global.gpu  — GPU-scope atomic
atom.global.sys  — system-scope atomic (NVLink-C2C coherence path)
SYS/GPU ratio    — coherence overhead

Build:
```bash
nvcc -O2 -std=c++17 uma_atomic_test.cu -o uma_atomic -lcudart
```

### spbm_analyzer.py — Power + Pressure Classifier

Reads spark_hwmon sensors, nvidia-smi, and PSI live. Classifies system state in real time and writes events to `events.json`.

Run:
```bash
python3 spbm_analyzer.py <outdir> [sparkview_anomaly_log]
```

### run_correlated.sh — Experiment Orchestrator

Runs all tools in a controlled phased experiment with shared SPBM telemetry and timestamp alignment.
Phase 0   pre-run check (clock, temp, SWAP)
Phase 1   uma_bw — default clocks
Phase 2   cooldown to baseline
Phase 3   uma_bw — capped clocks
Phase 4   cooldown to baseline
Phase 5   uma_contention sweep
Phase 6   package all outputs into timestamped zip

---

## Quick Start

### 1. Build

```bash
nvcc -O2 -std=c++17 -I./include uma_bandwidth_test.cu -o uma_bw -lcudart -lpthread
nvcc -O2 -std=c++17 -I./include uma_contention.cu -o uma_contention -lcudart -lpthread
nvcc -O2 -std=c++17 uma_atomic_test.cu -o uma_atomic -lcudart
```

On GB10 — use CUDA 13.0 explicitly:
```bash
/usr/local/cuda-13.0/bin/nvcc -O2 -std=c++17 -I./include uma_bandwidth_test.cu -o uma_bw -lcudart -lpthread
/usr/local/cuda-13.0/bin/nvcc -O2 -std=c++17 -I./include uma_contention.cu -o uma_contention -lcudart -lpthread
```

### 2. Calibrate peak bandwidth

```bash
./uma_bw --calibrate-peak
```

### 3. Run sparkview (recommended — in separate terminal)

```bash
cd ~/sparkview && source sparkview-venv/bin/activate && python3 main.py
```

### 4. Run full diagnostic

```bash
./run_correlated.sh
```

---

## Output

Each run produces a timestamped zip containing:
uma_bw_run1.txt              default clock bandwidth
uma_bw_run2.txt              capped clock bandwidth
uma_contention_sweep.txt     full contention table
uma_bw_results.json
uma_contention_results.json
peak_calibration.json
spbm_*.txt                   raw power stream
run_guard.log                thermal guard log
events.json                  classified events
timeline.json                nanosecond event log
sparkview logs               if sparkview was running

---

## Interpreting Results

### Bandwidth runs
Run 1 vs Run 2 delta:
large delta   → clock cap affects bandwidth (power-limited)
small delta   → bandwidth is memory-bound, not clock-bound

### Contention sweep
cpu-write+gpu-read drop%:
on DISCRETE_PCIE  → PCIe contention (page migration)
on GB10 UMA       → LPDDR5X fabric arbitration

### Events
MEMORY+POWER + CRITICAL → system approaching freeze
MEMORY only             → fabric saturated, clock still healthy
POWER only              → clock-limited, memory headroom remains

---

## GB10 Confirmed Baselines

From community contributors (azampatti, pontostroy) — CUDA 13.0, driver 580.142:
GPU read idle        161–166 GB/s
GPU write idle       115–116 GB/s
CPU read               7.6–7.7 GB/s
UMA fault latency     16.5 ns p50 (40 cycles)
COLD/WARM ratio        1.00x

Driver gaps confirmed:
NVML memory clock     N/A — use --calibrate-peak
CUPTI UVM events      CUPTI_ERROR_NOT_READY
Peak BW from driver   0 GB/s

## CUDA Version Requirement
CUDA 13.0   confirmed working on GB10
CUDA 13.1   %clock64 broken on GB10 — do not use
CUDA 13.2   %clock64 returns 0, overflow — do not use

## PTX Forward Compatibility

All PTX instructions are generic portable primitives — no architecture-specific suffixes.

Validate before running on new hardware:
```bash
CUDA_FORCE_PTX_JIT=1 ./uma_bw
CUDA_FORCE_PTX_JIT=1 ./uma_contention --mode gpu-read
```

---

## Known Limitations

- UVM internal state not directly observable
- Memory clock unavailable via NVML
- Classification based on inference from external signals
- GB10 only — not designed for discrete PCIe platforms

---

## Related Tools

- [sparkview](https://github.com/parallelArchitect/sparkview) — GB10-aware GPU monitor with PSI pressure and clock state detection
- [nvidia-uma-fault-probe](https://github.com/parallelArchitect/nvidia-uma-fault-probe) — UMA fault latency and bandwidth (forum-facing stable version)

## Community

NVIDIA Developer Forums baseline thread:
https://forums.developer.nvidia.com/t/gb10-hardware-baseline-first-direct-measurements-and-findings/367851

---

## Author

parallelArchitect
Human-directed GPU engineering with AI assistance

## License

MIT
