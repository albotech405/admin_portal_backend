from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, model_validator
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import logging

from app.core.dependencies import require_admin
from app.core.supabase import get_supabase
from app.core.firebase import send_push_multicast, send_push_notification

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Internal push helper (reused by other routers) ────────────────────────


def _get_user_fcm_tokens(sb, user_ids: List[str]) -> Dict[str, List[str]]:
    """
    Return a mapping of user_id → list[fcm_token] from the device_tokens table.
    Falls back to users.fcm_token if device_tokens table is unavailable.
    """
    token_map: Dict[str, List[str]] = {uid: [] for uid in user_ids}
    try:
        result = (
            sb.table("device_tokens")
            .select("user_id, token")
            .in_("user_id", user_ids)
            .execute()
        )
        for row in result.data or []:
            uid = row["user_id"]
            if uid in token_map:
                token_map[uid].append(row["token"])
        return token_map
    except Exception:
        pass

    # Fallback: users.fcm_token column
    try:
        result = (
            sb.table("users")
            .select("id, fcm_token")
            .in_("id", user_ids)
            .execute()
        )
        for row in result.data or []:
            if row.get("fcm_token"):
                token_map[row["id"]] = [row["fcm_token"]]
    except Exception:
        pass

    return token_map


def send_push_to_users(
    user_ids: List[str],
    title: str,
    body: str,
    notification_type: str = "system",
    data: Optional[Dict[str, str]] = None,
    persist: bool = True,
) -> Dict[str, int]:
    """
    Send push notification to a list of internal user IDs.
    Optionally persists rows to the notifications table.
    Returns {"sent": n, "failed": m}.
    """
    if not user_ids:
        return {"sent": 0, "failed": 0}

    sb = get_supabase()
    token_map = _get_user_fcm_tokens(sb, user_ids)
    all_tokens = [t for tokens in token_map.values() for t in tokens]

    sent = 0
    failed = len(user_ids) - len([uid for uid, toks in token_map.items() if toks])

    if all_tokens:
        try:
            counts = send_push_multicast(all_tokens, title, body, data)
            sent = counts.get("success", 0)
            failed += counts.get("failure", 0)
        except Exception as exc:
            logger.warning("FCM multicast error: %s", exc)
            failed += len(all_tokens)

    if persist:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "id": str(uuid4()),
                "user_id": uid,
                "notification_type": notification_type,
                "title": title,
                "content": body,
                "status": "unread",
                "created_at": now,
            }
            for uid in user_ids
        ]
        chunk_size = 100
        try:
            for i in range(0, len(rows), chunk_size):
                sb.table("notifications").insert(rows[i: i + chunk_size]).execute()
        except Exception as exc:
            logger.warning("Failed to persist notifications: %s", exc)

    return {"sent": sent, "failed": failed}

# ── Request / Response models ──────────────────────────────────────────────


class SendNotificationBody(BaseModel):
    # target: "all" | "all_users" | "all_drivers" | "all_customers" | "specific"
    target: str
    user_ids: Optional[List[str]] = None  # required when target = "specific"
    title: str
    body: Optional[str] = None        # legacy field name
    message: Optional[str] = None     # frontend field name (alias for body)
    notification_type: str = "system"
    schedule_at: Optional[str] = None

    @model_validator(mode="after")
    def resolve_message(self):
        if not self.body and self.message:
            self.body = self.message
        if not self.body:
            raise ValueError("Either 'body' or 'message' must be provided")
        return self


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


class BroadcastResult(BaseModel):
    sent: int
    failed: int


class SendTargetedBody(BaseModel):
    title: str
    body: Optional[str] = None        # legacy field name
    message: Optional[str] = None     # frontend field name (alias for body)
    notification_type: str = "system"  # targeted
    role: Optional[str] = None          # "driver" | "customer" | None = all
    is_active: Optional[bool] = None    # True = active only
    user_ids: Optional[List[str]] = None  # explicit list overrides filters
    schedule_at: Optional[str] = None

    @model_validator(mode="after")
    def resolve_message(self):
        if not self.body and self.message:
            self.body = self.message
        if not self.body:
            raise ValueError("Either 'body' or 'message' must be provided")
        return self


class SendToUserBody(BaseModel):
    user_id: str
    title: str
    body: str
    notification_type: str = "system"  # direct
    fcm_token: Optional[str] = None  # override the DB-stored token if provided
    data: Optional[Dict[str, str]] = None  # extra key/value pairs sent via FCM


class SendToUserResponse(BaseModel):
    success: bool
    push_delivered: bool
    message: str


# ── Internal persistence helper ────────────────────────────────────────────


