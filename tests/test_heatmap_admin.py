import unittest

from app.services.analytics.router import (
    HeatmapCell,
    HeatmapResponse,
    _bucket,
    _lat_lng_from_point,
)
from app.core.supabase import rpc_missing


class HeatmapBucketingTests(unittest.TestCase):
    def test_bucket_rounds_to_nearest_grid_cell(self) -> None:
        self.assertEqual(_bucket(-4.324, 0.01), -4.32)
        self.assertEqual(_bucket(-4.327, 0.01), -4.33)
        self.assertEqual(_bucket(15.313, 0.01), 15.31)

    def test_bucket_is_stable_across_repeated_calls_for_same_cell(self) -> None:
        a = (_bucket(-4.321, 0.01), _bucket(15.312, 0.01))
        b = (_bucket(-4.324, 0.01), _bucket(15.314, 0.01))
        self.assertEqual(a, b)

    def test_bucket_respects_custom_grid_size(self) -> None:
        self.assertEqual(_bucket(-4.36, 0.1), -4.4)
        self.assertEqual(_bucket(-4.34, 0.1), -4.3)


class LatLngFromPointTests(unittest.TestCase):
    def test_accepts_latitude_longitude_keys(self) -> None:
        self.assertEqual(_lat_lng_from_point({"latitude": -4.3, "longitude": 15.3}), (-4.3, 15.3))

    def test_accepts_lat_lng_keys(self) -> None:
        self.assertEqual(_lat_lng_from_point({"lat": -4.3, "lng": 15.3}), (-4.3, 15.3))

    def test_zero_latitude_near_equator_is_not_treated_as_missing(self) -> None:
        self.assertEqual(_lat_lng_from_point({"latitude": 0.0, "longitude": 15.3}), (0.0, 15.3))

    def test_returns_none_for_missing_or_unparseable_coordinates(self) -> None:
        self.assertIsNone(_lat_lng_from_point({"name": "Gombe"}))
        self.assertIsNone(_lat_lng_from_point(None))
        self.assertIsNone(_lat_lng_from_point("not-a-dict"))
        self.assertIsNone(_lat_lng_from_point({"latitude": "not-a-number", "longitude": 15.3}))

    def test_prefers_latitude_over_lat_when_both_present(self) -> None:
        self.assertEqual(
            _lat_lng_from_point({"latitude": -4.3, "lat": -99.0, "longitude": 15.3, "lng": 99.0}),
            (-4.3, 15.3),
        )


class HeatmapModelTests(unittest.TestCase):
    def test_heatmap_cell_defaults_to_zero_counts(self) -> None:
        cell = HeatmapCell(grid_lat=-4.32, grid_lng=15.31)
        self.assertEqual(cell.demand_count, 0)
        self.assertEqual(cell.supply_count, 0)

    def test_heatmap_response_defaults_are_safe_for_partial_data(self) -> None:
        resp = HeatmapResponse()
        self.assertEqual(resp.cells, [])
        self.assertEqual(resp.demand_points_skipped, 0)


class HeatmapRpcMissingTests(unittest.TestCase):
    def test_rpc_missing_detects_postgrest_function_not_found_for_heatmap(self) -> None:
        self.assertTrue(rpc_missing(Exception(
            '{"code":"PGRST202","message":"Could not find the function public.get_admin_heatmap"}'
        )))
        self.assertFalse(rpc_missing(Exception("connection refused")))


if __name__ == "__main__":
    unittest.main()
