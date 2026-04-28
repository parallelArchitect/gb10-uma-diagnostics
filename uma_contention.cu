/*
 * uma_contention.cu
 * UMA Contention Probe v1.0
 *
 * Measures bandwidth degradation under CPU/GPU contention
 * on unified memory platforms (GB10/GH200) and discrete PCIe.
 *
 * Modes:
 *   --mode gpu-read
 *   --mode gpu-write
 *   --mode cpu-read
 *   --mode cpu-write
 *   --mode cpu-read-gpu-read
 *   --mode cpu-write-gpu-read
 *   --mode cpu-write-gpu-write
 *   --mode sweep
 *
 * Build:
 *   x86_64 : nvcc -O2 -std=c++17 -I./include uma_contention.cu -o uma_contention -lcudart -lpthread
 *   aarch64: nvcc -O2 -std=c++17 -I./include uma_contention.cu -o uma_contention -lcudart -lpthread
 *
 * Optional:
 *   --peak-from peak_calibration.json
 *   --json-only
 */

#include <cuda_runtime.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <pthread.h>
#include <math.h>
#include "include/time_sync.h"
#include "include/timeline.h"

#define TOOL_VERSION      "1.0.0"
#define BUFFER_GB         4
#define BUFFER_BYTES      ((size_t)BUFFER_GB * 1024ULL * 1024ULL * 1024ULL)
#define THREADS_PER_BLOCK 256
#define WARMUP_RUNS       2
#define MEASURE_RUNS      5
#define MAX_RUNS          16
#define JSON_OUTPUT       "uma_contention_results.json"

#define CUDA_CHECK(call) do { \
    cudaError_t e = (call); \
    if (e != cudaSuccess) { \
        fprintf(stderr, "CUDA error %s:%d: %s\n", \
                __FILE__, __LINE__, cudaGetErrorString(e)); \
        exit(1); \
    } \
} while(0)

/* ------------------------------------------------------------------ */
/* Platform detection                                                   */
/* ------------------------------------------------------------------ */

typedef enum {
    PLAT_HW_COHERENT_UMA,
    PLAT_DISCRETE_PCIE,
    PLAT_SOFTWARE_UMA,
    PLAT_UNKNOWN
} PlatformType;

typedef struct {
    char         name[256];
    int          sm_major;
    int          sm_minor;
    PlatformType type;
    int          hw_coherent;
    int          host_page_tables;
    double       peak_bw_gbs;
} Platform;

static Platform detect_platform(int device) {
    Platform p = {};
    cudaDeviceProp prop;
    CUDA_CHECK(cudaGetDeviceProperties(&prop, device));
    strncpy(p.name, prop.name, 255);
    p.sm_major = prop.major;
    p.sm_minor = prop.minor;

    int hpt = 0;
    cudaDeviceGetAttribute(&hpt,
        cudaDevAttrPageableMemoryAccessUsesHostPageTables, device);
    p.host_page_tables = hpt;
    p.hw_coherent      = hpt;

    if (hpt && prop.concurrentManagedAccess) {
        p.type        = PLAT_HW_COHERENT_UMA;
        p.peak_bw_gbs = 0.0; /* memory clock N/A on GB10 */
    } else if (prop.concurrentManagedAccess) {
        p.type = PLAT_DISCRETE_PCIE;
        int bus_bits = 0, mem_khz = 0;
        cudaDeviceGetAttribute(&bus_bits,
            cudaDevAttrGlobalMemoryBusWidth, device);
        cudaDeviceGetAttribute(&mem_khz,
            cudaDevAttrMemoryClockRate, device);
        p.peak_bw_gbs = (bus_bits > 0 && mem_khz > 0) ?
            2.0 * (bus_bits / 8.0) * mem_khz * 1000.0 / 1e9 : 0.0;
    } else {
        p.type        = PLAT_SOFTWARE_UMA;
        p.peak_bw_gbs = 0.0;
    }
    return p;
}

static const char *plat_name(PlatformType t) {
    switch(t) {
    case PLAT_HW_COHERENT_UMA: return "HARDWARE_COHERENT_UMA";
    case PLAT_DISCRETE_PCIE:   return "DISCRETE_PCIE";
    case PLAT_SOFTWARE_UMA:    return "SOFTWARE_UMA";
    default:                   return "UNKNOWN";
    }
}

/* ------------------------------------------------------------------ */
/* GPU kernels — PTX cache operators                                    */
/* ------------------------------------------------------------------ */

static float *g_sink = nullptr;

