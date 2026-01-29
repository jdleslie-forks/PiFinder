"""
Lazy-loading catalog system for reduced memory footprint.

Provides the same interface as Catalogs but uses SQL queries
instead of loading all ~160K objects into memory at startup.

Memory savings: ~190 MB → ~20-40 MB
"""

import logging
import datetime
import pytz
from typing import List, Dict, Optional, Set

import PiFinder.calc_utils as calc_utils
from PiFinder.state import SharedStateObj
from PiFinder.db.catalog_query import CatalogQuery
from PiFinder.db.observations_db import ObservationsDatabase
from PiFinder.composite_object import CompositeObject, MagnitudeObject
from PiFinder.config import Config

logger = logging.getLogger("LazyCatalogs")


class LazyCompositeObject(CompositeObject):
    """
    CompositeObject that lazy-loads names and other details.
    """

    _catalog_query: Optional[CatalogQuery] = None
    _names_loaded: bool = False
    _names_cache: List[str] = []

    @classmethod
    def set_catalog_query(cls, cq: CatalogQuery):
        """Set shared CatalogQuery instance for lazy loading."""
        cls._catalog_query = cq

    @classmethod
    def from_dict_lazy(cls, data: Dict, logged_ids: Set[int]) -> 'LazyCompositeObject':
        """Create instance from dict without loading names."""
        obj = cls()
        obj.object_id = data.get('object_id')
        obj.obj_type = data.get('obj_type', '')
        obj.ra = data.get('ra', 0)
        obj.dec = data.get('dec', 0)
        obj.const = data.get('const', '')
        obj.size = data.get('size', '')
        obj.surface_brightness = data.get('surface_brightness')
        obj.catalog_code = data.get('catalog_code', '')
        obj.sequence = data.get('sequence', 0)
        obj.description = data.get('description', '')
        obj.image_name = data.get('image_name')
        obj.logged = obj.object_id in logged_ids

        # Fast magnitude parsing - avoid numpy operations during list loading
        # The mag field in DB is JSON: {"mags": [...], "filter_mag": X}
        mag_data = data.get('mag')
        if mag_data:
            try:
                import json
                mag_dict = json.loads(mag_data)
                # Use pre-computed filter_mag from DB if available
                filter_mag = mag_dict.get('filter_mag', 99)
                mags = mag_dict.get('mags', [])
                # Create MagnitudeObject without recalculating
                obj.mag = MagnitudeObject.__new__(MagnitudeObject)
                obj.mag.mags = mags
                obj.mag.filter_mag = filter_mag
                # Fast mag_str - avoid numpy min/max
                floats = [float(x) for x in mags if isinstance(x, (int, float)) or (isinstance(x, str) and x.replace('.','',1).replace('-','',1).isdigit())]
                if not floats or filter_mag == 99:
                    obj.mag_str = "-"
                elif len(floats) == 1:
                    obj.mag_str = f"{floats[0]:.1f}"
                else:
                    obj.mag_str = f"{min(floats):.1f}/{max(floats):.1f}"
            except (json.JSONDecodeError, KeyError, TypeError):
                obj.mag = MagnitudeObject([])
                obj.mag_str = "-"
        else:
            obj.mag = MagnitudeObject([])
            obj.mag_str = "-"

        # Names loaded lazily
        obj._names_loaded = False
        obj._names_cache = []

        return obj

    @property
    def names(self) -> List[str]:
        """Lazy-load names on first access."""
        if not self._names_loaded and self._catalog_query and self.object_id:
            self._names_cache = self._catalog_query.get_names(self.object_id)
            self._names_loaded = True
        return self._names_cache

    @names.setter
    def names(self, value: List[str]):
        self._names_cache = value
        self._names_loaded = True


