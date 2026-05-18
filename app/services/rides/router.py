from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, Any, List
from app.core.dependencies import require_admin
from app.core.supabase import get_supabase

router = APIRouter(prefix="/rides", tags=["rides"])


class OfferItem(BaseModel):
    id: str
    driver_id: str
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    price: float
    status: str
    created_at: str
    is_update: bool = False


class RideOfferHistoryResponse(BaseModel):
    ride_id: str
    ride_request_id: Optional[str] = None
    offers: List[OfferItem] = []
    original_price: Optional[float] = None
    final_price: Optional[float] = None
    update_count: int = 0


class RideResponse(BaseModel):
    id: str
    ride_request_id: Optional[str] = None
    customer_id: str
    driver_id: Optional[str] = None
    customer_phone: Optional[str] = None
    driver_phone: Optional[str] = None
    picking_point: Optional[Any] = None
    destination: Optional[Any] = None
    customer_comment: Optional[str] = None
    price: float = 0.0
    status: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    cancelled_by: Optional[str] = None
    cancellation_reason: Optional[str] = None
    created_at: str


class RideDetailResponse(RideResponse):
    customer_name: Optional[str] = None
    customer_avatar_url: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    driver_avatar_url: Optional[str] = None
    distance_km: Optional[float] = None
    duration_minutes: Optional[float] = None
    platform_commission_amount: Optional[float] = None
    arrived_at: Optional[str] = None
    vehicle_snapshot: Optional[Any] = None
    category: Optional[str] = None
    stops: Optional[Any] = None
    reason_code: Optional[str] = None
    reason_text: Optional[str] = None


class TripResponse(BaseModel):
    id: str
    ride_request_id: Optional[str] = None
    customer_id: str
    driver_id: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    picking_point: Optional[Any] = None
    destination: Optional[Any] = None
    price: float = 0.0
    status: str = "completed"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    distance_km: Optional[float] = None
    duration_minutes: Optional[float] = None
    platform_commission_amount: Optional[float] = None
    category: Optional[str] = None
    customer_rating: Optional[int] = None
    driver_rating: Optional[int] = None
    created_at: str


class RideRequestResponse(BaseModel):
    id: str
    customer_id: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    picking_point: Optional[Any] = None
    destination: Optional[Any] = None
    comment: Optional[str] = None
    suggested_price: float = 0.0
    status: str
    vehicle_type: str
    category: str = "standard"
    created_at: str
    expires_at: Optional[str] = None
    bid_count: int = 0
    is_stale: bool = False


class CancelRideRequestBody(BaseModel):
    reason: str


class ForceEndTripBody(BaseModel):
    reason: str
    initiated_by: str = "operations_manager"  # "super_admin" or "operations_manager"


class SendPushBody(BaseModel):
    target: str  # "customer" or "driver"
    title: str
    message: str


class ActiveTripItem(BaseModel):
    id: str
    ride_request_id: Optional[str] = None
    customer_id: str
    driver_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    picking_point: Optional[Any] = None
    destination: Optional[Any] = None
    price: float = 0.0
    status: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str
    driver_latitude: Optional[float] = None
    driver_longitude: Optional[float] = None
    duration_minutes: Optional[float] = None
    distance_km: Optional[float] = None


_RIDE_FIELDS = set(RideResponse.model_fields.keys())

_RIDE_DETAIL_FIELDS = {
    "id", "ride_request_id", "customer_id", "driver_id", "customer_phone",
    "driver_phone", "picking_point", "destination", "customer_comment",
    "price", "status", "started_at", "completed_at", "cancelled_at",
    "cancelled_by", "cancellation_reason", "created_at", "customer_name",
    "customer_avatar_url", "driver_name", "driver_phone", "driver_avatar_url",
    "distance_km", "duration_minutes", "platform_commission_amount",
    "arrived_at", "vehicle_snapshot", "category", "stops",
    "reason_code", "reason_text",
}