__global__ void gpu_read_kernel(const float * __restrict__ buf,
                                 float *sink, size_t n) {
    size_t idx    = blockIdx.x * (size_t)blockDim.x + threadIdx.x;
    size_t stride = (size_t)blockDim.x * gridDim.x;
    float  acc    = 0.0f;
    for (size_t i = idx; i < n; i += stride) {
        float val;
        asm volatile("ld.global.cg.f32 %0, [%1];"
                     : "=f"(val) : "l"(buf + i));
        acc += val;
    }
    if (threadIdx.x == 0 && blockIdx.x == 0) *sink = acc;
}

__global__ void gpu_write_kernel(float *buf, size_t n, float val) {
    size_t idx    = blockIdx.x * (size_t)blockDim.x + threadIdx.x;
    size_t stride = (size_t)blockDim.x * gridDim.x;
    for (size_t i = idx; i < n; i += stride)
        asm volatile("st.global.cs.f32 [%0], %1;"
                     : : "l"(buf + i), "f"(val));
}

/* ------------------------------------------------------------------ */
/* CPU measurement                                                      */
/* ------------------------------------------------------------------ */

static double now_sec(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static double cpu_read_bw(const float *buf, size_t n) {
    volatile float sink = 0;
    double t0 = now_sec();
    float acc = 0;
    for (size_t i = 0; i < n; i++) acc += buf[i];
    sink = acc; (void)sink;
    return (double)(n * sizeof(float)) / (now_sec() - t0) / 1e9;
}

static double cpu_write_bw(float *buf, size_t n) {
    double t0 = now_sec();
    memset(buf, 0, n * sizeof(float));
    return (double)(n * sizeof(float)) / (now_sec() - t0) / 1e9;
}

/* ------------------------------------------------------------------ */
/* GPU timed run                                                        */
/* ------------------------------------------------------------------ */

static double gpu_timed_run(void (*fn)(float*, size_t, float),
                             float *buf, size_t n, float val) {
    cudaEvent_t ev_s, ev_e;
    CUDA_CHECK(cudaEventCreate(&ev_s));
    CUDA_CHECK(cudaEventCreate(&ev_e));
    CUDA_CHECK(cudaEventRecord(ev_s));
    fn(buf, n, val);
    CUDA_CHECK(cudaEventRecord(ev_e));
    CUDA_CHECK(cudaEventSynchronize(ev_e));
    float ms = 0;
    CUDA_CHECK(cudaEventElapsedTime(&ms, ev_s, ev_e));
    CUDA_CHECK(cudaEventDestroy(ev_s));
    CUDA_CHECK(cudaEventDestroy(ev_e));
    return (double)(n * sizeof(float)) / (ms / 1000.0) / 1e9;
}

static void launch_read(float *a, size_t n, float v) {
    (void)v;
    int blk = (int)((n + THREADS_PER_BLOCK - 1) / THREADS_PER_BLOCK);
    gpu_read_kernel<<<blk, THREADS_PER_BLOCK>>>(a, g_sink, n);
}

static void launch_write(float *a, size_t n, float v) {
    int blk = (int)((n + THREADS_PER_BLOCK - 1) / THREADS_PER_BLOCK);
    gpu_write_kernel<<<blk, THREADS_PER_BLOCK>>>(a, n, v);
}

/* ------------------------------------------------------------------ */
/* Contention result                                                    */
/* ------------------------------------------------------------------ */

typedef struct {
    const char *mode;
    double      gpu_bw_gbs;
    double      cpu_bw_gbs;
    double      total_bw_gbs;
    double      gpu_drop_pct;   /* vs gpu-only baseline */
    double      cpu_drop_pct;   /* vs cpu-only baseline */
    double      efficiency_pct; /* total vs empirical peak */
} ContentionResult;

/* ------------------------------------------------------------------ */
/* CPU contention thread args                                           */
/* ------------------------------------------------------------------ */

typedef struct {
    float  *buf;
    size_t  n;
    double  bw;
    int     write_mode; /* 0=read, 1=write */
    int     warmup;
    int     measure;
} CpuArg;

static void *cpu_contention_thread(void *arg) {
    CpuArg *a = (CpuArg *)arg;
    for (int i = 0; i < a->warmup; i++)
        a->write_mode ? cpu_write_bw(a->buf, a->n)
                      : cpu_read_bw(a->buf, a->n);
    double s = 0;
    for (int i = 0; i < a->measure; i++)
        s += a->write_mode ? cpu_write_bw(a->buf, a->n)
                           : cpu_read_bw(a->buf, a->n);
    a->bw = s / a->measure;
    return NULL;
}

/* ------------------------------------------------------------------ */
/* Peak calibration loader                                              */
/* ------------------------------------------------------------------ */

static double load_peak_calibration(const char *path,
                                     char *source_out) {
    FILE *fp = fopen(path, "r");
    if (!fp) { strncpy(source_out, "unavailable", 63); return 0.0; }
    double peak = 0.0;
    char line[256];
    while (fgets(line, sizeof(line), fp)) {
        if (strstr(line, "empirical_peak_bw_gbps"))
            sscanf(line, " \"empirical_peak_bw_gbps\": %lf", &peak);
    }
    fclose(fp);
    if (peak > 0.0) strncpy(source_out, "empirical_calibration", 63);
    else            strncpy(source_out, "unavailable", 63);
    return peak;
}

/* ------------------------------------------------------------------ */
/* Prefetch helpers                                                     */
/* ------------------------------------------------------------------ */

static void prefetch_gpu(float *buf, size_t bytes, int device) {
#if CUDART_VERSION >= 12020
    cudaMemLocation loc = {cudaMemLocationTypeDevice, device};
    cudaMemPrefetchAsync(buf, bytes, loc, 0);
#else
    cudaMemPrefetchAsync(buf, bytes, device, 0);
#endif
    cudaDeviceSynchronize();
}

static void prefetch_cpu(float *buf, size_t bytes) {
#if CUDART_VERSION >= 12020
    cudaMemLocation loc = {cudaMemLocationTypeHost, 0};
    cudaMemPrefetchAsync(buf, bytes, loc, 0);
#else
    cudaMemPrefetchAsync(buf, bytes, cudaCpuDeviceId, 0);
#endif
    cudaDeviceSynchronize();
}

/* ------------------------------------------------------------------ */
/* Individual mode runners                                              */
/* ------------------------------------------------------------------ */

static ContentionResult run_gpu_read(float *buf, size_t n,
                                      int device) {
    ContentionResult r = {"gpu-read", 0, 0, 0, 0, 0, 0};
    prefetch_gpu(buf, n * sizeof(float), device);
    for (int i = 0; i < WARMUP_RUNS; i++) launch_read(buf, n, 0);
    cudaDeviceSynchronize();
    double s = 0;
    for (int i = 0; i < MEASURE_RUNS; i++)
        s += gpu_timed_run(launch_read, buf, n, 0);
    r.gpu_bw_gbs   = s / MEASURE_RUNS;
    r.total_bw_gbs = r.gpu_bw_gbs;
    return r;
}

static ContentionResult run_gpu_write(float *buf, size_t n,
                                       int device) {
    ContentionResult r = {"gpu-write", 0, 0, 0, 0, 0, 0};
    prefetch_gpu(buf, n * sizeof(float), device);
    for (int i = 0; i < WARMUP_RUNS; i++) launch_write(buf, n, 1.0f);
    cudaDeviceSynchronize();
    double s = 0;
    for (int i = 0; i < MEASURE_RUNS; i++)
        s += gpu_timed_run(launch_write, buf, n, 1.0f);
    r.gpu_bw_gbs   = s / MEASURE_RUNS;
    r.total_bw_gbs = r.gpu_bw_gbs;
    return r;
}

static ContentionResult run_cpu_read(float *buf, size_t n) {
    ContentionResult r = {"cpu-read", 0, 0, 0, 0, 0, 0};
    prefetch_cpu(buf, n * sizeof(float));
    for (int i = 0; i < WARMUP_RUNS; i++) cpu_read_bw(buf, n);
    double s = 0;
    for (int i = 0; i < MEASURE_RUNS; i++) s += cpu_read_bw(buf, n);
    r.cpu_bw_gbs   = s / MEASURE_RUNS;
    r.total_bw_gbs = r.cpu_bw_gbs;
    return r;
}

static ContentionResult run_cpu_write(float *buf, size_t n) {
    ContentionResult r = {"cpu-write", 0, 0, 0, 0, 0, 0};
    prefetch_cpu(buf, n * sizeof(float));
    for (int i = 0; i < WARMUP_RUNS; i++) cpu_write_bw(buf, n);
    double s = 0;
    for (int i = 0; i < MEASURE_RUNS; i++) s += cpu_write_bw(buf, n);
    r.cpu_bw_gbs   = s / MEASURE_RUNS;
    r.total_bw_gbs = r.cpu_bw_gbs;
    return r;
}

static ContentionResult run_cpu_read_gpu_read(float *buf, size_t n,
                                               int device) {
    ContentionResult r = {"cpu-read+gpu-read", 0, 0, 0, 0, 0, 0};

    /* Split buffer — GPU gets first half, CPU gets second half */
    prefetch_gpu(buf,         n/2 * sizeof(float), device);
    prefetch_cpu(buf + n/2,   n/2 * sizeof(float));

    CpuArg cpu_arg = {buf + n/2, n/2, 0.0, 0, WARMUP_RUNS, MEASURE_RUNS};
    pthread_t tid;
    pthread_create(&tid, NULL, cpu_contention_thread, &cpu_arg);

    for (int i = 0; i < WARMUP_RUNS; i++) launch_read(buf, n/2, 0);
    cudaDeviceSynchronize();
    double s = 0;
    for (int i = 0; i < MEASURE_RUNS; i++)
        s += gpu_timed_run(launch_read, buf, n/2, 0);

    pthread_join(tid, NULL);
    r.gpu_bw_gbs   = s / MEASURE_RUNS;
    r.cpu_bw_gbs   = cpu_arg.bw;
    r.total_bw_gbs = r.gpu_bw_gbs + r.cpu_bw_gbs;
    return r;
}

static ContentionResult run_cpu_write_gpu_read(float *buf, size_t n,
                                                int device) {
    ContentionResult r = {"cpu-write+gpu-read", 0, 0, 0, 0, 0, 0};

    /* Both access same pool — no split, maximum contention */
    prefetch_gpu(buf, n * sizeof(float), device);

    CpuArg cpu_arg = {buf, n, 0.0, 1, WARMUP_RUNS, MEASURE_RUNS};
    pthread_t tid;
    pthread_create(&tid, NULL, cpu_contention_thread, &cpu_arg);

    for (int i = 0; i < WARMUP_RUNS; i++) launch_read(buf, n, 0);
    cudaDeviceSynchronize();
    double s = 0;
    for (int i = 0; i < MEASURE_RUNS; i++)
        s += gpu_timed_run(launch_read, buf, n, 0);

    pthread_join(tid, NULL);
    r.gpu_bw_gbs   = s / MEASURE_RUNS;
    r.cpu_bw_gbs   = cpu_arg.bw;
    r.total_bw_gbs = r.gpu_bw_gbs + r.cpu_bw_gbs;
    return r;
}

static ContentionResult run_cpu_write_gpu_write(float *buf, size_t n,
                                                 int device) {
    ContentionResult r = {"cpu-write+gpu-write", 0, 0, 0, 0, 0, 0};

    prefetch_gpu(buf, n * sizeof(float), device);

    CpuArg cpu_arg = {buf, n, 0.0, 1, WARMUP_RUNS, MEASURE_RUNS};
    pthread_t tid;
    pthread_create(&tid, NULL, cpu_contention_thread, &cpu_arg);

    for (int i = 0; i < WARMUP_RUNS; i++) launch_write(buf, n, 1.0f);
    cudaDeviceSynchronize();
    double s = 0;
    for (int i = 0; i < MEASURE_RUNS; i++)
        s += gpu_timed_run(launch_write, buf, n, 1.0f);

    pthread_join(tid, NULL);
    r.gpu_bw_gbs   = s / MEASURE_RUNS;
    r.cpu_bw_gbs   = cpu_arg.bw;
    r.total_bw_gbs = r.gpu_bw_gbs + r.cpu_bw_gbs;
    return r;
}

/* ------------------------------------------------------------------ */
/* Drop calculation                                                     */
/* ------------------------------------------------------------------ */

static void compute_drops(ContentionResult *results, int n,
                           double gpu_baseline, double cpu_baseline,
                           double empirical_peak) {
    for (int i = 0; i < n; i++) {
        if (gpu_baseline > 0)
            results[i].gpu_drop_pct =
                (gpu_baseline - results[i].gpu_bw_gbs)
                / gpu_baseline * 100.0;
        if (cpu_baseline > 0)
            results[i].cpu_drop_pct =
                (cpu_baseline - results[i].cpu_bw_gbs)
                / cpu_baseline * 100.0;
        if (empirical_peak > 0)
            results[i].efficiency_pct =
                results[i].total_bw_gbs / empirical_peak * 100.0;
    }
}

/* ------------------------------------------------------------------ */
/* JSON output                                                          */
/* ------------------------------------------------------------------ */

static void write_json(const char *path,
                       const Platform *p,
                       ContentionResult *results, int n,
                       double empirical_peak,
                       const char *peak_source) {
    FILE *f = fopen(path, "w");
    if (!f) return;

    time_t t = time(NULL);
    char ts[64];
    strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%SZ", gmtime(&t));

    fprintf(f, "{\n");
    fprintf(f, "  \"tool\": \"uma-contention-probe\",\n");
    fprintf(f, "  \"version\": \"%s\",\n", TOOL_VERSION);
    fprintf(f, "  \"timestamp\": \"%s\",\n", ts);
    fprintf(f, "  \"platform\": {\n");
    fprintf(f, "    \"gpu_name\": \"%s\",\n", p->name);
    fprintf(f, "    \"uma_type\": \"%s\",\n", plat_name(p->type));
    fprintf(f, "    \"hw_coherent\": %s\n",
            p->hw_coherent ? "true" : "false");
    fprintf(f, "  },\n");
    fprintf(f, "  \"peak\": {\n");
    fprintf(f, "    \"empirical_peak_bw_gbps\": %.3f,\n", empirical_peak);
    fprintf(f, "    \"peak_source\": \"%s\"\n", peak_source);
    fprintf(f, "  },\n");
    fprintf(f, "  \"results\": [\n");

    for (int i = 0; i < n; i++) {
        fprintf(f, "    {\n");
        fprintf(f, "      \"mode\": \"%s\",\n",       results[i].mode);
        fprintf(f, "      \"gpu_bw_gbs\": %.2f,\n",   results[i].gpu_bw_gbs);
        fprintf(f, "      \"cpu_bw_gbs\": %.2f,\n",   results[i].cpu_bw_gbs);
        fprintf(f, "      \"total_bw_gbs\": %.2f,\n", results[i].total_bw_gbs);
        fprintf(f, "      \"gpu_drop_pct\": %.1f,\n", results[i].gpu_drop_pct);
        fprintf(f, "      \"cpu_drop_pct\": %.1f,\n", results[i].cpu_drop_pct);
        fprintf(f, "      \"efficiency_pct\": %.1f\n",results[i].efficiency_pct);
        fprintf(f, "    }%s\n", i < n-1 ? "," : "");
    }

    fprintf(f, "  ]\n");
    fprintf(f, "}\n");
    fclose(f);
}

/* ------------------------------------------------------------------ */
/* Print table                                                          */
/* ------------------------------------------------------------------ */

static void print_table(ContentionResult *results, int n,
                         double empirical_peak,
                         const char *peak_source) {
    printf("\n%-24s  %10s  %10s  %10s  %10s  %10s\n",
           "Mode", "GPU GB/s", "CPU GB/s", "Total GB/s",
           "GPU Drop%", "Efficiency%");
    printf("%-24s  %10s  %10s  %10s  %10s  %10s\n",
           "------------------------",
           "----------", "----------", "----------",
           "----------", "----------");

    for (int i = 0; i < n; i++) {
        int gpu_only = (results[i].gpu_bw_gbs > 0 && results[i].cpu_bw_gbs == 0);
        int cpu_only = (results[i].cpu_bw_gbs > 0 && results[i].gpu_bw_gbs == 0);
        printf("%-24s  %10.2f  %10.2f  %10.2f  ",
               results[i].mode,
               results[i].gpu_bw_gbs,
               results[i].cpu_bw_gbs,
               results[i].total_bw_gbs);
        if (cpu_only) printf("%10s  ", "--");
        else          printf("%9.1f%%  ", results[i].gpu_drop_pct);
        printf("%9.1f%%", results[i].efficiency_pct);
        if (results[i].efficiency_pct > 100.0)
            printf(" *");
        printf("\n");
    }

    if (empirical_peak > 0)
        printf("\nPeak source: %s (%.3f GB/s)\n",
               peak_source, empirical_peak);
}

/* ------------------------------------------------------------------ */
/* Main                                                                 */
/* ------------------------------------------------------------------ */

int main(int argc, char **argv) {
    int  json_only    = 0;
    char mode[64]     = "sweep";
    char peak_from[256] = "";

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--json-only") == 0)
            json_only = 1;
        if (strcmp(argv[i], "--mode") == 0 && i+1 < argc)
            strncpy(mode, argv[++i], 63);
        if (strcmp(argv[i], "--peak-from") == 0 && i+1 < argc)
            strncpy(peak_from, argv[++i], 255);
    }

    int device = 0;
    CUDA_CHECK(cudaSetDevice(device));

    Platform p = detect_platform(device);

    char   peak_source[64] = "unavailable";
    double empirical_peak  = 0.0;
    if (peak_from[0])
        empirical_peak = load_peak_calibration(peak_from, peak_source);

    if (!json_only) {
        printf("=== UMA Contention Probe v%s ===\n", TOOL_VERSION);
        printf("GPU      : %s (SM %d.%d)\n",
               p.name, p.sm_major, p.sm_minor);
        printf("Platform : %s\n", plat_name(p.type));
        printf("Coherent : %s\n",
               p.hw_coherent ? "yes (hardware)" : "no");
        printf("Mode     : %s\n", mode);
        if (empirical_peak > 0)
            printf("Peak     : %.3f GB/s (%s)\n\n",
                   empirical_peak, peak_source);
        else
            printf("Peak     : unavailable (run --calibrate-peak first)\n\n");
    }

    size_t n = BUFFER_BYTES / sizeof(float);
    float *buf_a, *sink;
    CUDA_CHECK(cudaMallocManaged(&buf_a, BUFFER_BYTES));
    CUDA_CHECK(cudaMallocManaged(&sink,  sizeof(float)));
    g_sink = sink;
    memset(buf_a, 0, BUFFER_BYTES);

    ContentionResult results[8];
    int nresults = 0;

    time_sync_t ts;
    timeline_t  tl;
    time_sync_init(&ts);
    timeline_open(&tl, "timeline.json");
    timeline_event(&tl, "contention_start", ts.t0_ns);

    /* Run selected mode(s) */
    if (strcmp(mode, "gpu-read") == 0 || strcmp(mode, "sweep") == 0) {
        if (!json_only) printf("Running gpu-read...\n");
        results[nresults++] = run_gpu_read(buf_a, n, device);
    }
    if (strcmp(mode, "gpu-write") == 0 || strcmp(mode, "sweep") == 0) {
        if (!json_only) printf("Running gpu-write...\n");
        results[nresults++] = run_gpu_write(buf_a, n, device);
    }
    if (strcmp(mode, "cpu-read") == 0 || strcmp(mode, "sweep") == 0) {
        if (!json_only) printf("Running cpu-read...\n");
        results[nresults++] = run_cpu_read(buf_a, n);
    }
    if (strcmp(mode, "cpu-write") == 0 || strcmp(mode, "sweep") == 0) {
        if (!json_only) printf("Running cpu-write...\n");
        results[nresults++] = run_cpu_write(buf_a, n);
    }
    if (strcmp(mode, "cpu-read-gpu-read") == 0 ||
        strcmp(mode, "sweep") == 0) {
        if (!json_only) printf("Running cpu-read+gpu-read...\n");
        results[nresults++] = run_cpu_read_gpu_read(buf_a, n, device);
    }
    if (strcmp(mode, "cpu-write-gpu-read") == 0 ||
        strcmp(mode, "sweep") == 0) {
        if (!json_only) printf("Running cpu-write+gpu-read...\n");
        results[nresults++] = run_cpu_write_gpu_read(buf_a, n, device);
    }
    if (strcmp(mode, "cpu-write-gpu-write") == 0 ||
        strcmp(mode, "sweep") == 0) {
        if (!json_only) printf("Running cpu-write+gpu-write...\n");
        results[nresults++] = run_cpu_write_gpu_write(buf_a, n, device);
    }

    timeline_event(&tl, "contention_end", ts_now_ns());
    timeline_close(&tl);

    /* Compute drops vs solo baselines */
    double gpu_baseline = 0.0, cpu_baseline = 0.0;
    for (int i = 0; i < nresults; i++) {
        if (strcmp(results[i].mode, "gpu-read") == 0)
            gpu_baseline = results[i].gpu_bw_gbs;
        if (strcmp(results[i].mode, "cpu-read") == 0)
            cpu_baseline = results[i].cpu_bw_gbs;
    }
    compute_drops(results, nresults, gpu_baseline,
                  cpu_baseline, empirical_peak);

    if (!json_only)
        print_table(results, nresults, empirical_peak, peak_source);

    write_json(JSON_OUTPUT, &p, results, nresults,
               empirical_peak, peak_source);

    if (!json_only)
        printf("\nJSON: %s\n", JSON_OUTPUT);

    CUDA_CHECK(cudaFree(buf_a));
    CUDA_CHECK(cudaFree(sink));
    return 0;
}
