# Memory Profiling Tools

Tools for measuring PiFinder memory usage across different configurations.

## Files

| File | Purpose |
|------|---------|
| `profile_memory.py` | Measures RSS across all PiFinder processes |
| `measure_library_memory.py` | Measures individual library import costs |
| `run_memory_benchmark.sh` | Orchestrates multi-configuration benchmarks |

## Quick Start

Run complete benchmark:
```bash
./run_memory_benchmark.sh
```

Profile single configuration:
```bash
python3 profile_memory.py --config baseline --duration 60
python3 profile_memory.py --config optimized --duration 60
```

Measure individual library costs:
```bash
python3 measure_library_memory.py
```

## How It Works

**Import Patching:** Uses `sys.meta_path` to block imports without code changes:
```python
class PandasBlocker:
    def find_module(self, fullname, path=None):
        if fullname.startswith('pandas'):
            raise ImportError("pandas blocked by profiler")
        return None
```

**Process Monitoring:**
1. Starts PiFinder with `--camera debug --keyboard local`
2. Samples RSS every 1 second for 60 seconds
3. Records maximum RSS per process
4. Calculates total and per-library savings

**Metric:** RSS (Resident Set Size) via `psutil.Process.memory_info().rss`

## Configurations

```bash
python3 profile_memory.py --config baseline        # All libraries
python3 profile_memory.py --config no-pandas       # Block pandas
python3 profile_memory.py --config no-sklearn      # Block sklearn
python3 profile_memory.py --config no-scipy-ndimage
python3 profile_memory.py --config no-scipy-transform
python3 profile_memory.py --config optimized       # This branch
```

## Troubleshooting

- **PiFinder won't start:** Check `pip list | grep -E "pandas|sklearn|scipy|psutil"`
- **Results vary:** x86 workstation differs from Pi (ARM). Focus on relative savings.
- **Import patch fails:** Check `sitecustomize.py` in python/ directory

See `OPTIMIZATION_RESULTS.md` for measured results.
