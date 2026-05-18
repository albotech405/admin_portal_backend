from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone
from uuid import uuid4

from app.core.dependencies import require_admin
from app.core.supabase import get_supabase

router = APIRouter()

# ── Request / Response models ──────────────────────────────────────────────


class SendNotificationBody(BaseModel):
    target: str  # "all_users" | "all_drivers" | "all_customers" | "specific"
    user_ids: Optional[List[str]] = None  # required when target = "specific"
    title: str
    body: str
    notification_type: str = "admin_broadcast"
    schedule_at: Optional[str] = None  # ISO datetime string for future send


class NotificationHistoryItem(BaseModel):
    id: str
    user_id: str
    user_name: Optional[str] = None
    user_role: Optional[str] = None
    title: str
    content: str
    notification_type: str
    status: str
    created_at: Optional[str] = None
    read_at: Optional[str] = None


class NotificationHistoryResponse(BaseModel):
    items: List[NotificationHistoryItem]
    total: int


class NotificationUserItem(BaseModel):
    id: str
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class NotificationUsersResponse(BaseModel):
    items: List[NotificationUserItem]
    total: int


class SendNotificationResponse(BaseModel):
    success: bool
    recipient_count: int
    message: str


class SendTargetedBody(BaseModel):
    title: str
    body: str
    notification_type: str = "admin_targeted"
    role: Optional[str] = None          # "driver" | "customer" | None = all
    is_active: Optional[bool] = None    # True = active only
    user_ids: Optional[List[str]] = None  # explicit list overrides filters
    schedule_at: Optional[str] = None


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/admin/notifications/send", response_model=SendNotificationResponse)
def send_notification(body: SendNotificationBody, _user=Depends(require_admin)):
    """Send a notification to a target audience or specific users."""
    sb = get_supabase()

    # Resolve recipient user IDs based on target
    user_ids: List[str] = []

    try:
        if body.target == "specific":
            if not body.user_ids or len(body.user_ids) == 0:
                raise HTTPException(status_code=400, detail="user_ids required when target=specific")
            user_ids = body.user_ids

        elif body.target == "all_users":
            result = sb.table("users").select("id").eq("is_active", True).execute()
            user_ids = [row["id"] for row in (result.data or [])]

        elif body.target == "all_drivers":
            result = sb.table("users").select("id").eq("role", "driver").eq("is_active", True).execute()
            user_ids = [row["id"] for row in (result.data or [])]

        elif body.target == "all_customers":
            result = sb.table("users").select("id").eq("role", "customer").eq("is_active", True).execute()
            user_ids = [row["id"] for row in (result.data or [])]

        else:
            raise HTTPException(status_code=400, detail=f"Unknown target: {body.target}")

        if len(user_ids) == 0:
            return SendNotificationResponse(success=True, recipient_count=0, message="No recipients found for the given target")

        # Build notification rows
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for uid in user_ids:
            rows.append({
                "id": str(uuid4()),
                "user_id": uid,
                "notification_type": body.notification_type,
                "title": body.title,
                "content": body.body,
                "status": "scheduled" if body.schedule_at else "sent",
                "created_at": now,
                "read_at": None,
            })

        # Batch insert (chunked to avoid payload limits)
        chunk_size = 100
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            sb.table("notifications").insert(chunk).execute()

        return SendNotificationResponse(
            success=True,
            recipient_count=len(user_ids),
            message=f"Notification sent to {len(user_ids)} recipient(s)",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send notification: {str(e)}")


@router.get("/admin/notifications/history", response_model=NotificationHistoryResponse)
def get_notification_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    _user=Depends(require_admin),
):
    """View sent notification history with pagination."""
    sb = get_supabase()

    try:
        query = (
            sb.table("notifications")
            .select("id, user_id, title, content, notification_type, status, created_at, read_at")
            .order("created_at", desc=True)
        )

        if status:
            query = query.eq("status", status)

        # Get total count
        count_query = sb.table("notifications").select("id", count="exact")
        if status:
            count_query = count_query.eq("status", status)
        count_result = count_query.execute()
        total = count_result.count or 0

        # Get paginated results
        result = query.range(offset, offset + limit - 1).execute()
        items = result.data or []

        # Enrich with user info (batch fetch user names)
        user_ids = list(set(item["user_id"] for item in items if item.get("user_id")))
        user_map = {}
        if user_ids:
            try:
                user_result = (
                    sb.table("users")
                    .select("id, full_name, role")
                    .in_("id", user_ids)
                    .execute()
                )
                for u in (user_result.data or []):
                    user_map[u["id"]] = u
            except Exception:
                pass  # Non-critical enrichment

        history = []
        for item in items:
            user_info = user_map.get(item.get("user_id", ""), {})
            history.append(
                NotificationHistoryItem(
                    id=item["id"],
                    user_id=item.get("user_id", ""),
                    user_name=user_info.get("full_name"),
                    user_role=user_info.get("role"),
                    title=item.get("title", ""),
                    content=item.get("content", ""),
                    notification_type=item.get("notification_type", ""),
                    status=item.get("status", ""),
                    created_at=item.get("created_at"),
                    read_at=item.get("read_at"),
                )
            )

        return NotificationHistoryResponse(items=history, total=total)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch notification history: {str(e)}")


