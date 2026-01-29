#!/usr/bin/env python3
"""
Library memory footprint profiler.

Measures RSS increase from importing specific libraries in isolation.
This gives accurate measurements of library memory overhead independent
of the full PiFinder application.

Usage:
    python3 measure_library_memory.py
"""

import subprocess
import sys
import json
from typing import Dict, List, Tuple

# Libraries to measure
LIBRARIES = [
    # (import_statement, description)
    ("import numpy", "numpy (baseline - always loaded)"),
    ("import pandas", "pandas (CSV/DataFrame processing)"),
    ("from sklearn.neighbors import BallTree", "sklearn.BallTree (spatial queries)"),
    ("from scipy.spatial.transform import Rotation", "scipy.spatial.transform (IMU quaternions)"),
    ("from scipy.spatial import KDTree", "scipy.KDTree (alternative to BallTree)"),
    ("from scipy.ndimage import label", "scipy.ndimage (image processing)"),
    ("from pyquaternion import Quaternion", "pyquaternion (lightweight quaternions)"),
    ("import sqlite3", "sqlite3 (stdlib - pandas alternative)"),
    ("import skyfield.api", "skyfield (ephemeris calculations)"),
    ("from skyfield.data import hipparcos", "skyfield.data.hipparcos (star catalog)"),
]


def measure_import(import_stmt: str) -> Tuple[float, bool]:
    """
    Measure RSS increase from a single import in a fresh subprocess.

    Returns (rss_mb, success)
    """
    code = f'''
import os
import psutil

# Measure baseline
proc = psutil.Process(os.getpid())
baseline_rss = proc.memory_info().rss

# Perform import
try:
    {import_stmt}
    success = True
except ImportError as e:
    success = False
    print(f"Import failed: {{e}}", file=__import__('sys').stderr)

# Measure after import
after_rss = proc.memory_info().rss
delta_mb = (after_rss - baseline_rss) / (1024 * 1024)

print(f"{{delta_mb:.2f}},{{success}}")
'''

    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            parts = result.stdout.strip().split(",")
            return float(parts[0]), parts[1] == "True"
        else:
            print(f"  Error: {result.stderr.strip()}")
            return 0.0, False

    except subprocess.TimeoutExpired:
        return 0.0, False
    except Exception as e:
        print(f"  Exception: {e}")
        return 0.0, False


def measure_cumulative(*import_stmts: str) -> Tuple[float, bool]:
    """
    Measure RSS from multiple imports together (cumulative impact).
    """
    imports = "\n    ".join(import_stmts)
    code = f'''
import os
import psutil

proc = psutil.Process(os.getpid())
baseline_rss = proc.memory_info().rss

try:
    {imports}
    success = True
except ImportError as e:
    success = False
    print(f"Import failed: {{e}}", file=__import__('sys').stderr)

after_rss = proc.memory_info().rss
delta_mb = (after_rss - baseline_rss) / (1024 * 1024)

print(f"{{delta_mb:.2f}},{{success}}")
'''

    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            parts = result.stdout.strip().split(",")
            return float(parts[0]), parts[1] == "True"
        else:
            print(f"  Error: {result.stderr.strip()}")
            return 0.0, False

    except Exception as e:
        print(f"  Exception: {e}")
        return 0.0, False


def main():
    print("=" * 70)
    print("Library Memory Footprint Analysis")
    print("=" * 70)
    print(f"Python: {sys.version}")
    print()

    # Measure individual libraries
    print("Individual Library Memory Footprint:")
    print("-" * 70)
    results: Dict[str, Dict] = {}

    for import_stmt, description in LIBRARIES:
        rss_mb, success = measure_import(import_stmt)
        status = "OK" if success else "FAILED"
        print(f"  {description:<45} {rss_mb:>8.1f} MB  [{status}]")
        results[import_stmt] = {
            "description": description,
            "rss_mb": rss_mb,
            "success": success
        }

    print()
    print("=" * 70)
    print("Comparative Analysis: Baseline vs Optimized Stack")
    print("=" * 70)
    print()

    # Baseline stack (what original PiFinder uses)
    print("BASELINE STACK (original PiFinder dependencies):")
    print("-" * 70)
    baseline_imports = [
        "import numpy",
        "import pandas",
        "from sklearn.neighbors import BallTree",
        "from scipy.spatial.transform import Rotation",
    ]
    baseline_rss, baseline_ok = measure_cumulative(*baseline_imports)
    print(f"  numpy + pandas + sklearn.BallTree + scipy.Rotation")
    print(f"  Total: {baseline_rss:.1f} MB")
    print()

    # Optimized stack (what our branch uses)
    print("OPTIMIZED STACK (catalog-memory-optimization branch):")
    print("-" * 70)
    optimized_imports = [
        "import numpy",
        "import sqlite3",
        "from scipy.spatial import KDTree",
        "from pyquaternion import Quaternion",
    ]
    optimized_rss, optimized_ok = measure_cumulative(*optimized_imports)
    print(f"  numpy + sqlite3 + scipy.KDTree + pyquaternion")
    print(f"  Total: {optimized_rss:.1f} MB")
    print()

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    if baseline_ok and optimized_ok:
        savings = baseline_rss - optimized_rss
        pct = (savings / baseline_rss * 100) if baseline_rss > 0 else 0
        print(f"  Baseline stack:   {baseline_rss:>8.1f} MB")
        print(f"  Optimized stack:  {optimized_rss:>8.1f} MB")
        print(f"  Memory savings:   {savings:>8.1f} MB ({pct:.1f}%)")
    else:
        print("  Could not compute savings - some imports failed")

    # Save results
    output = {
        "individual": results,
        "baseline_stack": {
            "imports": baseline_imports,
            "rss_mb": baseline_rss,
            "success": baseline_ok
        },
        "optimized_stack": {
            "imports": optimized_imports,
            "rss_mb": optimized_rss,
            "success": optimized_ok
        }
    }

    with open("library_memory_results.json", "w") as f:
        json.dump(output, f, indent=2)
    print()
    print(f"Results saved to library_memory_results.json")


