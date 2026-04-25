#!/bin/bash
# Run all three UMA probes in sequence with thermal cooldown.

cd "$(dirname "$0")"

# Check all binaries are built
for bin in uma_probe uma_atomic uma_bw; do
    if [ ! -f "./$bin" ]; then
        echo "Error: tools not built. Run:"
        echo ""
        echo "  nvcc -O2 -std=c++17 probe_launcher.cu -o uma_probe -lcudart -lcuda -lpthread"
        echo "  nvcc -O2 -std=c++17 -arch=sm_90 uma_atomic_test.cu -o uma_atomic -lcudart -lcuda -lpthread"
        echo "  nvcc -O2 -std=c++17 uma_bandwidth_test.cu -o uma_bw -lcudart -lcuda -lpthread"
        exit 1
    fi
done

echo "=========================================="
echo " nvidia-uma-fault-probe — full suite run"
echo "=========================================="
echo ""

# sparkview detection
if pgrep -f "sparkview.*main.py" > /dev/null; then
    echo "sparkview is already running."
    echo "Close it first for a clean session log, then rerun this script."
    exit 1
elif [ -f "$HOME/sparkview_v2/main.py" ]; then
    echo "sparkview found — launching in new terminal..."
    gnome-terminal -- bash -c "cd $HOME/sparkview_v2 && source sparkview-venv/bin/activate && python3 main.py; exec bash" 2>/dev/null &
    sleep 2
elif [ -f "$HOME/sparkview/main.py" ]; then
    echo "sparkview found — launching in new terminal..."
    gnome-terminal -- bash -c "cd $HOME/sparkview && source sparkview-venv/bin/activate && python3 main.py; exec bash" 2>/dev/null &
    sleep 2
else
    echo "sparkview not found — recommended for thermal monitoring."
    echo "Install: https://github.com/parallelArchitect/sparkview"
fi

echo ""
echo "Press Enter to start, or Ctrl+C to cancel."
read -r

echo "=== uma_probe ==="
./uma_probe
echo "Cooling down (10s)..."
sleep 10

echo ""
echo "=== uma_atomic ==="
./uma_atomic
echo "Cooling down (10s)..."
sleep 10

echo ""
echo "=== uma_bw ==="
./uma_bw
echo "Cooling down (30s)..."
sleep 30

echo ""
./collect_results.sh
