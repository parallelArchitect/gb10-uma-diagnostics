#pragma once
/*
 * time_sync.h
 *
 * CPU-side timeline anchor for cross-domain correlation.
 * Uses CLOCK_MONOTONIC_RAW for stable nanosecond timestamps.
 */

#include <stdint.h>
#include <time.h>

static inline uint64_t ts_now_ns() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

/*
 * TimeSync context
 * t0_ns = experiment start time (CPU domain)
 */
typedef struct {
    uint64_t t0_ns;
} time_sync_t;

static inline void time_sync_init(time_sync_t *ts) {
    ts->t0_ns = ts_now_ns();
}

/*
 * Convert elapsed milliseconds (cudaEvent) to absolute ns
 */
static inline uint64_t ts_ms_to_ns(time_sync_t *ts, float ms) {
    return ts->t0_ns + (uint64_t)(ms * 1e6);
}
