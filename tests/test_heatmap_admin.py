# NOTE: This suite covers pure functions (grid bucketing, zoom mapping, viewport
# validation, time-window resolution, imbalance math, operating-area fallback
# parsing) and Pydantic model defaults only, per this repo's existing
# unittest-on-pure-functions convention (no live-DB mocking framework exists here).
# Full behavioral coverage of the RPC/fallback paths against real
# ride_requests/rides/driver_profiles rows (empty-area results, demand-only vs
# supply-only cells from real data, category/vehicle_type filtering against real
# rows, viewport pushdown correctness under load) requires live Supabase access,
# which this session/environment does not have. No performance/timing numbers are
# asserted anywhere in this file for the same reason.

import unittest
from datetime import timedelta
from fastapi import HTTPException

from app.services.analytics.router import (
    HeatmapCell,
    HeatmapResponse,
    _bucket,
    _lat_lng_from_point,
    _grid_size_from_zoom,
    _resolve_grid_size,
    _resolve_time_window,
    _validate_viewport,
    _compute_imbalance,
    _get_default_operating_area_bbox,
    _FALLBACK_OPERATING_AREA_BBOX,
    _DEFAULT_GRID_SIZE_DEG,
    _compute_cancellation_rate,
    _compute_demand_trend,
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


class ZoomToGridSizeTests(unittest.TestCase):
    def test_zoom_zero_maps_to_base_grid_size(self) -> None:
        self.assertEqual(_grid_size_from_zoom(0), 0.5)

    def test_higher_zoom_yields_smaller_grid(self) -> None:
        self.assertGreater(_grid_size_from_zoom(1), _grid_size_from_zoom(2))
        self.assertGreater(_grid_size_from_zoom(2), _grid_size_from_zoom(3))

    def test_zoom_clamped_to_minimum_grid_size(self) -> None:
        self.assertEqual(_grid_size_from_zoom(10), 0.001)
        self.assertEqual(_grid_size_from_zoom(20), 0.001)

    def test_negative_zoom_does_not_exceed_maximum_grid_size(self) -> None:
        self.assertLessEqual(_grid_size_from_zoom(-5), 0.5)


class ResolveGridSizeTests(unittest.TestCase):
    def test_explicit_grid_size_deg_wins_over_zoom(self) -> None:
        self.assertEqual(_resolve_grid_size(0.05, 10), 0.05)

    def test_zoom_used_when_grid_size_deg_absent(self) -> None:
        self.assertEqual(_resolve_grid_size(None, 1), _grid_size_from_zoom(1))

    def test_default_v1_grid_size_when_both_absent(self) -> None:
        self.assertEqual(_resolve_grid_size(None, None), _DEFAULT_GRID_SIZE_DEG)
        self.assertEqual(_resolve_grid_size(None, None), 0.01)


class ResolveTimeWindowTests(unittest.TestCase):
    def test_minutes_overrides_days_when_present(self) -> None:
        self.assertEqual(_resolve_time_window(7, 30), timedelta(minutes=30))

    def test_days_used_when_minutes_absent(self) -> None:
        self.assertEqual(_resolve_time_window(7, None), timedelta(days=7))

    def test_sub_day_presets(self) -> None:
        for preset_minutes in (15, 30, 60, 180, 1440, 10080, 43200):
            self.assertEqual(_resolve_time_window(7, preset_minutes), timedelta(minutes=preset_minutes))


class ViewportValidationTests(unittest.TestCase):
    def test_valid_viewport_passes(self) -> None:
        _validate_viewport(north=2, south=1, east=4, west=3)  # should not raise

    def test_north_less_than_or_equal_south_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _validate_viewport(north=1, south=2, east=4, west=3)
        self.assertEqual(ctx.exception.status_code, 400)

        with self.assertRaises(HTTPException):
            _validate_viewport(north=1, south=1, east=4, west=3)

    def test_east_less_than_or_equal_west_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _validate_viewport(north=2, south=1, east=3, west=4)
        self.assertEqual(ctx.exception.status_code, 400)

        with self.assertRaises(HTTPException):
            _validate_viewport(north=2, south=1, east=3, west=3)


class ImbalanceComputationTests(unittest.TestCase):
    def test_positive_imbalance_when_demand_exceeds_supply(self) -> None:
        self.assertEqual(_compute_imbalance(20, 5), 15)

    def test_negative_imbalance_when_supply_exceeds_demand(self) -> None:
        self.assertEqual(_compute_imbalance(2, 10), -8)

    def test_zero_imbalance_when_equal(self) -> None:
        self.assertEqual(_compute_imbalance(5, 5), 0)

    def test_imbalance_with_demand_only_cell(self) -> None:
        self.assertEqual(_compute_imbalance(7, 0), 7)

    def test_imbalance_with_supply_only_cell(self) -> None:
        self.assertEqual(_compute_imbalance(0, 4), -4)


class _FakeMaybeSingleResult:
    def __init__(self, data):
        self.data = data


class _FakeConfigQuery:
    def __init__(self, row):
        self._row = row

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        return _FakeMaybeSingleResult(self._row)


class _FakeSupabaseClient:
    """Minimal stand-in for the parts of the Supabase client that
    _get_config_value touches, so _get_default_operating_area_bbox can be
    tested without live Supabase access (none exists in this environment)."""

    def __init__(self, row):
        self._row = row

    def table(self, _name):
        return _FakeConfigQuery(self._row)


class OperatingAreaDefaultTests(unittest.TestCase):
    def test_fallback_bbox_used_when_config_value_missing(self) -> None:
        sb = _FakeSupabaseClient(row=None)
        self.assertEqual(_get_default_operating_area_bbox(sb), _FALLBACK_OPERATING_AREA_BBOX)

    def test_configured_bbox_parsed_from_json_string(self) -> None:
        sb = _FakeSupabaseClient(row={"value": '{"north": 1.0, "south": 0.0, "east": 2.0, "west": -1.0}'})
        self.assertEqual(
            _get_default_operating_area_bbox(sb),
            {"north": 1.0, "south": 0.0, "east": 2.0, "west": -1.0},
        )

    def test_malformed_config_json_falls_back_to_constant(self) -> None:
        sb = _FakeSupabaseClient(row={"value": "not-json"})
        self.assertEqual(_get_default_operating_area_bbox(sb), _FALLBACK_OPERATING_AREA_BBOX)

    def test_incomplete_config_json_falls_back_to_constant(self) -> None:
        sb = _FakeSupabaseClient(row={"value": '{"north": 1.0}'})
        self.assertEqual(_get_default_operating_area_bbox(sb), _FALLBACK_OPERATING_AREA_BBOX)


class HeatmapCellV2ModelTests(unittest.TestCase):
    def test_heatmap_cell_imbalance_defaults_to_zero(self) -> None:
        cell = HeatmapCell(grid_lat=-4.32, grid_lng=15.31)
        self.assertEqual(cell.imbalance, 0)

    def test_heatmap_response_v2_optional_fields_default_to_none(self) -> None:
        resp = HeatmapResponse()
        self.assertIsNone(resp.minutes)
        self.assertIsNone(resp.zoom)
        self.assertIsNone(resp.viewport)


class CancellationRateTests(unittest.TestCase):
    def test_normal_fraction(self) -> None:
        self.assertEqual(_compute_cancellation_rate(3, 10), 0.3)

    def test_zero_cancellations_nonzero_total_is_zero_not_none(self) -> None:
        self.assertEqual(_compute_cancellation_rate(0, 10), 0.0)

    def test_empty_denominator_returns_none(self) -> None:
        self.assertIsNone(_compute_cancellation_rate(0, 0))

    def test_nonzero_cancelled_zero_total_returns_none(self) -> None:
        # Shouldn't happen in practice (cancelled <= total always), but the
        # denominator guard must not divide by zero regardless.
        self.assertIsNone(_compute_cancellation_rate(2, 0))

    def test_negative_total_returns_none(self) -> None:
        self.assertIsNone(_compute_cancellation_rate(0, -1))


class DemandTrendTests(unittest.TestCase):
    def test_increasing_beyond_threshold(self) -> None:
        pct, label = _compute_demand_trend(15, 10)
        self.assertEqual(pct, 50.0)
        self.assertEqual(label, "increasing")

    def test_decreasing_beyond_threshold(self) -> None:
        pct, label = _compute_demand_trend(5, 10)
        self.assertEqual(pct, -50.0)
        self.assertEqual(label, "decreasing")

    def test_stable_within_band(self) -> None:
        pct, label = _compute_demand_trend(105, 100)
        self.assertEqual(pct, 5.0)
        self.assertEqual(label, "stable")

    def test_boundary_exactly_at_positive_threshold_is_increasing(self) -> None:
        pct, label = _compute_demand_trend(110, 100)
        self.assertEqual(pct, 10.0)
        self.assertEqual(label, "increasing")

    def test_boundary_exactly_at_negative_threshold_is_decreasing(self) -> None:
        pct, label = _compute_demand_trend(90, 100)
        self.assertEqual(pct, -10.0)
        self.assertEqual(label, "decreasing")

    def test_boundary_just_inside_threshold_is_stable(self) -> None:
        pct, label = _compute_demand_trend(109, 100)
        self.assertEqual(pct, 9.0)
        self.assertEqual(label, "stable")

    def test_zero_previous_period_is_undefined(self) -> None:
        self.assertEqual(_compute_demand_trend(5, 0), (None, None))

    def test_both_periods_zero_is_undefined(self) -> None:
        self.assertEqual(_compute_demand_trend(0, 0), (None, None))


class HeatmapCellV3ModelDefaultsTests(unittest.TestCase):
    def test_v3_fields_default_to_none_not_zero(self) -> None:
        cell = HeatmapCell(grid_lat=-4.32, grid_lng=15.31)
        self.assertIsNone(cell.cancellation_count)
        self.assertIsNone(cell.cancellation_rate)
        self.assertIsNone(cell.demand_trend_pct)
        self.assertIsNone(cell.demand_trend_label)


class HeatmapResponseGeographicScopeTests(unittest.TestCase):
    def test_geographic_scope_defaults_to_none(self) -> None:
        resp = HeatmapResponse()
        self.assertIsNone(resp.geographic_scope)


if __name__ == "__main__":
    unittest.main()
