#!/bin/bash
#
# Memory benchmarking orchestrator for PiFinder
#
# Runs memory profiling across different configurations:
# 1. Baseline (origin/HEAD with all libraries)
# 2. Incremental library removal (using import patches on baseline)
# 3. Optimized (catalog-memory-optimization branch)
#

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_DIR="$REPO_ROOT/python"
RESULTS_FILE="$REPO_ROOT/memory_benchmark_results.json"
LOG_FILE="$REPO_ROOT/memory_benchmark.log"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    echo -e "${GREEN}[$(date +'%H:%M:%S')]${NC} $1" | tee -a "$LOG_FILE"
}

error() {
    echo -e "${RED}[$(date +'%H:%M:%S')]${NC} ERROR: $1" | tee -a "$LOG_FILE"
}

warn() {
    echo -e "${YELLOW}[$(date +'%H:%M:%S')]${NC} WARNING: $1" | tee -a "$LOG_FILE"
}

cleanup_pifinder() {
    log "Cleaning up any existing PiFinder processes..."
    pkill -f "PiFinder.main" || true
    sleep 2
}

setup_venv() {
    local branch=$1
    log "Setting up Python environment for $branch..."

    cd "$PYTHON_DIR"

    # Remove old venv
    if [ -d ".venv" ]; then
        rm -rf .venv
    fi

    # Create new venv with Python 3.9
    python3 -m venv .venv
    source .venv/bin/activate

    # Install dependencies
    log "Installing dependencies..."
    pip install --upgrade pip > /dev/null 2>&1
    pip install psutil > /dev/null 2>&1
    pip install -r requirements.txt 2>&1 | grep -v "^Requirement already satisfied" || true

    log "Environment ready"
}

run_profile() {
    local config=$1
    local branch=$2

    log "="
    log "Profiling: $config (branch: $branch)"
    log "="

    cd "$PYTHON_DIR"
    source .venv/bin/activate

    python3 "$REPO_ROOT/profile_memory.py" \
        --config "$config" \
        --duration 60 \
        --output "${REPO_ROOT}/memory_profile_${config}.json"

    if [ $? -eq 0 ]; then
        log "✓ Profiling completed for $config"
    else
        error "✗ Profiling failed for $config"
        return 1
    fi
}

# Main execution
main() {
    log "=========================================="
    log "PiFinder Memory Benchmark"
    log "=========================================="

    # Initialize log
    echo "" > "$LOG_FILE"

    cleanup_pifinder

    # Save current branch
    CURRENT_BRANCH=$(git branch --show-current)
    log "Current branch: $CURRENT_BRANCH"

    # Stash any local changes
    git stash push -u -m "Memory benchmark stash" || true

    #
    # Phase 1: Baseline (origin/HEAD)
    #
    log "Phase 1: Measuring baseline (origin/HEAD)"
    git checkout origin/HEAD 2>&1 | tee -a "$LOG_FILE" || {
        error "Failed to checkout origin/HEAD"
        git stash pop || true
        exit 1
    }

    setup_venv "origin/HEAD"
    run_profile "baseline" "origin/HEAD" || {
        error "Baseline profiling failed"
        git checkout "$CURRENT_BRANCH"
        git stash pop || true
        exit 1
    }

    cleanup_pifinder

    #
    # Phase 2: Incremental library removal on baseline
    #
    log "Phase 2: Testing individual library removal"

    for config in "no-pandas" "no-sklearn" "no-scipy-ndimage" "no-scipy-transform"; do
        log "Testing configuration: $config"
        run_profile "$config" "origin/HEAD" || warn "Config $config failed"
        cleanup_pifinder
        sleep 5
    done

    #
    # Phase 3: Optimized branch
    #
    log "Phase 3: Measuring optimized (catalog-memory-optimization)"

    git checkout catalog-memory-optimization 2>&1 | tee -a "$LOG_FILE" || {
        error "Failed to checkout catalog-memory-optimization"
        git checkout "$CURRENT_BRANCH"
        git stash pop || true
        exit 1
    }

    setup_venv "catalog-memory-optimization"

    # Build databases if needed
    if [ ! -f "$REPO_ROOT/astro_data/bright_stars.db" ]; then
        log "Building star database..."
        python3 scripts/build_star_database.py 2>&1 | tee -a "$LOG_FILE"
    fi

    if [ ! -f "$REPO_ROOT/astro_data/pifinder_objects.db" ]; then
        log "Building object catalogs..."
        python3 -m PiFinder.catalog_imports.main 2>&1 | tee -a "$LOG_FILE"
    fi

    run_profile "optimized" "catalog-memory-optimization" || warn "Optimized profiling failed"

    cleanup_pifinder

    #
    # Restore original state
    #
    log "Restoring original branch: $CURRENT_BRANCH"
    git checkout "$CURRENT_BRANCH" 2>&1 | tee -a "$LOG_FILE"
    git stash pop || true

    #
    # Generate report
    #
    log "=========================================="
    log "Generating combined report..."
    log "=========================================="

    python3 - <<'PYTHON_SCRIPT'
import json
import glob

# Collect all results
results = []
for filename in sorted(glob.glob("memory_profile_*.json")):
    with open(filename) as f:
        data = json.load(f)
        if isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)

# Save combined results
with open("memory_benchmark_results.json", "w") as f:
    json.dump(results, f, indent=2)

# Print summary
baseline = next((r for r in results if r['config'] == 'baseline'), None)
optimized = next((r for r in results if r['config'] == 'optimized'), None)

if baseline and optimized:
    baseline_total = baseline['max_rss'].get('TOTAL', 0)
    optimized_total = optimized['max_rss'].get('TOTAL', 0)
    savings = baseline_total - optimized_total

    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    print(f"Baseline (origin/HEAD):              {baseline_total:>10.1f} MB")
    print(f"Optimized (catalog-memory-opt):      {optimized_total:>10.1f} MB")
    print(f"Total Savings:                        {savings:>10.1f} MB ({savings/baseline_total*100:.1f}%)")
    print("="*80)

    # Per-library breakdown
    print("\nPER-LIBRARY SAVINGS:")
    print("-"*80)

    configs = ['no-pandas', 'no-sklearn', 'no-scipy-ndimage', 'no-scipy-transform']
    for config in configs:
        result = next((r for r in results if r['config'] == config), None)
        if result:
            total = result['max_rss'].get('TOTAL', 0)
            lib_savings = baseline_total - total
            lib_name = config.replace('no-', '')
            print(f"{lib_name:<25} {lib_savings:>10.1f} MB ({lib_savings/baseline_total*100:>6.1f}%)")

    print("-"*80)

else:
    print("\nWarning: Missing baseline or optimized results")

print(f"\nDetailed results saved to: memory_benchmark_results.json")
PYTHON_SCRIPT

    log "=========================================="
    log "Benchmark complete!"
    log "Results: $RESULTS_FILE"
    log "Log: $LOG_FILE"
    log "=========================================="
}

# Run main
main "$@"
