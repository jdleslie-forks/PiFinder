"""
SQL-based catalog query layer for lazy loading.

Replaces eager loading of all 160K+ objects with on-demand queries.
Reduces catalog memory from ~190 MB to ~20-40 MB.
"""

import logging
from typing import List, Dict, Optional, Tuple, Set
from collections import defaultdict

import PiFinder.utils as utils
from PiFinder.db.db import Database

logger = logging.getLogger("CatalogQuery")

# Spatial index constants (must match spatial_index.pyx)
CELL_SIZE = 10.0  # degrees
RA_CELLS = 36     # 360 / 10
DEC_CELLS = 18    # 180 / 10


def ra_dec_to_cell_id(ra: float, dec: float) -> int:
    """Convert RA/Dec to cell ID for spatial indexing."""
    cell_ra = int(ra / CELL_SIZE)
    cell_dec = int((dec + 90.0) / CELL_SIZE)
    # Clamp to valid range
    if cell_ra >= RA_CELLS:
        cell_ra = RA_CELLS - 1
    if cell_dec >= DEC_CELLS:
        cell_dec = DEC_CELLS - 1
    if cell_dec < 0:
        cell_dec = 0
    return cell_ra * DEC_CELLS + cell_dec


def get_neighbor_cells(ra: float, dec: float, radius_deg: float = 15.0) -> List[int]:
    """Get all cell IDs that could contain objects within radius_deg."""
    center_ra = int(ra / CELL_SIZE)
    center_dec = int((dec + 90.0) / CELL_SIZE)

    # How many cells to search in each direction
    cell_radius = int(radius_deg / CELL_SIZE) + 1

    cells = set()

    # Handle polar regions specially
    if center_dec <= cell_radius or center_dec >= (DEC_CELLS - 1 - cell_radius):
        cell_radius_ra = RA_CELLS // 2  # Half the sky in RA
    else:
        cell_radius_ra = cell_radius

    for dra in range(-cell_radius_ra, cell_radius_ra + 1):
        for ddec in range(-cell_radius, cell_radius + 1):
            nra = (center_ra + dra) % RA_CELLS  # Wrap RA
            ndec = center_dec + ddec

            if 0 <= ndec < DEC_CELLS:
                cell_id = nra * DEC_CELLS + ndec
                cells.add(cell_id)

    return list(cells)


