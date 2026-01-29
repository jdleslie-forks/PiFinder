# SQLite Index Documentation


## Index Overview

All indexes are created automatically on first database access via `CatalogQuery._ensure_indexes()` in `python/PiFinder/db/catalog_query.py`.

### Objects Table

| Index | Column(s) | Purpose |
|-------|-----------|---------|
| `idx_objects_cell_id` | `cell_id` | Spatial queries using 5° sky cells for NEAREST mode |
| `idx_objects_ra_dec` | `ra, dec` | Coordinate-based filtering and range queries |

### Catalog Objects Table

| Index | Column(s) | Purpose |
|-------|-----------|---------|
| `idx_catalog_objects_code` | `catalog_code` | Single-catalog lookups |
| `idx_catalog_objects_code_seq` | `catalog_code, sequence` | Catalog browsing in sequence order |
| `idx_catalog_objects_object_id` | `object_id` | Object-to-catalog joins |

### Names Table

| Index | Column(s) | Purpose |
|-------|-----------|---------|
| `idx_names_object_id` | `object_id` | Name lookups by object ID |
| `idx_names_common_name` | `common_name COLLATE NOCASE` | Case-insensitive name search |

### Object Images Table

| Index | Column(s) | Purpose |
|-------|-----------|---------|
| `idx_object_images_object_id` | `object_id` | JOIN performance for image lookups |

## Performance Impact

### Essential: object_images Index

The `idx_object_images_object_id` index was the most impactful optimization. Without it, every catalog query performed a full table scan on 148K rows due to the LEFT JOIN for image data.

| Metric | Before | After |
|--------|--------|-------|
| Single query | 450ms+ | <1ms |
| Catalog page load (Pi Zero 2W) | 40+ seconds | 24-40ms |

### Query Performance (Pi Zero 2W)

| Operation | Time |
|-----------|------|
| Single catalog, filtered | 50-150ms |
| Multi/All-catalog, filtered | 100-300ms |
| Nearby spatial query | 50-200ms |
| Name search | 20-100ms |

## ANALYZE Optimization

On first database access, `CatalogQuery._ensure_analyzed()` runs SQLite's `ANALYZE` command to populate query planner statistics (`sqlite_stat1` table). This helps SQLite choose optimal execution paths for complex queries.

## Spatial Indexing: Cell ID System

The `cell_id` column divides the sky into 5° cells for efficient spatial queries:

- Cell calculation: `cell_id = floor(ra/5) * 100 + floor((dec+90)/5)`
- NEAREST mode queries neighboring cells based on search radius
- Handles RA wraparound (0°/360°) and polar regions

