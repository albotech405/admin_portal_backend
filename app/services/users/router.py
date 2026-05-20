from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.core.supabase import get_supabase

router = APIRouter(prefix="/users", tags=["users"])


# ── Helpers ────────────────────────────────────────────────────────────────


def _resolve_internal_user_id(auth_uid: str) -> str:
    """Resolve the Supabase auth UID to the internal users.id (app PK)."""
    sb = get_supabase()
    result = (
        sb.table("users")
        .select("id")
        .eq("supabase_uid", auth_uid)
        .limit(1)
        .execute()
    )
    row = result.data[0] if result.data else None
    if not row:
        # Fallback: maybe the table uses id = auth_uid directly
        result2 = sb.table("users").select("id").eq("id", auth_uid).limit(1).execute()
        row = result2.data[0] if result2.data else None
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return row["id"]


# ── Device Token Models ────────────────────────────────────────────────────


class DeviceTokenBody(BaseModel):
    token: str
    platform: str = "android"  # "android" | "ios"


class DeviceTokenDeleteBody(BaseModel):
    token: str


# ── Device Token Endpoints ─────────────────────────────────────────────────


@router.post("/me/device-token", status_code=200)
def register_device_token(
    body: DeviceTokenBody,
    user: dict = Depends(get_current_user),
):
    """Register or refresh an FCM device token for the authenticated user."""
    auth_uid = user["id"]
    user_id = _resolve_internal_user_id(auth_uid)

    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    # Upsert on (user_id, token) — update updated_at on re-registration
    sb.table("device_tokens").upsert(
        {
            "user_id": user_id,
            "token": body.token,
            "platform": body.platform,
            "updated_at": now,
        },
        on_conflict="user_id,token",
    ).execute()

    return {"message": "Device token registered"}


@router.delete("/me/device-token", status_code=200)
def remove_device_token(
    body: DeviceTokenDeleteBody,
    user: dict = Depends(get_current_user),
):
    """Remove an FCM device token on logout."""
    auth_uid = user["id"]
    user_id = _resolve_internal_user_id(auth_uid)

    sb = get_supabase()
    sb.table("device_tokens").delete().eq("user_id", user_id).eq("token", body.token).execute()

    return {"message": "Device token removed"}


# ── Notification Endpoints (user-facing) ──────────────────────────────────


@router.patch("/me/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: str,
    user: dict = Depends(get_current_user),
):
    """Mark a notification as read for the authenticated user."""
    auth_uid = user["id"]
    user_id = _resolve_internal_user_id(auth_uid)

    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    result = (
        sb.table("notifications")
        .update({"read_at": now})
        .eq("id", notification_id)
        .eq("user_id", user_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Notification not found")

    return {"read_at": now}


@router.get("/me/notifications/unread-count")
def get_unread_notification_count(
    user: dict = Depends(get_current_user),
):
    """Return count of unread notifications for the authenticated user."""
    auth_uid = user["id"]
    user_id = _resolve_internal_user_id(auth_uid)

    sb = get_supabase()
    result = (
        sb.table("notifications")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .is_("read_at", "null")
        .execute()
    )

    return {"count": result.count or 0}