class LazyCatalogFilter:
    """
    Filter that can be converted to SQL WHERE clauses.
    """

    def __init__(
        self,
        shared_state: SharedStateObj,
        magnitude: Optional[float] = None,
        object_types: Optional[List[str]] = None,
        altitude: int = -1,
        observed: str = "Any",
        constellations: List[str] = [],
        selected_catalogs: List[str] = [],
    ):
        self.shared_state = shared_state
        self._magnitude = magnitude
        self._object_types = object_types or []
        self._altitude = altitude
        self._observed = observed
        self._constellations = constellations or []
        self._selected_catalogs = set(selected_catalogs)
        self.dirty_time = 0
        self.fast_aa = None

    def load_from_config(self, config_object: Config):
        """Load filter values from configuration object."""
        self._magnitude = config_object.get_option("filter.magnitude")
        self._object_types = config_object.get_option("filter.object_types", [])
        self._altitude = config_object.get_option("filter.altitude", -1)
        self._observed = config_object.get_option("filter.observed", "Any")
        self._constellations = config_object.get_option("filter.constellations", [])
        self._selected_catalogs = set(
            config_object.get_option("filter.selected_catalogs", [])
        )

    @property
    def magnitude(self):
        return self._magnitude

    @magnitude.setter
    def magnitude(self, value):
        self._magnitude = value
        self.dirty_time = datetime.datetime.now().timestamp()

    @property
    def object_types(self):
        return self._object_types

    @object_types.setter
    def object_types(self, value):
        self._object_types = value or []
        self.dirty_time = datetime.datetime.now().timestamp()

    @property
    def altitude(self):
        return self._altitude

    @altitude.setter
    def altitude(self, value):
        self._altitude = value
        self.dirty_time = datetime.datetime.now().timestamp()

    @property
    def observed(self):
        return self._observed

    @observed.setter
    def observed(self, value):
        self._observed = value
        self.dirty_time = datetime.datetime.now().timestamp()

    @property
    def constellations(self):
        return self._constellations

    @constellations.setter
    def constellations(self, value):
        self._constellations = value or []
        self.dirty_time = datetime.datetime.now().timestamp()

    @property
    def selected_catalogs(self):
        return self._selected_catalogs

    @selected_catalogs.setter
    def selected_catalogs(self, value):
        self._selected_catalogs = set(value) if value else set()
        self.dirty_time = datetime.datetime.now().timestamp()

    def calc_fast_aa(self, shared_state):
        """Calculate FastAltAz for altitude filtering."""
        location = shared_state.location()
        dt = shared_state.datetime()
        if shared_state.altaz_ready():
            self.fast_aa = calc_utils.FastAltAz(
                location.lat,
                location.lon,
                dt,
            )

    def is_dirty(self) -> bool:
        """Check if filter has changed since last application."""
        return self.dirty_time > getattr(self, 'last_filtered_time', 0)

    def apply_altitude_filter(self, objects: List[LazyCompositeObject]) -> List[LazyCompositeObject]:
        """Apply altitude filter using vectorized calculation."""
        if self._altitude == -1 or not self.fast_aa or not objects:
            return objects

        # Extract RA/Dec arrays for batch processing
        ra_list = []
        dec_list = []
        valid_indices = []

        for i, obj in enumerate(objects):
            try:
                ra_list.append(float(obj.ra))
                dec_list.append(float(obj.dec))
                valid_indices.append(i)
            except (TypeError, ValueError):
                continue

        if not ra_list:
            return []

        # Vectorized altitude calculation
        altitudes = self.fast_aa.radec_to_alt_batch(ra_list, dec_list)

        # Filter by altitude threshold
        result = []
        for idx, alt in zip(valid_indices, altitudes):
            if alt >= self._altitude:
                result.append(objects[idx])

        return result

    def apply(self, objects: List[LazyCompositeObject]) -> List[LazyCompositeObject]:
        """Apply all filters to objects (for compatibility with CatalogFilter)."""
        self.calc_fast_aa(self.shared_state)
        self.last_filtered_time = datetime.datetime.now().timestamp()
        return self.apply_altitude_filter(objects)


