# PiFinder Memory Optimization Results

## Summary

Memory optimizations enabling PiFinder on Pi Zero 2W (512MB RAM).

**Results:**
- ~150MB saved via lazy catalog loading
- ~114MB saved via dependency elimination (measured across 4 processes)
- Startup: 2-3 seconds (down from 5-8 seconds)
- Database removed from git (38MB), built during setup

---

## 1. Lazy Catalog Loading

**Problem:** Eager loading pre-loaded ~150,000 objects (~190MB) into RAM at startup.

**Solution:** `LazyCatalogs` class queries database on-demand with 100-object pagination.

**File:** `python/PiFinder/catalogs_lazy.py`

```python
def get_objects(
    self,
    only_selected: bool = True,
    filtered: bool = True,
    limit: int = 100,
    offset: int = 0,
    nearby_position: Optional[tuple] = None,
) -> List[LazyCompositeObject]:
    """Query database for objects matching current filter."""
```

**Performance:**
- First page: ~80-200ms
- Subsequent pages: ~50-100ms
- Memory saved: ~150-170MB

---

## 2. Spatial Query Optimization (NEAREST Mode)

**Problem:** NEAREST mode required loading all objects into KDTree.

**Solution:** Cell-based spatial queries using existing `cell_id` index (5° sky cells).

**File:** `python/PiFinder/db/catalog_query.py`

```python
def get_nearby_objects(
    self,
    ra: float,
    dec: float,
    radius_deg: float = 15.0,
    limit: int = 100,
) -> List[Dict]:
    """Get objects near a position using spatial index."""
    cells = get_neighbor_cells(ra, dec, radius_deg)
    # Query objects in those cells, calculate distances, sort
```

**Performance:**
- Query time: 50-200ms for 100 nearest objects
- Handles RA wraparound and polar regions
- Works with multiple selected catalogs

---

## 3. Dependency Elimination

**Measured Library Memory Footprint:**

| Library | Memory | Replacement |
|---------|--------|-------------|
| pandas | 53.7 MB | sqlite3 (0.9 MB) |
| sklearn.BallTree | 135.8 MB | scipy.KDTree (already loaded) |
| scipy.Rotation | 50.2 MB | pyquaternion (15.2 MB) |

### Changes Made

**pandas → SQLite** (`plot.py`, `scripts/build_star_database.py`)
- Star data parsed at build time, stored in SQLite
- Savings: 52.8 MB

**sklearn.BallTree → scipy.KDTree** (`nearby.py`)
- Convert RA/Dec to 3D Cartesian for Euclidean metric
- scipy.KDTree already loaded by tetra3
- Savings: 85.8 MB (sklearn eliminated entirely)

**scipy.Rotation → pyquaternion** (`imu_pi.py`)
- Lightweight pure-Python quaternion library
- Savings: 35.0 MB

### Multi-Process Impact

| Process | Before | After | Saved |
|---------|--------|-------|-------|
| Main (UI) | 135.9 MB | 56.7 MB | 79.2 MB |
| Solver | 51.8 MB | 51.6 MB | 0.2 MB |
| IMU | 50.1 MB | 15.0 MB | 35.1 MB |
| Server | 30.6 MB | 30.7 MB | -0.1 MB |
| **Total** | **268.4 MB** | **154.0 MB** | **114.4 MB** |

---

## 4. Database Build-Time Generation

**Change:** Removed 38MB `pifinder_objects.db` from git. Built during setup.

**Setup script additions:**
```bash
python3 scripts/build_star_database.py
python3 -m PiFinder.catalog_imports.main
```

**Trade-offs:**
- Smaller repository
- Requires internet during setup
- Longer setup time

---

## 5. Bug Fixes

**Virtual Catalog Import Fix**
```python
# Before (broken):
from PiFinder.catalogs import CometCatalog
# After (fixed):
from PiFinder.comet_catalog import CometCatalog
```

**NEAREST Mode No-Solve Crash**
```python
solution = self.shared_state.solution()
if solution is None or solution.get("RA") is None:
    self.message(_("No Solve Yet"), 1)
    self.current_sort = SortOrder.CATALOG_SEQUENCE
```

---

## 6. API Changes

**Before:**
```python
from PiFinder.catalogs import CatalogBuilder, CatalogFilter
catalogs = CatalogBuilder().build(shared_state)
filter = CatalogFilter(shared_state=shared_state)
catalogs.set_catalog_filter(filter)
```

**After:**
```python
from PiFinder.catalogs_lazy import create_lazy_catalogs
catalogs = create_lazy_catalogs(shared_state)
catalogs.catalog_filter.load_from_config(cfg)
```

---

## 7. Database Schema (Existing, Leveraged)

```sql
CREATE TABLE objects (
    id INTEGER PRIMARY KEY,
    ra REAL, dec REAL,
    const TEXT, obj_type TEXT,
    mag TEXT, size TEXT,
    surface_brightness REAL,
    cell_id INTEGER  -- 5° sky cells for spatial queries
);

CREATE INDEX idx_objects_cell_id ON objects(cell_id);

CREATE TABLE catalog_objects (
    object_id INTEGER,
    catalog_code TEXT,
    sequence INTEGER
);

CREATE INDEX idx_catalog_objects_code_seq ON catalog_objects(catalog_code, sequence);
```

---

## 8. Performance Characteristics

**Query Performance:**
- Single catalog, filtered: 50-150ms
- Multi-catalog, filtered: 100-300ms
- Nearby spatial query: 50-200ms
- Name search: 20-100ms

**Memory (Pi Zero 2W):**
- Total PiFinder RSS: ~120-160MB
- Stable over 3+ hour sessions
- No memory leaks observed

---

## 9. Feature Status

| Feature | Status |
|---------|--------|
| Browse catalogs | Working (paginated) |
| Filter objects | Working |
| Search by name | Working |
| Sort by Catalog/RA | Working |
| Sort by Nearest | Working |
| Planets catalog | Working |
| Comets catalog | Working |
| Plate solving | Working |
| Web interface | Working |
| Observation logging | Working |

---

## 10. Known Limitations

- **Sort by RA across catalogs:** Requires loading all objects
- **Comet download:** Can block startup if network fails
- **Constellation filter:** No index, slower than other filters

---

## 11. Key Files

| File | Purpose |
|------|---------|
| `python/PiFinder/catalogs_lazy.py` | Lazy catalog loading |
| `python/PiFinder/db/catalog_query.py` | Spatial queries |
| `python/scripts/build_star_database.py` | Build-time star DB |
| `pifinder_setup_zero2w.sh` | Pi Zero 2W setup |
| `pi_config_files/60-ioschedulers.rules` | BFQ I/O scheduler |
| `measure_library_memory.py` | Memory profiling |

---

## 12. Hardware Tested

- Raspberry Pi Zero 2W (512MB) - Production ready
- Raspberry Pi 4 (4GB) - Compatible
