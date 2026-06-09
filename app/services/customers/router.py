import re
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Any
from datetime import datetime, timezone
from uuid import uuid4
from app.core.dependencies import require_admin, require_role
from app.core.supabase import get_supabase
from app.services.audit.router import write_audit_log

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
    has_driver_profile: bool = False
    linked_driver_id: Optional[str] = None
    platform_roles: List[str] = Field(default_factory=lambda: ["customer"])
    linked_driver: Optional[dict] = None


class CustomerAdminListResponse(BaseModel):
    customers: List[CustomerAdminItem]
    total: int


class CustomerDetailResponse(CustomerAdminItem):
    total_rides: int = 0
    total_spent: float = 0.0
    privacy_preferences: Optional[dict] = None


class BanUnbanBody(BaseModel):
    reason: Optional[str] = None


class CreateCustomerRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100)
    phone_number: str = Field(..., min_length=7, max_length=20)
    email: Optional[str] = None

    @field_validator("phone_number")
    @classmethod
    def phone_digits_only(cls, v: str) -> str:
        if not re.match(r"^\+?[\d\s\-]{7,20}$", v):
            raise ValueError("Invalid phone number format")
        return v


class CreateCustomerResponse(BaseModel):
    id: str
    full_name: str
    phone_number: str
    role: str
    otp_sent: bool


def _email_or_placeholder(email: Optional[str], phone: str) -> str:
    if email:
        return email
    digits = re.sub(r"\D", "", phone)
    return f"{digits}@noemail.placeholder.local"


_CUSTOMER_FIELDS = {
    "id", "full_name", "phone_number", "email", "is_active", "is_admin",
    "gender", "profile_image_url", "customer_rating", "total_customer_ratings",
    "created_at", "updated_at", "privacy_preferences",
}


def _build_driver_link_map(sb, user_ids: List[str]) -> dict[str, dict]:
    if not user_ids:
        return {}

    result = (
        sb.table("driver_profiles")
        .select("id, user_id, verification_status")
        .in_("user_id", user_ids)
        .execute()
    )
    return {
        str(row.get("user_id")): {
            "linked_driver_id": str(row.get("id")),
            "linked_driver": {
                "id": str(row.get("id")),
                "verification_status": row.get("verification_status"),
            },
        }
        for row in (result.data or [])
        if row.get("user_id") and row.get("id")
    }


def _customer_identity_fields(driver_link: Optional[dict]) -> dict:
    has_driver_profile = bool(driver_link)
    platform_roles = ["customer"]
    if has_driver_profile:
        platform_roles.append("driver")

    return {
        "has_driver_profile": has_driver_profile,
        "linked_driver_id": driver_link.get("linked_driver_id") if driver_link else None,
        "platform_roles": platform_roles,
        "linked_driver": driver_link.get("linked_driver") if driver_link else None,
    }


@router.post(
    "/admin/create",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateCustomerResponse,
    dependencies=[Depends(require_role("operations", "super_admin"))],
)
def create_customer(body: CreateCustomerRequest, _user: dict = Depends(require_role("operations", "super_admin"))):
    sb = get_supabase()

    # 1. Check phone uniqueness
    try:
        phone_check = sb.table("users").select("id").eq("phone_number", body.phone_number).limit(1).execute()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
    if phone_check.data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Phone number already registered")

    # 2. Create Supabase Auth user (phone-only, no password — OTP login)
    try:
        from gotrue.types import AdminUserAttributes
        auth_result = sb.auth.admin.create_user(
            AdminUserAttributes(phone=body.phone_number, phone_confirm=True)
        )
        auth_uid = auth_result.user.id
    except Exception as exc:
        msg = str(exc).lower()
        if "already registered" in msg or "already been registered" in msg:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Phone number already registered")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    # 3. Insert into users table
    new_user_id = str(uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        sb.table("users").insert({
            "id": new_user_id,
            "supabase_uid": auth_uid,
            "full_name": body.full_name,
            "phone_number": body.phone_number,
            "email": body.email,
            "role": "customer",
            "is_active": True,
            "created_at": now_iso,
            "updated_at": now_iso,
        }).execute()
    except Exception as exc:
        # Best-effort: clean up auth user so we don't leave orphaned auth accounts
        try:
            sb.auth.admin.delete_user(auth_uid)
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    # 4. Insert into customer_profiles if the table exists
    try:
        sb.table("customer_profiles").insert({
            "user_id": new_user_id,
            "created_at": now_iso,
        }).execute()
    except Exception:
        pass  # Table may not exist; non-fatal

    # 5. Trigger OTP — sends SMS via Twilio; non-fatal if it fails
    otp_sent = True
    try:
        sb.auth.sign_in_with_otp({"phone": body.phone_number})
    except Exception as otp_exc:
        import logging as _logging
        _logging.getLogger(__name__).warning("OTP send failed for %s: %s", body.phone_number, otp_exc)
        otp_sent = False

    # 6. Audit log
    write_audit_log(
        sb=sb,
        admin_user=_user,
        action_type="manual_user_create",
        entity_type="users",
        entity_id=new_user_id,
        summary=f"Admin manually created customer: {body.full_name} ({body.phone_number})",
        after_state={"full_name": body.full_name, "phone_number": body.phone_number, "role": "customer"},
    )

    return CreateCustomerResponse(
        id=new_user_id,
        full_name=body.full_name,
        phone_number=body.phone_number,
        role="customer",
        otp_sent=otp_sent,
    )


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

    user_ids = [str(row.get("id")) for row in (result.data or []) if row.get("id")]
    try:
        driver_link_map = _build_driver_link_map(sb, user_ids)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    customers = []
    for r in result.data or []:
        row_fields = {k: v for k, v in r.items() if k in _CUSTOMER_FIELDS}
        identity_fields = _customer_identity_fields(driver_link_map.get(str(r.get("id"))))
        customers.append(CustomerAdminItem(**row_fields, **identity_fields))

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
    try:
        driver_link_map = _build_driver_link_map(sb, [user_id])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return CustomerDetailResponse(
        **row_fields,
        total_rides=total_rides,
        total_spent=total_spent,
        **_customer_identity_fields(driver_link_map.get(user_id)),
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
