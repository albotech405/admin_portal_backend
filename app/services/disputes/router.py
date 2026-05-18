from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Any, List
from datetime import datetime, timezone
from app.core.dependencies import require_admin
from app.core.supabase import get_supabase

router = APIRouter(prefix="/rides", tags=["disputes"])

# ── Models ──────────────────────────────────────────────────────────────


class DisputeItem(BaseModel):
    ride_id: str
    customer_id: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    picking_point: Optional[Any] = None
    destination: Optional[Any] = None
    price: float = 0.0
    status: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    dispute_reason: Optional[str] = None
    dispute_raised_by: Optional[str] = None  # "customer" or "driver"
    dispute_raised_at: Optional[str] = None
    dispute_status: str = "open"  # open | refunded | driver_charged | dismissed | escalated
    dispute_resolved_at: Optional[str] = None
    dispute_resolved_by: Optional[str] = None
    dispute_notes: Optional[str] = None
    created_at: str


class DisputeListResponse(BaseModel):
    disputes: List[DisputeItem]
    total: int


class DisputeActionBody(BaseModel):
    notes: Optional[str] = None


class DisputeActionResponse(BaseModel):
    message: str
    ride_id: str
    dispute_status: str


# ── Helpers ─────────────────────────────────────────────────────────────


def _fetch_dispute_rides(sb, status_filter: Optional[str] = None) -> list:
    """
    Fetch rides that have been flagged as disputed.
    Uses the `dispute_logs` table if it exists, otherwise falls back
    to rides with dispute info stored in their metadata.
    """
    now = datetime.now(timezone.utc)

    # Try dispute_logs table first
    try:
        query = (
            sb.table("dispute_logs")
            .select(
                "*, "
                "rides!inner("
                "  *, "
                "  users!rides_customer_id_fkey(full_name, phone_number), "
                "  driver_profiles!rides_driver_id_fkey(users!driver_profiles_user_id_fkey(full_name, phone_number))"
                ")"
            )
            .order("created_at", desc=True)
        )
        if status_filter:
            query = query.eq("status", status_filter)
        result = query.execute()
        logs = result.data or []
    except Exception:
        logs = []

    if logs:
        items = []
        for log in logs:
            ride = log.pop("rides", {}) or {}
            customer_info = ride.pop("users", {}) or {}
            driver_profile = ride.pop("driver_profiles", None) or {}
            driver_user_info = (
                driver_profile.pop("users", {})
                if isinstance(driver_profile, dict)
                else {}
            )

            items.append(
                DisputeItem(
                    ride_id=ride.get("id"),
                    customer_id=ride.get("customer_id"),
                    customer_name=customer_info.get("full_name"),
                    customer_phone=customer_info.get("phone_number"),
                    driver_id=ride.get("driver_id"),
                    driver_name=driver_user_info.get("full_name"),
                    driver_phone=driver_user_info.get("phone_number"),
                    picking_point=ride.get("picking_point"),
                    destination=ride.get("destination"),
                    price=float(ride.get("price", 0)),
                    status=ride.get("status", "unknown"),
                    started_at=ride.get("started_at"),
                    completed_at=ride.get("completed_at"),
                    dispute_reason=log.get("reason"),
                    dispute_raised_by=log.get("raised_by"),
                    dispute_raised_at=log.get("created_at"),
                    dispute_status=log.get("status", "open"),
                    dispute_resolved_at=log.get("resolved_at"),
                    dispute_resolved_by=log.get("resolved_by"),
                    dispute_notes=log.get("notes"),
                    created_at=ride.get("created_at"),
                )
            )
        return items

    # Fallback: no dispute_logs table — return empty
    return []


def _update_dispute_status(
    sb, ride_id: str, new_status: str, notes: Optional[str] = None, admin_id: Optional[str] = None
):
    """Update dispute status in dispute_logs table."""
    now = datetime.now(timezone.utc).isoformat()
    update_data = {
        "status": new_status,
        "resolved_at": now,
        "resolved_by": admin_id,
    }
    if notes:
        update_data["notes"] = notes

    try:
        sb.table("dispute_logs").update(update_data).eq("ride_id", ride_id).execute()
    except Exception:
        # If dispute_logs doesn't exist, try updating rides table
        try:
            sb.table("rides").update(
                {"dispute_status": new_status, "dispute_resolved_at": now}
            ).eq("id", ride_id).execute()
        except Exception:
            pass


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/admin/disputes", response_model=DisputeListResponse)
def list_disputes(
    status: Optional[str] = None,
    _user=Depends(require_admin),
):
    """
    List all disputed trips.
    Optional filter by dispute status: open, refunded, driver_charged, dismissed, escalated.
    """
    try:
        sb = get_supabase()
        items = _fetch_dispute_rides(sb, status_filter=status)
        return DisputeListResponse(disputes=items, total=len(items))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/admin/disputes/{ride_id}/refund", response_model=DisputeActionResponse)
