"""Admin dispute management.

Full rewrite (not an extension) of the prior disputes module, which depended on
a `dispute_logs` table that never existed in production (confirmed via a real
production schema dump) and a `rides.dispute_status`/`rides.dispute_resolved_at`
fallback that also never existed -- every dispute action could return HTTP 200
"success" while writing nothing anywhere. The one exception was
`charge_driver_dispute`, which moved real money via direct Python balance
mutation with zero audit trail.

This version is backed by real tables (`disputes`, `dispute_history`, see
sql/20260901_disputes_tables.sql) and real atomic RPCs for financial actions
(`dispute_refund_customer`, `dispute_charge_driver`, see
sql/20260901_dispute_financial_rpcs.sql), mirroring the existing
`approve_wallet_topup` pattern: assert_admin -> lock+validate -> mutate balance
-> insert ledger row -> update the dispute row -> notify -> audit log, all in
one Postgres transaction.

Disputes are admin-created only for V1 -- no customer/driver-facing dispute
infrastructure exists anywhere in this repo to build on.

Every non-financial status-changing endpoint (assign/escalate/dismiss/resolve/
reopen) conditions its UPDATE on the status it read (`.eq("status", from_status)`)
and treats a zero-row result as a 409, not just a Python-side is_valid_transition
check followed by an unconditional UPDATE -- PostgREST issues each call as its
own statement with no held lock across the read, so two concurrent requests
reading the same from_status could otherwise both pass validation and the
second UPDATE would silently overwrite the first (found and fixed during
review; the same chained .eq().eq() pattern already exists in
app/services/admin_mgmt/router.py). The financial actions (refund/
charge-driver) don't need this -- they run inside dispute_refund_customer/
dispute_charge_driver, which take `FOR UPDATE` row locks inside one Postgres
transaction instead.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime, timezone
from uuid import uuid4

from app.core.dependencies import require_admin, require_role
from app.core.supabase import get_supabase, call_rpc, first_row, rpc_error_to_http_exception
from app.services.audit.router import write_audit_log
from app.services.disputes.transitions import is_valid_transition

router = APIRouter(prefix="/disputes", tags=["disputes"])


# ── Models ──────────────────────────────────────────────────────────────


class CreateDisputeBody(BaseModel):
    filed_for: str  # "customer" | "driver"
    customer_id: Optional[str] = None
    driver_id: Optional[str] = None
    ride_id: Optional[str] = None
    dispute_type: str
    priority: str = "normal"
    description: str
    disputed_amount_cdf: Optional[float] = None
    attachment_urls: List[str] = Field(default_factory=list)

    @field_validator("filed_for")
    @classmethod
    def filed_for_valid(cls, v: str) -> str:
        if v not in ("customer", "driver"):
            raise ValueError("filed_for must be 'customer' or 'driver'")
        return v


class AssignDisputeBody(BaseModel):
    assigned_to_admin_id: str
    notes: Optional[str] = None


class ResolveDisputeBody(BaseModel):
    resolution_type: str  # "no_action" | "company_adjustment"
    resolution_notes: Optional[str] = None


class DismissEscalateReopenBody(BaseModel):
    notes: Optional[str] = None


class RefundDisputeBody(BaseModel):
    amount_cdf: float = Field(..., gt=0)
    notes: Optional[str] = None


class ChargeDriverDisputeBody(BaseModel):
    amount_cdf: float = Field(..., gt=0)
    notes: Optional[str] = None


class DisputeItem(BaseModel):
    id: str
    ride_id: Optional[str] = None
    customer_id: Optional[str] = None
    driver_id: Optional[str] = None
    filed_by_admin_id: str
    filed_for: str
    dispute_type: str
    priority: str = "normal"
    status: str = "open"
    assigned_to_admin_id: Optional[str] = None
    description: str
    attachment_urls: List[str] = Field(default_factory=list)
    resolution_type: Optional[str] = None
    resolution_notes: Optional[str] = None
    resolved_by_admin_id: Optional[str] = None
    resolved_at: Optional[str] = None
    disputed_amount_cdf: Optional[float] = None
    resolution_amount_cdf: Optional[float] = None
    created_at: str
    updated_at: str


class DisputeListResponse(BaseModel):
    disputes: List[DisputeItem]
    total: int
    limit: Optional[int] = None
    offset: int = 0


class DisputeHistoryItem(BaseModel):
    id: str
    dispute_id: str
    admin_id: Optional[str] = None
    action: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    notes: Optional[str] = None
    metadata: Optional[dict] = None
    created_at: str


class DisputeDetailResponse(DisputeItem):
    history: List[DisputeHistoryItem] = Field(default_factory=list)


_DISPUTE_FIELDS = set(DisputeItem.model_fields.keys())


def _record_history(sb, *, dispute_id: str, admin_id: Optional[str], action: str,
                     from_status: Optional[str] = None, to_status: Optional[str] = None,
                     notes: Optional[str] = None, now_iso: Optional[str] = None) -> None:
    sb.table("dispute_history").insert({
        "id": str(uuid4()),
        "dispute_id": dispute_id,
        "admin_id": admin_id,
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "notes": notes,
        "created_at": now_iso or datetime.now(timezone.utc).isoformat(),
    }).execute()


# ── Create / read ───────────────────────────────────────────────────────


@router.post("/admin/create", status_code=201, response_model=DisputeItem)
def create_dispute(body: CreateDisputeBody, _user=Depends(require_role("support", "finance", "operations"))):
    """Admin-only intake: file a dispute on behalf of a customer or driver.
    filed_by_admin_id (the acting admin) is always distinct from filed_for/
    customer_id/driver_id (whose grievance this is)."""
    if body.filed_for == "customer" and not body.customer_id:
        raise HTTPException(status_code=400, detail="customer_id is required when filed_for='customer'")
    if body.filed_for == "driver" and not body.driver_id:
        raise HTTPException(status_code=400, detail="driver_id is required when filed_for='driver'")

    sb = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    dispute_id = str(uuid4())
    row = {
        "id": dispute_id,
        "ride_id": body.ride_id,
        "customer_id": body.customer_id,
        "driver_id": body.driver_id,
        "filed_by_admin_id": _user.get("id"),
        "filed_for": body.filed_for,
        "dispute_type": body.dispute_type,
        "priority": body.priority,
        "status": "open",
        "description": body.description,
        "attachment_urls": body.attachment_urls,
        "disputed_amount_cdf": body.disputed_amount_cdf,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    try:
        result = sb.table("disputes").insert(row).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.data:
        raise HTTPException(status_code=500, detail="Dispute insert returned no row")

    _record_history(sb, dispute_id=dispute_id, admin_id=_user.get("id"), action="created",
                     to_status="open", now_iso=now_iso)

    write_audit_log(
        sb=sb, admin_user=_user, action_type="dispute_created", entity_type="disputes", entity_id=dispute_id,
        summary=f"Admin filed dispute for {body.filed_for}", after_state=row,
    )

    created = result.data[0]
    return DisputeItem(**{k: v for k, v in created.items() if k in _DISPUTE_FIELDS})


@router.get("/admin/list", response_model=DisputeListResponse)
def list_disputes(
    status: Optional[str] = Query(None),
    filed_for: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    assigned_to_admin_id: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=200, description="Page size; omit to return every match."),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    sb = get_supabase()
    try:
        query = sb.table("disputes").select("*", count="exact")
        if status:
            query = query.eq("status", status)
        if filed_for:
            query = query.eq("filed_for", filed_for)
        if priority:
            query = query.eq("priority", priority)
        if assigned_to_admin_id:
            query = query.eq("assigned_to_admin_id", assigned_to_admin_id)
        query = query.order("created_at", desc=True).order("id")
        if limit is not None:
            query = query.range(offset, offset + limit - 1)
        result = query.execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    total_matched = result.count if result.count is not None else len(result.data or [])
    items = [DisputeItem(**{k: v for k, v in row.items() if k in _DISPUTE_FIELDS}) for row in (result.data or [])]
    return DisputeListResponse(disputes=items, total=total_matched, limit=limit, offset=offset)


@router.get("/admin/{dispute_id}", response_model=DisputeDetailResponse)
def get_dispute_detail(dispute_id: str, _user=Depends(require_admin)):
    sb = get_supabase()
    try:
        current = sb.table("disputes").select("*").eq("id", dispute_id).maybe_single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not current.data:
        raise HTTPException(status_code=404, detail="Dispute not found")

    try:
        history_result = (
            sb.table("dispute_history")
            .select("*")
            .eq("dispute_id", dispute_id)
            .order("created_at", desc=True)
            .execute()
        )
        history = [DisputeHistoryItem(**h) for h in (history_result.data or [])]
    except Exception:
        history = []

    row_fields = {k: v for k, v in current.data.items() if k in _DISPUTE_FIELDS}
    return DisputeDetailResponse(**row_fields, history=history)


# ── Non-financial status transitions ───────────────────────────────────


def _fetch_dispute_or_404(sb, dispute_id: str) -> dict:
    current = sb.table("disputes").select("*").eq("id", dispute_id).maybe_single().execute()
    if not current.data:
        raise HTTPException(status_code=404, detail="Dispute not found")
    return current.data


@router.patch("/admin/{dispute_id}/assign", response_model=DisputeItem)
def assign_dispute(dispute_id: str, body: AssignDisputeBody,
                    _user=Depends(require_role("support", "finance", "operations"))):
    sb = get_supabase()
    current = _fetch_dispute_or_404(sb, dispute_id)
    from_status = current["status"]
    to_status = "assigned"
    if not is_valid_transition(from_status, to_status):
        raise HTTPException(status_code=400, detail=f"Cannot assign a dispute in status '{from_status}'")

    now_iso = datetime.now(timezone.utc).isoformat()
    updated_rows = sb.table("disputes").update({
        "status": to_status, "assigned_to_admin_id": body.assigned_to_admin_id, "updated_at": now_iso,
    }).eq("id", dispute_id).eq("status", from_status).execute()
    if not updated_rows.data:
        raise HTTPException(status_code=409,
                             detail="Dispute status changed concurrently; refetch and retry")
    _record_history(sb, dispute_id=dispute_id, admin_id=_user.get("id"), action="assigned",
                     from_status=from_status, to_status=to_status, notes=body.notes, now_iso=now_iso)
    write_audit_log(sb=sb, admin_user=_user, action_type="dispute_assigned", entity_type="disputes",
                     entity_id=dispute_id, summary=f"Admin assigned dispute to {body.assigned_to_admin_id}",
                     before_state={"status": from_status},
                     after_state={"status": to_status, "assigned_to_admin_id": body.assigned_to_admin_id})

    updated = _fetch_dispute_or_404(sb, dispute_id)
    return DisputeItem(**{k: v for k, v in updated.items() if k in _DISPUTE_FIELDS})


@router.patch("/admin/{dispute_id}/escalate", response_model=DisputeItem)
def escalate_dispute(dispute_id: str, body: DismissEscalateReopenBody = DismissEscalateReopenBody(),
                      _user=Depends(require_role("support", "finance", "operations"))):
    sb = get_supabase()
    current = _fetch_dispute_or_404(sb, dispute_id)
    from_status = current["status"]
    to_status = "escalated"
    if not is_valid_transition(from_status, to_status):
        raise HTTPException(status_code=400, detail=f"Cannot escalate a dispute in status '{from_status}'")

    now_iso = datetime.now(timezone.utc).isoformat()
    updated_rows = sb.table("disputes").update(
        {"status": to_status, "updated_at": now_iso}
    ).eq("id", dispute_id).eq("status", from_status).execute()
    if not updated_rows.data:
        raise HTTPException(status_code=409,
                             detail="Dispute status changed concurrently; refetch and retry")
    _record_history(sb, dispute_id=dispute_id, admin_id=_user.get("id"), action="escalated",
                     from_status=from_status, to_status=to_status, notes=body.notes, now_iso=now_iso)
    write_audit_log(sb=sb, admin_user=_user, action_type="dispute_escalated", entity_type="disputes",
                     entity_id=dispute_id, summary="Admin escalated dispute",
                     before_state={"status": from_status}, after_state={"status": to_status})

    updated = _fetch_dispute_or_404(sb, dispute_id)
    return DisputeItem(**{k: v for k, v in updated.items() if k in _DISPUTE_FIELDS})


@router.patch("/admin/{dispute_id}/dismiss", response_model=DisputeItem)
def dismiss_dispute(dispute_id: str, body: DismissEscalateReopenBody = DismissEscalateReopenBody(),
                     _user=Depends(require_role("support", "finance", "operations"))):
    sb = get_supabase()
    current = _fetch_dispute_or_404(sb, dispute_id)
    from_status = current["status"]
    to_status = "dismissed"
    if not is_valid_transition(from_status, to_status):
        raise HTTPException(status_code=400, detail=f"Cannot dismiss a dispute in status '{from_status}'")

    now_iso = datetime.now(timezone.utc).isoformat()
    updated_rows = sb.table("disputes").update({
        "status": to_status, "resolution_type": "dismissed", "resolution_notes": body.notes,
        "resolved_by_admin_id": _user.get("id"), "resolved_at": now_iso, "updated_at": now_iso,
    }).eq("id", dispute_id).eq("status", from_status).execute()
    if not updated_rows.data:
        raise HTTPException(status_code=409,
                             detail="Dispute status changed concurrently; refetch and retry")
    _record_history(sb, dispute_id=dispute_id, admin_id=_user.get("id"), action="dismissed",
                     from_status=from_status, to_status=to_status, notes=body.notes, now_iso=now_iso)
    write_audit_log(sb=sb, admin_user=_user, action_type="dispute_dismissed", entity_type="disputes",
                     entity_id=dispute_id, summary="Admin dismissed dispute",
                     before_state={"status": from_status}, after_state={"status": to_status})

    updated = _fetch_dispute_or_404(sb, dispute_id)
    return DisputeItem(**{k: v for k, v in updated.items() if k in _DISPUTE_FIELDS})


@router.patch("/admin/{dispute_id}/reopen", response_model=DisputeItem)
def reopen_dispute(dispute_id: str, body: DismissEscalateReopenBody = DismissEscalateReopenBody(),
                    _user=Depends(require_role("operations"))):
    """Reopening a resolved/dismissed dispute overrides a prior admin's decision
    -- gated at 'operations', above the tier that made routine resolutions."""
    sb = get_supabase()
    current = _fetch_dispute_or_404(sb, dispute_id)
    from_status = current["status"]
    to_status = "reopened"
    if not is_valid_transition(from_status, to_status):
        raise HTTPException(status_code=400, detail=f"Cannot reopen a dispute in status '{from_status}'")

    now_iso = datetime.now(timezone.utc).isoformat()
    updated_rows = sb.table("disputes").update(
        {"status": to_status, "updated_at": now_iso}
    ).eq("id", dispute_id).eq("status", from_status).execute()
    if not updated_rows.data:
        raise HTTPException(status_code=409,
                             detail="Dispute status changed concurrently; refetch and retry")
    _record_history(sb, dispute_id=dispute_id, admin_id=_user.get("id"), action="reopened",
                     from_status=from_status, to_status=to_status, notes=body.notes, now_iso=now_iso)
    write_audit_log(sb=sb, admin_user=_user, action_type="dispute_reopened", entity_type="disputes",
                     entity_id=dispute_id, summary="Admin reopened dispute",
                     before_state={"status": from_status}, after_state={"status": to_status})

    updated = _fetch_dispute_or_404(sb, dispute_id)
    return DisputeItem(**{k: v for k, v in updated.items() if k in _DISPUTE_FIELDS})


@router.patch("/admin/{dispute_id}/resolve", response_model=DisputeItem)
def resolve_dispute(dispute_id: str, body: ResolveDisputeBody,
                     _user=Depends(require_role("support", "finance", "operations"))):
    """Non-financial resolution only: 'no_action' or 'company_adjustment'.
    company_adjustment (the company absorbs the cost, charges neither party)
    additionally requires super_admin -- it's the one resolution with no
    counterparty accountability check otherwise. No ledger row is written for
    either resolution_type: no product-defined ledger semantic exists for
    company_adjustment, and no_action never touches money by definition."""
    if body.resolution_type not in ("no_action", "company_adjustment"):
        raise HTTPException(status_code=400,
                             detail="resolution_type must be 'no_action' or 'company_adjustment' for this endpoint "
                                    "(use /refund or /charge-driver for financial resolutions)")
    if body.resolution_type == "company_adjustment" and _user.get("admin_role") != "super_admin":
        raise HTTPException(status_code=403, detail="company_adjustment resolution requires super_admin")

    sb = get_supabase()
    current = _fetch_dispute_or_404(sb, dispute_id)
    from_status = current["status"]
    to_status = "resolved"
    if not is_valid_transition(from_status, to_status):
        raise HTTPException(status_code=400, detail=f"Cannot resolve a dispute in status '{from_status}'")

    now_iso = datetime.now(timezone.utc).isoformat()
    updated_rows = sb.table("disputes").update({
        "status": to_status, "resolution_type": body.resolution_type, "resolution_notes": body.resolution_notes,
        "resolved_by_admin_id": _user.get("id"), "resolved_at": now_iso, "updated_at": now_iso,
    }).eq("id", dispute_id).eq("status", from_status).execute()
    if not updated_rows.data:
        raise HTTPException(status_code=409,
                             detail="Dispute status changed concurrently; refetch and retry")
    _record_history(sb, dispute_id=dispute_id, admin_id=_user.get("id"), action=f"resolved_{body.resolution_type}",
                     from_status=from_status, to_status=to_status, notes=body.resolution_notes, now_iso=now_iso)
    write_audit_log(sb=sb, admin_user=_user, action_type="dispute_resolved", entity_type="disputes",
                     entity_id=dispute_id, summary=f"Admin resolved dispute ({body.resolution_type})",
                     before_state={"status": from_status},
                     after_state={"status": to_status, "resolution_type": body.resolution_type})

    updated = _fetch_dispute_or_404(sb, dispute_id)
    return DisputeItem(**{k: v for k, v in updated.items() if k in _DISPUTE_FIELDS})


# ── Financial resolutions (atomic RPCs) ────────────────────────────────


@router.patch("/admin/{dispute_id}/refund")
def refund_dispute(dispute_id: str, body: RefundDisputeBody, _user=Depends(require_role("finance", "operations"))):
    """Atomic customer refund via the dispute_refund_customer RPC (see
    sql/20260901_dispute_financial_rpcs.sql) -- mutates users.wallet_balance_cdf,
    inserts a customer_wallet_transactions ledger row, updates the dispute's own
    status/resolution fields, notifies the customer, and writes to admin_logs,
    all in one Postgres transaction. Never mutates balances directly in Python.

    Note: rpc_error_to_http_exception (app/core/supabase.py) only maps a few
    specific admin/permission error substrings to 403/400 -- business-rule
    errors raised by the RPC itself (dispute not found, wrong status, invalid
    amount) fall through its generic 500 case. This matches the exact same
    pre-existing characteristic of approve_wallet_topup's own business-rule
    errors (e.g. "wallet top-up request is already approved") -- not a
    regression introduced here, and not fixed here since that would change
    shared, out-of-scope RPC-error-handling behavior."""
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")
    try:
        result = first_row(call_rpc("dispute_refund_customer", {
            "p_admin_id": admin_id,
            "p_dispute_id": dispute_id,
            "p_amount_cdf": body.amount_cdf,
            "p_notes": body.notes,
        }))
    except Exception as e:
        raise rpc_error_to_http_exception(e)
    if not result:
        raise HTTPException(status_code=404, detail="Dispute not found")
    return result


@router.patch("/admin/{dispute_id}/charge-driver")
def charge_driver_dispute(dispute_id: str, body: ChargeDriverDisputeBody,
                           _user=Depends(require_role("finance", "operations"))):
    """Atomic driver charge via the dispute_charge_driver RPC (see
    sql/20260901_dispute_financial_rpcs.sql) -- mutates driver_profiles.
    credit_balance, inserts a wallet_transactions ledger row, updates the
    dispute's own status/resolution fields, notifies the driver, and writes to
    admin_logs, all in one Postgres transaction. Rejects (raises) rather than
    floors to zero when the charge exceeds the driver's balance -- the admin
    must resolve the shortfall explicitly (partial charge, or a
    company_adjustment resolve for the remainder) rather than silently
    under-collecting."""
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")
    try:
        result = first_row(call_rpc("dispute_charge_driver", {
            "p_admin_id": admin_id,
            "p_dispute_id": dispute_id,
            "p_amount_cdf": body.amount_cdf,
            "p_notes": body.notes,
        }))
    except Exception as e:
        raise rpc_error_to_http_exception(e)
    if not result:
        raise HTTPException(status_code=404, detail="Dispute not found")
    return result
