from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.dependencies import require_admin
from app.core.supabase import get_supabase

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminNotification(BaseModel):
    id: str
    user_id: str
    title: str
    content: str
    notification_type: str
    status: str
    read_at: Optional[str] = None
    created_at: str


class AdminNotificationListResponse(BaseModel):
    notifications: List[AdminNotification]
    total: int


class AdminLogEntry(BaseModel):
    id: str
    action: str
    admin_id: Optional[str] = None
    target_id: Optional[str] = None
    target_table: Optional[str] = None
    metadata: dict[str, Any] = {}
    created_at: str


class AdminLogListResponse(BaseModel):
    logs: List[AdminLogEntry]
    total: int


@router.get("/notifications", response_model=AdminNotificationListResponse)
def list_notifications(
    limit: int = Query(25, ge=1, le=100),
    status: Optional[str] = Query(None),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        query = sb.table("notifications").select("*").limit(limit)
        if status:
            query = query.eq("status", status)
        result = query.order("created_at", desc=True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    notifications = [AdminNotification(**row) for row in (result.data or [])]
    return AdminNotificationListResponse(notifications=notifications, total=len(notifications))


@router.get("/logs", response_model=AdminLogListResponse)
def list_admin_logs(
    limit: int = Query(50, ge=1, le=200),
    action: Optional[str] = Query(None),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        query = sb.table("admin_logs").select("*").limit(limit)
        if action:
            query = query.eq("action", action)
        result = query.order("created_at", desc=True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    logs = [AdminLogEntry(**row) for row in (result.data or [])]
    return AdminLogListResponse(logs=logs, total=len(logs))