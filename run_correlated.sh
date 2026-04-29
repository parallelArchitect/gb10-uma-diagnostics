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

# Recovery detection tuning
# GB10: clock stays at ~2405MHz in P0 active idle — clock gating disabled on GB10
# Recovery is detected via temp return to baseline + power stabilization
THROTTLE_CLOCK_THRESHOLD=850   # below this under load = PROCHOT active
MAX_WAIT_SECONDS=300
SAMPLE_INTERVAL=5
BASELINE_SAMPLES=5
TEMP_MARGIN=2                  # degrees C above baseline
POWER_MARGIN=2                 # watts variance for stability check
STABILITY_SAMPLES=3            # consecutive samples required for stability

# Baseline — measured per run in get_stable_baseline(), never hardcoded
BASELINE_TEMP=50
BASELINE_POWER=0

# Detect GB10 — determines recovery detection path
# GB10 P0 active idle: ~2405MHz (clock not a recovery signal)
# Pascal idle: drops to ~135-455MHz (clock IS a recovery signal)
IS_GB10=0
nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -q "GB10" && IS_GB10=1

# Signal history arrays for stability detection
TEMP_HISTORY=()
POWER_HISTORY=()

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

get_power() {
    # returns integer watts — strip decimal
    nvidia-smi --query-gpu=power.draw \
        --format=csv,noheader,nounits 2>/dev/null | tr -d ' ' | cut -d. -f1
}

# Check if temp is still on a downward slope (still cooling)
# Requires 3 samples — a single flat step is not sufficient
is_cooling() {
    if [ "${#TEMP_HISTORY[@]}" -lt 3 ]; then
        return 0  # not enough data — assume still cooling
    fi
    local t1=${TEMP_HISTORY[-1]}
    local t2=${TEMP_HISTORY[-2]}
    local t3=${TEMP_HISTORY[-3]}
    # downward trend across all three = still cooling
    if [ "$t1" -le "$t2" ] && [ "$t2" -le "$t3" ]; then
        return 0
    fi
    return 1  # slope has flattened
}

# Check if temp has returned to baseline range
temp_near_baseline() {
    if [ "${#TEMP_HISTORY[@]}" -lt 1 ]; then return 1; fi
    local current=${TEMP_HISTORY[-1]}
    if [ "$current" -le $((BASELINE_TEMP + TEMP_MARGIN)) ]; then
        return 0
    fi
    return 1
}

# Check if power has stabilized (not oscillating)
# Variance across last STABILITY_SAMPLES must be within POWER_MARGIN
power_stable() {
    if [ "${#POWER_HISTORY[@]}" -lt "$STABILITY_SAMPLES" ]; then
        return 1
    fi
    local i max_diff=0
    local prev=${POWER_HISTORY[-$STABILITY_SAMPLES]}
    for i in "${POWER_HISTORY[@]: -$((STABILITY_SAMPLES-1))}"; do
        local diff=$(( i - prev ))
        [ "$diff" -lt 0 ] && diff=$(( -diff ))
        [ "$diff" -gt "$max_diff" ] && max_diff=$diff
        prev=$i
    done
    [ "$max_diff" -le "$POWER_MARGIN" ] && return 0
    return 1
}

get_stable_baseline() {
    log "  Capturing baseline (${BASELINE_SAMPLES} samples)..."
    local sum_clk=0 sum_tmp=0 sum_pwr=0
    local tmp_samples=() pwr_samples=()

    for i in $(seq 1 $BASELINE_SAMPLES); do
        local CLK TMP PWR
        CLK=$(get_clock)
        TMP=$(get_temp)
        PWR=$(get_power)
        sum_clk=$((sum_clk + CLK))
        sum_tmp=$((sum_tmp + TMP))
        sum_pwr=$((sum_pwr + PWR))
        tmp_samples+=("$TMP")
        pwr_samples+=("$PWR")
        sleep 1
    done

    BASELINE_CLOCK=$((sum_clk / BASELINE_SAMPLES))
    BASELINE_TEMP=$((sum_tmp / BASELINE_SAMPLES))
    BASELINE_POWER=$((sum_pwr / BASELINE_SAMPLES))

    # Warn if baseline is unstable — system may not be settled yet
    local tmin tmax pmin pmax
    tmin=$(printf "%s\n" "${tmp_samples[@]}" | sort -n | head -1)
    tmax=$(printf "%s\n" "${tmp_samples[@]}" | sort -n | tail -1)
    pmin=$(printf "%s\n" "${pwr_samples[@]}" | sort -n | head -1)
    pmax=$(printf "%s\n" "${pwr_samples[@]}" | sort -n | tail -1)

    log "  Baseline clock: ${BASELINE_CLOCK}MHz  temp: ${BASELINE_TEMP}C  power: ${BASELINE_POWER}W"
    [ $(( tmax - tmin )) -gt 2 ] && log "  ⚠ Temp spread $((tmax-tmin))C during baseline — system may still be settling"
    [ $(( pmax - pmin )) -gt 5 ] && log "  ⚠ Power spread $((pmax-pmin))W during baseline — system may still be settling"
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
        log "  ⚠ Throttle detected: ${CLK}MHz — PROCHOT likely active"
        return 0
    fi
    log "  ✓ No throttle: ${CLK}MHz"
    return 1
}

