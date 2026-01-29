#!/usr/bin/env python3
"""
Memory profiling harness for PiFinder library substitution analysis.

Measures RSS across all PiFinder processes with different library configurations
to validate memory savings from pandas, sklearn, scipy component removals.

Usage:
    python3 profile_memory.py --config baseline
    python3 profile_memory.py --config no-pandas
    python3 profile_memory.py --config no-sklearn
    python3 profile_memory.py --config no-scipy-ndimage
    python3 profile_memory.py --config no-scipy-transform
    python3 profile_memory.py --config optimized
"""

import sys
import os
import time
import subprocess
import psutil
import signal
import argparse
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import json

# Add python dir to path for imports
REPO_ROOT = Path(__file__).parent
PYTHON_DIR = REPO_ROOT / "python"
sys.path.insert(0, str(PYTHON_DIR))


class ImportPatcher:
    """Patches imports to simulate library removal without code changes."""

    @staticmethod
    def patch_pandas():
        """Prevent pandas from loading."""
        return """
import sys
class PandasBlocker:
    def find_module(self, fullname, path=None):
        if fullname.startswith('pandas'):
            raise ImportError(f"pandas blocked by profiler: {fullname}")
        return None
sys.meta_path.insert(0, PandasBlocker())
"""

    @staticmethod
    def patch_sklearn():
        """Prevent sklearn from loading."""
        return """
import sys
class SklearnBlocker:
    def find_module(self, fullname, path=None):
        if fullname.startswith('sklearn'):
            raise ImportError(f"sklearn blocked by profiler: {fullname}")
        return None
sys.meta_path.insert(0, SklearnBlocker())
"""

    @staticmethod
    def patch_scipy_ndimage():
        """Prevent scipy.ndimage from loading."""
        return """
import sys
class ScipyNdimageBlocker:
    def find_module(self, fullname, path=None):
        if 'scipy.ndimage' in fullname:
            raise ImportError(f"scipy.ndimage blocked by profiler: {fullname}")
        return None
sys.meta_path.insert(0, ScipyNdimageBlocker())
"""

    @staticmethod
    def patch_scipy_transform():
        """Prevent scipy.spatial.transform from loading."""
        return """
import sys
class ScipyTransformBlocker:
    def find_module(self, fullname, path=None):
        if 'scipy.spatial.transform' in fullname:
            raise ImportError(f"scipy.spatial.transform blocked by profiler: {fullname}")
        return None
sys.meta_path.insert(0, ScipyTransformBlocker())
"""

    @staticmethod
    def get_patch(config: str) -> str:
        """Get import patch code for configuration."""
        patches = {
            'baseline': '',  # No patches, all libraries load
            'no-pandas': ImportPatcher.patch_pandas(),
            'no-sklearn': ImportPatcher.patch_sklearn(),
            'no-scipy-ndimage': ImportPatcher.patch_scipy_ndimage(),
            'no-scipy-transform': ImportPatcher.patch_scipy_transform(),
            'optimized': ''  # Uses actual optimized code
        }
        return patches.get(config, '')


class ProcessMonitor:
    """Monitors RSS of PiFinder processes."""

    def __init__(self):
        self.measurements: List[Dict] = []
        self.start_time = time.time()

    def measure_process_tree(self, root_pid: int) -> Dict[str, int]:
        """Measure RSS of process and all children."""
        try:
            root = psutil.Process(root_pid)
            processes = [root] + root.children(recursive=True)

            rss_by_name = {}
            total_rss = 0

            for proc in processes:
                try:
                    info = proc.as_dict(attrs=['pid', 'name', 'cmdline', 'memory_info'])
                    rss_mb = info['memory_info'].rss / (1024 * 1024)
                    total_rss += rss_mb

                    # Identify process role
                    cmdline = ' '.join(info.get('cmdline', []))
                    if 'PiFinder.main' in cmdline:
                        role = 'Main'
                    elif 'solver' in cmdline.lower():
                        role = 'Solver'
                    elif 'camera' in cmdline.lower():
                        role = 'Camera'
                    elif 'imu' in cmdline.lower():
                        role = 'IMU'
                    elif 'server' in cmdline.lower() or 'web' in cmdline.lower():
                        role = 'Webserver'
                    elif 'gps' in cmdline.lower():
                        role = 'GPS'
                    else:
                        role = info['name']

                    if role in rss_by_name:
                        rss_by_name[role] += rss_mb
                    else:
                        rss_by_name[role] = rss_mb

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            rss_by_name['TOTAL'] = total_rss
            return rss_by_name

        except psutil.NoSuchProcess:
            return {}

    def record_measurement(self, pid: int):
        """Record a snapshot of memory usage."""
        measurement = {
            'timestamp': time.time() - self.start_time,
            'rss': self.measure_process_tree(pid)
        }
        self.measurements.append(measurement)

    def get_max_rss(self) -> Dict[str, float]:
        """Get maximum RSS for each process role."""
        max_rss = {}

        for measurement in self.measurements:
            for role, rss in measurement['rss'].items():
                if role not in max_rss or rss > max_rss[role]:
                    max_rss[role] = rss

        return max_rss


