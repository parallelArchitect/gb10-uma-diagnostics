# nvidia-uma-fault-probe

Ground-truth measurement of NVIDIA Unified Memory behavior.

Low-level probes for fault latency, memory bandwidth, and atomic
coherence cost using PTX instrumentation inside the kernel.

All metrics are captured with `%clock64` and PTX cache/atomic scope
operators — not CUPTI callbacks or NVML polling.

Covers both discrete PCIe and UMA platforms.

Written in CUDA C with inline PTX. No separate PTX files.
No dependencies beyond CUDA. Engineers share JSON output for remote analysis.

---

## Tools

### uma_probe — UMA Fault Latency Probe

Measures cycle-accurate memory access latency using `ld.global.cv` and
`%clock64` inside the kernel.

Three passes expose the full UMA behavior profile:

| Pass     | Setup                      | Measures                        |
|----------|----------------------------|---------------------------------|
| COLD     | CPU touches all pages      | First-touch access cost         |
| WARM     | GPU prefetch before launch | Resident access latency         |
| PRESSURE | Mixed CPU/GPU residency    | Thrash latency                  |

The COLD/WARM ratio is the key signal. Run the tool on your hardware — results vary by platform.

---

### uma_bw — UMA Bandwidth Test

Measures achieved memory bandwidth using PTX cache operators:

- `ld.global.cg` — cache at L2, bypass L1 (read)
- `st.global.cs` — bypass L2, true DRAM write bandwidth

Tests GPU read, GPU write, GPU copy, CPU read, CPU write,
and concurrent CPU+GPU access to the same memory pool.

Peak bandwidth is derived from hardware attributes at runtime.
On GB10, memory clock is not exposed by the driver; peak is
reported as 0 rather than fabricated.

On GB10 the concurrent test measures Grace CPU and GB10 GPU
accessing the same LPDDR5X pool simultaneously.

---

### uma_atomic — UMA Atomic Coherence Probe

Measures cycle-accurate latency of atomic operations at GPU scope
vs system scope on unified managed memory.

Three passes:

| Pass       | PTX Operation             | Measures                              |
|------------|---------------------------|---------------------------------------|
| GPU-scope  | `atom.global.gpu.add.u32` | Atomic latency within GPU memory      |
| SYS-scope  | `atom.global.sys.add.u32` | Atomic latency through coherence path |
| CONTENTION | `atom.global.sys` + CPU   | True concurrent access cost           |


The SYS/GPU ratio is the coherence signal. On discrete PCIe 
ratio is ~1.0x (no coherence protocol). On GB10 NVLink-C2C 
hardware coherence is transparent — first community measurement 
shows 1.00x ratio at atomic instruction level.

---

## Why PTX

PTX (Parallel Thread Execution) is NVIDIA's virtual machine assembly.
Inline PTX inside CUDA C is compiled natively by nvcc for the target GPU —
no separate PTX files, no runtime JIT.

- `%clock64` measures cycles inside the kernel, no driver overhead
- Cache operators `.cg` and `.cv` control memory path behavior
- `st.global.cs` bypasses L2, measuring true DRAM write bandwidth
- Atomic scope operators `.gpu` and `.sys` expose coherence behavior
- Ground truth from inside the kernel — not from callbacks

Note: Nsight Systems UVM profiling is not supported on GB10 (confirmed by NVIDIA).
These measurements provide direct visibility in its absence.

References:
- CUDA Programming Guide (Unified Memory model):
  https://docs.nvidia.com/cuda/cuda-c-programming-guide/
- PTX ISA (instruction-level measurement basis):
  https://docs.nvidia.com/cuda/parallel-thread-execution/

---

## Build

Requirements: CUDA 12.x or 13.x, C++17, Linux (x86_64 or aarch64)

### uma_probe

```bash
# x86_64:
nvcc -O2 -std=c++17 probe_launcher.cu -o uma_probe -lcudart -lcuda

# aarch64 (GB10 DGX Spark):
nvcc -O2 -std=c++17 probe_launcher.cu -o uma_probe -lcudart -lcuda -lpthread
```

### uma_bw

```bash
# x86_64:
nvcc -O2 -std=c++17 uma_bandwidth_test.cu -o uma_bw -lcudart

# aarch64 (GB10 DGX Spark):
nvcc -O2 -std=c++17 uma_bandwidth_test.cu -o uma_bw -lcudart -lpthread
```

### uma_atomic

```bash
# x86_64 (SM 6.0+ required for scoped atomics):
nvcc -O2 -std=c++17 -arch=sm_60 uma_atomic_test.cu -o uma_atomic -lcudart -lcuda

# aarch64 (GB10 DGX Spark):
nvcc -O2 -std=c++17 -arch=sm_90 uma_atomic_test.cu -o uma_atomic -lcudart -lcuda -lpthread
```

---

## Run

```bash
./uma_probe              # human-readable output + JSON log
./uma_probe --json-only  # JSON only

./uma_bw                 # human-readable output + JSON log
./uma_bw --json-only     # JSON only

./uma_atomic             # human-readable output + JSON log
./uma_atomic --json-only # JSON only
```

---

## Scripts

### run_all.sh — Run All Tools

Runs all three probes in sequence with thermal cooldown between each.

```bash
./run_all.sh
```

- Checks all binaries are built — exits with build instructions if not
- Detects sparkview and launches it automatically for thermal monitoring
- Runs uma_probe → 10s cooldown → uma_atomic → 10s cooldown → uma_bw → 30s cooldown
- Calls collect_results.sh automatically when done

If sparkview is already running when `run_all.sh` is launched, the script exits with:

