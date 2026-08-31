from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional, Any
from app.core.dependencies import require_admin
from app.core.supabase import (
    get_supabase, count_of, call_rpc, first_row, rpc_missing,
    IN_FILTER_CHUNK as _IN_FILTER_CHUNK,
)
from app.services.config.router import _get_config_value
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import logging

router = APIRouter(prefix="/analytics", tags=["analytics"])

logger = logging.getLogger(__name__)


class DashboardMetricsResponse(BaseModel):
    pending_payments_count: int = 0
    pending_drivers_count: int = 0
    active_drivers_count: int = 0
    active_rides_count: int = 0
    active_sos_count: int = 0
    stale_requests_count: int = 0


class DriverOfferUpdateRate(BaseModel):
    driver_id: str
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    total_offers: int = 0
    updated_offers: int = 0
    update_rate: float = 0.0


class OfferUpdateMetricsResponse(BaseModel):
    avg_updates_per_completed_trip: float = 0.0
    high_update_rate_drivers: List[DriverOfferUpdateRate] = []


class DriverLocationItem(BaseModel):
    driver_id: str
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    latitude: float
    longitude: float
    is_online: bool = False
    updated_at: Optional[str] = None
    heading: Optional[float] = None


class DriverLocationsResponse(BaseModel):
    drivers: List[DriverLocationItem] = []


