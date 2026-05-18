from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime, timezone
from uuid import uuid4
from app.core.dependencies import require_admin
from app.core.supabase import get_supabase

router = APIRouter(prefix="/customers", tags=["customers"])


class CustomerAdminItem(BaseModel):
    id: str
    full_name: str
    phone_number: str
    email: Optional[str] = None
    is_active: bool
    is_admin: bool = False
    gender: Optional[str] = None
    profile_image_url: Optional[str] = None
    customer_rating: float = 0.0
    total_customer_ratings: int = 0
    created_at: str
    updated_at: str


class CustomerAdminListResponse(BaseModel):
    customers: List[CustomerAdminItem]
    total: int


class CustomerDetailResponse(CustomerAdminItem):
    total_rides: int = 0
    total_spent: float = 0.0
    privacy_preferences: Optional[dict] = None


class BanUnbanBody(BaseModel):
    reason: Optional[str] = None


_CUSTOMER_FIELDS = {
    "id", "full_name", "phone_number", "email", "is_active", "is_admin",
    "gender", "profile_image_url", "customer_rating", "total_customer_ratings",
    "created_at", "updated_at", "privacy_preferences",
}


@router.get("/admin/list", response_model=CustomerAdminListResponse)
def list_customers(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        query = sb.table("users").select("*").neq("role", "driver")

        if status == "active":
            query = query.eq("is_active", True)
        elif status == "suspended":
            query = query.eq("is_active", False)

        if search:
            query = query.or_(
                f"full_name.ilike.%{search}%,phone_number.ilike.%{search}%"
            )

        result = query.order("created_at", desc=True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    customers = []
    for r in result.data or []:
        row_fields = {k: v for k, v in r.items() if k in _CUSTOMER_FIELDS}
        customers.append(CustomerAdminItem(**row_fields))

    return CustomerAdminListResponse(customers=customers, total=len(customers))


@router.get("/admin/{user_id}", response_model=CustomerDetailResponse)
def get_customer_detail(user_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Customer not found")

    r = result.data

    # Count total rides for this customer
    try:
        rides_result = (
            sb.table("rides")
            .select("id, price", count="exact")
            .eq("customer_id", user_id)
            .execute()
        )
        total_rides = len(rides_result.data or [])
        total_spent = sum(
            (float(r["price"]) for r in (rides_result.data or []) if r.get("price")),
            0.0,
        )
    except Exception:
        total_rides = 0
        total_spent = 0.0

    row_fields = {k: v for k, v in r.items() if k in _CUSTOMER_FIELDS}
    return CustomerDetailResponse(
        **row_fields,
        total_rides=total_rides,
        total_spent=total_spent,
    )


@router.patch("/admin/{user_id}/ban")
def ban_customer(user_id: str, body: Optional[BanUnbanBody] = None, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = sb.table("users").update({"is_active": False}).eq("id", user_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Customer not found")
    return {"message": "Customer banned", "reason": body.reason if body else None}


@router.patch("/admin/{user_id}/unban")
def unban_customer(user_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = sb.table("users").update({"is_active": True}).eq("id", user_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Customer not found")
    return {"message": "Customer unbanned"}


@router.get("/admin/{user_id}/trips")
def get_customer_trips(
    user_id: str,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("rides")
            .select("id, status, picking_point, destination, price, created_at, driver_id")
            .eq("customer_id", user_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        count_result = (
            sb.table("rides")
            .select("id", count="exact")
            .eq("customer_id", user_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"trips": result.data or [], "total": count_result.count or 0}


@router.get("/admin/{user_id}/saved-addresses")
def get_customer_saved_addresses(user_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = (
            sb.table("saved_addresses")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"addresses": result.data or []}


@router.get("/admin/{user_id}/emergency-contacts")
def get_customer_emergency_contacts(user_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = (
            sb.table("emergency_contacts")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"contacts": result.data or []}


@router.get("/admin/{user_id}/notifications")
def get_customer_notifications(
    user_id: str,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("notifications")
            .select("id, notification_type, title, content, status, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        count_result = (
            sb.table("notifications")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"notifications": result.data or [], "total": count_result.count or 0}


@router.get("/admin/{user_id}/activity")
def get_customer_activity(
    user_id: str,
    limit: int = Query(30, ge=1, le=100),
    _user=Depends(require_admin),
):
    """Merged activity feed: recent rides + recent notifications."""
    sb = get_supabase()
    events: List[dict] = []

    try:
        rides = (
            sb.table("rides")
            .select("id, status, picking_point, destination, price, created_at")
            .eq("customer_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        for r in rides.data or []:
            pickup = (r.get("picking_point") or {}).get("name", "")
            dropoff = (r.get("destination") or {}).get("name", "")
            events.append({
                "type": "ride",
                "id": r["id"],
                "summary": f"Ride {r.get('status', '')} — {pickup} → {dropoff}",
                "amount": r.get("price"),
                "created_at": r.get("created_at"),
            })
    except Exception:
        pass

    try:
        notifs = (
            sb.table("notifications")
            .select("id, notification_type, title, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        for n in notifs.data or []:
            events.append({
                "type": "notification",
                "id": n["id"],
                "summary": n.get("title", ""),
                "notification_type": n.get("notification_type"),
                "created_at": n.get("created_at"),
            })
    except Exception:
        pass

    events.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"events": events[:limit]}


@router.get("/admin/{user_id}/gdpr/export")
def gdpr_export_customer(user_id: str, _user=Depends(require_admin)):
    """Return a JSON dump of all personal data for this customer."""
    sb = get_supabase()
    data: dict[str, Any] = {}

    try:
        ur = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
        data["profile"] = ur.data or {}
    except Exception:
        data["profile"] = {}

    try:
        rides = sb.table("rides").select("*").eq("customer_id", user_id).execute()
        data["rides"] = rides.data or []
    except Exception:
        data["rides"] = []

    try:
        addrs = sb.table("saved_addresses").select("*").eq("user_id", user_id).execute()
        data["saved_addresses"] = addrs.data or []
    except Exception:
        data["saved_addresses"] = []

    try:
        contacts = sb.table("emergency_contacts").select("*").eq("user_id", user_id).execute()
        data["emergency_contacts"] = contacts.data or []
    except Exception:
        data["emergency_contacts"] = []

    try:
        notifs = sb.table("notifications").select("*").eq("user_id", user_id).execute()
        data["notifications"] = notifs.data or []
    except Exception:
        data["notifications"] = []

    return data


@router.post("/admin/{user_id}/gdpr/erasure-request", status_code=201)
def request_gdpr_erasure(user_id: str, _user=Depends(require_admin)):
    """Create a pending GDPR erasure request for this customer."""
    sb = get_supabase()
    try:
        existing = (
            sb.table("gdpr_erasure_requests")
            .select("id, status")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .maybe_single()
            .execute()
        )
        if existing.data:
            return {"id": existing.data["id"], "message": "Pending request already exists"}

        req_id = str(uuid4())
        sb.table("gdpr_erasure_requests").insert({
            "id": req_id,
            "user_id": user_id,
            "requested_by_admin": _user.get("id"),
            "status": "pending",
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"id": req_id, "message": "Erasure request created"}


@router.get("/admin/{user_id}/gdpr/erasure-requests")
def list_gdpr_erasure_requests(user_id: str, _user=Depends(require_admin)):
    sb = get_supabase()
    try:
        result = (
            sb.table("gdpr_erasure_requests")
            .select("*")
            .eq("user_id", user_id)
            .order("requested_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"requests": result.data or []}


@router.patch("/admin/{user_id}/gdpr/erasure-requests/{req_id}/approve")
def approve_gdpr_erasure(user_id: str, req_id: str, _user=Depends(require_admin)):
    """Mark an erasure request as processed (actual deletion is handled outside)."""
    sb = get_supabase()
    try:
        result = (
            sb.table("gdpr_erasure_requests")
            .update({
                "status": "approved",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", req_id)
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Erasure request not found")
    return {"message": "Erasure request approved"}