@router.post("/admin/notifications/send-targeted", response_model=SendNotificationResponse)
def send_targeted_notification(body: SendTargetedBody, _user=Depends(require_admin)):
    """Send to an explicit user list or a filtered segment (role + active status)."""
    sb = get_supabase()
    try:
        user_ids: List[str] = []

        if body.user_ids:
            user_ids = body.user_ids
        else:
            query = sb.table("users").select("id")
            if body.role:
                query = query.eq("role", body.role)
            if body.is_active is not None:
                query = query.eq("is_active", body.is_active)
            result = query.execute()
            user_ids = [r["id"] for r in (result.data or [])]

        if not user_ids:
            return SendNotificationResponse(success=True, recipient_count=0, message="No recipients matched filters")

        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "id": str(uuid4()),
                "user_id": uid,
                "notification_type": body.notification_type,
                "title": body.title,
                "content": body.body,
                "status": "scheduled" if body.schedule_at else "sent",
                "created_at": now,
            }
            for uid in user_ids
        ]

        chunk_size = 100
        for i in range(0, len(rows), chunk_size):
            sb.table("notifications").insert(rows[i : i + chunk_size]).execute()

        return SendNotificationResponse(
            success=True,
            recipient_count=len(user_ids),
            message=f"Targeted notification sent to {len(user_ids)} recipient(s)",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/notifications/segment-preview")
def preview_segment(
    role: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    _user=Depends(require_admin),
):
    """Return recipient count for a segment without sending."""
    sb = get_supabase()
    try:
        query = sb.table("users").select("id", count="exact")
        if role:
            query = query.eq("role", role)
        if is_active is not None:
            query = query.eq("is_active", is_active)
        result = query.execute()
        return {"recipient_count": result.count or 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/notifications/users", response_model=NotificationUsersResponse)
def get_notification_users(
    search: Optional[str] = Query(None),
    role: Optional[str] = Query(None, regex="^(driver|customer|all)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    """List users for targeting (drivers/customers with search)."""
    sb = get_supabase()

    try:
        query = sb.table("users").select("id, full_name, phone_number, role, is_active")

        if role and role != "all":
            query = query.eq("role", role)

        if search:
            query = query.or_(f"full_name.ilike.%{search}%,phone_number.ilike.%{search}%")

        # Get total count
        count_query = sb.table("users").select("id", count="exact")
        if role and role != "all":
            count_query = count_query.eq("role", role)
        if search:
            count_query = count_query.or_(f"full_name.ilike.%{search}%,phone_number.ilike.%{search}%")
        count_result = count_query.execute()
        total = count_result.count or 0

        result = query.range(offset, offset + limit - 1).execute()
        items = result.data or []

        users = [
            NotificationUserItem(
                id=u["id"],
                full_name=u.get("full_name"),
                phone_number=u.get("phone_number"),
                role=u.get("role"),
                is_active=u.get("is_active"),
            )
            for u in items
        ]

        return NotificationUsersResponse(items=users, total=total)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch users: {str(e)}")