class LazyCatalogWrapper:
    """
    Lightweight wrapper for catalog metadata.
    Provides catalog_code and get_status() for compatibility with object_list.py
    """
    def __init__(self, catalog_code: str):
        self.catalog_code = catalog_code

    def get_status(self):
        """Return READY status for static catalogs."""
        from PiFinder.catalog_base import CatalogState, CatalogStatus
        return CatalogStatus(current=CatalogState.READY, previous=CatalogState.READY, data=None)


class LazyCatalogs:
    """
    Lazy-loading replacement for Catalogs.

    Uses SQL queries instead of loading all objects into memory.
    Provides the same interface as Catalogs for drop-in replacement.
    """

    def __init__(self, shared_state: SharedStateObj):
        self.shared_state = shared_state
        self._catalog_query = CatalogQuery()
        self._catalog_query.ensure_cell_id_column()

        # Set up lazy loading for CompositeObjects
        LazyCompositeObject.set_catalog_query(self._catalog_query)

        # Load logged object IDs for filtering
        self._logged_ids: Set[int] = set()
        self._load_logged_ids()

        # Catalog metadata (small - just names and counts)
        self._catalog_codes = self._catalog_query.get_catalog_codes()

        # Filter
        self.catalog_filter = LazyCatalogFilter(shared_state)

        # Cache for current query results
        self._cached_objects: List[LazyCompositeObject] = []
        self._cache_valid = False

        # Virtual catalogs (planets, comets) stored in memory
        self._virtual_catalogs: Dict[str, 'VirtualCatalog'] = {}

        logger.info(f"LazyCatalogs initialized with {len(self._catalog_codes)} catalogs")

    def _load_logged_ids(self):
        """Load set of logged object IDs."""
        try:
            obs_db = ObservationsDatabase()
            # Get all observed catalog/sequence pairs and convert to object IDs
            observed_entries = obs_db.get_observed_objects()
            for entry in observed_entries:
                # Look up object_id from catalog_code + sequence
                obj = self._catalog_query.get_object_by_catalog_sequence(
                    entry['catalog'], entry['sequence']
                )
                if obj:
                    self._logged_ids.add(obj['object_id'])
        except Exception as e:
            logger.warning(f"Could not load logged IDs: {e}")

        self._catalog_query.set_logged_ids(self._logged_ids)

    def set_catalog_filter(self, catalog_filter: LazyCatalogFilter):
        """Set the catalog filter."""
        self.catalog_filter = catalog_filter
        self._cache_valid = False

    def filter_catalogs(self):
        """Mark cache as invalid - next query will refresh."""
        self._cache_valid = False

    def get_catalogs(self, only_selected: bool = True):
        """
        Get list of catalogs.

        Returns catalog wrapper objects for virtual catalogs (planets, comets),
        and LazyCatalogWrapper for regular catalogs.
        """
        result = []
        codes_to_return = []

        if only_selected:
            codes_to_return = [c for c in self._catalog_codes
                             if c in self.catalog_filter.selected_catalogs]
        else:
            codes_to_return = self._catalog_codes

        for code in codes_to_return:
            if code in self._virtual_catalogs:
                # Return actual virtual catalog object (has get_status() etc)
                result.append(self._virtual_catalogs[code])
            else:
                # Return lightweight wrapper for database catalogs
                result.append(LazyCatalogWrapper(code))

        return result

    def get_codes(self, only_selected: bool = True) -> List[str]:
        """Get list of catalog codes (strings only)."""
        if only_selected:
            return [c for c in self._catalog_codes
                    if c in self.catalog_filter.selected_catalogs]
        return self._catalog_codes

    def has_code(self, catalog_code: str, only_selected: bool = True) -> bool:
        """Check if catalog code exists."""
        codes = self.get_codes(only_selected)
        return catalog_code in codes

    def get_objects(
        self,
        only_selected: bool = True,
        filtered: bool = True,
        limit: int = 500,
        offset: int = 0,
        nearby_position: Optional[tuple] = None,
    ) -> List[LazyCompositeObject]:
        """
        Get objects matching current filter.

        Unlike the eager-loading version, this queries the database
        and returns a limited number of results.

        Args:
            only_selected: Only return objects from selected catalogs
            filtered: Apply magnitude/type/constellation/altitude filters
            limit: Maximum number of objects to return
            offset: Offset for pagination (ignored for nearby queries)
            nearby_position: (ra, dec) tuple for spatial nearest-neighbor query
        """
        import time
        t_start = time.time()

        # Get selected catalog codes
        if only_selected:
            catalog_codes = list(self.catalog_filter.selected_catalogs)
            if not catalog_codes:
                return []
        else:
            catalog_codes = None  # All catalogs except WDS

        # Build SQL query parameters
        max_magnitude = self.catalog_filter.magnitude if filtered else None

        t_prep = time.time()

        # Use spatial query for nearby mode
        if nearby_position is not None:
            ra, dec = nearby_position
            results = self._catalog_query.get_nearby_objects(
                ra=ra,
                dec=dec,
                radius_deg=30.0,  # Search 30 degree radius
                catalog_codes=catalog_codes,
                max_magnitude=max_magnitude,
                limit=limit,
            )
        else:
            # Normal filtered query
            obj_types = self.catalog_filter.object_types if filtered else None
            constellations = self.catalog_filter.constellations if filtered else None

            # Determine if we need logged-only filter
            logged_only = (
                filtered and
                self.catalog_filter.observed == "Yes"
            )

            results = self._catalog_query.get_objects_filtered(
                catalog_codes=catalog_codes,
                obj_types=obj_types if obj_types else None,
                max_magnitude=max_magnitude,
                constellations=constellations if constellations else None,
                logged_only=logged_only,
                limit=limit,
                offset=offset,
            )

        t_query = time.time()

        # Convert to LazyCompositeObjects
        objects = [
            LazyCompositeObject.from_dict_lazy(row, self._logged_ids)
            for row in results
        ]

        t_convert = time.time()
        logger.info(f"get_objects timing: prep={t_prep-t_start:.3f}s, query={t_query-t_prep:.3f}s, convert={t_convert-t_query:.3f}s ({len(results)} rows)")

        # Apply altitude filter (can't be done in SQL)
        if filtered and self.catalog_filter.altitude != -1:
            self.catalog_filter.calc_fast_aa(self.shared_state)
            objects = self.catalog_filter.apply_altitude_filter(objects)

        # Apply "not observed" filter
        if filtered and self.catalog_filter.observed == "No":
            objects = [obj for obj in objects if not obj.logged]

        # Add virtual catalog objects (planets, comets)
        for vc in self._virtual_catalogs.values():
            if not only_selected or vc.catalog_code in self.catalog_filter.selected_catalogs:
                objects.extend(vc.get_objects())

        self._cached_objects = objects
        self._cache_valid = True

        return objects

    def get_object(self, catalog_code: str, sequence: int) -> Optional[LazyCompositeObject]:
        """Get single object by catalog code and sequence."""
        # Check virtual catalogs first
        if catalog_code in self._virtual_catalogs:
            vc = self._virtual_catalogs[catalog_code]
            return vc.get_object_by_sequence(sequence)

        # Query database
        result = self._catalog_query.get_object_by_catalog_sequence(
            catalog_code, sequence
        )
        if result:
            return LazyCompositeObject.from_dict_lazy(result, self._logged_ids)
        return None

    def search_by_text(self, search_text: str) -> List[LazyCompositeObject]:
        """Search objects by name or catalog designation."""
        if not search_text:
            return []

        results = []

        # Try catalog+sequence search (e.g., "M31", "NGC 7000")
        cat_results = self._catalog_query.search_by_catalog_sequence(search_text)
        for row in cat_results:
            results.append(LazyCompositeObject.from_dict_lazy(row, self._logged_ids))

        # Name search
        name_results = self._catalog_query.search_by_name(search_text, limit=50)
        for row in name_results:
            # Avoid duplicates
            if not any(r.object_id == row['object_id'] for r in results):
                results.append(LazyCompositeObject.from_dict_lazy(row, self._logged_ids))

        return results

    def add_virtual_catalog(self, catalog: 'VirtualCatalog'):
        """Add a virtual catalog (planets, comets)."""
        self._virtual_catalogs[catalog.catalog_code] = catalog
        if catalog.catalog_code not in self._catalog_codes:
            self._catalog_codes.append(catalog.catalog_code)

    def remove_virtual_catalog(self, catalog_code: str):
        """Remove a virtual catalog."""
        if catalog_code in self._virtual_catalogs:
            del self._virtual_catalogs[catalog_code]

    def select_catalogs(self, catalog_codes: List[str]):
        """Select catalogs by code."""
        for code in catalog_codes:
            self.catalog_filter.selected_catalogs.add(code)

    def select_no_catalogs(self):
        """Deselect all catalogs."""
        self.catalog_filter.selected_catalogs = set()

    def select_all_catalogs(self):
        """Select all catalogs."""
        self.catalog_filter.selected_catalogs = set(self._catalog_codes)

    def count(self) -> int:
        """Count selected catalogs."""
        return len(self.get_catalogs(only_selected=True))

    def is_loading(self) -> bool:
        """
        Check if background catalog loading is still in progress.

        For LazyCatalogs, always returns False since objects are loaded
        on-demand via SQL queries rather than background loading.

        Returns:
            False (lazy loading doesn't use background threads)
        """
        return False

    def get_catalog_by_code(self, catalog_code: str):
        """
        Get catalog by code.

        Returns a lightweight catalog info dict instead of full Catalog object.
        """
        if catalog_code in self._virtual_catalogs:
            return self._virtual_catalogs[catalog_code]
        # Return a simple dict with catalog info
        return {'catalog_code': catalog_code}

    def close(self):
        """Close database connection."""
        self._catalog_query.close()


