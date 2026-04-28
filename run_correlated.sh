#!/bin/bash
cd "$(dirname "$0")"
# run_correlated.sh — power + bandwidth correlation run
# GB10 / DGX Spark target platform only
# Requires: uma_bw, uma_contention, sensors (spark_hwmon)

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
HOST=$(hostname)
OUTDIR="correlated_${HOST}_${TIMESTAMP}"
SPBM_LOG="spbm_${HOST}_${TIMESTAMP}.txt"
LOGFILE="$OUTDIR/run_guard.log"

# GB10 (DGX Spark) only — idle clock ~208MHz
IDLE_CLOCK_THRESHOLD=500
THROTTLE_CLOCK_THRESHOLD=850
MAX_WAIT_SECONDS=300
SAMPLE_INTERVAL=5
BASELINE_SAMPLES=5
BASELINE_TEMP=50

log() {
    echo "$(date +%s%3N) $*" >> "$LOGFILE"
    echo "$*"
}

show_progress() {
    local MSG="$1" PID="$2" ELAPSED=0 W=20
    while kill -0 $PID 2>/dev/null; do
        local F=$(( ELAPSED < W ? ELAPSED : W ))
        local EMPTY=$(( W - F ))
        local BAR="" i
        for i in $(seq 1 $F);     do BAR="${BAR}█"; done
        for i in $(seq 1 $EMPTY); do BAR="${BAR}░"; done
        printf '\r  [%s] %s %ds ' "$BAR" "$MSG" "$ELAPSED"
        sleep 1
        ELAPSED=$((ELAPSED+1))
    done
    printf '\r  [████████████████████] %s done.\n' "$MSG"
}

get_clock() {
    nvidia-smi --query-gpu=clocks.gr --format=csv,noheader,nounits 2>/dev/null | tr -d ' '
}

get_temp() {
    nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | tr -d ' '
}

get_stable_baseline() {
    log "  Capturing baseline (${BASELINE_SAMPLES} samples)..."
    local sum_clk=0 sum_tmp=0
    for i in $(seq 1 $BASELINE_SAMPLES); do
        local CLK TMP
        CLK=$(get_clock)
        TMP=$(get_temp)
        sum_clk=$((sum_clk + CLK))
        sum_tmp=$((sum_tmp + TMP))
        sleep 1
    done
    BASELINE_CLOCK=$((sum_clk / BASELINE_SAMPLES))
    BASELINE_TEMP=$((sum_tmp / BASELINE_SAMPLES))
    log "  Baseline clock: ${BASELINE_CLOCK}MHz  temp: ${BASELINE_TEMP}C"
}

check_throttle_under_load() {
    log "  Checking for throttle under load..."
    timeout 3s ./uma_bw >/dev/null 2>&1 &
    local BW_PID=$!
    sleep 1
    local CLK
    CLK=$(get_clock)
    kill $BW_PID 2>/dev/null
    wait $BW_PID 2>/dev/null
    if [ -n "$CLK" ] && [ "$CLK" -lt "$THROTTLE_CLOCK_THRESHOLD" ]; then
        log "  ⚠ Throttle detected: ${CLK}MHz"
        return 0
    fi
    log "  ✓ No throttle: ${CLK}MHz"
    return 1
}

wait_for_idle() {
    local LABEL="$1" ELAPSED=0 TEMP_MARGIN=2
    log "[$LABEL] Waiting for idle..."
    while true; do
        local CLK TMP
        CLK=$(get_clock)
        TMP=$(get_temp)
        if [ -n "$CLK" ] && [ "$CLK" -lt "$IDLE_CLOCK_THRESHOLD" ] && \
           [ -n "$TMP" ] && [ "$TMP" -le $((BASELINE_TEMP + TEMP_MARGIN)) ]; then
            log "  ✓ Idle: ${CLK}MHz / ${TMP}C"
            break
        fi
        if [ "$ELAPSED" -ge "$MAX_WAIT_SECONDS" ]; then
            log "  ⚠ Timeout — proceeding"
            break
        fi
        echo "$(date +%s%3N)  CLK=${CLK}MHz TMP=${TMP}C (${ELAPSED}s)" >> "$LOGFILE"
        sleep "$SAMPLE_INTERVAL"
        ELAPSED=$((ELAPSED + SAMPLE_INTERVAL))
    done
}