class CatalogQuery(Database):
    """
    SQL-based catalog access with filtering and pagination.

    Provides lazy loading of catalog objects, replacing the eager
    loading approach that consumed ~190 MB of memory.
    """

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = utils.pifinder_db
        conn, cursor = self.get_database(db_path)
        super().__init__(conn, cursor, db_path)
        self._ensure_indexes()
        self._ensure_analyzed()
        self._logged_ids: Optional[Set[int]] = None

    def _ensure_indexes(self):
        """Create indexes for efficient queries if they don't exist."""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_objects_cell_id ON objects(cell_id)",
            "CREATE INDEX IF NOT EXISTS idx_objects_ra_dec ON objects(ra, dec)",
            "CREATE INDEX IF NOT EXISTS idx_catalog_objects_code ON catalog_objects(catalog_code)",
            "CREATE INDEX IF NOT EXISTS idx_catalog_objects_code_seq ON catalog_objects(catalog_code, sequence)",
            "CREATE INDEX IF NOT EXISTS idx_catalog_objects_object_id ON catalog_objects(object_id)",
            "CREATE INDEX IF NOT EXISTS idx_names_object_id ON names(object_id)",
            "CREATE INDEX IF NOT EXISTS idx_names_common_name ON names(common_name COLLATE NOCASE)",
            # Critical for LEFT JOIN performance - without this, queries take 450ms+ instead of <1ms
            "CREATE INDEX IF NOT EXISTS idx_object_images_object_id ON object_images(object_id)",
        ]
        for idx_sql in indexes:
            try:
                self.cursor.execute(idx_sql)
            except Exception as e:
                # cell_id column might not exist yet
                if "cell_id" in idx_sql:
                    logger.debug("cell_id index skipped - column may not exist yet")
                else:
                    logger.warning(f"Failed to create index: {e}")
        self.conn.commit()

    def _ensure_analyzed(self):
        """Run ANALYZE if not recently done to optimize query planning."""
        # Check if sqlite_stat1 exists and has data (indicates ANALYZE was run)
        try:
            self.cursor.execute(
                "SELECT 1 FROM sqlite_stat1 WHERE tbl='objects' LIMIT 1"
            )
            if self.cursor.fetchone():
                return  # Already analyzed
        except Exception:
            pass  # Table doesn't exist, need to analyze

        logger.info("Running ANALYZE for query optimization...")
        try:
            self.cursor.execute("ANALYZE")
            self.conn.commit()
            logger.info("ANALYZE complete")
        except Exception as e:
            logger.warning(f"ANALYZE failed: {e}")

    def ensure_cell_id_column(self) -> bool:
        """
        Add cell_id column to objects table if it doesn't exist.
        Returns True if column was added or already exists.
        """
        # Check if column exists
        self.cursor.execute("PRAGMA table_info(objects)")
        columns = [row[1] for row in self.cursor.fetchall()]

        if 'cell_id' in columns:
            logger.debug("cell_id column already exists")
            return True

        logger.info("Adding cell_id column to objects table...")
        try:
            self.cursor.execute("ALTER TABLE objects ADD COLUMN cell_id INTEGER")
            self.conn.commit()

            # Populate cell_id for all objects
            self._populate_cell_ids()

            # Create index
            self.cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_objects_cell_id ON objects(cell_id)"
            )
            self.conn.commit()

            logger.info("cell_id column added and populated")
            return True
        except Exception as e:
            logger.error(f"Failed to add cell_id column: {e}")
            return False

    def _populate_cell_ids(self):
        """Populate cell_id for all objects."""
        self.cursor.execute("SELECT id, ra, dec FROM objects WHERE cell_id IS NULL")
        rows = self.cursor.fetchall()

        if not rows:
            return

        logger.info(f"Populating cell_id for {len(rows)} objects...")

        for obj_id, ra, dec in rows:
            if ra is not None and dec is not None:
                cell_id = ra_dec_to_cell_id(float(ra), float(dec))
                self.cursor.execute(
                    "UPDATE objects SET cell_id = ? WHERE id = ?",
                    (cell_id, obj_id)
                )

        self.conn.commit()
        logger.info("cell_id population complete")

    def set_logged_ids(self, logged_ids: Set[int]):
        """Set the set of logged object IDs for filtering."""
        self._logged_ids = logged_ids

    def get_catalog_codes(self) -> List[str]:
        """Get list of available catalog codes."""
        self.cursor.execute("SELECT catalog_code FROM catalogs ORDER BY catalog_code")
        return [row[0] for row in self.cursor.fetchall()]

    def get_objects_filtered(
        self,
        catalog_codes: Optional[List[str]] = None,
        obj_types: Optional[List[str]] = None,
        max_magnitude: Optional[float] = None,
        min_magnitude: Optional[float] = None,
        constellations: Optional[List[str]] = None,
        logged_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict]:
        """
        Query catalog objects with SQL-level filtering.

        Returns lightweight dicts suitable for list display.
        """
        # Build query
        sql = """
            SELECT
                o.id as object_id,
                o.obj_type,
                o.ra,
                o.dec,
                o.const,
                o.size,
                o.mag,
                o.surface_brightness,
                co.catalog_code,
                co.sequence,
                co.description,
                oi.image_name
            FROM catalog_objects co
            JOIN objects o ON co.object_id = o.id
            LEFT JOIN object_images oi ON o.id = oi.object_id
            WHERE 1=1
        """
        params = []

        # Exclude WDS by default (matches current behavior)
        if catalog_codes:
            placeholders = ",".join("?" * len(catalog_codes))
            sql += f" AND co.catalog_code IN ({placeholders})"
            params.extend(catalog_codes)
        else:
            sql += " AND co.catalog_code != 'WDS'"

        if obj_types:
            placeholders = ",".join("?" * len(obj_types))
            sql += f" AND o.obj_type IN ({placeholders})"
            params.extend(obj_types)

        if max_magnitude is not None:
            # mag is stored as JSON with filter_mag field
            sql += " AND json_extract(o.mag, '$.filter_mag') <= ?"
            params.append(max_magnitude)

        if min_magnitude is not None:
            sql += " AND json_extract(o.mag, '$.filter_mag') >= ?"
            params.append(min_magnitude)

        if constellations:
            placeholders = ",".join("?" * len(constellations))
            sql += f" AND o.const IN ({placeholders})"
            params.extend(constellations)

        if logged_only and self._logged_ids:
            if self._logged_ids:
                placeholders = ",".join("?" * len(self._logged_ids))
                sql += f" AND o.id IN ({placeholders})"
                params.extend(self._logged_ids)
            else:
                # No logged objects, return empty
                return []

        sql += " ORDER BY co.catalog_code, co.sequence"
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        self.cursor.execute(sql, params)

        results = []
        for row in self.cursor.fetchall():
            results.append({
                'object_id': row[0],
                'obj_type': row[1],
                'ra': row[2],
                'dec': row[3],
                'const': row[4],
                'size': row[5],
                'mag': row[6],
                'surface_brightness': row[7],
                'catalog_code': row[8],
                'sequence': row[9],
                'description': row[10],
                'image_name': row[11],
            })

        return results

    def get_nearby_objects(
        self,
        ra: float,
        dec: float,
        radius_deg: float = 15.0,
        catalog_codes: Optional[List[str]] = None,
        max_magnitude: Optional[float] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """
        Get objects near a position using spatial index.

        Returns objects sorted by distance from the query point.
        """
        # Get cells to search
        cells = get_neighbor_cells(ra, dec, radius_deg)

        if not cells:
            return []

        # Build query
        placeholders = ",".join("?" * len(cells))
        sql = f"""
            SELECT
                o.id as object_id,
                o.obj_type,
                o.ra,
                o.dec,
                o.const,
                o.size,
                o.mag,
                o.surface_brightness,
                co.catalog_code,
                co.sequence,
                co.description,
                oi.image_name
            FROM objects o
            JOIN catalog_objects co ON o.id = co.object_id
            LEFT JOIN object_images oi ON o.id = oi.object_id
            WHERE o.cell_id IN ({placeholders})
        """
        params = list(cells)

        # Exclude WDS by default
        if catalog_codes:
            code_placeholders = ",".join("?" * len(catalog_codes))
            sql += f" AND co.catalog_code IN ({code_placeholders})"
            params.extend(catalog_codes)
        else:
            sql += " AND co.catalog_code != 'WDS'"

        if max_magnitude is not None:
            sql += " AND json_extract(o.mag, '$.filter_mag') <= ?"
            params.append(max_magnitude)

        self.cursor.execute(sql, params)

        # Calculate actual distances and filter
        import math
        results = []

        for row in self.cursor.fetchall():
            obj_ra = float(row[2]) if row[2] else 0
            obj_dec = float(row[3]) if row[3] else 0

            # Calculate angular distance
            dist = self._angular_distance(ra, dec, obj_ra, obj_dec)

            if dist <= radius_deg:
                results.append({
                    'object_id': row[0],
                    'obj_type': row[1],
                    'ra': obj_ra,
                    'dec': obj_dec,
                    'const': row[4],
                    'size': row[5],
                    'mag': row[6],
                    'surface_brightness': row[7],
                    'catalog_code': row[8],
                    'sequence': row[9],
                    'description': row[10],
                    'image_name': row[11],
                    'distance': dist,
                })

        # Sort by distance and limit
        results.sort(key=lambda x: x['distance'])
        return results[:limit]

    def _angular_distance(self, ra1: float, dec1: float, ra2: float, dec2: float) -> float:
        """Calculate angular distance in degrees using spherical law of cosines."""
        import math

        ra1_rad = math.radians(ra1)
        dec1_rad = math.radians(dec1)
        ra2_rad = math.radians(ra2)
        dec2_rad = math.radians(dec2)

        cos_dist = (
            math.sin(dec1_rad) * math.sin(dec2_rad) +
            math.cos(dec1_rad) * math.cos(dec2_rad) * math.cos(ra2_rad - ra1_rad)
        )

        # Clamp to [-1, 1]
        cos_dist = max(-1.0, min(1.0, cos_dist))

        return math.degrees(math.acos(cos_dist))

    def get_object_by_id(self, object_id: int) -> Optional[Dict]:
        """Fetch single object with full details for detail view."""
        sql = """
            SELECT
                o.id as object_id,
                o.obj_type,
                o.ra,
                o.dec,
                o.const,
                o.size,
                o.mag,
                o.surface_brightness,
                co.catalog_code,
                co.sequence,
                co.description,
                oi.image_name
            FROM objects o
            JOIN catalog_objects co ON o.id = co.object_id
            LEFT JOIN object_images oi ON o.id = oi.object_id
            WHERE o.id = ?
            LIMIT 1
        """
        self.cursor.execute(sql, (object_id,))
        row = self.cursor.fetchone()

        if not row:
            return None

        return {
            'object_id': row[0],
            'obj_type': row[1],
            'ra': row[2],
            'dec': row[3],
            'const': row[4],
            'size': row[5],
            'mag': row[6],
            'surface_brightness': row[7],
            'catalog_code': row[8],
            'sequence': row[9],
            'description': row[10],
            'image_name': row[11],
        }

    def get_object_by_catalog_sequence(
        self,
        catalog_code: str,
        sequence: int
    ) -> Optional[Dict]:
        """Fetch object by catalog code and sequence number."""
        sql = """
            SELECT
                o.id as object_id,
                o.obj_type,
                o.ra,
                o.dec,
                o.const,
                o.size,
                o.mag,
                o.surface_brightness,
                co.catalog_code,
                co.sequence,
                co.description,
                oi.image_name
            FROM catalog_objects co
            JOIN objects o ON co.object_id = o.id
            LEFT JOIN object_images oi ON o.id = oi.object_id
            WHERE co.catalog_code = ? AND co.sequence = ?
            LIMIT 1
        """
        self.cursor.execute(sql, (catalog_code, sequence))
        row = self.cursor.fetchone()

        if not row:
            return None

        return {
            'object_id': row[0],
            'obj_type': row[1],
            'ra': row[2],
            'dec': row[3],
            'const': row[4],
            'size': row[5],
            'mag': row[6],
            'surface_brightness': row[7],
            'catalog_code': row[8],
            'sequence': row[9],
            'description': row[10],
            'image_name': row[11],
        }

    def get_names(self, object_id: int) -> List[str]:
        """Lazy-load names for single object."""
        self.cursor.execute(
            "SELECT common_name FROM names WHERE object_id = ?",
            (object_id,)
        )
        return list(set(row[0].strip() for row in self.cursor.fetchall()))

    def search_by_name(
        self,
        search_term: str,
        catalog_codes: Optional[List[str]] = None,
        limit: int = 50
    ) -> List[Dict]:
        """Search objects by common name."""
        sql = """
            SELECT DISTINCT
                o.id as object_id,
                o.obj_type,
                o.ra,
                o.dec,
                o.const,
                o.size,
                o.mag,
                o.surface_brightness,
                co.catalog_code,
                co.sequence,
                co.description,
                oi.image_name,
                n.common_name
            FROM names n
            JOIN objects o ON n.object_id = o.id
            JOIN catalog_objects co ON o.id = co.object_id
            LEFT JOIN object_images oi ON o.id = oi.object_id
            WHERE n.common_name LIKE ?
        """
        params = [f"%{search_term}%"]

        if catalog_codes:
            placeholders = ",".join("?" * len(catalog_codes))
            sql += f" AND co.catalog_code IN ({placeholders})"
            params.extend(catalog_codes)
        else:
            sql += " AND co.catalog_code != 'WDS'"

        sql += " ORDER BY n.common_name LIMIT ?"
        params.append(limit)

        self.cursor.execute(sql, params)

        results = []
        for row in self.cursor.fetchall():
            results.append({
                'object_id': row[0],
                'obj_type': row[1],
                'ra': row[2],
                'dec': row[3],
                'const': row[4],
                'size': row[5],
                'mag': row[6],
                'surface_brightness': row[7],
                'catalog_code': row[8],
                'sequence': row[9],
                'description': row[10],
                'image_name': row[11],
                'matched_name': row[12],
            })

        return results

    def search_by_catalog_sequence(
        self,
        search_term: str,
        limit: int = 50
    ) -> List[Dict]:
        """
        Search by catalog code and sequence (e.g., 'M31', 'NGC 7000').
        """
        # Parse search term
        search_term = search_term.strip().upper()

        # Try to extract catalog code and sequence
        import re
        match = re.match(r'^([A-Z]+)\s*(\d+)$', search_term)

        if not match:
            return []

        catalog_code = match.group(1)
        sequence = int(match.group(2))

        result = self.get_object_by_catalog_sequence(catalog_code, sequence)
        return [result] if result else []

    def count_filtered(
        self,
        catalog_codes: Optional[List[str]] = None,
        obj_types: Optional[List[str]] = None,
        max_magnitude: Optional[float] = None,
        logged_only: bool = False,
    ) -> int:
        """Get count of objects matching filters (for pagination UI)."""
        sql = """
            SELECT COUNT(DISTINCT o.id)
            FROM catalog_objects co
            JOIN objects o ON co.object_id = o.id
            WHERE 1=1
        """
        params = []

        if catalog_codes:
            placeholders = ",".join("?" * len(catalog_codes))
            sql += f" AND co.catalog_code IN ({placeholders})"
            params.extend(catalog_codes)
        else:
            sql += " AND co.catalog_code != 'WDS'"

        if obj_types:
            placeholders = ",".join("?" * len(obj_types))
            sql += f" AND o.obj_type IN ({placeholders})"
            params.extend(obj_types)

        if max_magnitude is not None:
            sql += " AND json_extract(o.mag, '$.filter_mag') <= ?"
            params.append(max_magnitude)

        if logged_only and self._logged_ids:
            placeholders = ",".join("?" * len(self._logged_ids))
            sql += f" AND o.id IN ({placeholders})"
            params.extend(self._logged_ids)

        self.cursor.execute(sql, params)
        return self.cursor.fetchone()[0]

    def close(self):
        """Close database connection."""
        self.conn.close()
