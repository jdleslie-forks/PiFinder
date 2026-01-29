"""
Nearby objects finder using scipy.spatial.KDTree.

Uses 3D Cartesian coordinates for spherical nearest-neighbor queries.
This replaces both sklearn.BallTree and the Cython SpatialIndex with
a lightweight, maintainable solution.
"""

from PiFinder.catalogs import CompositeObject
from typing import List
import time
import numpy as np
from scipy.spatial import KDTree
import logging

logger = logging.getLogger("Catalog.Nearby")
MAX_DEVIATION = 1.0
MAX_TIME = 2


def ra_dec_to_cartesian(ra_deg: np.ndarray, dec_deg: np.ndarray) -> np.ndarray:
    """
    Convert RA/Dec (degrees) to 3D unit vectors on the celestial sphere.

    This allows using Euclidean distance in 3D as a proxy for angular distance.
    For small angles: chord_distance ≈ 2 * sin(angle/2)
    """
    ra_rad = np.deg2rad(ra_deg)
    dec_rad = np.deg2rad(dec_deg)

    x = np.cos(dec_rad) * np.cos(ra_rad)
    y = np.cos(dec_rad) * np.sin(ra_rad)
    z = np.sin(dec_rad)

    return np.column_stack([x, y, z])


def angular_to_chord_distance(angle_deg: float) -> float:
    """Convert angular distance (degrees) to chord distance in 3D unit sphere."""
    return 2 * np.sin(np.deg2rad(angle_deg) / 2)


class Nearby:
    """Nearby class to calculate and display the closest objects"""

    def __init__(self, shared_state) -> None:
        self.shared_state = shared_state
        self.closest_objects_finder = ClosestObjectsFinder()
        self.last_ra = 0
        self.last_dec = 0
        self.last_refresh = 0

    def set_items(self, items: list[CompositeObject]):
        self.closest_objects_finder.calculate_objects_balltree(
            objects=items,
        )

    def should_refresh(self):
        solution = self.shared_state.solution()
        if not solution or solution["RA"] is None:
            # No solution yet (initial state before first successful solve)
            return False
        ra, dec = solution["RA"], solution["Dec"]
        # After first successful solve, RA/Dec are guaranteed to be valid
        should = (
            abs(ra - self.last_ra) > MAX_DEVIATION
            or abs(dec - self.last_dec) > MAX_DEVIATION
            or (time.time() - self.last_refresh) > MAX_TIME
        )
        logger.debug(
            "Should refresh? %s, %s, %s, %s",
            should,
            ra - self.last_ra,
            dec - self.last_dec,
            time.time() - self.last_refresh,
        )
        return should

    def refresh(self):
        solution = self.shared_state.solution()
        if not solution or solution["RA"] is None:
            # No solution yet (initial state before first successful solve)
            return []
        # After first successful solve, RA/Dec are guaranteed to be valid
        ra, dec = solution["RA"], solution["Dec"]
        self.last_ra = ra
        self.last_dec = dec
        self.last_refresh = time.time()

        self.result = self.closest_objects_finder.get_closest_objects(ra, dec)
        return self.result


class ClosestObjectsFinder:
    """
    Finds closest celestial objects using scipy.spatial.KDTree.

    Uses 3D Cartesian coordinates on the unit sphere for efficient
    nearest-neighbor queries with proper spherical geometry.
    """

    def __init__(self):
        self._kdtree = None
        self._objects = None

    def calculate_objects_balltree(self, objects: list[CompositeObject]) -> None:
        """
        Build KDTree spatial index from catalog objects.

        Converts RA/Dec to 3D unit vectors for spherical queries.
        """
        deduplicated_objects = deduplicate_objects(objects)

        if not deduplicated_objects:
            self._kdtree = None
            self._objects = None
            return

        self._objects = np.array(deduplicated_objects)

        # Convert RA/Dec to 3D Cartesian coordinates
        ra = np.array([obj.ra for obj in deduplicated_objects], dtype=np.float64)
        dec = np.array([obj.dec for obj in deduplicated_objects], dtype=np.float64)
        xyz = ra_dec_to_cartesian(ra, dec)

        # Build KDTree
        self._kdtree = KDTree(xyz)

    def get_closest_objects(self, ra: float, dec: float, n: int = 0) -> List[CompositeObject]:
        """
        Get the n closest objects to the given RA/Dec position.

        Args:
            ra: Right ascension in degrees
            dec: Declination in degrees
            n: Number of objects to return (0 = all)

        Returns:
            List of closest CompositeObjects, sorted by distance
        """
        if self._kdtree is None or self._objects is None:
            return []

        n_objects = len(self._objects)

        if n == 0:
            n = n_objects

        # Convert query point to 3D
        query_xyz = ra_dec_to_cartesian(
            np.array([ra], dtype=np.float64),
            np.array([dec], dtype=np.float64)
        )

        # Query k nearest neighbors
        k = min(n, n_objects)
        distances, indices = self._kdtree.query(query_xyz, k=k)

        # Handle single result case (query returns scalar, not array)
        if k == 1:
            indices = [indices[0]]
        else:
            indices = indices[0]

        return self._objects[indices].tolist()


def deduplicate_objects(
    unfiltered_objects: list[CompositeObject],
) -> list[CompositeObject]:
    """
    Remove duplicate objects, preferring Messier > NGC > others.
    """
    deduplicated_dict = {}

    # Define precedence for catalog codes
    precedence = {"M": 2, "NGC": 1}

    for obj in unfiltered_objects:
        if obj.object_id not in deduplicated_dict:
            deduplicated_dict[obj.object_id] = obj
        else:
            existing_obj = deduplicated_dict[obj.object_id]
            existing_precedence = precedence.get(existing_obj.catalog_code, 0)
            new_precedence = precedence.get(obj.catalog_code, 0)
            if new_precedence > existing_precedence:
                deduplicated_dict[obj.object_id] = obj

    return list(deduplicated_dict.values())