```
sparkview is already running.
Close it first for a clean session log, then rerun this script.
```

If sparkview is not installed:

```
sparkview not found — recommended for thermal monitoring.
Install: https://github.com/parallelArchitect/sparkview
```

---

### collect_results.sh — Package Results

Packages all JSON result files plus the latest sparkview anomaly log.

```bash
./collect_results.sh
```

Creates `uma_results_<hostname>_<timestamp>.zip` containing:
- `uma_probe_results.json`
- `uma_bw_results.json`
- `uma_atomic_results.json`
- `sparkview_summary.json` (if sparkview logged an anomaly)
- `sparkview_anomaly.log.gz` (if sparkview logged an anomaly)

Prompts after packaging:

```
Results packaged: uma_results_mc_20260425_021713.zip (3 of 3 tools)

What would you like to do?
  [1] Share — open GitHub Issues to upload
  [2] Local — keep results, no upload
```

To share results with the community:
https://github.com/parallelArchitect/nvidia-uma-fault-probe/issues

---

## Example Output — Pascal GTX 1080 (SM 6.1, validated)

**uma_probe:**
```
GPU      : NVIDIA GeForce GTX 1080 (SM 6.1)
Platform : DISCRETE_PCIE
COLD  p50:     49.0 ns  (85 cycles)
WARM  p50:     46.2 ns  (80 cycles)
COLD/WARM ratio: 1.06x
```

**uma_bw:**
```
GPU      : NVIDIA GeForce GTX 1080 (SM 6.1)
Platform : DISCRETE_PCIE
GPU read  : 254.90 GB/s  stddev 0.82
GPU write : 261.69 GB/s  [PTX .cs]
GPU copy  :   6.64 GB/s
CPU read  :   5.23 GB/s
CPU write :  18.26 GB/s
Conc total: 264.16 GB/s
```

**uma_atomic:**
```
GPU      : NVIDIA GeForce GTX 1080 (SM 6.1)
Platform : DISCRETE_PCIE
GPU-scope p50 :    187.5 ns  (325 cycles) [atom.global.gpu]
SYS-scope p50 :    187.5 ns  (325 cycles) [atom.global.sys]
SYS/GPU ratio : 1.00x
Coherence cost: 0.0 ns overhead
```

---

## Example Output — GB10 DGX Spark (SM 12.1, validated)

First community measurement, 2026-04-23. VLLM loaded, model idle.

**uma_probe:**
```
GPU      : NVIDIA GB10 (SM 12.1)
Platform : HARDWARE_COHERENT_UMA
COLD  p50:     16.5 ns  (40 cycles)
WARM  p50:     16.5 ns  (40 cycles)
COLD/WARM ratio: 1.00x
```

**uma_bw:**
```
GPU      : NVIDIA GB10 (SM 12.1)
Platform : HARDWARE_COHERENT_UMA
GPU read  : 161.31 GB/s  stddev 2.82
GPU write : 116.15 GB/s  [PTX .cs]
GPU copy  : 164.45 GB/s
CPU read  :   7.62 GB/s
CPU write :  57.95 GB/s
Conc total: 162.89 GB/s
```

**uma_atomic:**
```
GPU      : NVIDIA GB10 (SM 12.1)
Platform : HARDWARE_COHERENT_UMA
GPU-scope p50 :      9.9 ns  (24 cycles) [atom.global.gpu]
SYS-scope p50 :      9.9 ns  (24 cycles) [atom.global.sys]
SYS/GPU ratio : 1.00x
Coherence cost: 0.0 ns overhead
```

---

## Supported Architectures

| Architecture               | SM       | uma_probe | uma_bw    | uma_atomic |
|----------------------------|----------|-----------|-----------|------------|
| Pascal                     | 6.0, 6.1 | validated | validated | validated  |
| Volta                      | 7.0      | expected  | expected  | expected   |
| Turing                     | 7.5      | expected  | expected  | expected   |
| Ampere                     | 8.0, 8.6 | expected  | expected  | expected   |
| Ada Lovelace               | 8.9      | expected  | expected  | expected   |
| Hopper                     | 9.0      | expected  | expected  | expected   |
| Blackwell GB10 (DGX Spark) | 12.1     | validated | validated | validated  |
| Blackwell GB202 (RTX 5090) | 12.0     | pending   | pending   | pending    |

---

## Relationship to Other Tools

| Tool                          | Measures                      | Method                       | Type           |
|-------------------------------|-------------------------------|------------------------------|----------------|
| uma_probe                     | Memory access latency (ns)    | PTX %clock64 + ld.global.cv  | Point-in-time  |
| uma_bw                        | Bandwidth (GB/s)              | PTX .cg/.cs + CUDA events    | Point-in-time  |
| uma_atomic                    | Atomic coherence latency (ns) | PTX %clock64 + atom.global   | Point-in-time  |
| cuda-unified-memory-analyzer  | UMA pressure, fault rate, migration efficiency | CUPTI + NVML (Pascal validated; GB10 in progress) | Point-in-time  |
| sparkview                     | System health + UMA pressure  | NVML + PSI + clock state     | Continuous monitor |

These tools establish the hardware baseline. sparkview monitors the system
continuously against that baseline and logs anomalies automatically.

cuda-unified-memory-analyzer: https://github.com/parallelArchitect/cuda-unified-memory-analyzer
sparkview: https://github.com/parallelArchitect/sparkview

GB10 findings and community data:
https://forums.developer.nvidia.com/t/gb10-hardware-baseline-first-direct-measurements-and-findings/367851

---

## License

MIT
