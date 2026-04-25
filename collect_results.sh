#!/bin/bash
OUTDIR="uma_results_$(hostname)_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

FOUND=0
for f in uma_probe_results.json uma_bw_results.json uma_atomic_results.json; do
    if [ -f "$f" ]; then
        cp "$f" "$OUTDIR/"
        FOUND=$((FOUND + 1))
    else
        echo "Warning: $f not found — run the tool first"
    fi
done

# Include latest sparkview anomaly log if available
SPARK_LOG=$(ls -t ~/sparkview_logs/*/summary.json 2>/dev/null | head -1)
if [ -n "$SPARK_LOG" ]; then
    SPARK_DIR=$(dirname "$SPARK_LOG")
    cp "$SPARK_DIR/summary.json" "$OUTDIR/sparkview_summary.json" 2>/dev/null
    cp "$SPARK_DIR/anomaly.log.gz" "$OUTDIR/sparkview_anomaly.log.gz" 2>/dev/null
    echo "Included latest sparkview log: $SPARK_DIR"
fi

if [ "$FOUND" -eq 0 ]; then
    echo "No results found. Run uma_probe, uma_bw, and uma_atomic first."
    rm -rf "$OUTDIR"
    exit 1
fi

zip -r "${OUTDIR}.zip" "$OUTDIR"
rm -rf "$OUTDIR"

echo ""
echo "Results packaged: ${OUTDIR}.zip ($FOUND of 3 tools)"
echo ""
echo "What would you like to do?"
echo "  [1] Share — open GitHub Issues to upload"
echo "  [2] Local — keep results, no upload"
echo ""
read -p "Enter 1 or 2: " CHOICE

if [ "$CHOICE" = "1" ]; then
    echo "Opening GitHub Issues..."
    xdg-open "https://github.com/parallelArchitect/nvidia-uma-fault-probe/issues/new" 2>/dev/null || \
    echo "Go to: https://github.com/parallelArchitect/nvidia-uma-fault-probe/issues/new"
else
    echo "Results saved: $(pwd)/${OUTDIR}.zip"
    echo "Done."
fi