get_uma_pressure() {
    local LOG="$SV_ANOMALY"
    if [ -f "$LOG" ]; then
        tail -n 20 "$LOG" | awk '''/PSI:/ {
            for(i=1;i<=NF;i++){
                if($i=="some") some=$(i+1)
                if($i=="full") full=$(i+1)
            }
        }
        END {
            if (some != "" && full != "")
                printf "some %s full %s", some, full
        }'''
    fi
}

pre_run_check() {
    local CLK TMP
    CLK=$(get_clock)
    TMP=$(get_temp)
    echo ""
    echo "=== Pre-run system check ==="
    echo "  Clock: ${CLK}MHz  Temp: ${TMP}C"

    local SWAP_PCT
    SWAP_PCT=$(free | awk '/Swap/{if($2>0) printf "%.0f", $3/$2*100; else print "0"}')
    echo "  SWAP: ${SWAP_PCT}%"
    if [ -n "$SWAP_PCT" ] && [ "$SWAP_PCT" -gt 80 ]; then
        echo "  ✗ SWAP too high — run: sudo swapoff -a && sudo swapon -a"
        exit 1
    fi

    if [ -n "$CLK" ] && [ "$CLK" -ge "$IDLE_CLOCK_THRESHOLD" ]; then
        echo "  Waiting for idle..."
        BASELINE_TEMP=${TMP:-50}
        wait_for_idle "pre-run"
    fi

    get_stable_baseline

    if check_throttle_under_load; then
        echo "  ⚠ Throttling — waiting for recovery..."
        wait_for_idle "throttle-recovery"
    fi

    local UMA
    UMA=$(get_uma_pressure)
    if [ -n "$UMA" ]; then
        echo "  UMA  : $UMA"
    fi
    echo "  ✓ System ready."
}

echo "=========================================="
echo " power + bandwidth correlation run"
echo " GB10 / DGX Spark"
echo "=========================================="
echo ""

if [ ! -f "./uma_bw" ]; then
    echo "Error: uma_bw not found."
    echo "Build: nvcc -O2 -std=c++17 -I./include uma_bandwidth_test.cu -o uma_bw -lcudart -lpthread"
    exit 1
fi

if ! command -v sensors &>/dev/null; then
    echo "Error: sensors not found — install spark_hwmon."
    exit 1
fi