@router.get("/admin/dashboard", response_model=DashboardMetricsResponse)
@router.get("/admin/overview", response_model=DashboardMetricsResponse, include_in_schema=False)
def get_dashboard_metrics(_user=Depends(require_admin)):
    try:
        sb = get_supabase()
        metrics = DashboardMetricsResponse()

        # Each tile below is an independent count. They use head=True so PostgREST
        # returns only the count in the Content-Range header instead of shipping every
        # matching row back for len() to count — that made the endpoint slow down in
        # proportion to table size. They are also run concurrently, since the cost here
        # is round-trip latency to Supabase, not local work.

        def pending_payments() -> int:
            return count_of(
                sb.table("wallet_topup_requests")
                .select("id", count="exact", head=True)
                .eq("status", "pending")
            )

        def pending_drivers() -> int:
            return count_of(
                sb.table("driver_profiles")
                .select("id", count="exact", head=True)
                .eq("verification_status", "pending")
            )

        def active_drivers() -> int:
            return count_of(
                sb.table("driver_profiles")
                .select("id", count="exact", head=True)
                .eq("verification_status", "approved")
                .eq("is_online", True)
            )

        def active_rides() -> int:
            return count_of(
                sb.table("rides")
                .select("id", count="exact", head=True)
                .in_("status", ["in_progress", "driver_en_route", "arrived"])
            )

        def active_sos() -> int:
            return count_of(
                sb.table("sos_sessions")
                .select("id", count="exact", head=True)
                .eq("is_active", True)
            )

        def stale_requests() -> int:
            """Pending requests older than the alert threshold that have drawn no bids."""
            stale_threshold_minutes = 10  # default
            try:
                config_result = (
                    sb.table("app_config")
                    .select("value")
                    .eq("key", "stale_request_alert_threshold_minutes")
                    .maybe_single()
                    .execute()
                )
                if config_result.data and config_result.data.get("value"):
                    stale_threshold_minutes = int(config_result.data["value"])
            except Exception:
                pass

            cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_threshold_minutes)

            pending = (
                sb.table("ride_requests")
                .select("id")
                .eq("status", "pending")
                .lt("created_at", cutoff.isoformat())
                .execute()
            )
            pending_ids = [r["id"] for r in (pending.data or []) if r.get("id")]
            if not pending_ids:
                return 0

            # One bulk lookup of "which of these got a bid" instead of a query per
            # request. Chunked so the ?in=(...) filter cannot overflow the URL length.
            bid_on: set = set()
            for i in range(0, len(pending_ids), _IN_FILTER_CHUNK):
                chunk = pending_ids[i : i + _IN_FILTER_CHUNK]
                responses = (
                    sb.table("driver_responses")
                    .select("ride_request_id")
                    .in_("ride_request_id", chunk)
                    .execute()
                )
                bid_on.update(
                    r["ride_request_id"]
                    for r in (responses.data or [])
                    if r.get("ride_request_id")
                )

            return sum(1 for rid in pending_ids if rid not in bid_on)

        tiles = {
            "pending_payments_count": pending_payments,
            "pending_drivers_count": pending_drivers,
            "active_drivers_count": active_drivers,
            "active_rides_count": active_rides,
            "active_sos_count": active_sos,
            "stale_requests_count": stale_requests,
        }

        with ThreadPoolExecutor(max_workers=len(tiles)) as pool:
            futures = {field: pool.submit(fn) for field, fn in tiles.items()}
            for field, future in futures.items():
                try:
                    setattr(metrics, field, future.result())
                except Exception:
                    # A single unavailable tile must not blank the whole dashboard;
                    # it keeps the response model's default.
                    logger.exception("dashboard metric %s failed", field)

        return metrics

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/offer-update-metrics", response_model=OfferUpdateMetricsResponse)
def get_offer_update_metrics(_user=Depends(require_admin)):
    """
    Calculate offer update metrics:
    - Average number of offer updates per completed trip
    - Drivers with high offer update rate (> 60%)
    """
    try:
        sb = get_supabase()
        result = OfferUpdateMetricsResponse()

        # Get all completed rides
        try:
            completed_rides = (
                sb.table("rides")
                .select("id, driver_id")
                .eq("status", "completed")
                .execute()
            )
        except Exception:
            completed_rides = type("obj", (object,), {"data": []})()

        total_rides = len(completed_rides.data or [])
        if total_rides == 0:
            return result

        # For each completed ride, count how many driver_responses exist for its ride_request
        total_updates = 0
        driver_update_map = {}  # driver_id -> {"total_offers": 0, "updated_offers": 0, "name": "", "phone": ""}

        # A driver's offer list does not vary between their own rides, so fetch it once
        # per driver rather than once per ride. Previously a driver with N completed
        # rides triggered N identical queries; the accumulation below is unchanged.
        offers_by_driver: dict = {}

        def offers_for(driver_id) -> list:
            if driver_id not in offers_by_driver:
                try:
                    offers = (
                        sb.table("driver_responses")
                        .select("id, created_at, status")
                        .eq("driver_id", driver_id)
                        .execute()
                    )
                    offers_by_driver[driver_id] = list(offers.data or [])
                except Exception:
                    offers_by_driver[driver_id] = []
            return offers_by_driver[driver_id]

        for ride in completed_rides.data or []:
            driver_id = ride.get("driver_id")
            if driver_id and driver_id not in driver_update_map:
                driver_update_map[driver_id] = {
                    "total_offers": 0,
                    "updated_offers": 0,
                    "full_name": None,
                    "phone_number": None,
                }

            driver_offers = offers_for(driver_id)
            if driver_id in driver_update_map:
                driver_update_map[driver_id]["total_offers"] += len(driver_offers)

            # Count updates (more than 1 offer = updated)
            if len(driver_offers) > 1:
                updates = len(driver_offers) - 1
                total_updates += updates
                if driver_id in driver_update_map:
                    driver_update_map[driver_id]["updated_offers"] += updates

        # Calculate averages
        result.avg_updates_per_completed_trip = round(total_updates / total_rides, 2) if total_rides > 0 else 0.0

        # Identify high-rate drivers first, then batch-fetch their names in one
        # .in_() lookup instead of one query per driver inside the loop.
        high_rate_ids = []
        for driver_id, data in driver_update_map.items():
            update_rate = data["updated_offers"] / data["total_offers"] if data["total_offers"] > 0 else 0.0
            data["_update_rate"] = update_rate
            if update_rate > 0.6:  # > 60% update rate
                high_rate_ids.append(driver_id)

        driver_names: dict = {}
        for i in range(0, len(high_rate_ids), _IN_FILTER_CHUNK):
            chunk = high_rate_ids[i : i + _IN_FILTER_CHUNK]
            try:
                driver_rows = (
                    sb.table("driver_profiles")
                    .select("id, users(full_name, phone_number)")
                    .in_("id", chunk)
                    .execute()
                )
                for row in driver_rows.data or []:
                    user_info = row.get("users") or {}
                    driver_names[row["id"]] = {
                        "full_name": user_info.get("full_name"),
                        "phone_number": user_info.get("phone_number"),
                    }
            except Exception:
                pass

        high_rate_drivers = []
        for driver_id in high_rate_ids:
            data = driver_update_map[driver_id]
            name_info = driver_names.get(driver_id, {})
            high_rate_drivers.append(DriverOfferUpdateRate(
                driver_id=driver_id,
                full_name=name_info.get("full_name"),
                phone_number=name_info.get("phone_number"),
                total_offers=data["total_offers"],
                updated_offers=data["updated_offers"],
                update_rate=round(data["_update_rate"], 2),
            ))

        result.high_update_rate_drivers = high_rate_drivers
        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/driver-locations", response_model=DriverLocationsResponse)
def get_driver_locations(
    online_only: bool = True,
    stale_minutes: int = 5,
    _user=Depends(require_admin),
):
    """
    Get current locations of active drivers.
    Reads latitude/longitude stored directly on driver_profiles.
    Falls back to the driver_locations table if available.

    - online_only (default True): only return drivers marked as online
    - stale_minutes: unused for driver_profiles source, kept for API compatibility
    """
    try:
        sb = get_supabase()

        # Primary: query driver_profiles (has lat/lng + is_online directly)
        try:
            dp_query = (
                sb.table("driver_profiles")
                .select("id, user_id, latitude, longitude, is_online, updated_at, users(full_name, phone_number)")
                .not_.is_("latitude", "null")
                .not_.is_("longitude", "null")
            )
            if online_only:
                dp_query = dp_query.eq("is_online", True)
            dp_result = dp_query.execute()
            rows = dp_result.data or []
        except Exception:
            return DriverLocationsResponse(drivers=[])

        drivers = []
        for row in rows:
            user_info = row.pop("users", {}) or {}
            try:
                drivers.append(DriverLocationItem(
                    driver_id=row["id"],
                    full_name=user_info.get("full_name"),
                    phone_number=user_info.get("phone_number"),
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                    is_online=bool(row.get("is_online", False)),
                    updated_at=row.get("updated_at"),
                ))
            except Exception:
                continue

        return DriverLocationsResponse(drivers=drivers)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


