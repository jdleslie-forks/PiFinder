"""Tests for lazy catalog loading system."""

import pytest
import tracemalloc


class MockLocation:
    lat = 40.0
    lon = -75.0


class MockSharedState:
    def location(self):
        return MockLocation()

    def datetime(self):
        import datetime
        import pytz
        return datetime.datetime.now(pytz.UTC)

    def altaz_ready(self):
        return True

    def solution(self):
        return {'RA': 180.0, 'Dec': 45.0}


class TestCatalogQuery:
    """Tests for CatalogQuery SQL layer."""

    @pytest.fixture
    def catalog_query(self):
        from PiFinder.db.catalog_query import CatalogQuery
        cq = CatalogQuery()
        cq.ensure_cell_id_column()
        return cq

    def test_get_catalog_codes(self, catalog_query):
        """Test getting available catalog codes."""
        codes = catalog_query.get_catalog_codes()
        assert len(codes) > 0
        assert 'M' in codes
        assert 'NGC' in codes

    def test_filtered_query_basic(self, catalog_query):
        """Test basic filtered query."""
        results = catalog_query.get_objects_filtered(
            catalog_codes=['M'],
            limit=10
        )
        assert len(results) == 10
        assert all(r['catalog_code'] == 'M' for r in results)

    def test_filtered_query_magnitude(self, catalog_query):
        """Test magnitude filter uses JSON extraction."""
        results = catalog_query.get_objects_filtered(
            catalog_codes=['M'],
            max_magnitude=6.0,
            limit=50
        )
        import json
        for r in results:
            if r['mag']:
                mag_data = json.loads(r['mag'])
                assert mag_data.get('filter_mag', 99) <= 6.0

    def test_filtered_query_obj_type(self, catalog_query):
        """Test object type filter."""
        results = catalog_query.get_objects_filtered(
            catalog_codes=['M', 'NGC'],
            obj_types=['Gx'],
            limit=20
        )
        assert all(r['obj_type'] == 'Gx' for r in results)

    def test_nearby_query(self, catalog_query):
        """Test spatial query."""
        # Query near M31 (RA=10.68, Dec=41.27)
        results = catalog_query.get_nearby_objects(
            ra=10.68, dec=41.27,
            radius_deg=2.0,
            limit=10
        )
        assert len(results) > 0
        # Results should be sorted by distance
        if len(results) > 1:
            assert results[0]['distance'] <= results[1]['distance']

    def test_search_by_name(self, catalog_query):
        """Test name search."""
        results = catalog_query.search_by_name('andromeda', limit=10)
        assert len(results) > 0
        # Should find M31/NGC224
        names_found = [r.get('matched_name', '').lower() for r in results]
        assert any('andromeda' in n for n in names_found)

    def test_get_object_by_catalog_sequence(self, catalog_query):
        """Test getting specific object."""
        result = catalog_query.get_object_by_catalog_sequence('M', 31)
        assert result is not None
        assert result['catalog_code'] == 'M'
        assert result['sequence'] == 31

    def test_get_names(self, catalog_query):
        """Test lazy name loading."""
        # Get M31's object_id first
        obj = catalog_query.get_object_by_catalog_sequence('M', 31)
        names = catalog_query.get_names(obj['object_id'])
        assert len(names) > 0
        assert any('andromeda' in n.lower() for n in names)


class TestLazyCatalogs:
    """Tests for LazyCatalogs class."""

    @pytest.fixture
    def lazy_catalogs(self):
        from PiFinder.catalogs_lazy import LazyCatalogs
        return LazyCatalogs(MockSharedState())

    def test_initialization(self, lazy_catalogs):
        """Test LazyCatalogs initializes correctly."""
        codes = lazy_catalogs.get_codes(only_selected=False)
        assert len(codes) > 0

    def test_filter_and_query(self, lazy_catalogs):
        """Test filtering and querying objects."""
        lazy_catalogs.catalog_filter.selected_catalogs = {'M'}
        lazy_catalogs.catalog_filter.magnitude = 8.0

        objects = lazy_catalogs.get_objects(
            only_selected=True,
            filtered=True,
            limit=20
        )
        assert len(objects) > 0
        assert all(obj.catalog_code == 'M' for obj in objects)

    def test_get_object(self, lazy_catalogs):
        """Test getting specific object."""
        m31 = lazy_catalogs.get_object('M', 31)
        assert m31 is not None
        assert m31.catalog_code == 'M'
        assert m31.sequence == 31

    def test_lazy_name_loading(self, lazy_catalogs):
        """Test that names are loaded lazily."""
        m31 = lazy_catalogs.get_object('M', 31)
        # Names should load on access
        names = m31.names
        assert len(names) > 0

    def test_search(self, lazy_catalogs):
        """Test text search."""
        results = lazy_catalogs.search_by_text('M31')
        assert len(results) > 0

    def test_select_catalogs(self, lazy_catalogs):
        """Test catalog selection."""
        lazy_catalogs.select_no_catalogs()
        assert len(lazy_catalogs.catalog_filter.selected_catalogs) == 0

        lazy_catalogs.select_catalogs(['M', 'NGC'])
        assert 'M' in lazy_catalogs.catalog_filter.selected_catalogs
        assert 'NGC' in lazy_catalogs.catalog_filter.selected_catalogs


class TestMemorySavings:
    """Test that lazy loading actually saves memory."""

    def test_lazy_vs_eager_memory(self):
        """Compare memory usage: lazy vs eager loading."""
        # Eager loading memory test
        tracemalloc.start()
        from PiFinder.db.objects_db import ObjectsDatabase
        db = ObjectsDatabase()
        catalog_objects = [dict(row) for row in db.get_catalog_objects()]
        objects = {row['id']: dict(row) for row in db.get_objects()}
        names = db.get_object_id_to_names()
        eager_current, eager_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Clean up
        del catalog_objects, objects, names, db

        # Lazy loading memory test
        tracemalloc.start()
        from PiFinder.db.catalog_query import CatalogQuery
        cq = CatalogQuery()
        codes = cq.get_catalog_codes()
        results = cq.get_objects_filtered(catalog_codes=['M', 'NGC'], limit=100)
        lazy_current, lazy_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Lazy should use significantly less memory
        assert lazy_peak < eager_peak * 0.5  # At least 50% savings
        print(f"\nMemory comparison:")
        print(f"  Eager: {eager_peak/1e6:.1f} MB peak")
        print(f"  Lazy:  {lazy_peak/1e6:.1f} MB peak")
        print(f"  Savings: {(eager_peak - lazy_peak)/1e6:.1f} MB ({100*(1-lazy_peak/eager_peak):.0f}%)")