# sparkview — passive read only, never launched by this script
SV_LATEST=$(ls -td ~/sparkview_logs/* 2>/dev/null | head -1)
SV_ANOMALY=""
if [ -n "$SV_LATEST" ] && [ -f "$SV_LATEST/anomaly.log" ]; then
    SV_ANOMALY="$SV_LATEST/anomaly.log"
    echo "  sparkview log found: $SV_LATEST"
else
    echo "  sparkview not detected — start it manually for power anomaly logging."
fi

echo ""
echo "Output dir : $OUTDIR"
echo ""
echo "Press Enter to start, or Ctrl+C to cancel."
read -r

mkdir -p "$OUTDIR"

python3 spbm_analyzer.py "$OUTDIR" "$SV_ANOMALY" > /dev/null 2>&1 &
ANALYZER_PID=$!

pre_run_check

# --- RUN 1: default clocks ---
echo ""
echo "=== RUN 1: default clocks ==="
echo "START_RUN1 $(date +%s%3N)" >> "$OUTDIR/$SPBM_LOG"
while true; do
    echo "$(date +%s%3N)"
    sensors 2>/dev/null | grep -E "spbm|power|temp|prochot|PL|dc_input|gpu|soc"
    echo "-----"
    sleep 0.2
done >> "$OUTDIR/$SPBM_LOG" &
SPBM_PID=$!
./uma_bw > "$OUTDIR/uma_bw_run1.txt" 2>&1 &
BW_PID=$!
show_progress "uma_bw run 1" $BW_PID
wait $BW_PID
echo "END_RUN1 $(date +%s%3N)" >> "$OUTDIR/$SPBM_LOG"
kill $SPBM_PID 2>/dev/null
wait $SPBM_PID 2>/dev/null
wait_for_idle "post-run1"

# --- RUN 2: capped clocks ---
echo ""
echo "=== RUN 2: capped clocks ==="
nvidia-smi -lgc 2100,2100 > /dev/null 2>&1 && echo "  Clock cap applied." || echo "  Clock cap skipped."
echo "START_RUN2 $(date +%s%3N)" >> "$OUTDIR/$SPBM_LOG"
while true; do
    echo "$(date +%s%3N)"
    sensors 2>/dev/null | grep -E "spbm|power|temp|prochot|PL|dc_input|gpu|soc"
    echo "-----"
    sleep 0.2
done >> "$OUTDIR/$SPBM_LOG" &
SPBM_PID=$!
./uma_bw > "$OUTDIR/uma_bw_run2.txt" 2>&1 &
BW_PID=$!
show_progress "uma_bw run 2" $BW_PID
wait $BW_PID
echo "END_RUN2 $(date +%s%3N)" >> "$OUTDIR/$SPBM_LOG"
kill $SPBM_PID 2>/dev/null
wait $SPBM_PID 2>/dev/null
nvidia-smi -rgc > /dev/null 2>&1
wait_for_idle "post-run2"

# --- Phase 5: contention sweep ---
echo ""
echo "=== Contention sweep ==="
if [ ! -f "./uma_contention" ]; then
    echo "  uma_contention not found — skipping."
else
    if [ ! -f "peak_calibration.json" ]; then
        echo "  No peak_calibration.json — running calibration..."
        ./uma_bw --calibrate-peak > /dev/null 2>&1
        wait_for_idle "post-calibration"
    fi
    echo "START_CONTENTION $(date +%s%3N)" >> "$OUTDIR/$SPBM_LOG"
    while true; do
        echo "$(date +%s%3N)"
        sensors 2>/dev/null | grep -E "spbm|power|temp|prochot|PL|dc_input|gpu|soc"
        echo "-----"
        sleep 0.2
    done >> "$OUTDIR/$SPBM_LOG" &
    SPBM_PID=$!
    ./uma_contention --mode sweep --peak-from peak_calibration.json > "$OUTDIR/uma_contention_sweep.txt" 2>&1 &
    CT_PID=$!
    show_progress "contention sweep" $CT_PID
    wait $CT_PID
    echo "END_CONTENTION $(date +%s%3N)" >> "$OUTDIR/$SPBM_LOG"
    kill $SPBM_PID 2>/dev/null
    wait $SPBM_PID 2>/dev/null
    wait_for_idle "post-contention"
    [ -f "uma_contention_results.json" ] && cp "uma_contention_results.json" "$OUTDIR/"
    [ -f "peak_calibration.json" ]       && cp "peak_calibration.json"       "$OUTDIR/"
    [ -f "timeline.json" ]               && cp "timeline.json"               "$OUTDIR/"
fi

for f in uma_bw_results.json uma_probe_results.json uma_atomic_results.json; do
    [ -f "$f" ] && cp "$f" "$OUTDIR/"
done

SPARK_LOG=$(ls -t ~/sparkview_logs/*/summary.json 2>/dev/null | head -1)
if [ -n "$SPARK_LOG" ]; then
    SPARK_DIR=$(dirname "$SPARK_LOG")
    cp "$SPARK_DIR/summary.json" "$OUTDIR/sparkview_summary.json" 2>/dev/null
    cp "$SPARK_DIR/anomaly.log.gz" "$OUTDIR/sparkview_anomaly.log.gz" 2>/dev/null
    echo "  sparkview log included."
fi

kill $ANALYZER_PID 2>/dev/null
wait $ANALYZER_PID 2>/dev/null
[ -f "events.json" ] && cp "events.json" "$OUTDIR/"

zip -r "${OUTDIR}.zip" "$OUTDIR"
rm -rf "$OUTDIR"

echo ""
echo "Results packaged: ${OUTDIR}.zip"
echo ""
echo "  [1] Share — open GitHub Issues"
echo "  [2] Local — keep results"
echo ""
read -p "Enter 1 or 2: " CHOICE
if [ "$CHOICE" = "1" ]; then
    xdg-open "https://github.com/parallelArchitect/nvidia-uma-fault-probe/issues/new" 2>/dev/null || \
    echo "Go to: https://github.com/parallelArchitect/nvidia-uma-fault-probe/issues/new"
else
    echo "Done: $(pwd)/${OUTDIR}.zip"
fi