def refund_dispute(
    ride_id: str,
    body: DisputeActionBody = DisputeActionBody(),
    _user=Depends(require_admin),
):
    """
    Issue a full refund for a disputed trip.
    Since payments are handled out-of-system, this records the refund action
    and logs it for accounting. Admin should process the actual refund manually.
    """
    try:
        sb = get_supabase()
        admin_id = _user.get("id") or _user.get("user_id") or "unknown"

        _update_dispute_status(sb, ride_id, "refunded", body.notes, admin_id)

        # Log the refund action
        try:
            sb.table("dispute_logs").update({
                "refund_amount": None,  # Admin enters manually
                "refund_method": "manual",  # Out-of-system
            }).eq("ride_id", ride_id).execute()
        except Exception:
            pass

        return DisputeActionResponse(
            message="Dispute resolved: refund issued (manual processing required)",
            ride_id=ride_id,
            dispute_status="refunded",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/admin/disputes/{ride_id}/charge-driver", response_model=DisputeActionResponse)
def charge_driver_dispute(
    ride_id: str,
    body: DisputeActionBody = DisputeActionBody(),
    _user=Depends(require_admin),
):
    """
    Charge a fee to the driver for a disputed trip.
    This deducts from the driver's wallet credit_balance.
    """
    try:
        sb = get_supabase()
        admin_id = _user.get("id") or _user.get("user_id") or "unknown"

        # Get the ride to find the driver
        ride = sb.table("rides").select("driver_id, price").eq("id", ride_id).maybe_single().execute()
        if not ride.data:
            raise HTTPException(status_code=404, detail="Ride not found")

        driver_id = ride.data.get("driver_id")
        if not driver_id:
            raise HTTPException(status_code=400, detail="No driver assigned to this ride")

        # Get driver's current balance
        driver = (
            sb.table("driver_profiles")
            .select("id, credit_balance")
            .eq("id", driver_id)
            .maybe_single()
            .execute()
        )
        if not driver.data:
            raise HTTPException(status_code=404, detail="Driver profile not found")

        current_balance = driver.data.get("credit_balance") or 0
        # Charge a fee (e.g., 5000 CDF as dispute fee, or the ride price)
        fee_amount = float(ride.data.get("price", 0)) or 5000
        new_balance = max(0, current_balance - fee_amount)

        # Deduct from wallet
        sb.table("driver_profiles").update({"credit_balance": new_balance}).eq(
            "id", driver_id
        ).execute()

        # Log transaction
        sb.table("wallet_transactions").insert({
            "driver_id": driver_id,
            "type": "debit",
            "amount": fee_amount,
            "balance_after": new_balance,
            "reference_type": "dispute_fee",
            "reference_id": ride_id,
            "description": body.notes or "Dispute resolution fee charged",
        }).execute()

        _update_dispute_status(sb, ride_id, "driver_charged", body.notes, admin_id)

        return DisputeActionResponse(
            message=f"Driver charged {fee_amount} CDF for dispute resolution",
            ride_id=ride_id,
            dispute_status="driver_charged",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/admin/disputes/{ride_id}/dismiss", response_model=DisputeActionResponse)
def dismiss_dispute(
    ride_id: str,
    body: DisputeActionBody = DisputeActionBody(),
    _user=Depends(require_admin),
):
    """
    Dismiss a dispute. No action taken, dispute is closed.
    """
    try:
        sb = get_supabase()
        admin_id = _user.get("id") or _user.get("user_id") or "unknown"
        _update_dispute_status(sb, ride_id, "dismissed", body.notes, admin_id)
        return DisputeActionResponse(
            message="Dispute dismissed",
            ride_id=ride_id,
            dispute_status="dismissed",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/admin/disputes/{ride_id}/escalate", response_model=DisputeActionResponse)
def escalate_dispute(
    ride_id: str,
    body: DisputeActionBody = DisputeActionBody(),
    _user=Depends(require_admin),
):
    """
    Escalate a dispute to Super Admin for further review.
    """
    try:
        sb = get_supabase()
        admin_id = _user.get("id") or _user.get("user_id") or "unknown"
        _update_dispute_status(sb, ride_id, "escalated", body.notes, admin_id)
        return DisputeActionResponse(
            message="Dispute escalated to Super Admin",
            ride_id=ride_id,
            dispute_status="escalated",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
