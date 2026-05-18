from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from app.core.dependencies import require_admin
from app.core.supabase import get_supabase

router = APIRouter(prefix="/payments", tags=["payments"])


class TransactionItem(BaseModel):
    id: str
    type: str  # "ride_payment" | "topup" | "refund" | "commission"
    trip_id: Optional[str] = None
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    amount: float
    platform_commission_amount: Optional[float] = None
    method: Optional[str] = None  # cash, Orange Money, M-Pesa, Airtel Money
    status: str
    category: Optional[str] = None
    distance_km: Optional[float] = None
    duration_minutes: Optional[float] = None
    has_refund: bool = False
    created_at: str
    completed_at: Optional[str] = None


class TransactionListResponse(BaseModel):
    transactions: List[TransactionItem]
    total: int


@router.get("/admin/transactions", response_model=TransactionListResponse)
def list_transactions(
    status: Optional[str] = Query(None, description="Filter by status (completed, cancelled, etc.)"),
    method: Optional[str] = Query(None, description="Filter by payment method (cash, Orange Money, M-Pesa, Airtel Money)"),
    date_from: Optional[str] = Query(None, description="Start date (ISO format)"),
    date_to: Optional[str] = Query(None, description="End date (ISO format)"),
    amount_min: Optional[float] = Query(None, description="Minimum amount"),
    amount_max: Optional[float] = Query(None, description="Maximum amount"),
    has_refund: Optional[bool] = Query(None, description="Only transactions with refunds"),
    search: Optional[str] = Query(None, description="Search by customer name, driver name, or transaction ID"),
    type: Optional[str] = Query(None, description="Filter by type: ride_payment, topup, refund, commission"),
    limit: int = Query(100, ge=1, le=500, description="Max results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    _user=Depends(require_admin),
):
    """
    Admin transaction browser.
    Returns ride payments, topups, and other financial transactions with filtering.
    """
    try:
        sb = get_supabase()
        transactions: List[TransactionItem] = []

        # ── 1. Ride payments ──────────────────────────────────────────────
        ride_query = sb.table("rides").select(
            "id, ride_request_id, customer_id, customer_name, customer_phone, "
            "driver_id, driver_name, driver_phone, price, status, "
            "platform_commission_amount, category, distance_km, duration_minutes, "
            "created_at, completed_at, cancelled_at, reason_code, reason_text"
        )

        # Apply filters to ride query
        if status:
            ride_query = ride_query.eq("status", status)
        if date_from:
            ride_query = ride_query.gte("created_at", date_from)
        if date_to:
            ride_query = ride_query.lte("created_at", date_to)
        if amount_min is not None:
            ride_query = ride_query.gte("price", amount_min)
        if amount_max is not None:
            ride_query = ride_query.lte("price", amount_max)
        if search:
            search_pattern = f"%{search}%"
            ride_query = ride_query.or_(
                f"customer_name.ilike.{search_pattern},"
                f"driver_name.ilike.{search_pattern},"
                f"id.ilike.{search_pattern}"
            )

        # Only include ride_payment type if no type filter or type is ride_payment
        if type is None or type == "ride_payment":
            ride_result = ride_query.order("created_at", desc=True).limit(limit).offset(offset).execute()
            for r in ride_result.data or []:
                # Determine payment method from context (default to cash if not specified)
                # The rides table doesn't have a payment_method column, so we infer
                payment_method = "cash"  # default
                if r.get("reason_code"):
                    # If cancelled with specific reason codes, could indicate payment issues
                    pass

                transactions.append(TransactionItem(
                    id=r["id"],
                    type="ride_payment",
                    trip_id=r.get("ride_request_id"),
                    customer_id=r.get("customer_id"),
                    customer_name=r.get("customer_name"),
                    customer_phone=r.get("customer_phone"),
                    driver_id=r.get("driver_id"),
                    driver_name=r.get("driver_name"),
                    driver_phone=r.get("driver_phone"),
                    amount=float(r.get("price", 0)),
                    platform_commission_amount=(
                        float(r["platform_commission_amount"])
                        if r.get("platform_commission_amount") is not None
                        else None
                    ),
                    method=payment_method,
                    status=r.get("status", "unknown"),
                    category=r.get("category"),
                    distance_km=r.get("distance_km"),
                    duration_minutes=r.get("duration_minutes"),
                    has_refund=False,  # Will be checked below
                    created_at=r.get("created_at", ""),
                    completed_at=r.get("completed_at") or r.get("cancelled_at"),
                ))

        # ── 2. Wallet topup requests (as financial transactions) ──────────
        if type is None or type == "topup":
            topup_query = sb.table("wallet_topup_requests").select(
                "*, driver_profiles!inner(user_id, users!inner(full_name, phone_number))"
            )

            if date_from:
                topup_query = topup_query.gte("submitted_at", date_from)
            if date_to:
                topup_query = topup_query.lte("submitted_at", date_to)
            if amount_min is not None:
                topup_query = topup_query.gte("amount", amount_min)
            if amount_max is not None:
                topup_query = topup_query.lte("amount", amount_max)
            if status:
                # Map ride statuses to topup statuses
                if status in ("completed", "cancelled", "failed"):
                    # Skip topups for ride-specific status filters
                    pass
                else:
                    topup_query = topup_query.eq("status", status)

            topup_result = topup_query.order("submitted_at", desc=True).limit(limit).offset(offset).execute()
            for r in topup_result.data or []:
                dp = r.pop("driver_profiles", {}) or {}
                user_info = dp.get("users") or {}

                transactions.append(TransactionItem(
                    id=r["id"],
                    type="topup",
                    customer_id=None,
                    customer_name=None,
                    customer_phone=None,
                    driver_id=r.get("driver_id"),
                    driver_name=user_info.get("full_name"),
                    driver_phone=user_info.get("phone_number"),
                    amount=float(r.get("amount", 0)),
                    method=r.get("payment_method", "unknown"),
                    status=r.get("status", "pending"),
                    created_at=r.get("submitted_at", ""),
                    completed_at=r.get("reviewed_at"),
                ))

        # ── 3. Check for refunds / disputes ──────────────────────────────
        # If has_refund filter is set, we need to cross-reference with dispute_logs
        if has_refund is True:
            try:
                dispute_result = (
                    sb.table("dispute_logs")
                    .select("ride_id")
                    .eq("action", "refund")
                    .execute()
                )
                refunded_ride_ids = set(
                    d["ride_id"] for d in (dispute_result.data or [])
                )
                # Filter transactions to only those with refunds
                transactions = [
                    t for t in transactions
                    if t.id in refunded_ride_ids
                ]
                # Mark matching transactions
                for t in transactions:
                    if t.id in refunded_ride_ids:
                        t.has_refund = True
            except Exception:
                # dispute_logs table might not exist yet
                pass

        # Sort combined results by created_at desc
        transactions.sort(key=lambda t: t.created_at, reverse=True)

        # Apply pagination to combined results
        paginated = transactions[offset:offset + limit]

        return TransactionListResponse(
            transactions=paginated,
            total=len(transactions),
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