wait_for_idle() {
    local LABEL="$1" ELAPSED=0
    # Reset signal history at start of each wait period
    TEMP_HISTORY=()
    POWER_HISTORY=()

    log "[$LABEL] Waiting for recovery..."

    local CLK TMP PWR
    CLK=$(get_clock)
    TMP=$(get_temp)
    PWR=$(get_power)

    if [ "$IS_GB10" -eq 1 ]; then
        # GB10 fast-path pre-check:
        # If system is already at baseline on entry — pass immediately.
        # Handles well-cooled units (external fan) where recovery is
        # instantaneous and history buildup would cause false timeout.
        # Works for both paths:
        #   external fan → already recovered, pass immediately
        #   stock unit   → not recovered, fall through to history loop
        local ALREADY_RECOVERED=0
        awk "BEGIN {
            t=$TMP; p=$PWR
            bt=$BASELINE_TEMP; bp=$BASELINE_POWER
            tm=$TEMP_MARGIN; pm=$POWER_MARGIN
            pdiff = p - bp; if (pdiff < 0) pdiff = -pdiff
            if (t <= bt + tm && pdiff <= pm) exit 0
            exit 1
        }" && ALREADY_RECOVERED=1
        if [ "$ALREADY_RECOVERED" -eq 1 ]; then
            log "  ✓ Recovered immediately: ${CLK}MHz / ${TMP}C / ${PWR}W"
            return 0
        fi
    fi

    while true; do
        CLK=$(get_clock)
        TMP=$(get_temp)
        PWR=$(get_power)
        local READY=0

        # Accumulate signal history — keep last 5 samples
        TEMP_HISTORY+=("$TMP")
        POWER_HISTORY+=("$PWR")
        [ "${#TEMP_HISTORY[@]}"  -gt 5 ] && TEMP_HISTORY=("${TEMP_HISTORY[@]: -5}")
        [ "${#POWER_HISTORY[@]}" -gt 5 ] && POWER_HISTORY=("${POWER_HISTORY[@]: -5}")

        if [ "$IS_GB10" -eq 1 ]; then
            # GB10 recovery model:
            # Clock stays at ~2405MHz in P0 — not a recovery signal
            # Recovery = temp returned to baseline AND slope flattened AND power stable
            # Power drops fast (electrical), temp lags (thermal inertia)
            # Both must confirm before declaring recovery
            if temp_near_baseline && ! is_cooling && power_stable; then
                READY=1
            fi
        else
            # Pascal: clock drop is a reliable idle signal
            if [ -n "$CLK" ] && [ "$CLK" -lt 500 ] && \
               [ -n "$TMP" ] && [ "$TMP" -le $((BASELINE_TEMP + TEMP_MARGIN)) ]; then
                READY=1
            fi
        fi

        if [ "$READY" -eq 1 ]; then
            log "  ✓ Recovered: ${CLK}MHz / ${TMP}C / ${PWR}W"
            break
        fi

        if [ "$ELAPSED" -ge "$MAX_WAIT_SECONDS" ]; then
            log "  ⚠ Timeout — proceeding"
            break
        fi

        echo "$(date +%s%3N)  CLK=${CLK}MHz TMP=${TMP}C PWR=${PWR}W (${ELAPSED}s)" >> "$LOGFILE"
        sleep "$SAMPLE_INTERVAL"
        ELAPSED=$((ELAPSED + SAMPLE_INTERVAL))
    done
}

get_uma_pressure() {
    local LOG="$SV_ANOMALY"
    if [ -f "$LOG" ]; then
        tail -n 20 "$LOG" | awk '/PSI:/ {
            for(i=1;i<=NF;i++){
                if($i=="some") some=$(i+1)
                if($i=="full") full=$(i+1)
            }
        }
        END {
            if (some != "" && full != "")
                printf "some %s full %s", some, full
        }'
    fi
}

pre_run_check() {
    local CLK TMP PWR
    CLK=$(get_clock)
    TMP=$(get_temp)
    PWR=$(get_power)
    echo ""
    echo "=== Pre-run system check ==="
    echo "  Clock: ${CLK}MHz  Temp: ${TMP}C  Power: ${PWR}W"

    local SWAP_PCT
    SWAP_PCT=$(free | awk '/Swap/{if($2>0) printf "%.0f", $3/$2*100; else print "0"}')
    echo "  SWAP: ${SWAP_PCT}%"
    if [ -n "$SWAP_PCT" ] && [ "$SWAP_PCT" -gt 80 ]; then
        echo "  ✗ SWAP too high — run: sudo swapoff -a && sudo swapon -a"
        exit 1
    fi

    # Seed baseline before first wait so recovery detection has a reference
    BASELINE_TEMP=${TMP:-50}
    BASELINE_POWER=${PWR:-30}
    wait_for_idle "pre-run"

    # Measure proper baseline after initial recovery
    get_stable_baseline

    if check_throttle_under_load; then
        echo "  ⚠ Throttling — waiting for recovery..."
        wait_for_idle "throttle-recovery"
    fi

    local UMA
    UMA=$(get_uma_pressure)
    [ -n "$UMA" ] && echo "  UMA  : $UMA"
    echo "  ✓ System ready."
}

echo "=========================================="
echo " power + bandwidth correlation run"
echo " GB10 / DGX Spark"
echo "=========================================="
echo ""

if [ ! -f "./uma_bw" ]; then
    echo "Error: uma_bw not found."
    echo "Build: /usr/local/cuda-13.0/bin/nvcc -O2 -std=c++17 -I./include uma_bandwidth_test.cu -o uma_bw -lcudart -lpthread"
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

# --- Contention sweep ---
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
    xdg-open "https://github.com/parallelArchitect/gb10-uma-diagnostics/issues/new" 2>/dev/null || \
    echo "Go to: https://github.com/parallelArchitect/gb10-uma-diagnostics/issues/new"
else
    echo "Done: $(pwd)/${OUTDIR}.zip"
fi
