from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from app.core.dependencies import require_admin
from app.core.supabase import call_rpc, first_row, get_supabase
from app.services.notifications.router import send_push_to_users

router = APIRouter(prefix="/drivers", tags=["drivers"])


class DriverAdminListItem(BaseModel):
    id: str
    user_id: str
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    license_number: str
    license_expiry: str
    vehicle_type: Optional[str] = None
    verification_status: str
    is_online: bool = False
    rating: float = 0.0
    total_trips: int = 0
    credit_balance: float = 0.0
    submitted_at: Optional[str] = None
    activation_date: Optional[str] = None
    verification_feedback: Optional[str] = None
    is_suspended: bool = False
    created_at: str


class VehicleDetailsResponse(BaseModel):
    id: str
    vehicle_type: str
    license_plate: str
    make: str
    model: str
    year: int
    color: str
    passenger_capacity: Optional[int] = None
    has_air_conditioning: Optional[bool] = None
    provides_helmet: Optional[bool] = None


class DriverDocumentResponse(BaseModel):
    id: str
    document_type: str
    file_url: str
    status: str


class DriverDetailResponse(DriverAdminListItem):
    vehicle: Optional[VehicleDetailsResponse] = None
    documents: List[DriverDocumentResponse] = []


class DriverProfileFullResponse(DriverDetailResponse):
    """Full driver profile returned to both admin and driver-facing lookups.
    Includes verification_feedback so drivers can see the exact rejection reason."""
    verification_feedback: Optional[str] = None


class DriverAdminListResponse(BaseModel):
    drivers: List[DriverAdminListItem]
    total: int


_DRIVER_FIELDS = {f for f in DriverAdminListItem.model_fields if f not in ("full_name", "phone_number", "total_trips")}


