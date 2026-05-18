from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone
from uuid import uuid4
import csv
import io

from app.core.dependencies import require_admin
from app.core.supabase import get_supabase

router = APIRouter(prefix="/admin/audit", tags=["audit"])


# ── Models ─────────────────────────────────────────────────────────────────────

class AuditLogItem(BaseModel):
    id: str
    admin_user_id: Optional[str] = None
    admin_email: Optional[str] = None
    action_type: str
    entity_type: str
    entity_id: Optional[str] = None
    summary: str
    before_state: Optional[dict] = None
    after_state: Optional[dict] = None
    ip_address: Optional[str] = None
    created_at: str


class AuditLogListResponse(BaseModel):
    items: List[AuditLogItem]
    total: int


# ── Helper used by other routers ───────────────────────────────────────────────

def write_audit_log(
    sb,
    admin_user: dict,
    action_type: str,
    entity_type: str,
    entity_id: str,
    summary: str,
    before_state: Optional[dict] = None,
    after_state: Optional[dict] = None,
):
    """Insert one audit log row. Silently swallows errors so callers never fail."""
    try:
        admin_id = admin_user.get("id")
        admin_email = None
        if admin_id:
            try:
                ur = sb.table("users").select("email").eq("id", admin_id).maybe_single().execute()
                admin_email = ur.data.get("email") if ur.data else None
            except Exception:
                pass

        sb.table("admin_audit_log").insert({
            "id": str(uuid4()),
            "admin_user_id": admin_id,
            "admin_email": admin_email,
            "action_type": action_type,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "summary": summary,
            "before_state": before_state,
            "after_state": after_state,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        pass


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/log", response_model=AuditLogListResponse)
def get_audit_log(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    admin_user_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    sb = get_supabase()
    try:
        query = sb.table("admin_audit_log").select("*")
        count_query = sb.table("admin_audit_log").select("id", count="exact")

        if date_from:
            query = query.gte("created_at", date_from)
            count_query = count_query.gte("created_at", date_from)
        if date_to:
            query = query.lte("created_at", date_to)
            count_query = count_query.lte("created_at", date_to)
        if action_type:
            query = query.eq("action_type", action_type)
            count_query = count_query.eq("action_type", action_type)
        if entity_type:
            query = query.eq("entity_type", entity_type)
            count_query = count_query.eq("entity_type", entity_type)
        if admin_user_id:
            query = query.eq("admin_user_id", admin_user_id)
            count_query = count_query.eq("admin_user_id", admin_user_id)

        count_result = count_query.execute()
        total = count_result.count or 0

        result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        rows = result.data or []

        items = [
            AuditLogItem(
                id=r["id"],
                admin_user_id=r.get("admin_user_id"),
                admin_email=r.get("admin_email"),
                action_type=r.get("action_type", ""),
                entity_type=r.get("entity_type", ""),
                entity_id=r.get("entity_id"),
                summary=r.get("summary", ""),
                before_state=r.get("before_state"),
                after_state=r.get("after_state"),
                ip_address=r.get("ip_address"),
                created_at=r.get("created_at", ""),
            )
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return AuditLogListResponse(items=items, total=total)


@router.get("/log/export")
def export_audit_log_csv(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    _user=Depends(require_admin),
):
    sb = get_supabase()
    try:
        query = sb.table("admin_audit_log").select("*")
        if date_from:
            query = query.gte("created_at", date_from)
        if date_to:
            query = query.lte("created_at", date_to)
        if action_type:
            query = query.eq("action_type", action_type)
        if entity_type:
            query = query.eq("entity_type", entity_type)
        result = query.order("created_at", desc=True).limit(5000).execute()
        rows = result.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "id", "created_at", "admin_email", "action_type",
        "entity_type", "entity_id", "summary", "ip_address",
    ])
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "id": r.get("id", ""),
            "created_at": r.get("created_at", ""),
            "admin_email": r.get("admin_email", ""),
            "action_type": r.get("action_type", ""),
            "entity_type": r.get("entity_type", ""),
            "entity_id": r.get("entity_id", ""),
            "summary": r.get("summary", ""),
            "ip_address": r.get("ip_address", ""),
        })

    csv_bytes = output.getvalue().encode("utf-8")
    filename = f"audit_log_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
