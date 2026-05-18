from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from app.core.dependencies import require_admin
from app.core.supabase import call_rpc, first_row, get_supabase

router = APIRouter(prefix="/wallet", tags=["wallet"])


class TopupRequest(BaseModel):
    id: str
    driver_id: str
    amount: float
    status: str
    payment_method: Optional[str] = None
    proof_image_url: Optional[str] = None
    notes: Optional[str] = None
    submitted_at: str
    reviewed_at: Optional[str] = None
    reviewed_by: Optional[str] = None
    rejection_reason: Optional[str] = None
    reference_number: Optional[str] = None
    sender_name: Optional[str] = None
    mpesa_conversation_id: Optional[str] = None
    mpesa_transaction_id: Optional[str] = None
    full_name: Optional[str] = None
    phone_number: Optional[str] = None


class TopupRequestsResponse(BaseModel):
    requests: List[TopupRequest]
    total: int


class WalletTransaction(BaseModel):
    id: str
    driver_id: str
    type: str
    amount: float
    balance_after: float
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    description: Optional[str] = None
    created_at: str


class WalletBalanceResponse(BaseModel):
    driver_id: str
    balance: float


class WalletTransactionListResponse(BaseModel):
    transactions: List[WalletTransaction]


class RejectBody(BaseModel):
    rejection_reason: Optional[str] = None


@router.get("/admin/topup/requests", response_model=TopupRequestsResponse)
def list_topup_requests(
    status: Optional[str] = Query(None),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        query = sb.table("wallet_topup_requests").select(
            "*, driver_profiles(user_id, users(full_name, phone_number))"
        )
        if status:
            query = query.eq("status", status)
        result = query.order("submitted_at", desc=True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    rows = []
    for r in result.data or []:
        dp = r.pop("driver_profiles", {}) or {}
        user_info = (dp.get("users") or {})
        rows.append(TopupRequest(
            **r,
            full_name=user_info.get("full_name"),
            phone_number=user_info.get("phone_number"),
        ))

    return TopupRequestsResponse(requests=rows, total=len(rows))


@router.patch("/admin/topup/requests/{request_id}/approve")
def approve_topup(request_id: str, _user=Depends(require_admin)):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")

    try:
        result = first_row(
            call_rpc(
                "approve_wallet_topup",
                {"p_request_id": request_id, "p_admin_id": admin_id},
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result:
        raise HTTPException(status_code=404, detail="Request not found")

    return result


@router.patch("/admin/topup/requests/{request_id}/reject")
def reject_topup(request_id: str, body: Optional[RejectBody] = None, _user=Depends(require_admin)):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")

    try:
        result = first_row(
            call_rpc(
                "reject_wallet_topup",
                {
                    "p_request_id": request_id,
                    "p_admin_id": admin_id,
                    "p_reason": body.rejection_reason if body else None,
                },
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result:
        raise HTTPException(status_code=404, detail="Request not found")
    return result


@router.get("/admin/driver/{driver_id}/balance", response_model=WalletBalanceResponse)
def get_driver_balance(driver_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = sb.table("driver_profiles").select("id, credit_balance").eq("id", driver_id).maybe_single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Driver not found")
    return WalletBalanceResponse(driver_id=driver_id, balance=result.data.get("credit_balance") or 0)


@router.get("/admin/driver/{driver_id}/transactions", response_model=WalletTransactionListResponse)
def get_driver_transactions(driver_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = (
            sb.table("wallet_transactions")
            .select("*")
            .eq("driver_id", driver_id)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    transactions = [WalletTransaction(**t) for t in (result.data or [])]
    return WalletTransactionListResponse(transactions=transactions)