def _persist_notifications(
    sb,
    user_ids: List[str],
    title: str,
    body_text: str,
    notification_type: str = "system",
):
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "id": str(uuid4()),
            "user_id": uid,
            "notification_type": notification_type,
            "title": title,
            "content": body_text,
            "status": "unread",
            "created_at": now,
        }
        for uid in user_ids
    ]
    chunk_size = 100
    for i in range(0, len(rows), chunk_size):
        sb.table("notifications").insert(rows[i: i + chunk_size]).execute()


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/admin/notifications/send", response_model=BroadcastResult)
def send_notification(body: SendNotificationBody, _user=Depends(require_admin)):
    """Broadcast a notification to a target audience or specific users."""
    sb = get_supabase()

    user_ids: List[str] = []

    try:
        if body.target in ("all", "all_users"):
            result = sb.table("users").select("id").eq("is_active", True).execute()
            user_ids = [row["id"] for row in (result.data or [])]

        elif body.target == "all_drivers":
            result = (
                sb.table("users").select("id")
                .eq("role", "driver").eq("is_active", True).execute()
            )
            user_ids = [row["id"] for row in (result.data or [])]

        elif body.target == "all_customers":
            result = (
                sb.table("users").select("id")
                .eq("role", "customer").eq("is_active", True).execute()
            )
            user_ids = [row["id"] for row in (result.data or [])]

        elif body.target == "specific":
            if not body.user_ids:
                raise HTTPException(status_code=400, detail="user_ids required when target=specific")
            user_ids = body.user_ids

        else:
            raise HTTPException(status_code=400, detail=f"Unknown target: {body.target}")

        if not user_ids:
            return BroadcastResult(sent=0, failed=0)

        if body.schedule_at:
            # Scheduled — persist only, no immediate push
            _persist_notifications(sb, user_ids, body.title, body.body, body.notification_type)
            return BroadcastResult(sent=0, failed=0)

        counts = send_push_to_users(
            user_ids, body.title, body.body,
            notification_type=body.notification_type, persist=True,
        )
        return BroadcastResult(sent=counts["sent"], failed=counts["failed"])

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


@router.post("/admin/notifications/send-targeted", response_model=BroadcastResult)
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
            return BroadcastResult(sent=0, failed=0)

        if body.schedule_at:
            _persist_notifications(sb, user_ids, body.title, body.body, body.notification_type)
            return BroadcastResult(sent=0, failed=0)

        counts = send_push_to_users(
            user_ids, body.title, body.body,
            notification_type=body.notification_type, persist=True,
        )
        return BroadcastResult(sent=counts["sent"], failed=counts["failed"])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/notifications/segment-preview")
def preview_segment(
    role: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    min_rides: Optional[int] = Query(None, description="Minimum completed rides"),
    registration_after: Optional[str] = Query(None, description="ISO date string, e.g. 2025-01-01"),
    _user=Depends(require_admin),
):
    """Return recipient count + sample for a segment without sending."""
    sb = get_supabase()
    try:
        query = sb.table("users").select("id, full_name")
        if role:
            query = query.eq("role", role)
        if is_active is not None:
            query = query.eq("is_active", is_active)
        if registration_after:
            query = query.gte("created_at", registration_after)

        result = query.execute()
        users = result.data or []

        # Filter by min_rides if requested (requires joined count — do it client-side)
        if min_rides is not None and min_rides > 0:
            filtered = []
            for u in users:
                try:
                    rides_result = (
                        sb.table("rides")
                        .select("id", count="exact")
                        .eq("customer_id", u["id"])
                        .eq("status", "completed")
                        .execute()
                    )
                    count = rides_result.count or 0
                    if count >= min_rides:
                        filtered.append(u)
                except Exception:
                    pass
            users = filtered

        sample = [{"id": u["id"], "name": u.get("full_name")} for u in users[:5]]
        return {"count": len(users), "sample_users": sample}
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


@router.post("/admin/notifications/send-to-user", response_model=SendToUserResponse)
def send_notification_to_user(body: SendToUserBody, _user=Depends(require_admin)):
    """
    Send a push notification directly to a single app user.
    The notification is logged to the notifications table and, if the user
    has an FCM token stored, immediately delivered to their device.
    """
    sb = get_supabase()
    try:
        user_result = (
            sb.table("users")
            .select("id, full_name")
            .eq("id", body.user_id)
            .single()
            .execute()
        )
        user_data = user_result.data
        if not user_data:
            raise HTTPException(status_code=404, detail="User not found")

        push_delivered = False
        if body.fcm_token:
            push_delivered = send_push_notification(
                token=body.fcm_token, title=body.title, body=body.body, data=body.data
            )
        else:
            token_map = _get_user_fcm_tokens(sb, [body.user_id])
            tokens = token_map.get(body.user_id, [])
            for tok in tokens:
                if send_push_notification(token=tok, title=body.title, body=body.body, data=body.data):
                    push_delivered = True

        sb.table("notifications").insert(
            {
                "id": str(uuid4()),
                "user_id": body.user_id,
                "notification_type": body.notification_type,
                "title": body.title,
                "content": body.body,
                "status": "unread",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()

        return SendToUserResponse(
            success=True,
            push_delivered=push_delivered,
            message=(
                f"Notification sent to {user_data.get('full_name', body.user_id)}"
                + (" and delivered via push" if push_delivered else " (no device token — in-app only)")
            ),
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send notification: {str(e)}")


@router.get("/admin/notifications/stats")
def get_notification_stats(_user=Depends(require_admin)):
    """Aggregate statistics for the notification dashboard."""
    sb = get_supabase()
    try:
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        total_result = sb.table("notifications").select("id", count="exact").execute()
        total_sent = total_result.count or 0

        recent_result = (
            sb.table("notifications")
            .select("id", count="exact")
            .gte("created_at", seven_days_ago)
            .execute()
        )
        sent_last_7_days = recent_result.count or 0

        # Count by notification_type as proxy for broadcast vs targeted
        type_result = sb.table("notifications").select("notification_type").execute()
        by_type: Dict[str, int] = {}
        for row in type_result.data or []:
            t = row.get("notification_type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1

        return {
            "total_sent": total_sent,
            "sent_last_7_days": sent_last_7_days,
            "failed_last_7_days": 0,  # Not tracked server-side currently
            "by_type": by_type,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