@router.get("/admin/list", response_model=DriverAdminListResponse)
def list_drivers(
    verification_status: Optional[str] = Query(None),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        query = sb.table("driver_profiles").select(
            "*, users(full_name, phone_number)"
        )
        if verification_status:
            query = query.eq("verification_status", verification_status)
        result = query.order("created_at", desc=True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    drivers = []
    for r in result.data or []:
        user_info = r.pop("users", {}) or {}
        row_fields = {k: v for k, v in r.items() if k in _DRIVER_FIELDS}
        drivers.append(DriverAdminListItem(
            **row_fields,
            full_name=user_info.get("full_name"),
            phone_number=user_info.get("phone_number"),
            total_trips=r.get("total_rides", 0) or 0,
        ))

    return DriverAdminListResponse(drivers=drivers, total=len(drivers))


@router.get("/by-user/{user_id}", response_model=DriverProfileFullResponse)
def get_driver_by_user(user_id: str, _user=Depends(require_admin)):
    """Fetch a driver's full profile by their user_id (auth uid)."""
    try:
        sb = get_supabase()
        result = (
            sb.table("driver_profiles")
            .select("*, users(full_name, phone_number), vehicle_details(*), driver_documents(*)")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Driver profile not found for this user")

    row = result.data
    user_info = row.pop("users", {}) or {}
    vehicle_data = row.pop("vehicle_details", None)
    if isinstance(vehicle_data, list):
        vehicle_data = vehicle_data[0] if vehicle_data else None
    documents = row.pop("driver_documents", []) or []
    base_fields = {k: v for k, v in row.items() if k in _DRIVER_FIELDS and k != "verification_feedback"}

    return DriverProfileFullResponse(
        **base_fields,
        verification_feedback=row.get("verification_feedback"),
        full_name=user_info.get("full_name"),
        phone_number=user_info.get("phone_number"),
        total_trips=row.get("total_rides", 0) or 0,
        vehicle=VehicleDetailsResponse(**vehicle_data) if vehicle_data else None,
        documents=[DriverDocumentResponse(**doc) for doc in documents],
    )


@router.get("/{driver_id}", response_model=DriverProfileFullResponse)
def get_driver_detail(driver_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = (
            sb.table("driver_profiles")
            .select("*, users(full_name, phone_number), vehicle_details(*), driver_documents(*)")
            .eq("id", driver_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Driver not found")

    row = result.data
    user_info = row.pop("users", {}) or {}
    vehicle_data = row.pop("vehicle_details", None)
    if isinstance(vehicle_data, list):
        vehicle_data = vehicle_data[0] if vehicle_data else None
    documents = row.pop("driver_documents", []) or []
    base_fields = {k: v for k, v in row.items() if k in _DRIVER_FIELDS and k != "verification_feedback"}

    return DriverProfileFullResponse(
        **base_fields,
        verification_feedback=row.get("verification_feedback"),
        full_name=user_info.get("full_name"),
        phone_number=user_info.get("phone_number"),
        total_trips=row.get("total_rides", 0) or 0,
        vehicle=VehicleDetailsResponse(**vehicle_data) if vehicle_data else None,
        documents=[DriverDocumentResponse(**doc) for doc in documents],
    )


@router.patch("/{driver_id}/activate")
def activate_driver(driver_id: str, _user=Depends(require_admin)):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")

    try:
        result = first_row(
            call_rpc("approve_driver", {"p_driver_id": driver_id, "p_admin_id": admin_id})
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")
    return result


@router.patch("/{driver_id}/deactivate")
def deactivate_driver(
    driver_id: str,
    feedback: Optional[str] = Query(None),
    _user=Depends(require_admin),
):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")

    try:
        result = first_row(
            call_rpc(
                "reject_driver",
                {
                    "p_driver_id": driver_id,
                    "p_reason": feedback,
                    "p_admin_id": admin_id,
                },
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")
    return result


class RejectDriverBody(BaseModel):
    feedback: Optional[str] = None
    reason: Optional[str] = None


@router.patch("/{driver_id}/reject")
def reject_driver(driver_id: str, body: RejectDriverBody, _user=Depends(require_admin)):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")
    reason = body.feedback or body.reason
    try:
        result = first_row(
            call_rpc("reject_driver", {"p_driver_id": driver_id, "p_reason": reason, "p_admin_id": admin_id})
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")
    return result


class SuspendDriverBody(BaseModel):
    reason: str
    end_date: Optional[str] = None
    appeal_url: Optional[str] = None


@router.patch("/{driver_id}/suspend")
def suspend_driver(driver_id: str, body: SuspendDriverBody, _user=Depends(require_admin)):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")
    try:
        result = first_row(
            call_rpc(
                "reject_driver",
                {"p_driver_id": driver_id, "p_reason": body.reason, "p_admin_id": admin_id},
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")

    # Push notification to driver
    try:
        sb = get_supabase()
        dp = sb.table("driver_profiles").select("user_id").eq("id", driver_id).maybe_single().execute()
        if dp.data and dp.data.get("user_id"):
            msg = f"Your driver account has been suspended. Reason: {body.reason}"
            send_push_to_users(
                [dp.data["user_id"]], "Account Suspended", msg,
                notification_type="driver_update", persist=False,
            )
    except Exception:
        pass

    return result


@router.patch("/{driver_id}/unsuspend")
def unsuspend_driver(driver_id: str, _user=Depends(require_admin)):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")
    try:
        result = first_row(
            call_rpc("approve_driver", {"p_driver_id": driver_id, "p_admin_id": admin_id})
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")

    # Push notification to driver
    try:
        sb = get_supabase()
        dp = sb.table("driver_profiles").select("user_id").eq("id", driver_id).maybe_single().execute()
        if dp.data and dp.data.get("user_id"):
            send_push_to_users(
                [dp.data["user_id"]],
                "Account Reinstated",
                "Your driver account has been reinstated. You can go online and accept rides again.",
                notification_type="driver_update",
                persist=False,
            )
    except Exception:
        pass

    return result


@router.patch("/{driver_id}/block")
def block_driver(driver_id: str, _user=Depends(require_admin)):
    """Block driver if wallet balance is zero or negative (calls block_driver_if_no_balance RPC)."""
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")
    try:
        result = first_row(
            call_rpc(
                "block_driver_if_no_balance",
                {"p_driver_id": driver_id, "p_admin_id": admin_id},
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")
    return result


class CategoryUpdateBody(BaseModel):
    category: str


@router.patch("/{driver_id}/category")
def update_driver_category(driver_id: str, body: CategoryUpdateBody, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        sb.table("driver_profiles").update({"vehicle_type": body.category}).eq("id", driver_id).execute()
        result = sb.table("driver_profiles").select(
            "*, users(full_name, phone_number)"
        ).eq("id", driver_id).maybe_single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.data:
        raise HTTPException(status_code=404, detail="Driver not found")
    return result.data


class DocumentRejectBody(BaseModel):
    reason: str


@router.patch("/{driver_id}/documents/{document_id}/approve")
def approve_driver_document(driver_id: str, document_id: str, _user=Depends(require_admin)):
    """Approve an individual driver document."""
    try:
        sb = get_supabase()
        result = (
            sb.table("driver_documents")
            .update({"status": "approved"})
            .eq("id", document_id)
            .eq("driver_id", driver_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.data:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"message": "Document approved", "document_id": document_id}


@router.patch("/{driver_id}/documents/{document_id}/reject")
def reject_driver_document(driver_id: str, document_id: str, body: DocumentRejectBody, _user=Depends(require_admin)):
    """Reject an individual driver document with a reason."""
    try:
        sb = get_supabase()
        result = (
            sb.table("driver_documents")
            .update({"status": "rejected", "rejection_reason": body.reason})
            .eq("id", document_id)
            .eq("driver_id", driver_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.data:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"message": "Document rejected", "document_id": document_id, "reason": body.reason}


@router.delete("/{driver_id}")
def delete_driver(driver_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        sb.table("driver_profiles").delete().eq("id", driver_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"message": "Driver deleted"}


@router.get("/{driver_id}/trips")
def get_driver_trips(
    driver_id: str,
    limit: int = Query(50),
    offset: int = Query(0),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("rides")
            .select("*")
            .eq("driver_id", driver_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"trips": result.data or [], "total": len(result.data or [])}


@router.get("/{driver_id}/earnings")
def get_driver_earnings(
    driver_id: str,
    limit: int = Query(50),
    offset: int = Query(0),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("wallet_transactions")
            .select("*")
            .eq("driver_id", driver_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        balance_result = (
            sb.table("driver_profiles")
            .select("credit_balance")
            .eq("id", driver_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    balance = (balance_result.data or {}).get("credit_balance", 0)
    return {"transactions": result.data or [], "current_balance": balance}


@router.get("/{driver_id}/ratings")
def get_driver_ratings(
    driver_id: str,
    limit: int = Query(50),
    offset: int = Query(0),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("ride_ratings")
            .select("*")
            .eq("driver_id", driver_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        profile = (
            sb.table("driver_profiles")
            .select("rating, total_rides")
            .eq("id", driver_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "ratings": result.data or [],
        "average_rating": (profile.data or {}).get("rating", 0),
        "total_trips": (profile.data or {}).get("total_rides", 0),
    }


@router.get("/{driver_id}/compliance")
def get_driver_compliance(driver_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        docs = sb.table("driver_documents").select("*").eq("driver_id", driver_id).execute()
        profile = (
            sb.table("driver_profiles")
            .select("license_number, license_expiry, verification_status")
            .eq("id", driver_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "documents": docs.data or [],
        "license_number": (profile.data or {}).get("license_number"),
        "license_expiry": (profile.data or {}).get("license_expiry"),
        "verification_status": (profile.data or {}).get("verification_status"),
    }


@router.get("/{driver_id}/activity")
def get_driver_activity(
    driver_id: str,
    limit: int = Query(50),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        rides = (
            sb.table("rides")
            .select("id, status, created_at, completed_at, cancelled_at")
            .eq("driver_id", driver_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"activity": rides.data or []}
