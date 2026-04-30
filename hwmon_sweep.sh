#!/bin/bash
# hwmon_sweep.sh — sweep all hwmon devices and report channels
# For GB10 DGX Spark — identifies spark_hwmon (spbm) chip and all exposed channels
# Output: printed to terminal + saved to hwmon_sweep_<hostname>_<timestamp>.zip

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
HOST=$(hostname)
OUTFILE="hwmon_sweep_${HOST}_${TIMESTAMP}.txt"

sweep() {
    echo "=== hwmon device sweep ==="
    echo "Host     : $HOST"
    echo "Time     : $(date)"
    echo ""

    for hwmon in /sys/class/hwmon/hwmon*; do
        name=$(cat "$hwmon/name" 2>/dev/null)
        echo "--- $hwmon ($name) ---"

        # Temperature channels
        for f in "$hwmon"/temp*_input; do
            [ -f "$f" ] || continue
            base="${f%_input}"
            label=$(cat "${base}_label" 2>/dev/null || echo "unlabeled")
            val=$(cat "$f" 2>/dev/null)
            [ -n "$val" ] && printf "  TEMP  %-20s %s\n" "$label" "$(echo "scale=1; $val/1000" | bc)°C"
        done

        # Power channels
        for f in "$hwmon"/power*_input; do
            [ -f "$f" ] || continue
            base="${f%_input}"
            label=$(cat "${base}_label" 2>/dev/null || echo "unlabeled")
            val=$(cat "$f" 2>/dev/null)
            [ -n "$val" ] && printf "  POWER %-20s %s\n" "$label" "$(echo "scale=2; $val/1000000" | bc)W"
        done

        # Energy channels
        for f in "$hwmon"/energy*_input; do
            [ -f "$f" ] || continue
            base="${f%_input}"
            label=$(cat "${base}_label" 2>/dev/null || echo "unlabeled")
            val=$(cat "$f" 2>/dev/null)
            [ -n "$val" ] && printf "  ENERGY%-20s %s\n" "$label" "$(echo "scale=0; $val/1000" | bc)mJ"
        done

        # Custom spark_hwmon sysfs attributes
        for attr in prochot pl_level tj_max_c; do
            [ -f "$hwmon/$attr" ] && printf "  STATUS%-20s %s\n" "$attr" "$(cat $hwmon/$attr 2>/dev/null)"
        done

        echo ""
    done

    # Also dump raw sysfs labels for debugging
    echo "=== Raw sysfs labels ==="
    cat /sys/class/hwmon/hwmon*/temp*_label 2>/dev/null | sort -u
    cat /sys/class/hwmon/hwmon*/power*_label 2>/dev/null | sort -u
    cat /sys/class/hwmon/hwmon*/energy*_label 2>/dev/null | sort -u
    echo ""

    echo "=== sensors output ==="
    sensors 2>/dev/null || echo "sensors not available"
    echo ""

    echo "=== sensors -j ==="
    sensors -j 2>/dev/null || echo "sensors -j not available"
}

# Print to terminal AND save to file
sweep | tee "$OUTFILE"

# Package into zip
zip "${OUTFILE%.txt}.zip" "$OUTFILE"
rm "$OUTFILE"

echo ""
echo "Results saved: ${OUTFILE%.txt}.zip"
echo ""
echo "  [1] Share — open GitHub Issues"
echo "  [2] Local — keep results"
echo ""
read -p "Enter 1 or 2: " CHOICE
if [ "$CHOICE" = "1" ]; then
    xdg-open "https://github.com/parallelArchitect/gb10-uma-diagnostics/issues/new" 2>/dev/null || \
    echo "Go to: https://github.com/parallelArchitect/gb10-uma-diagnostics/issues/new"
else
    echo "Done: $(pwd)/${OUTFILE%.txt}.zip"
fi