_HEATMAP_ROW_CAP = 50_000  # safety cap for the client-side fallback, same idiom as customer_wallet._METRICS_ROW_CAP

# V2 geographic localization constants (Heatmap V2 -- viewport/zoom/operating-area).
_ZOOM_BASE_GRID_DEG = 0.5   # grid cell size in degrees at zoom 0 (whole-world view)
_GRID_SIZE_MIN = 0.001      # matches the existing Query(ge=0.001) bound
_GRID_SIZE_MAX = 0.5        # matches the existing Query(le=0.5) bound
_DEFAULT_GRID_SIZE_DEG = 0.01  # V1's literal default, used when grid_size_deg and zoom are both omitted

_FALLBACK_OPERATING_AREA_BBOX = {
    # Failsafe only, used if app_config has no default_operating_area_bbox row.
    # Approximate Kinshasa-metro bounds, confirmed with the user as a starting
    # estimate (2026-08-26) -- tighten via a direct UPDATE on that app_config row
    # as real coverage data becomes available. Not a precise administrative boundary,
    # and never referenced by name (DRC/Kinshasa) anywhere else in this endpoint's
    # logic -- only this seeded value is DRC-specific, so the mechanism generalizes
    # to any future city/country by updating the config row.
    "north": -4.20, "south": -4.50, "east": 15.40, "west": 15.15,
}


def _bucket(value: float, grid_size: float) -> float:
    return round(round(value / grid_size) * grid_size, 6)


def _lat_lng_from_point(value: Any) -> Optional[tuple]:
    if not isinstance(value, dict):
        return None
    lat = value.get("latitude")
    if lat is None:
        lat = value.get("lat")
    lng = value.get("longitude")
    if lng is None:
        lng = value.get("lng")
    if lat is None or lng is None:
        return None
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def _grid_size_from_zoom(zoom: int) -> float:
    """Slippy-map-style doubling: each +1 zoom level halves the cell size.
    Clamped to the existing [_GRID_SIZE_MIN, _GRID_SIZE_MAX] bounds so cell
    counts can't explode at high zoom."""
    raw = _ZOOM_BASE_GRID_DEG / (2 ** max(zoom, 0))
    return round(min(max(raw, _GRID_SIZE_MIN), _GRID_SIZE_MAX), 6)


def _resolve_grid_size(grid_size_deg: Optional[float], zoom: Optional[int]) -> float:
    """Explicit grid_size_deg always wins over zoom. If both are omitted, fall
    back to the literal V1 default (0.01) -- byte-identical to V1 behavior."""
    if grid_size_deg is not None:
        return grid_size_deg
    if zoom is not None:
        return _grid_size_from_zoom(zoom)
    return _DEFAULT_GRID_SIZE_DEG


def _resolve_time_window(days: int, minutes: Optional[int]) -> timedelta:
    """minutes always wins over days when present -- covers all of the sub-day
    presets (15/30/60/180/1440/10080/43200 minutes) with one flexible param."""
    if minutes is not None:
        return timedelta(minutes=minutes)
    return timedelta(days=days)


def _validate_viewport(north: float, south: float, east: float, west: float) -> None:
    if north <= south:
        raise HTTPException(status_code=400, detail="north must be greater than south")
    if east <= west:
        raise HTTPException(status_code=400, detail="east must be greater than west")


def _compute_imbalance(demand_count: int, supply_count: int) -> int:
    return demand_count - supply_count


_TREND_STABLE_THRESHOLD_PCT = 10.0  # |pct| < 10% => "stable"; >= +10% => "increasing"; <= -10% => "decreasing"


def _compute_cancellation_rate(cancelled: int, total: int) -> Optional[float]:
    """cancelled/total as a fraction, rounded to 4dp. None on empty denominator
    (total <= 0) -- an empty cell has no rate, not a fabricated 0% rate."""
    if total <= 0:
        return None
    return round(cancelled / total, 4)


def _compute_demand_trend(current: int, previous: int) -> tuple:
    """Pct change previous->current. previous <= 0 is undefined -> (None, None),
    never a fabricated infinite/zero value. Threshold-based label."""
    if previous <= 0:
        return None, None
    pct = round(((current - previous) / previous) * 100, 2)
    if pct >= _TREND_STABLE_THRESHOLD_PCT:
        label = "increasing"
    elif pct <= -_TREND_STABLE_THRESHOLD_PCT:
        label = "decreasing"
    else:
        label = "stable"
    return pct, label