@router.get("/admin/list", response_model=List[RideResponse])
def list_rides(
    status: Optional[str] = Query(None),
    limit: int = Query(100),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        query = sb.table("rides").select("*").limit(limit)
        if status:
            query = query.eq("status", status)
        result = query.order("created_at", desc=True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    rides = []
    for r in result.data or []:
        row_fields = {k: v for k, v in r.items() if k in _RIDE_FIELDS}
        rides.append(RideResponse(**row_fields))

    return rides


@router.get("/{ride_id}", response_model=RideDetailResponse)
def get_ride_detail(ride_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = (
            sb.table("rides")
            .select(
                "*, "
                "users!rides_customer_id_fkey(full_name, phone_number, profile_image_url), "
                "driver_profiles!rides_driver_id_fkey(users!driver_profiles_user_id_fkey(full_name, phone_number, profile_image_url))"
            )
            .eq("id", ride_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Ride not found")

    r = result.data
    customer_info = r.pop("users", {}) or {}
    driver_profile = r.pop("driver_profiles", None) or {}
    driver_user_info = driver_profile.pop("users", {}) if isinstance(driver_profile, dict) else {}

    row_fields = {k: v for k, v in r.items() if k in _RIDE_DETAIL_FIELDS}
    return RideDetailResponse(
        **row_fields,
        customer_name=customer_info.get("full_name"),
        customer_avatar_url=customer_info.get("profile_image_url"),
        driver_name=driver_user_info.get("full_name"),
        driver_phone=driver_user_info.get("phone_number"),
        driver_avatar_url=driver_user_info.get("profile_image_url"),
    )


@router.get("/trips/{trip_id}", response_model=TripResponse)
def get_trip_detail(trip_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = (
            sb.table("rides")
            .select(
                "*, "
                "users!rides_customer_id_fkey(full_name, phone_number), "
                "driver_profiles!rides_driver_id_fkey(users!driver_profiles_user_id_fkey(full_name, phone_number))"
            )
            .eq("id", trip_id)
            .in_("status", ["completed", "cancelled"])
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Trip not found")

    r = result.data
    customer_info = r.pop("users", {}) or {}
    driver_profile = r.pop("driver_profiles", None) or {}
    driver_user_info = driver_profile.pop("users", {}) if isinstance(driver_profile, dict) else {}

    # Fetch ratings
    customer_rating = None
    driver_rating = None
    try:
        cr = sb.table("ride_ratings").select("rate").eq("ride_id", trip_id).maybe_single().execute()
        if cr.data:
            customer_rating = cr.data.get("rate")
    except Exception:
        pass
    try:
        dr = sb.table("customer_ratings").select("rate").eq("ride_id", trip_id).maybe_single().execute()
        if dr.data:
            driver_rating = dr.data.get("rate")
    except Exception:
        pass

    return TripResponse(
        id=r.get("id"),
        ride_request_id=r.get("ride_request_id"),
        customer_id=r.get("customer_id"),
        driver_id=r.get("driver_id"),
        customer_name=customer_info.get("full_name"),
        customer_phone=customer_info.get("phone_number"),
        driver_name=driver_user_info.get("full_name"),
        driver_phone=driver_user_info.get("phone_number"),
        picking_point=r.get("picking_point"),
        destination=r.get("destination"),
        price=float(r.get("price", 0)),
        status=r.get("status", "completed"),
        started_at=r.get("started_at"),
        completed_at=r.get("completed_at"),
        distance_km=r.get("distance_km"),
        duration_minutes=r.get("duration_minutes"),
        platform_commission_amount=(
            float(r["platform_commission_amount"]) if r.get("platform_commission_amount") else None
        ),
        category=r.get("category"),
        customer_rating=customer_rating,
        driver_rating=driver_rating,
        created_at=r.get("created_at"),
    )


@router.get("/{ride_id}/offers", response_model=RideOfferHistoryResponse)
def get_ride_offers(ride_id: str, _user=Depends(require_admin)):
    """
    Get offer history for a ride, including original offer, updates, and final price.
    Useful for auditing fare disputes where a driver updated their offer.
    """
    try:
        sb = get_supabase()

        # Get the ride to find its ride_request_id
        ride_result = (
            sb.table("rides")
            .select("id, ride_request_id, price")
            .eq("id", ride_id)
            .maybe_single()
            .execute()
        )

        if not ride_result.data:
            raise HTTPException(status_code=404, detail="Ride not found")

        ride = ride_result.data
        ride_request_id = ride.get("ride_request_id")
        final_price = float(ride.get("price", 0)) if ride.get("price") else None

        offers = []
        if ride_request_id:
            # Query driver_responses for this ride request
            try:
                responses = (
                    sb.table("driver_responses")
                    .select("*, driver_profiles!inner(users!inner(full_name, phone_number))")
                    .eq("ride_request_id", ride_request_id)
                    .order("created_at", asc=True)
                    .execute()
                )
            except Exception:
                responses = type("obj", (object,), {"data": []})()

            for i, resp in enumerate(responses.data or []):
                driver_profile = resp.pop("driver_profiles", None) or {}
                driver_user = driver_profile.pop("users", {}) if isinstance(driver_profile, dict) else {}

                offers.append(OfferItem(
                    id=resp.get("id"),
                    driver_id=resp.get("driver_id"),
                    driver_name=driver_user.get("full_name"),
                    driver_phone=driver_user.get("phone_number"),
                    price=float(resp.get("price", 0)),
                    status=resp.get("status", "pending"),
                    created_at=resp.get("created_at"),
                    is_update=i > 0,  # First response is the original offer
                ))

        original_price = float(offers[0].price) if offers else None
        update_count = max(0, len(offers) - 1)

        return RideOfferHistoryResponse(
            ride_id=ride_id,
            ride_request_id=ride_request_id,
            offers=offers,
            original_price=original_price,
            final_price=final_price,
            update_count=update_count,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/all", response_model=List[RideResponse])
def get_ride_history(
    limit: int = Query(100),
    offset: int = Query(0),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("rides")
            .select("*")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    rides = []
    for r in result.data or []:
        row_fields = {k: v for k, v in r.items() if k in _RIDE_FIELDS}
        rides.append(RideResponse(**row_fields))

    return rides


@router.get("/requests/active", response_model=List[RideRequestResponse])
def get_active_ride_requests(
    min_stale_minutes: int = Query(10, description="Minutes after which a request is considered stale"),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("ride_requests")
            .select(
                "*, users!ride_requests_customer_id_fkey(full_name, phone_number)"
            )
            .eq("status", "pending")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    requests = []
    for r in result.data or []:
        user_info = r.pop("users", {}) or {}

        # Count bids for this request
        bid_count = 0
        try:
            bids = (
                sb.table("driver_responses")
                .select("id", count="exact")
                .eq("ride_request_id", r.get("id"))
                .execute()
            )
            bid_count = len(bids.data or [])
        except Exception:
            pass

        # Determine if stale
        created_str = r.get("created_at")
        is_stale = False
        if created_str:
            try:
                created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                age_minutes = (now - created_at).total_seconds() / 60
                is_stale = age_minutes > min_stale_minutes and bid_count == 0
            except Exception:
                pass

        requests.append(RideRequestResponse(
            id=r.get("id"),
            customer_id=r.get("customer_id"),
            customer_name=user_info.get("full_name"),
            customer_phone=user_info.get("phone_number"),
            picking_point=r.get("picking_point"),
            destination=r.get("destination"),
            comment=r.get("comment"),
            suggested_price=float(r.get("suggested_price", 0)),
            status=r.get("status", "pending"),
            vehicle_type=r.get("vehicle_type", "car"),
            category=r.get("category", "standard"),
            created_at=r.get("created_at"),
            expires_at=r.get("expires_at"),
            bid_count=bid_count,
            is_stale=is_stale,
        ))

    return requests


@router.post("/request/{request_id}/cancel")
def cancel_ride_request(
    request_id: str,
    body: CancelRideRequestBody,
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        # Verify the request exists and is pending
        req = (
            sb.table("ride_requests")
            .select("*")
            .eq("id", request_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not req.data:
        raise HTTPException(status_code=404, detail="Ride request not found")

    if req.data.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Ride request is not pending")

    try:
        # Cancel the request
        sb.table("ride_requests").update({"status": "cancelled"}).eq("id", request_id).execute()

        # Create a notification for the customer
        customer_id = req.data.get("customer_id")
        if customer_id:
            sb.table("notifications").insert({
                "user_id": customer_id,
                "notification_type": "ride_request_cancelled",
                "title": "Ride request cancelled",
                "content": body.reason,
                "status": "unread",
            }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "message": "Ride request cancelled",
        "request_id": request_id,
        "reason": body.reason,
    }


# ── Live Trip Intervention (§5.3) ───────────────────────────────────────


@router.get("/admin/active", response_model=List[ActiveTripItem])
def list_active_trips(
    _user=Depends(require_admin),
):
    """
    List all currently active/in-progress rides with live driver location.
    Used by the Live Trip Intervention panel.
    """
    try:
        sb = get_supabase()
        result = (
            sb.table("rides")
            .select(
                "*, "
                "users!rides_customer_id_fkey(full_name, phone_number), "
                "driver_profiles!rides_driver_id_fkey("
                "  users!driver_profiles_user_id_fkey(full_name, phone_number), "
                "  latitude, longitude"
                ")"
            )
            .in_("status", ["started", "arrived", "in_progress"])
            .order("started_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    items = []
    for r in result.data or []:
        customer_info = r.pop("users", {}) or {}
        driver_profile = r.pop("driver_profiles", None) or {}
        driver_user_info = (
            driver_profile.pop("users", {})
            if isinstance(driver_profile, dict)
            else {}
        )

        items.append(ActiveTripItem(
            id=r.get("id"),
            ride_request_id=r.get("ride_request_id"),
            customer_id=r.get("customer_id"),
            driver_id=r.get("driver_id"),
            customer_name=customer_info.get("full_name"),
            customer_phone=customer_info.get("phone_number"),
            driver_name=driver_user_info.get("full_name"),
            driver_phone=driver_user_info.get("phone_number"),
            picking_point=r.get("picking_point"),
            destination=r.get("destination"),
            price=float(r.get("price", 0)),
            status=r.get("status", "started"),
            started_at=r.get("started_at"),
            completed_at=r.get("completed_at"),
            created_at=r.get("created_at"),
            driver_latitude=(
                float(driver_profile["latitude"])
                if isinstance(driver_profile, dict) and driver_profile.get("latitude")
                else None
            ),
            driver_longitude=(
                float(driver_profile["longitude"])
                if isinstance(driver_profile, dict) and driver_profile.get("longitude")
                else None
            ),
            duration_minutes=r.get("duration_minutes"),
            distance_km=r.get("distance_km"),
        ))

    return items


@router.patch("/admin/{ride_id}/force-end")
def force_end_trip(
    ride_id: str,
    body: ForceEndTripBody,
    _user=Depends(require_admin),
):
    """
    Force-end an active trip (emergency only).
    Requires Super Admin or Operations Manager role + reason.
    """
    try:
        sb = get_supabase()

        # Verify the ride exists and is active
        ride = (
            sb.table("rides")
            .select("*")
            .eq("id", ride_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not ride.data:
        raise HTTPException(status_code=404, detail="Ride not found")

    if ride.data.get("status") in ["completed", "cancelled"]:
        raise HTTPException(
            status_code=400,
            detail="Ride is already completed or cancelled",
        )

    admin_id = _user.get("id") or _user.get("user_id") or "unknown"
    now = datetime.now(timezone.utc).isoformat()

    try:
        # Update the ride status
        sb.table("rides").update({
            "status": "cancelled",
            "completed_at": now,
            "cancelled_at": now,
            "cancelled_by": admin_id,
            "cancellation_reason": f"Force-ended by admin ({body.initiated_by}): {body.reason}",
        }).eq("id", ride_id).execute()

        # Notify both parties
        customer_id = ride.data.get("customer_id")
        driver_id = ride.data.get("driver_id")

        if customer_id:
            sb.table("notifications").insert({
                "user_id": customer_id,
                "notification_type": "trip_force_ended",
                "title": "Trip ended by admin",
                "content": f"Your trip has been ended by Operations. Reason: {body.reason}",
                "status": "unread",
            }).execute()

        if driver_id:
            # Look up driver user_id from driver_profiles
            try:
                dp = (
                    sb.table("driver_profiles")
                    .select("user_id")
                    .eq("id", driver_id)
                    .maybe_single()
                    .execute()
                )
                driver_user_id = dp.data.get("user_id") if dp.data else None
                if driver_user_id:
                    sb.table("notifications").insert({
                        "user_id": driver_user_id,
                        "notification_type": "trip_force_ended",
                        "title": "Trip ended by admin",
                        "content": f"Your trip has been ended by Operations. Reason: {body.reason}",
                        "status": "unread",
                    }).execute()
            except Exception:
                pass
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "message": "Trip force-ended successfully",
        "ride_id": ride_id,
        "reason": body.reason,
        "initiated_by": body.initiated_by,
    }


@router.post("/admin/{ride_id}/send-push")
def send_push_to_trip_party(
    ride_id: str,
    body: SendPushBody,
    _user=Depends(require_admin),
):
    """
    Send a push notification to either the customer or driver
    of an active trip.
    """
    try:
        sb = get_supabase()

        # Verify the ride exists
        ride = (
            sb.table("rides")
            .select("*")
            .eq("id", ride_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not ride.data:
        raise HTTPException(status_code=404, detail="Ride not found")

    target_user_id = None
    target_label = ""

    if body.target == "customer":
        target_user_id = ride.data.get("customer_id")
        target_label = "customer"
    elif body.target == "driver":
        driver_id = ride.data.get("driver_id")
        if driver_id:
            try:
                dp = (
                    sb.table("driver_profiles")
                    .select("user_id")
                    .eq("id", driver_id)
                    .maybe_single()
                    .execute()
                )
                target_user_id = dp.data.get("user_id") if dp.data else None
            except Exception:
                pass
        target_label = "driver"

    if not target_user_id:
        raise HTTPException(
            status_code=400,
            detail=f"No {target_label} user ID found for this ride",
        )

    try:
        sb.table("notifications").insert({
            "user_id": target_user_id,
            "notification_type": "admin_message",
            "title": body.title,
            "content": body.message,
            "status": "unread",
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "message": f"Push notification sent to {target_label}",
        "ride_id": ride_id,
        "target": body.target,
        "title": body.title,
    }


class MarketplaceBidItem(BaseModel):
    driver_id: str
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    price: float
    created_at: str
    vehicle_make: Optional[str] = None
    vehicle_model: Optional[str] = None
    vehicle_color: Optional[str] = None
    vehicle_license_plate: Optional[str] = None


class MarketplaceRequestItem(BaseModel):
    id: str
    customer_id: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    picking_point: Optional[Any] = None
    destination: Optional[Any] = None
    comment: Optional[str] = None
    suggested_price: float = 0.0
    status: str
    vehicle_type: str
    category: str = "standard"
    created_at: str
    expires_at: Optional[str] = None
    bid_count: int = 0
    is_stale: bool = False
    bids: List[MarketplaceBidItem] = []


class MarketplaceResponse(BaseModel):
    requests: List[MarketplaceRequestItem]
    total_active: int
    stale_count: int


@router.get("/admin/marketplace", response_model=MarketplaceResponse)
def get_marketplace_view(
    min_stale_minutes: int = Query(10, description="Minutes after which a request is considered stale"),
    _user=Depends(require_admin),
):
    """
    Marketplace view of all active ride requests with their bids.
    Useful for debugging matchmaking and observing market behavior.
    """
    try:
        sb = get_supabase()
        result = (
            sb.table("ride_requests")
            .select(
                "*, users!ride_requests_customer_id_fkey(full_name, phone_number)"
            )
            .eq("status", "pending")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    requests_list = []
    stale_count = 0

    for r in result.data or []:
        user_info = r.pop("users", {}) or {}

        # Fetch bids (driver_responses) for this request
        bids = []
        bid_count = 0
        try:
            driver_resp = (
                sb.table("driver_responses")
                .select(
                    "*, driver_profiles!driver_responses_driver_id_fkey(user_id, users!driver_profiles_user_id_fkey(full_name, phone_number), vehicles(make, model, color, license_plate))"
                )
                .eq("ride_request_id", r.get("id"))
                .order("created_at", desc=False)
                .execute()
            )
            bid_count = len(driver_resp.data or [])
            for dr in driver_resp.data or []:
                dp = dr.pop("driver_profiles", {}) or {}
                user_info_driver = dp.pop("users", {}) or {}
                vehicle_info = dp.pop("vehicles", {}) or {}
                bids.append(MarketplaceBidItem(
                    driver_id=dr.get("driver_id"),
                    driver_name=user_info_driver.get("full_name"),
                    driver_phone=user_info_driver.get("phone_number"),
                    price=float(dr.get("price", 0)),
                    created_at=dr.get("created_at"),
                    vehicle_make=vehicle_info.get("make"),
                    vehicle_model=vehicle_info.get("model"),
                    vehicle_color=vehicle_info.get("color"),
                    vehicle_license_plate=vehicle_info.get("license_plate"),
                ))
        except Exception:
            pass

        # Determine if stale
        created_str = r.get("created_at")
        is_stale = False
        if created_str:
            try:
                created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                age_minutes = (now - created_at).total_seconds() / 60
                is_stale = age_minutes > min_stale_minutes and bid_count == 0
            except Exception:
                pass

        if is_stale:
            stale_count += 1

        requests_list.append(MarketplaceRequestItem(
            id=r.get("id"),
            customer_id=r.get("customer_id"),
            customer_name=user_info.get("full_name"),
            customer_phone=user_info.get("phone_number"),
            picking_point=r.get("picking_point"),
            destination=r.get("destination"),
            comment=r.get("comment"),
            suggested_price=float(r.get("suggested_price", 0)),
            status=r.get("status", "pending"),
            vehicle_type=r.get("vehicle_type", "car"),
            category=r.get("category", "standard"),
            created_at=r.get("created_at"),
            expires_at=r.get("expires_at"),
            bid_count=bid_count,
            is_stale=is_stale,
            bids=bids,
        ))

    return MarketplaceResponse(
        requests=requests_list,
        total_active=len(requests_list),
        stale_count=stale_count,
    )
