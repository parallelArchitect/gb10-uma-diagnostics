/*
 * uma_fault_probe_kernel.cu
 * UMA Page Fault Latency Probe kernel — CUDA C replacement for hand-written PTX.
 * nvcc compiles this to native SASS for the target GPU.
 * No PTX files needed. Works on Pascal through Blackwell.
 */

#include <stdint.h>

__global__ void uma_fault_probe_kernel(
    const float * __restrict__ data,
    uint64_t     * __restrict__ latency,
    uint64_t      n)
{
    uint64_t tid = (uint64_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= n) return;

    /* Force cache bypass — volatile load, no L1/L2 reuse */
    const volatile float *ptr = (const volatile float *)data;

    uint64_t t0 = clock64();
    float val = ptr[tid];
    uint64_t t1 = clock64();

    /* Prevent compiler from optimizing away the load */
    if (val != val) latency[tid] = 0; /* NaN guard, never true */
    else latency[tid] = t1 - t0;
}