def measure_multiprocess():
    """Measure memory across PiFinder's multi-process architecture."""
    print()
    print("=" * 70)
    print("MULTI-PROCESS ANALYSIS: PiFinder Process Architecture")
    print("=" * 70)
    print()

    print("BASELINE PROCESS IMPORTS:")
    print("-" * 70)

    # Main Process (UI) - plot.py, nearby.py, catalogs, calc_utils
    main_baseline = [
        "import numpy",
        "import pandas",
        "from sklearn.neighbors import BallTree",
        "import skyfield.api",
    ]
    main_mem, _ = measure_cumulative(*main_baseline)
    print(f"Main (UI):     numpy + pandas + sklearn + skyfield     = {main_mem:>6.1f} MB")

    # Solver Process - tetra3 uses scipy
    solver_baseline = [
        "import numpy",
        "from scipy.spatial import KDTree",
        "from scipy.ndimage import label",
    ]
    solver_mem, _ = measure_cumulative(*solver_baseline)
    print(f"Solver:        numpy + scipy.spatial + scipy.ndimage   = {solver_mem:>6.1f} MB")

    # IMU Process
    imu_baseline = ["from scipy.spatial.transform import Rotation"]
    imu_mem, _ = measure_cumulative(*imu_baseline)
    print(f"IMU:           scipy.spatial.transform                 = {imu_mem:>6.1f} MB")

    # Server Process
    server_baseline = ["import numpy", "import skyfield.api", "from PIL import Image"]
    server_mem, _ = measure_cumulative(*server_baseline)
    print(f"Server:        numpy + skyfield + PIL                  = {server_mem:>6.1f} MB")

    baseline_total = main_mem + solver_mem + imu_mem + server_mem
    print(f"                                           TOTAL = {baseline_total:>6.1f} MB")
    print()

    print("OPTIMIZED PROCESS IMPORTS:")
    print("-" * 70)

    # Main Process (UI) - sqlite3 replaces pandas, scipy.KDTree replaces sklearn
    main_optimized = [
        "import numpy",
        "import sqlite3",
        "from scipy.spatial import KDTree",
        "import skyfield.api",
    ]
    main_opt_mem, _ = measure_cumulative(*main_optimized)
    print(f"Main (UI):     numpy + sqlite3 + scipy.KDTree + skyfield = {main_opt_mem:>6.1f} MB")

    # Solver Process - unchanged
    solver_optimized = [
        "import numpy",
        "from scipy.spatial import KDTree",
        "from scipy.ndimage import label",
    ]
    solver_opt_mem, _ = measure_cumulative(*solver_optimized)
    print(f"Solver:        numpy + scipy.spatial + scipy.ndimage   = {solver_opt_mem:>6.1f} MB")

    # IMU Process - pyquaternion replaces scipy.Rotation
    imu_optimized = ["from pyquaternion import Quaternion"]
    imu_opt_mem, _ = measure_cumulative(*imu_optimized)
    print(f"IMU:           pyquaternion                            = {imu_opt_mem:>6.1f} MB")

    # Server Process - unchanged
    server_optimized = ["import numpy", "import skyfield.api", "from PIL import Image"]
    server_opt_mem, _ = measure_cumulative(*server_optimized)
    print(f"Server:        numpy + skyfield + PIL                  = {server_opt_mem:>6.1f} MB")

    optimized_total = main_opt_mem + solver_opt_mem + imu_opt_mem + server_opt_mem
    print(f"                                           TOTAL = {optimized_total:>6.1f} MB")
    print()

    print("=" * 70)
    print("MULTI-PROCESS SAVINGS SUMMARY")
    print("=" * 70)
    savings = baseline_total - optimized_total
    print(f"Baseline total (4 key processes):    {baseline_total:>8.1f} MB")
    print(f"Optimized total (4 key processes):   {optimized_total:>8.1f} MB")
    print(f"Total multi-process savings:         {savings:>8.1f} MB")
    print()

    print("Per-process breakdown:")
    print(f"  Main process savings:     {main_mem - main_opt_mem:>6.1f} MB")
    print(f"  Solver process savings:   {solver_mem - solver_opt_mem:>6.1f} MB")
    print(f"  IMU process savings:      {imu_mem - imu_opt_mem:>6.1f} MB")
    print(f"  Server process savings:   {server_mem - server_opt_mem:>6.1f} MB")


if __name__ == "__main__":
    main()
    measure_multiprocess()
