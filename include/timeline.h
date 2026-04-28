#pragma once
/*
 * timeline.h
 *
 * Minimal event logger — JSON lines
 * One event per line (fast, streamable, SPBM-alignable)
 */

#include <stdio.h>
#include <stdint.h>

typedef struct {
    FILE *fp;
} timeline_t;

static inline void timeline_open(timeline_t *tl, const char *path) {
    tl->fp = fopen(path, "w");
}

static inline void timeline_close(timeline_t *tl) {
    if (tl->fp) fclose(tl->fp);
}

static inline void timeline_event(timeline_t *tl,
                                   const char *type,
                                   uint64_t t_ns) {
    if (!tl->fp) return;
    fprintf(tl->fp,
        "{\"t_ns\":%lu,\"type\":\"%s\"}\n",
        t_ns, type);
}

static inline void timeline_event_val(timeline_t *tl,
                                       const char *type,
                                       uint64_t t_ns,
                                       double val) {
    if (!tl->fp) return;
    fprintf(tl->fp,
        "{\"t_ns\":%lu,\"type\":\"%s\",\"value\":%.3f}\n",
        t_ns, type, val);
}