def _get_default_operating_area_bbox(sb) -> dict:
    raw = _get_config_value(sb, "default_operating_area_bbox", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if all(k in parsed for k in ("north", "south", "east", "west")):
                return {k: float(parsed[k]) for k in ("north", "south", "east", "west")}
        except (ValueError, TypeError, KeyError):
            pass
    return dict(_FALLBACK_OPERATING_AREA_BBOX)


def _bbox_from_service_area_row(row: dict) -> dict:
    """Shape a public.service_areas row (north/south/east/west columns) into the
    same bbox dict shape used by explicit viewport params and
    _get_default_operating_area_bbox. Pure/no I/O so it's directly unit-testable;
    the row's own CHECK (north > south AND east > west) constraint means a stored
    row can never violate the viewport invariant, so no extra validation is
    needed here."""
    return {k: float(row[k]) for k in ("north", "south", "east", "west")}


def _resolve_service_area_bbox(sb, service_area_id: str) -> dict:
    """Look up a public.service_areas row by id and return its bbox. Raises 404
    if no such area exists -- a lookup-by-id-not-found case, distinct from the
    400 _validate_viewport raises for malformed explicit viewport bounds.
    Does NOT filter by is_active: a previously-valid service_area_id should
    still resolve even if the area was later deactivated (deactivation controls
    whether an area is *offered* in GET /config/admin/service-areas, not
    whether a known id can still be queried here)."""
    result = (
        sb.table("service_areas")
        .select("north, south, east, west")
        .eq("id", service_area_id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail=f"service_area_id '{service_area_id}' not found")
    return _bbox_from_service_area_row(result.data)


class HeatmapCell(BaseModel):
    grid_lat: float
    grid_lng: float
    demand_count: int = 0
    supply_count: int = 0
    imbalance: int = 0  # demand_count - supply_count, computed server-side from the two authoritative counts
    # V3 operational-intelligence fields. All Optional/None by default -- never a
    # fabricated 0. cancellation_count/cancellation_rate are only populated when
    # source="rides" (ride_requests has no cancelled_at); demand_trend_pct/label
    # are populated for either source when a previous-period comparison exists.
    cancellation_count: Optional[int] = None
    cancellation_rate: Optional[float] = None
    demand_trend_pct: Optional[float] = None
    demand_trend_label: Optional[str] = None


def _build_heatmap_cells_from_rpc(raw_cells: list, demand_table: str) -> "List[HeatmapCell]":
    """Map get_admin_heatmap's raw jsonb cells into HeatmapCell objects,
    applying V3's derived fields. cancellation_count/cancellation_rate are only
    set when demand_table == "rides" (the RPC never populates them otherwise --
    see sql/20260828_heatmap_v3_operational_intelligence.sql's NULL-vs-0 note).
    A cell with source="rides" and genuinely zero cancellations comes back from
    the RPC as SQL NULL (no grouped row for that cell) -- `or 0` here is what
    turns "no row" into the correct "0 cancellations", but only on this branch,
    so a source="requests" cell's cancellation_count stays None (not 0)."""
    cells = []
    for c in raw_cells:
        demand_count = c.get("demand_count", 0)
        supply_count = c.get("supply_count", 0)
        prev_demand_count = c.get("prev_demand_count", 0)
        trend_pct, trend_label = _compute_demand_trend(demand_count, prev_demand_count)

        cancellation_count: Optional[int] = None
        cancellation_rate: Optional[float] = None
        if demand_table == "rides":
            cancellation_count = c.get("cancellation_count") or 0
            cancellation_rate = _compute_cancellation_rate(cancellation_count, demand_count)

        cells.append(HeatmapCell(
            grid_lat=c.get("grid_lat"),
            grid_lng=c.get("grid_lng"),
            demand_count=demand_count,
            supply_count=supply_count,
            imbalance=_compute_imbalance(demand_count, supply_count),
            cancellation_count=cancellation_count,
            cancellation_rate=cancellation_rate,
            demand_trend_pct=trend_pct,
            demand_trend_label=trend_label,
        ))
    return cells


class HeatmapResponse(BaseModel):
    generated_at: str = ""
    grid_size_deg: float = 0.01
    source: str = "requests"
    days: int = 7
    demand_points_included: int = 0
    demand_points_skipped: int = 0
    supply_points_included: int = 0
    cells: List[HeatmapCell] = []
    minutes: Optional[int] = None    # echo of the effective sub-day window, if used
    zoom: Optional[int] = None       # echo of the requested zoom level, if used
    viewport: Optional[dict] = None  # echo of the bbox actually applied (explicit or default-area), or None
    geographic_scope: Optional[str] = None  # "service_area" / "default_area" / "custom_viewport" / "unscoped"


@router.get("/admin/heatmap", response_model=HeatmapResponse)
def get_heatmap(
    days: int = Query(7, description="Days of ride_requests/rides history for demand counts (used only if `minutes` is omitted)"),
    minutes: Optional[int] = Query(None, ge=1, le=43200, description="Sub-day/overriding time window in minutes (e.g. 15/30/60/180/1440/10080/43200). Overrides `days` when present."),
    source: str = Query("requests", description="Demand source: 'requests' (ride_requests) or 'rides' (historical)"),
    category: Optional[str] = Query(None, description="Filter: standard/premium/lady_driver"),
    vehicle_type: Optional[str] = Query(None, description="Filter: car/moto/tuk_tuk/van/suv. Only applies to source='rides' and supply (driver_profiles) -- ride_requests has no vehicle_type column, so this filter is a no-op when source='requests'."),
    grid_size_deg: Optional[float] = Query(None, ge=0.001, le=0.5, description="Explicit grid cell size in degrees. Overrides zoom-derived size when given. Defaults to 0.01 (~1.1km) if grid_size_deg and zoom are both omitted."),
    zoom: Optional[int] = Query(None, ge=0, le=20, description="Map zoom level, mapped server-side to grid_size_deg when grid_size_deg is not explicitly given."),
    north: Optional[float] = Query(None, ge=-90, le=90, description="Viewport bbox north latitude bound. Must be given together with south/east/west."),
    south: Optional[float] = Query(None, ge=-90, le=90, description="Viewport bbox south latitude bound."),
    east: Optional[float] = Query(None, ge=-180, le=180, description="Viewport bbox east longitude bound."),
    west: Optional[float] = Query(None, ge=-180, le=180, description="Viewport bbox west longitude bound."),
    use_default_area: bool = Query(False, description="When true and no explicit viewport or service_area_id is given, apply the configured default operating-area bbox (app_config key default_operating_area_bbox)."),
    service_area_id: Optional[str] = Query(None, description="Look up a configured service_areas row (see GET /config/admin/service-areas) by id and use its bbox as the viewport. Takes precedence over use_default_area, but explicit north/south/east/west still wins."),
    _user=Depends(require_admin),
):
    """
    Grid-bucketed demand (ride_requests/rides picking_point) vs supply
    (driver_profiles lat/lng) counts, for an ops hotspot/heatmap view.

    Prefers the get_admin_heatmap Postgres RPC (grid-bucketing done
    server-side, see sql/20260825_heatmap_rpc.sql and
    sql/20260826_heatmap_v2_viewport_zoom.sql) so cell counts are correct
    regardless of table size. Falls back to a capped client-side bucketing
    pass (see _HEATMAP_ROW_CAP) only if those migrations haven't been applied
    yet -- that fallback path can silently under-report once row volume
    exceeds the cap, and cannot push viewport filtering into the demand-side
    query (picking_point is opaque JSON, not a queryable column via PostgREST)
    so it filters after per-row coordinate extraction instead.

    V2 additions (all optional, default to reproducing V1 behavior exactly):
    - `minutes` for sub-day time windows, overriding `days` when given.
    - `zoom` for zoom-aware grid resolution, overridden by explicit `grid_size_deg`.
    - `north`/`south`/`east`/`west` viewport bbox filtering (all four required together).
    - `use_default_area` to apply the configured operating-area bbox
      (app_config key `default_operating_area_bbox`) when no explicit viewport
      is given -- the mechanism is country-agnostic; only the seeded config
      value is DRC-specific.
    - `imbalance` on each cell (demand_count - supply_count).

    V2.1 addition: `service_area_id` resolves a configured public.service_areas
    row (see GET /config/admin/service-areas) to a bbox, so the frontend can
    request "Kinshasa" or any future configured city/area without ever sending
    or hardcoding its coordinates. Viewport precedence, most to least specific:
    explicit north/south/east/west > service_area_id > use_default_area > none.
    An unknown service_area_id is a 404, distinct from the 400 used for
    malformed explicit viewport bounds.

    V3 additions (see sql/20260828_heatmap_v3_operational_intelligence.sql):
    - `geographic_scope` on the response echoes which viewport-resolution
      mechanism was actually used ("custom_viewport"/"service_area"/
      "default_area"/"unscoped") -- derived purely from the precedence branch
      above, no new query.
    - `cancellation_count`/`cancellation_rate` per cell, only when
      `source=rides` (ride_requests has no cancelled_at). None (not 0) when
      not applicable or when a cell's total is 0.
    - `demand_trend_pct`/`demand_trend_label` per cell, comparing the current
      window's demand to the immediately preceding equivalent window. None
      when the previous period had no demand (undefined trend).
    - Average wait time is intentionally NOT exposed: no timestamp anywhere in
      this schema marks when a driver accepted/was matched to a request
      (`driver_responses.created_at` marks bid placement, not acceptance), so
      no meaningful wait-time metric can be computed without fabricating data.
    - V3 fields are RPC-only -- the Python fallback path leaves them at their
      Optional/None defaults (see the fallback's own code comment for why).
    """
    viewport_params = (north, south, east, west)
    given_count = sum(1 for v in viewport_params if v is not None)
    if given_count not in (0, 4):
        raise HTTPException(status_code=400, detail="north, south, east, west must all be provided together")
    if given_count == 4:
        _validate_viewport(north, south, east, west)

    demand_table = "rides" if source == "rides" else "requests"
    sb = get_supabase()
    admin_id = (_user or {}).get("id")
    generated_at = datetime.now(timezone.utc).isoformat()

    effective_grid_size = _resolve_grid_size(grid_size_deg, zoom)

    effective_viewport: Optional[dict] = None
    if given_count == 4:
        effective_viewport = {"north": north, "south": south, "east": east, "west": west}
        geographic_scope = "custom_viewport"
    elif service_area_id:
        effective_viewport = _resolve_service_area_bbox(sb, service_area_id)
        geographic_scope = "service_area"
    elif use_default_area:
        effective_viewport = _get_default_operating_area_bbox(sb)
        geographic_scope = "default_area"
    else:
        geographic_scope = "unscoped"

    if admin_id:
        try:
            result = first_row(call_rpc("get_admin_heatmap", {
                "p_admin_id": admin_id,
                "p_days": days,
                "p_source": demand_table,
                "p_category": category,
                "p_vehicle_type": vehicle_type,
                "p_grid_size": effective_grid_size,
                "p_minutes": minutes,
                "p_north": effective_viewport["north"] if effective_viewport else None,
                "p_south": effective_viewport["south"] if effective_viewport else None,
                "p_east": effective_viewport["east"] if effective_viewport else None,
                "p_west": effective_viewport["west"] if effective_viewport else None,
            }))
            if result:
                return HeatmapResponse(
                    generated_at=generated_at,
                    grid_size_deg=effective_grid_size,
                    source=demand_table,
                    days=days,
                    demand_points_included=result.get("demand_points_included", 0),
                    demand_points_skipped=result.get("demand_points_skipped", 0),
                    supply_points_included=result.get("supply_points_included", 0),
                    cells=_build_heatmap_cells_from_rpc(result.get("cells") or [], demand_table),
                    minutes=minutes,
                    zoom=zoom,
                    viewport=effective_viewport,
                    geographic_scope=geographic_scope,
                )
        except Exception as e:
            if not rpc_missing(e):
                raise HTTPException(status_code=500, detail=str(e))
            logger.warning(
                "get_admin_heatmap RPC not found, falling back to capped "
                "client-side grid bucketing. Apply sql/20260825_heatmap_rpc.sql "
                "and sql/20260826_heatmap_v2_viewport_zoom.sql."
            )

    cutoff_str = (datetime.now(timezone.utc) - _resolve_time_window(days, minutes)).isoformat()
    table_name = "rides" if demand_table == "rides" else "ride_requests"

    try:
        demand_query = (
            sb.table(table_name)
            .select("picking_point")
            .gte("created_at", cutoff_str)
            .limit(_HEATMAP_ROW_CAP)
        )
        if category:
            demand_query = demand_query.eq("category", category)
        if vehicle_type and table_name == "rides":
            # ride_requests has no vehicle_type column (confirmed absent --
            # every other read of it off a ride_requests row is defensive,
            # r.get("vehicle_type", "car"), unlike category which is filtered
            # directly elsewhere in this codebase). Filtering by it there
            # raises "column vehicle_type does not exist" (42703).
            demand_query = demand_query.eq("vehicle_type", vehicle_type)
        demand_rows = demand_query.execute().data or []
    except Exception:
        demand_rows = []

    try:
        supply_query = (
            sb.table("driver_profiles")
            .select("latitude, longitude")
            .eq("is_online", True)
            .not_.is_("latitude", "null")
            .not_.is_("longitude", "null")
            .limit(_HEATMAP_ROW_CAP)
        )
        if category:
            supply_query = supply_query.eq("category", category)
        if vehicle_type:
            supply_query = supply_query.eq("vehicle_type", vehicle_type)
        if effective_viewport:
            supply_query = (
                supply_query
                .gte("latitude", effective_viewport["south"])
                .lte("latitude", effective_viewport["north"])
                .gte("longitude", effective_viewport["west"])
                .lte("longitude", effective_viewport["east"])
            )
        supply_rows = supply_query.execute().data or []
    except Exception:
        supply_rows = []

    cell_map: dict = {}
    demand_included = 0
    demand_skipped = 0

    for row in demand_rows:
        point = _lat_lng_from_point(row.get("picking_point"))
        if not point:
            demand_skipped += 1
            continue
        lat, lng = point
        # picking_point is opaque JSON -- not a queryable column via PostgREST --
        # so viewport filtering for demand can only happen here, after per-row
        # extraction, rather than pushed into the .select() query above.
        if effective_viewport and not (
            effective_viewport["south"] <= lat <= effective_viewport["north"]
            and effective_viewport["west"] <= lng <= effective_viewport["east"]
        ):
            continue
        key = (_bucket(lat, effective_grid_size), _bucket(lng, effective_grid_size))
        cell_map.setdefault(key, {"demand": 0, "supply": 0})
        cell_map[key]["demand"] += 1
        demand_included += 1

    supply_included = 0
    for row in supply_rows:
        try:
            lat, lng = float(row["latitude"]), float(row["longitude"])
        except (TypeError, ValueError, KeyError):
            continue
        key = (_bucket(lat, effective_grid_size), _bucket(lng, effective_grid_size))
        cell_map.setdefault(key, {"demand": 0, "supply": 0})
        cell_map[key]["supply"] += 1
        supply_included += 1

    # V3 fields (cancellation_count/rate, demand_trend_pct/label) are deliberately
    # NOT computed in this fallback path -- each would need its own second
    # _HEATMAP_ROW_CAP-capped bucketing pass over the same already-degraded row
    # set (this fallback only runs when the RPC migration hasn't been applied),
    # compounding the under-reporting risk this fallback already discloses for
    # demand/supply. They stay at their Optional/None defaults here; the RPC
    # path (_build_heatmap_cells_from_rpc) is the only source of real values.
    cells = [
        HeatmapCell(
            grid_lat=k[0], grid_lng=k[1],
            demand_count=v["demand"], supply_count=v["supply"],
            imbalance=_compute_imbalance(v["demand"], v["supply"]),
        )
        for k, v in cell_map.items()
    ]

    return HeatmapResponse(
        generated_at=generated_at,
        grid_size_deg=effective_grid_size,
        source=demand_table,
        days=days,
        demand_points_included=demand_included,
        demand_points_skipped=demand_skipped,
        supply_points_included=supply_included,
        cells=cells,
        minutes=minutes,
        zoom=zoom,
        geographic_scope=geographic_scope,
        viewport=effective_viewport,
    )


class CancellationReasonBreakdown(BaseModel):
    reason_code: str
    reason_text: Optional[str] = None
    count: int = 0
    cancelled_by_customer: int = 0
    cancelled_by_driver: int = 0


class RepeatCancellationItem(BaseModel):
    user_id: str
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    user_type: str  # "customer" or "driver"
    cancellation_count: int = 0
    latest_cancellation_at: Optional[str] = None
    reason_codes: List[str] = []


class SafetyConcernCancellation(BaseModel):
    ride_id: str
    customer_id: str
    customer_name: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    reason_text: Optional[str] = None
    cancelled_by: Optional[str] = None
    cancelled_at: Optional[str] = None
    picking_point: Optional[Any] = None
    destination: Optional[Any] = None


class CancellationAnalyticsResponse(BaseModel):
    total_cancellations: int = 0
    reason_breakdown: List[CancellationReasonBreakdown] = []
    repeat_cancellations: List[RepeatCancellationItem] = []
    safety_concern_queue: List[SafetyConcernCancellation] = []


@router.get("/admin/cancellations", response_model=CancellationAnalyticsResponse)
def get_cancellation_analytics(
    days: int = Query(7, description="Number of days of history to analyze"),
    _user=Depends(require_admin),
):
    """
    Cancellation analytics dashboard per V2_A §1 and AdminSide.md §5.6.

    Returns:
    - Total cancellations in the period
    - Breakdown by reason_code and cancelled_by
    - Repeat cancellations (>3 in 24h flagged)
    - Safety concern queue (reason_code = "safety_concern")
    """
    try:
        sb = get_supabase()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        # Fetch all cancelled rides in the period
        cancelled_rides = (
            sb.table("rides")
            .select(
                "id, customer_id, driver_id, cancelled_by, cancelled_at, "
                "cancellation_reason, reason_code, reason_text, "
                "picking_point, destination"
            )
            .eq("status", "cancelled")
            .gte("cancelled_at", cutoff_str)
            .order("cancelled_at", desc=True)
            .execute()
        )

        rides_data = cancelled_rides.data or []

        # 1. Reason code breakdown
        reason_map = {}  # reason_code -> {count, by_customer, by_driver, reason_text}
        for r in rides_data:
            code = r.get("reason_code") or "unknown"
            if code not in reason_map:
                reason_map[code] = {
                    "count": 0,
                    "cancelled_by_customer": 0,
                    "cancelled_by_driver": 0,
                    "reason_text": r.get("reason_text"),
                }
            reason_map[code]["count"] += 1
            cancelled_by = r.get("cancelled_by")
            if cancelled_by == "customer":
                reason_map[code]["cancelled_by_customer"] += 1
            elif cancelled_by == "driver":
                reason_map[code]["cancelled_by_driver"] += 1

        reason_breakdown = [
            CancellationReasonBreakdown(
                reason_code=code,
                reason_text=data["reason_text"],
                count=data["count"],
                cancelled_by_customer=data["cancelled_by_customer"],
                cancelled_by_driver=data["cancelled_by_driver"],
            )
            for code, data in sorted(reason_map.items(), key=lambda x: x[1]["count"], reverse=True)
        ]

        # 2. Repeat cancellation detection (>3 cancellations in 24h by same user)
        # Group cancellations by user (customer or driver)
        now = datetime.now(timezone.utc)
        cutoff_24h = now - timedelta(hours=24)
        cutoff_24h_str = cutoff_24h.isoformat()

        recent_cancellations = [r for r in rides_data if r.get("cancelled_at", "") >= cutoff_24h_str]

        customer_cancel_map = {}  # customer_id -> count
        driver_cancel_map = {}  # driver_id -> count
        customer_reason_map = {}  # customer_id -> set of reason_codes
        driver_reason_map = {}  # driver_id -> set of reason_codes
        customer_latest = {}
        driver_latest = {}

        for r in recent_cancellations:
            cancelled_by = r.get("cancelled_by")
            code = r.get("reason_code") or "unknown"

            if cancelled_by == "customer":
                cid = r.get("customer_id")
                if cid:
                    customer_cancel_map[cid] = customer_cancel_map.get(cid, 0) + 1
                    if cid not in customer_reason_map:
                        customer_reason_map[cid] = set()
                    customer_reason_map[cid].add(code)
                    if cid not in customer_latest or (r.get("cancelled_at") or "") > (customer_latest[cid] or ""):
                        customer_latest[cid] = r.get("cancelled_at")

            if cancelled_by == "driver":
                did = r.get("driver_id")
                if did:
                    driver_cancel_map[did] = driver_cancel_map.get(did, 0) + 1
                    if did not in driver_reason_map:
                        driver_reason_map[did] = set()
                    driver_reason_map[did].add(code)
                    if did not in driver_latest or (r.get("cancelled_at") or "") > (driver_latest[did] or ""):
                        driver_latest[did] = r.get("cancelled_at")

        # Batch-fetch every customer/driver name this section could need in at most
        # 2 queries total (chunked), instead of one query per flagged row.
        repeat_customer_ids = [cid for cid, count in customer_cancel_map.items() if count > 3]
        repeat_driver_ids = [did for did, count in driver_cancel_map.items() if count > 3]
        safety_rows = [r for r in rides_data if r.get("reason_code") == "safety_concern"]
        safety_customer_ids = [r.get("customer_id") for r in safety_rows if r.get("customer_id")]
        safety_driver_ids = [r.get("driver_id") for r in safety_rows if r.get("driver_id")]

        all_customer_ids = list({*repeat_customer_ids, *safety_customer_ids})
        all_driver_ids = list({*repeat_driver_ids, *safety_driver_ids})

        customer_info: dict = {}
        for i in range(0, len(all_customer_ids), _IN_FILTER_CHUNK):
            chunk = all_customer_ids[i : i + _IN_FILTER_CHUNK]
            try:
                rows = sb.table("users").select("id, full_name, phone_number").in_("id", chunk).execute()
                for row in rows.data or []:
                    customer_info[row["id"]] = {"full_name": row.get("full_name"), "phone_number": row.get("phone_number")}
            except Exception:
                pass

        driver_info: dict = {}
        for i in range(0, len(all_driver_ids), _IN_FILTER_CHUNK):
            chunk = all_driver_ids[i : i + _IN_FILTER_CHUNK]
            try:
                rows = sb.table("driver_profiles").select("id, users(full_name, phone_number)").in_("id", chunk).execute()
                for row in rows.data or []:
                    user_info = row.get("users") or {}
                    driver_info[row["id"]] = {"full_name": user_info.get("full_name"), "phone_number": user_info.get("phone_number")}
            except Exception:
                pass

        repeat_items = []

        # Flag customers with >3 cancellations in 24h
        for cid in repeat_customer_ids:
            info = customer_info.get(cid, {})
            repeat_items.append(RepeatCancellationItem(
                user_id=cid,
                full_name=info.get("full_name"),
                phone_number=info.get("phone_number"),
                user_type="customer",
                cancellation_count=customer_cancel_map[cid],
                latest_cancellation_at=customer_latest.get(cid),
                reason_codes=sorted(customer_reason_map.get(cid, set())),
            ))

        # Flag drivers with >3 cancellations in 24h
        for did in repeat_driver_ids:
            info = driver_info.get(did, {})
            repeat_items.append(RepeatCancellationItem(
                user_id=did,
                full_name=info.get("full_name"),
                phone_number=info.get("phone_number"),
                user_type="driver",
                cancellation_count=driver_cancel_map[did],
                latest_cancellation_at=driver_latest.get(did),
                reason_codes=sorted(driver_reason_map.get(did, set())),
            ))

        # 3. Safety concern queue
        safety_queue = []
        for r in safety_rows:
            did = r.get("driver_id")
            safety_queue.append(SafetyConcernCancellation(
                ride_id=r.get("id"),
                customer_id=r.get("customer_id"),
                customer_name=customer_info.get(r.get("customer_id"), {}).get("full_name"),
                driver_id=did,
                driver_name=driver_info.get(did, {}).get("full_name") if did else None,
                reason_text=r.get("reason_text") or r.get("cancellation_reason"),
                cancelled_by=r.get("cancelled_by"),
                cancelled_at=r.get("cancelled_at"),
                picking_point=r.get("picking_point"),
                destination=r.get("destination"),
            ))

        return CancellationAnalyticsResponse(
            total_cancellations=len(rides_data),
            reason_breakdown=reason_breakdown,
            repeat_cancellations=repeat_items,
            safety_concern_queue=safety_queue,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