class PiFinderProfiler:
    """Main profiling orchestrator."""

    def __init__(self, config: str, duration: int = 60):
        self.config = config
        self.duration = duration
        self.monitor = ProcessMonitor()
        self.pifinder_proc = None

    def setup_environment(self):
        """Setup Python path and patches."""
        patch_code = ImportPatcher.get_patch(self.config)

        if patch_code:
            # Write patch to temporary file that gets imported first
            patch_file = PYTHON_DIR / "_profiler_patches.py"
            with open(patch_file, 'w') as f:
                f.write(patch_code)

            # Set environment to load patches
            os.environ['PYTHONPATH'] = str(PYTHON_DIR)
            return patch_file
        return None

    def start_pifinder(self) -> Optional[int]:
        """Start PiFinder with debug peripherals."""
        cmd = [
            sys.executable,
            '-m', 'PiFinder.main',
            '-fh',  # Fake hardware
            '--camera', 'debug',
            '--keyboard', 'local',
            '-x'  # Don't know what this does but user uses it
        ]

        # Apply import patches via sitecustomize
        patch_code = ImportPatcher.get_patch(self.config)
        if patch_code:
            # Create sitecustomize in python dir
            sitecustomize = PYTHON_DIR / 'sitecustomize.py'
            with open(sitecustomize, 'w') as f:
                f.write(patch_code)

        try:
            # Start PiFinder
            self.pifinder_proc = subprocess.Popen(
                cmd,
                cwd=PYTHON_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=os.environ.copy()
            )

            # Wait for initialization
            time.sleep(5)

            if self.pifinder_proc.poll() is not None:
                stdout, stderr = self.pifinder_proc.communicate()
                print(f"PiFinder failed to start:")
                print(f"STDOUT: {stdout.decode()}")
                print(f"STDERR: {stderr.decode()}")
                return None

            return self.pifinder_proc.pid

        except Exception as e:
            print(f"Error starting PiFinder: {e}")
            return None

    def cleanup_patches(self):
        """Remove temporary patch files."""
        for patch_file in [PYTHON_DIR / 'sitecustomize.py', PYTHON_DIR / '_profiler_patches.py']:
            if patch_file.exists():
                patch_file.unlink()

    def profile(self) -> Dict:
        """Run profiling session."""
        print(f"{'='*60}")
        print(f"Profiling configuration: {self.config}")
        print(f"Duration: {self.duration}s")
        print(f"{'='*60}")

        # Setup
        self.setup_environment()

        # Start PiFinder
        pid = self.start_pifinder()
        if not pid:
            print("Failed to start PiFinder")
            self.cleanup_patches()
            return {}

        print(f"PiFinder started (PID: {pid})")
        print(f"Monitoring memory for {self.duration} seconds...")

        # Monitor for duration
        try:
            for i in range(self.duration):
                self.monitor.record_measurement(pid)
                print(f"  {i+1}s: {self.monitor.measurements[-1]['rss'].get('TOTAL', 0):.1f} MB", end='\r')
                time.sleep(1)

        except KeyboardInterrupt:
            print("\nProfiling interrupted")

        finally:
            # Cleanup
            print("\nStopping PiFinder...")
            if self.pifinder_proc:
                self.pifinder_proc.terminate()
                try:
                    self.pifinder_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.pifinder_proc.kill()

            self.cleanup_patches()

        # Get results
        max_rss = self.monitor.get_max_rss()

        print(f"\n{'='*60}")
        print(f"Results for {self.config}:")
        print(f"{'='*60}")
        for role, rss in sorted(max_rss.items()):
            if role == 'TOTAL':
                print(f"{'TOTAL':<15} {rss:>10.1f} MB")
            else:
                print(f"  {role:<13} {rss:>10.1f} MB")

        return {
            'config': self.config,
            'duration': self.duration,
            'max_rss': max_rss,
            'measurements': self.monitor.measurements
        }


def compare_results(results: List[Dict]):
    """Compare results across configurations."""
    if not results:
        return

    # Find baseline
    baseline = next((r for r in results if r['config'] == 'baseline'), None)
    if not baseline:
        print("No baseline found for comparison")
        return

    baseline_total = baseline['max_rss'].get('TOTAL', 0)

    print(f"\n{'='*80}")
    print(f"COMPARISON TO BASELINE ({baseline_total:.1f} MB)")
    print(f"{'='*80}")
    print(f"{'Configuration':<25} {'Total RSS':>12} {'Savings':>12} {'Percent':>10}")
    print(f"{'-'*80}")

    for result in results:
        config = result['config']
        total = result['max_rss'].get('TOTAL', 0)
        savings = baseline_total - total
        percent = (savings / baseline_total * 100) if baseline_total > 0 else 0

        print(f"{config:<25} {total:>10.1f} MB {savings:>10.1f} MB {percent:>9.1f}%")

    print(f"{'-'*80}")


def main():
    parser = argparse.ArgumentParser(description='Profile PiFinder memory usage')
    parser.add_argument('--config',
                       choices=['baseline', 'no-pandas', 'no-sklearn',
                               'no-scipy-ndimage', 'no-scipy-transform', 'optimized', 'all'],
                       default='all',
                       help='Configuration to profile')
    parser.add_argument('--duration', type=int, default=60,
                       help='Profiling duration in seconds')
    parser.add_argument('--output', type=str, default='memory_profile_results.json',
                       help='Output file for results')

    args = parser.parse_args()

    # Determine configs to run
    if args.config == 'all':
        configs = ['baseline', 'no-pandas', 'no-sklearn',
                  'no-scipy-ndimage', 'no-scipy-transform']
    else:
        configs = [args.config]

    # Run profiling
    results = []
    for config in configs:
        profiler = PiFinderProfiler(config, args.duration)
        result = profiler.profile()
        if result:
            results.append(result)

        # Brief pause between runs
        if config != configs[-1]:
            print("\nWaiting 10 seconds before next configuration...")
            time.sleep(10)

    # Save results
    with open(args.output, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {args.output}")

    # Compare
    compare_results(results)


if __name__ == '__main__':
    main()
