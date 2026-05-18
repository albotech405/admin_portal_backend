from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional, Any
from app.core.dependencies import require_admin
from app.core.supabase import get_supabase
from datetime import datetime, timezone, timedelta

router = APIRouter(prefix="/analytics", tags=["analytics"])


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
def get_dashboard_metrics(_user=Depends(require_admin)):
    try:
        sb = get_supabase()
        metrics = DashboardMetricsResponse()

        # 1. Pending payments count
        try:
            payments = (
                sb.table("wallet_topup_requests")
                .select("id", count="exact")
                .eq("status", "pending")
                .execute()
            )
            metrics.pending_payments_count = len(payments.data or [])
        except Exception:
            pass

        # 2. Pending drivers count
        try:
            drivers = (
                sb.table("driver_profiles")
                .select("id", count="exact")
                .eq("verification_status", "pending")
                .execute()
            )
            metrics.pending_drivers_count = len(drivers.data or [])
        except Exception:
            pass

        # 3. Active drivers (approved + online)
        try:
            active_drivers = (
                sb.table("driver_profiles")
                .select("id", count="exact")
                .eq("verification_status", "approved")
                .eq("is_online", True)
                .execute()
            )
            metrics.active_drivers_count = len(active_drivers.data or [])
        except Exception:
            pass

        # 4. Active rides (in_progress + driver_en_route + arrived)
        try:
            active_statuses = ["in_progress", "driver_en_route", "arrived"]
            active_rides = (
                sb.table("rides")
                .select("id", count="exact")
                .in_("status", active_statuses)
                .execute()
            )
            metrics.active_rides_count = len(active_rides.data or [])
        except Exception:
            pass

        # 5. Active SOS sessions
        try:
            active_sos = (
                sb.table("sos_sessions")
                .select("id", count="exact")
                .eq("is_active", True)
                .execute()
            )
            metrics.active_sos_count = len(active_sos.data or [])
        except Exception:
            pass

        # 6. Stale ride requests (pending > threshold with 0 bids)
        try:
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
            cutoff_str = cutoff.isoformat()

            pending_requests = (
                sb.table("ride_requests")
                .select("id, created_at")
                .eq("status", "pending")
                .lt("created_at", cutoff_str)
                .execute()
            )

            stale_count = 0
            for req in pending_requests.data or []:
                try:
                    bids = (
                        sb.table("driver_responses")
                        .select("id", count="exact")
                        .eq("ride_request_id", req.get("id"))
                        .execute()
                    )
                    if len(bids.data or []) == 0:
                        stale_count += 1
                except Exception:
                    stale_count += 1

            metrics.stale_requests_count = stale_count
        except Exception:
            pass

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

        for ride in completed_rides.data or []:
            driver_id = ride.get("driver_id")
            if driver_id and driver_id not in driver_update_map:
                driver_update_map[driver_id] = {
                    "total_offers": 0,
                    "updated_offers": 0,
                    "full_name": None,
                    "phone_number": None,
                }

            # Count offers for this ride's request
            # We approximate by counting driver_responses from this driver
            try:
                offers = (
                    sb.table("driver_responses")
                    .select("id, created_at, status")
                    .eq("driver_id", driver_id)
                    .execute()
                )
            except Exception:
                offers = type("obj", (object,), {"data": []})()

            driver_offers = [o for o in (offers.data or [])]
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

        # Get driver names for high-rate drivers
        high_rate_drivers = []
        for driver_id, data in driver_update_map.items():
            if data["total_offers"] > 0:
                update_rate = data["updated_offers"] / data["total_offers"]
            else:
                update_rate = 0.0

            if update_rate > 0.6:  # > 60% update rate
                # Fetch driver name
                try:
                    driver_info = (
                        sb.table("driver_profiles")
                        .select("users(full_name, phone_number)")
                        .eq("id", driver_id)
                        .maybe_single()
                        .execute()
                    )
                    if driver_info.data:
                        user_info = driver_info.data.get("users", {}) or {}
                        data["full_name"] = user_info.get("full_name")
                        data["phone_number"] = user_info.get("phone_number")
                except Exception:
                    pass

                high_rate_drivers.append(DriverOfferUpdateRate(
                    driver_id=driver_id,
                    full_name=data["full_name"],
                    phone_number=data["phone_number"],
                    total_offers=data["total_offers"],
                    updated_offers=data["updated_offers"],
                    update_rate=round(update_rate, 2),
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

    Queries the driver_locations table joined with driver_profiles and users
    to return driver positions with names and online status.

    - online_only (default True): only return drivers marked as online
    - stale_minutes (default 5): exclude locations older than this many minutes
    """
    try:
        sb = get_supabase()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        cutoff_str = cutoff.isoformat()

        # Query driver_locations joined with driver_profiles for online status
        locations_query = (
            sb.table("driver_locations")
            .select(
                "driver_id, latitude, longitude, updated_at, "
                "driver_profiles!inner(is_online, user_id)"
            )
            .gte("updated_at", cutoff_str)
        )

        if online_only:
            locations_query = locations_query.eq("driver_profiles.is_online", True)

        locations_result = locations_query.execute()
        location_rows = locations_result.data or []

        drivers = []
        for row in location_rows:
            driver_profile = row.get("driver_profiles", {}) or {}
            driver_id = row.get("driver_id")

            # Fetch user info (full_name, phone_number) for this driver
            full_name = None
            phone_number = None
            try:
                user_id = driver_profile.get("user_id")
                if user_id:
                    user_result = (
                        sb.table("users")
                        .select("full_name, phone_number")
                        .eq("id", user_id)
                        .maybe_single()
                        .execute()
                    )
                    if user_result.data:
                        full_name = user_result.data.get("full_name")
                        phone_number = user_result.data.get("phone_number")
            except Exception:
                pass

            drivers.append(DriverLocationItem(
                driver_id=driver_id,
                full_name=full_name,
                phone_number=phone_number,
                latitude=float(row.get("latitude", 0)),
                longitude=float(row.get("longitude", 0)),
                is_online=bool(driver_profile.get("is_online", False)),
                updated_at=row.get("updated_at"),
            ))

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

        repeat_items = []

        # Flag customers with >3 cancellations in 24h
        for cid, count in customer_cancel_map.items():
            if count > 3:
                # Fetch customer name
                name = None
                phone = None
                try:
                    user = sb.table("users").select("full_name, phone_number").eq("id", cid).maybe_single().execute()
                    if user.data:
                        name = user.data.get("full_name")
                        phone = user.data.get("phone_number")
                except Exception:
                    pass

                repeat_items.append(RepeatCancellationItem(
                    user_id=cid,
                    full_name=name,
                    phone_number=phone,
                    user_type="customer",
                    cancellation_count=count,
                    latest_cancellation_at=customer_latest.get(cid),
                    reason_codes=sorted(customer_reason_map.get(cid, set())),
                ))

        # Flag drivers with >3 cancellations in 24h
        for did, count in driver_cancel_map.items():
            if count > 3:
                name = None
                phone = None
                try:
                    driver = (
                        sb.table("driver_profiles")
                        .select("users(full_name, phone_number)")
                        .eq("id", did)
                        .maybe_single()
                        .execute()
                    )
                    if driver.data:
                        user_info = driver.data.get("users", {}) or {}
                        name = user_info.get("full_name")
                        phone = user_info.get("phone_number")
                except Exception:
                    pass

                repeat_items.append(RepeatCancellationItem(
                    user_id=did,
                    full_name=name,
                    phone_number=phone,
                    user_type="driver",
                    cancellation_count=count,
                    latest_cancellation_at=driver_latest.get(did),
                    reason_codes=sorted(driver_reason_map.get(did, set())),
                ))

        # 3. Safety concern queue
        safety_queue = []
        for r in rides_data:
            if r.get("reason_code") == "safety_concern":
                customer_name = None
                driver_name = None

                try:
                    cust = sb.table("users").select("full_name").eq("id", r.get("customer_id")).maybe_single().execute()
                    if cust.data:
                        customer_name = cust.data.get("full_name")
                except Exception:
                    pass

                did = r.get("driver_id")
                if did:
                    try:
                        drv = (
                            sb.table("driver_profiles")
                            .select("users(full_name)")
                            .eq("id", did)
                            .maybe_single()
                            .execute()
                        )
                        if drv.data:
                            user_info = drv.data.get("users", {}) or {}
                            driver_name = user_info.get("full_name")
                    except Exception:
                        pass

                safety_queue.append(SafetyConcernCancellation(
                    ride_id=r.get("id"),
                    customer_id=r.get("customer_id"),
                    customer_name=customer_name,
                    driver_id=r.get("driver_id"),
                    driver_name=driver_name,
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