class VirtualCatalog:
    """Base class for virtual catalogs (planets, comets) that are computed, not from DB."""

    def __init__(self, catalog_code: str, desc: str):
        self.catalog_code = catalog_code
        self.desc = desc
        self._objects: List[CompositeObject] = []
        self.initialized = False

    def get_objects(self) -> List[CompositeObject]:
        return self._objects

    def get_object_by_sequence(self, sequence: int) -> Optional[CompositeObject]:
        for obj in self._objects:
            if obj.sequence == sequence:
                return obj
        return None

    def add_object(self, obj: CompositeObject):
        self._objects.append(obj)

    def _get_objects(self) -> List[CompositeObject]:
        """Internal access for updates."""
        return self._objects


def create_lazy_catalogs(shared_state: SharedStateObj) -> LazyCatalogs:
    """
    Factory function to create LazyCatalogs with virtual catalogs.

    This replaces CatalogBuilder.build() for lazy loading mode.
    """
    catalogs = LazyCatalogs(shared_state)

    # Try to add virtual catalogs (planets, comets) if dependencies available
    try:
        from PiFinder.catalogs import PlanetCatalog
        from PiFinder.comet_catalog import CometCatalog

        # Add planet catalog
        planet_catalog = PlanetCatalog(
            dt=datetime.datetime.now().replace(tzinfo=pytz.timezone("UTC")),
            shared_state=shared_state,
        )
        catalogs.add_virtual_catalog(planet_catalog)

        # Add comet catalog
        comet_catalog = CometCatalog(
            dt=datetime.datetime.now().replace(tzinfo=pytz.timezone("UTC")),
            shared_state=shared_state,
        )
        catalogs.add_virtual_catalog(comet_catalog)

        logger.debug("Virtual catalogs (planets, comets) loaded")
    except ImportError as e:
        logger.warning(f"Virtual catalogs unavailable: {e}")

    return catalogs
