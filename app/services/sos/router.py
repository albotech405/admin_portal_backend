from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone
from app.core.dependencies import require_admin, get_current_user
from app.core.supabase import get_supabase
from app.services.notifications.router import send_push_to_users

router = APIRouter(prefix="/sos", tags=["sos"])


class SosSessionItem(BaseModel):
    id: str
    user_id: str
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    is_active: bool
    triggered_at: str
    expires_at: Optional[str] = None
    last_latitude: Optional[float] = None
    last_longitude: Optional[float] = None
    cancelled_at: Optional[str] = None
    ride_id: Optional[str] = None
    triggered_by_driver: bool = False
    responder_count: int = 0


class SosSessionListResponse(BaseModel):
    sessions: List[SosSessionItem]
    total: int


class SosResponderItem(BaseModel):
    id: str
    driver_user_id: str
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    notified_at: str
    responded: bool = False
    responded_at: Optional[str] = None


class SosSessionDetailResponse(BaseModel):
    id: str
    user_id: str
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    is_active: bool
    triggered_at: str
    expires_at: Optional[str] = None
    last_latitude: Optional[float] = None
    last_longitude: Optional[float] = None
    last_location_update: Optional[str] = None
    cancelled_at: Optional[str] = None
    ride_id: Optional[str] = None
    triggered_by_driver: bool = False
    alert_radius_km: float = 15.0
    responders: List[SosResponderItem] = []


class ResolveSosBody(BaseModel):
    resolution_notes: Optional[str] = None


class TriggerSosBody(BaseModel):
    ride_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    triggered_by_driver: bool = False


@router.post("/trigger")
def trigger_sos(
    body: TriggerSosBody,
    user: dict = Depends(get_current_user),
):
    """
    Triggered by the mobile app when an SOS event occurs.
    Creates an SOS session and immediately notifies all admin users via push.
    """
    from uuid import uuid4

    sb = get_supabase()
    auth_uid = user["id"]

    # Resolve internal user id
    user_row = None
    try:
        result = sb.table("users").select("id, full_name").eq("supabase_uid", auth_uid).limit(1).execute()
        user_row = result.data[0] if result.data else None
    except Exception:
        pass
    if not user_row:
        try:
            result = sb.table("users").select("id, full_name").eq("id", auth_uid).limit(1).execute()
            user_row = result.data[0] if result.data else None
        except Exception:
            pass

    user_id = (user_row or {}).get("id", auth_uid)
    user_name = (user_row or {}).get("full_name", "Unknown user")
    now = datetime.now(timezone.utc).isoformat()

    session_id = str(uuid4())
    try:
        sb.table("sos_sessions").insert({
            "id": session_id,
            "user_id": user_id,
            "is_active": True,
            "triggered_at": now,
            "ride_id": body.ride_id,
            "last_latitude": body.latitude,
            "last_longitude": body.longitude,
            "triggered_by_driver": body.triggered_by_driver,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create SOS session: {e}")

    # Notify all admin users
    try:
        admin_result = sb.table("users").select("id").eq("is_admin", True).eq("is_active", True).execute()
        admin_ids = [r["id"] for r in (admin_result.data or [])]
        if admin_ids:
            ride_info = f" on ride {body.ride_id}" if body.ride_id else ""
            send_push_to_users(
                admin_ids,
                "SOS Alert",
                f"SOS triggered by {user_name}{ride_info}. Immediate attention required.",
                notification_type="system",
                persist=True,
            )
    except Exception:
        pass

    return {"session_id": session_id, "message": "SOS triggered"}


@router.get("/admin/sessions", response_model=SosSessionListResponse)
def list_sos_sessions(
    is_active: Optional[bool] = Query(None),
    limit: int = Query(50),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        query = sb.table("sos_sessions").select(
            "*, users(full_name, phone_number)"
        )

        if is_active is not None:
            query = query.eq("is_active", is_active)

        result = query.order("triggered_at", desc=True).limit(limit).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    sessions = []
    for r in result.data or []:
        user_info = r.pop("users", {}) or {}

        # Count responders
        responder_count = 0
        try:
            responders = (
                sb.table("sos_driver_alerts")
                .select("id", count="exact")
                .eq("sos_session_id", r.get("id"))
                .execute()
            )
            responder_count = len(responders.data or [])
        except Exception:
            pass

        sessions.append(SosSessionItem(
            id=r.get("id"),
            user_id=r.get("user_id"),
            full_name=user_info.get("full_name"),
            phone_number=user_info.get("phone_number"),
            is_active=r.get("is_active", False),
            triggered_at=r.get("triggered_at"),
            expires_at=r.get("expires_at"),
            last_latitude=r.get("last_latitude"),
            last_longitude=r.get("last_longitude"),
            cancelled_at=r.get("cancelled_at"),
            ride_id=r.get("ride_id"),
            triggered_by_driver=r.get("triggered_by_driver", False),
            responder_count=responder_count,
        ))

    return SosSessionListResponse(sessions=sessions, total=len(sessions))


@router.get("/admin/sessions/{session_id}", response_model=SosSessionDetailResponse)
def get_sos_session_detail(session_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        result = (
            sb.table("sos_sessions")
            .select("*, users(full_name, phone_number)")
            .eq("id", session_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="SOS session not found")

    r = result.data
    user_info = r.pop("users", {}) or {}

    # Fetch responders
    responders = []
    try:
        resp_result = (
            sb.table("sos_driver_alerts")
            .select("*, users(full_name, phone_number)")
            .eq("sos_session_id", session_id)
            .execute()
        )
        for resp in resp_result.data or []:
            driver_user = resp.pop("users", {}) or {}
            responders.append(SosResponderItem(
                id=resp.get("id"),
                driver_user_id=resp.get("driver_user_id"),
                driver_name=driver_user.get("full_name"),
                driver_phone=driver_user.get("phone_number"),
                notified_at=resp.get("notified_at"),
                responded=resp.get("responded", False),
                responded_at=resp.get("responded_at"),
            ))
    except Exception:
        pass

    return SosSessionDetailResponse(
        id=r.get("id"),
        user_id=r.get("user_id"),
        full_name=user_info.get("full_name"),
        phone_number=user_info.get("phone_number"),
        is_active=r.get("is_active", False),
        triggered_at=r.get("triggered_at"),
        expires_at=r.get("expires_at"),
        last_latitude=r.get("last_latitude"),
        last_longitude=r.get("last_longitude"),
        last_location_update=r.get("last_location_update"),
        cancelled_at=r.get("cancelled_at"),
        ride_id=r.get("ride_id"),
        triggered_by_driver=r.get("triggered_by_driver", False),
        alert_radius_km=r.get("alert_radius_km", 15.0),
        responders=responders,
    )


@router.patch("/admin/sessions/{session_id}/resolve")
def resolve_sos_session(
    session_id: str,
    body: Optional[ResolveSosBody] = None,
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("sos_sessions")
            .update({"is_active": False, "cancelled_at": "now()"})
            .eq("id", session_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="SOS session not found")

    return {
        "message": "SOS session resolved",
        "session_id": session_id,
        "resolution_notes": body.resolution_notes if body else None,
    }
