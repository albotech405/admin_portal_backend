from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional, Any
from app.core.dependencies import require_admin
from app.core.supabase import get_supabase, count_of, IN_FILTER_CHUNK as _IN_FILTER_CHUNK
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
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
