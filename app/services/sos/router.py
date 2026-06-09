from __future__ import annotations

import asyncio
import json
import secrets
import logging
from typing import Any, Optional, List
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.dependencies import require_role, get_current_user
from app.core.supabase import get_supabase
from app.services.notifications.router import send_push_to_users

router = APIRouter(prefix="/sos", tags=["sos"])
logger = logging.getLogger(__name__)

SOS_ALLOWED_ROLES = ("operations", "super_admin")
require_sos_access = require_role(*SOS_ALLOWED_ROLES)


class SosSessionItem(BaseModel):
    id: str
    status: str
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_notes: Optional[str] = None
    expires_at: Optional[str] = None
    last_location_timestamp: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    cancelled_at: Optional[str] = None
    ride_id: Optional[str] = None
    triggered_by: Optional[str] = None
    customer_user_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    driver_id: Optional[str] = None
    driver_user_id: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    alert_type: Optional[str] = None
    alert_source: Optional[str] = None
    message: Optional[str] = None
    description: Optional[str] = None
    location_name: Optional[str] = None
    address: Optional[str] = None
    metadata: Optional[dict] = None
    triggered_by_driver: bool = False
    responder_count: int = 0
    tracking_url: Optional[str] = None


class SosSessionListResponse(BaseModel):
    sessions: List[SosSessionItem]
    total: int
    limit: int
    offset: int


class SosResponderItem(BaseModel):
    id: str
    driver_user_id: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    notified_at: Optional[str] = None
    responded: bool = False
    responded_at: Optional[str] = None


class SosSessionDetailResponse(SosSessionItem):
    alert_radius_km: float = 15.0
    emergency_contacts: List[EmergencyContactItem] = Field(default_factory=list)
    responders: List[SosResponderItem] = Field(default_factory=list)


class SosResponderListResponse(BaseModel):
    session_id: str
    notified_count: int
    responders: List[SosResponderItem] = Field(default_factory=list)


class EmergencyContactItem(BaseModel):
    id: str
    name: Optional[str] = None
    phone_number: Optional[str] = None
    contact_relationship: Optional[str] = None
    created_at: Optional[str] = None


class SosLocationBody(BaseModel):
    latitude: float
    longitude: float
    heading: Optional[float] = None
    speed: Optional[float] = None
    accuracy: Optional[float] = None


class ResolveSosBody(BaseModel):
    resolution_notes: Optional[str] = None


class TriggerSosBody(BaseModel):
    ride_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    triggered_by_driver: bool = False


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _iso_or_none(value: Any) -> Optional[str]:
    parsed = _parse_dt(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _dictish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _session_tracking_url(session_row: dict[str, Any]) -> Optional[str]:
    token = session_row.get("tracking_token")
    if not token:
        return None
    return f"{settings.PUBLIC_BASE_URL}/api/v1/sos/track/{token}/map"


def _query_users_by_ids(sb, user_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not user_ids:
        return {}
    result = sb.table("users").select("id, full_name, phone_number").in_("id", user_ids).execute()
    return {str(row.get("id")): row for row in (result.data or []) if row.get("id")}


def _query_rides_by_ids(sb, ride_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ride_ids:
        return {}
    result = (
        sb.table("rides")
        .select("id, customer_id, driver_id, customer_name, customer_phone, driver_name, driver_phone, status, picking_point, destination")
        .in_("id", ride_ids)
        .execute()
    )
    return {str(row.get("id")): row for row in (result.data or []) if row.get("id")}


def _query_driver_profiles_by_ids(sb, driver_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not driver_ids:
        return {}
    result = (
        sb.table("driver_profiles")
        .select("id, user_id, verification_status")
        .in_("id", driver_ids)
        .execute()
    )
    return {str(row.get("id")): row for row in (result.data or []) if row.get("id")}


def _query_driver_profiles_by_user_ids(sb, user_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not user_ids:
        return {}
    result = (
        sb.table("driver_profiles")
        .select("id, user_id, verification_status")
        .in_("user_id", user_ids)
        .execute()
    )
    return {str(row.get("user_id")): row for row in (result.data or []) if row.get("user_id")}


def _query_responder_counts(sb, session_ids: list[str]) -> dict[str, int]:
    if not session_ids:
        return {}
    result = (
        sb.table("sos_driver_alerts")
        .select("id, sos_session_id")
        .in_("sos_session_id", session_ids)
        .execute()
    )
    counts: dict[str, int] = {}
    for row in result.data or []:
        session_id = row.get("sos_session_id")
        if session_id:
            key = str(session_id)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _write_sos_admin_log(
    sb,
    admin_user: dict,
    *,
    action: str,
    sos_session_id: str,
    ride_id: Optional[str] = None,
    resolution_notes: Optional[str] = None,
) -> None:
    try:
        metadata = {
            "admin_role": admin_user.get("admin_role"),
            "sos_session_id": sos_session_id,
            "ride_id": ride_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if resolution_notes:
            metadata["resolution_notes"] = resolution_notes
        sb.table("admin_logs").insert({
            "action": action,
            "admin_id": admin_user.get("id"),
            "target_id": sos_session_id,
            "target_table": "sos_sessions",
            "metadata": metadata,
        }).execute()
    except Exception as exc:
        logger.warning("Failed to write SOS admin log: %s", exc)


def _normalize_alert_fields(session_row: dict[str, Any]) -> dict[str, Any]:
    metadata = _dictish(session_row.get("metadata"))
    raw_payload = _dictish(
        _first_present(
            metadata.get("raw_payload"),
            session_row.get("raw_payload"),
            session_row.get("payload"),
            session_row.get("alert_payload"),
            session_row.get("alert_data"),
        )
    )

    alert_source = _clean_text(
        _first_present(session_row.get("alert_source"), raw_payload.get("alert_source"), raw_payload.get("source"))
    )
    if not alert_source:
        alert_source = "driver_app" if session_row.get("triggered_by_driver") else "rider_app"

    alert_type = _clean_text(
        _first_present(session_row.get("alert_type"), raw_payload.get("alert_type"), raw_payload.get("type"), raw_payload.get("event_type"))
    ) or "panic_button"

    default_message = (
        "Driver triggered SOS during ride" if session_row.get("triggered_by_driver") else "Passenger triggered SOS during ride"
    ) if session_row.get("ride_id") else (
        "Driver triggered SOS" if session_row.get("triggered_by_driver") else "Customer triggered SOS"
    )

    metadata = {**metadata, "raw_payload": raw_payload}
    return {
        "alert_type": alert_type,
        "alert_source": alert_source,
        "message": _clean_text(_first_present(session_row.get("message"), raw_payload.get("message"), raw_payload.get("title"))) or default_message,
        "description": _clean_text(_first_present(session_row.get("description"), raw_payload.get("description"), raw_payload.get("details"), raw_payload.get("notes"))),
        "location_name": _clean_text(_first_present(session_row.get("location_name"), raw_payload.get("location_name"), raw_payload.get("name"), raw_payload.get("landmark"))),
        "address": _clean_text(_first_present(session_row.get("address"), raw_payload.get("address"), raw_payload.get("formatted_address"))),
        "latitude": _as_float(_first_present(session_row.get("last_latitude"), session_row.get("latitude"), raw_payload.get("latitude"), raw_payload.get("lat"))),
        "longitude": _as_float(_first_present(session_row.get("last_longitude"), session_row.get("longitude"), raw_payload.get("longitude"), raw_payload.get("lng"))),
        "metadata": metadata or None,
    }


def _resolve_sos_status(session_row: dict[str, Any]) -> str:
    stored_status = _clean_text(session_row.get("status"))
    if stored_status in {"active", "resolved", "cancelled"}:
        return stored_status
    if session_row.get("resolved_at") or session_row.get("resolved_by") or session_row.get("resolution_notes"):
        return "resolved"
    if not session_row.get("is_active", True) and session_row.get("cancelled_at"):
        return "resolved"
    return "active"


def _build_sos_session_item(
    session_row: dict[str, Any],
    *,
    rides_by_id: dict[str, dict[str, Any]],
    users_by_id: dict[str, dict[str, Any]],
    driver_profiles_by_id: dict[str, dict[str, Any]],
    driver_profiles_by_user_id: dict[str, dict[str, Any]],
    responder_counts: dict[str, int],
) -> SosSessionItem:
    session_id = str(session_row.get("id"))
    ride = rides_by_id.get(str(session_row.get("ride_id"))) if session_row.get("ride_id") else None
    triggered_by = str(session_row.get("user_id")) if session_row.get("user_id") else None
    trigger_user = users_by_id.get(triggered_by or "", {})

    customer_user_id = str(ride.get("customer_id")) if ride and ride.get("customer_id") else None
    if not customer_user_id and not session_row.get("triggered_by_driver"):
        customer_user_id = triggered_by
    customer_user = users_by_id.get(customer_user_id or "", {})

    driver_profile = None
    if ride and ride.get("driver_id"):
        driver_profile = driver_profiles_by_id.get(str(ride.get("driver_id")))
    if driver_profile is None and session_row.get("triggered_by_driver") and triggered_by:
        driver_profile = driver_profiles_by_user_id.get(triggered_by)

    driver_id = str(driver_profile.get("id")) if driver_profile and driver_profile.get("id") else (str(ride.get("driver_id")) if ride and ride.get("driver_id") else None)
    driver_user_id = str(driver_profile.get("user_id")) if driver_profile and driver_profile.get("user_id") else (triggered_by if session_row.get("triggered_by_driver") else None)
    driver_user = users_by_id.get(driver_user_id or "", {})

    resolved_by = str(session_row.get("resolved_by")) if session_row.get("resolved_by") else None
    alert_fields = _normalize_alert_fields(session_row)

    return SosSessionItem(
        id=session_id,
        status=_resolve_sos_status(session_row),
        created_at=_iso_or_none(session_row.get("created_at") or session_row.get("triggered_at")),
        resolved_at=_iso_or_none(session_row.get("resolved_at")),
        resolved_by=resolved_by,
        resolution_notes=_clean_text(session_row.get("resolution_notes")),
        expires_at=_iso_or_none(session_row.get("expires_at")),
        last_location_timestamp=_iso_or_none(session_row.get("last_location_update")),
        latitude=alert_fields["latitude"],
        longitude=alert_fields["longitude"],
        cancelled_at=_iso_or_none(session_row.get("cancelled_at")),
        ride_id=str(session_row.get("ride_id")) if session_row.get("ride_id") else None,
        triggered_by=triggered_by,
        customer_user_id=customer_user_id,
        customer_name=_clean_text(_first_present(ride.get("customer_name") if ride else None, customer_user.get("full_name"), trigger_user.get("full_name") if not session_row.get("triggered_by_driver") else None)),
        customer_phone=_clean_text(_first_present(ride.get("customer_phone") if ride else None, customer_user.get("phone_number"), trigger_user.get("phone_number") if not session_row.get("triggered_by_driver") else None)),
        driver_id=driver_id,
        driver_user_id=driver_user_id,
        driver_name=_clean_text(_first_present(ride.get("driver_name") if ride else None, driver_user.get("full_name"), trigger_user.get("full_name") if session_row.get("triggered_by_driver") else None)),
        driver_phone=_clean_text(_first_present(ride.get("driver_phone") if ride else None, driver_user.get("phone_number"), trigger_user.get("phone_number") if session_row.get("triggered_by_driver") else None)),
        alert_type=alert_fields["alert_type"],
        alert_source=alert_fields["alert_source"],
        message=alert_fields["message"],
        description=alert_fields["description"],
        location_name=alert_fields["location_name"],
        address=alert_fields["address"],
        metadata=alert_fields["metadata"],
        triggered_by_driver=bool(session_row.get("triggered_by_driver", False)),
        responder_count=responder_counts.get(session_id, 0),
        tracking_url=_session_tracking_url(session_row),
    )


def _load_sos_context(sb, session_rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, int]]:
    ride_ids = [str(row.get("ride_id")) for row in session_rows if row.get("ride_id")]
    rides_by_id = _query_rides_by_ids(sb, ride_ids)

    triggered_user_ids = [str(row.get("user_id")) for row in session_rows if row.get("user_id")]
    customer_user_ids = [str(ride.get("customer_id")) for ride in rides_by_id.values() if ride.get("customer_id")]
    driver_ids = [str(ride.get("driver_id")) for ride in rides_by_id.values() if ride.get("driver_id")]
    driver_profiles_by_id = _query_driver_profiles_by_ids(sb, driver_ids)
    driver_profiles_by_user_id = _query_driver_profiles_by_user_ids(sb, triggered_user_ids)
    driver_user_ids = [str(profile.get("user_id")) for profile in list(driver_profiles_by_id.values()) + list(driver_profiles_by_user_id.values()) if profile.get("user_id")]
    resolved_by_ids = [str(row.get("resolved_by")) for row in session_rows if row.get("resolved_by")]

    users_by_id = _query_users_by_ids(
        sb,
        list({*triggered_user_ids, *customer_user_ids, *driver_user_ids, *resolved_by_ids}),
    )
    responder_counts = _query_responder_counts(sb, [str(row.get("id")) for row in session_rows if row.get("id")])
    return rides_by_id, users_by_id, driver_profiles_by_id, driver_profiles_by_user_id, responder_counts


def _get_sos_session_item(sb, session_row: dict[str, Any]) -> SosSessionItem:
    rides_by_id, users_by_id, driver_profiles_by_id, driver_profiles_by_user_id, responder_counts = _load_sos_context(sb, [session_row])
    return _build_sos_session_item(
        session_row,
        rides_by_id=rides_by_id,
        users_by_id=users_by_id,
        driver_profiles_by_id=driver_profiles_by_id,
        driver_profiles_by_user_id=driver_profiles_by_user_id,
        responder_counts=responder_counts,
    )


def _normalize_emergency_contacts(rows: list[dict[str, Any]]) -> list[EmergencyContactItem]:
    contacts: list[EmergencyContactItem] = []
    for row in rows or []:
        contacts.append(EmergencyContactItem(
            id=str(row.get("id", "")),
            name=_clean_text(_first_present(row.get("name"), row.get("full_name"), row.get("contact_name"))),
            phone_number=_clean_text(_first_present(row.get("phone_number"), row.get("phone"), row.get("contact_phone"))),
            contact_relationship=_clean_text(_first_present(row.get("contact_relationship"), row.get("relationship"), row.get("relation"))),
            created_at=_iso_or_none(row.get("created_at")),
        ))
    return contacts


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
    tracking_token = secrets.token_hex(16)
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
            "tracking_token": tracking_token,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create SOS session: {e}")

    # Notify all admin users via push notification
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

    # Broadcast to admin WebSocket connections (best-effort)
    tracking_url = f"{settings.PUBLIC_BASE_URL}/api/v1/sos/track/{tracking_token}/map"
    ws_payload = {
        "session_id": session_id,
        "user_id": user_id,
        "full_name": user_name,
        "triggered_by_driver": body.triggered_by_driver,
        "ride_id": body.ride_id,
        "latitude": body.latitude,
        "longitude": body.longitude,
        "triggered_at": now,
        "tracking_url": tracking_url,
    }
    try:
        from app.services.ws.router import manager as ws_manager
        asyncio.get_event_loop().create_task(ws_manager.broadcast("sos_triggered", ws_payload))
    except RuntimeError:
        pass

    try:
        from app.services.live_location.router import emit_live_location_event
        emit_live_location_event("session_started", sos_session_id=session_id)
    except Exception:
        pass

    return {
        "session_id": session_id,
        "tracking_url": tracking_url,
        "message": "SOS triggered",
    }


@router.get("/admin/sessions", response_model=SosSessionListResponse)
def list_sos_sessions(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user=Depends(require_sos_access),
):
    try:
        sb = get_supabase()
        fetch_limit = max(limit + offset + 100, 250)
        result = sb.table("sos_sessions").select("*").order("triggered_at", desc=True).limit(fetch_limit).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    session_rows = result.data or []
    rides_by_id, users_by_id, driver_profiles_by_id, driver_profiles_by_user_id, responder_counts = _load_sos_context(sb, session_rows)

    sessions = [
        _build_sos_session_item(
            row,
            rides_by_id=rides_by_id,
            users_by_id=users_by_id,
            driver_profiles_by_id=driver_profiles_by_id,
            driver_profiles_by_user_id=driver_profiles_by_user_id,
            responder_counts=responder_counts,
        )
        for row in session_rows
    ]

    if status:
        sessions = [session for session in sessions if session.status == status]
    if search:
        needle = search.strip().lower()
        sessions = [
            session for session in sessions
            if any(
                needle in str(value).lower()
                for value in (
                    session.id,
                    session.ride_id,
                    session.customer_name,
                    session.customer_phone,
                    session.driver_name,
                    session.driver_phone,
                    session.triggered_by,
                )
                if value
            )
        ]

    paged = sessions[offset: offset + limit]
    return SosSessionListResponse(sessions=paged, total=len(sessions), limit=limit, offset=offset)


@router.get("/admin/sessions/{session_id}", response_model=SosSessionDetailResponse)
def get_sos_session_detail(session_id: str, _user=Depends(require_sos_access)):
    try:
        sb = get_supabase()
        result = (
            sb.table("sos_sessions")
            .select("*")
            .eq("id", session_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="SOS session not found")

    session_row = result.data
    session = _get_sos_session_item(sb, session_row)

    # Fetch responders
    responders = []
    try:
        resp_result = (
            sb.table("sos_driver_alerts")
            .select("*")
            .eq("sos_session_id", session_id)
            .execute()
        )
        responder_user_ids = [str(resp.get("driver_user_id")) for resp in (resp_result.data or []) if resp.get("driver_user_id")]
        responder_users = _query_users_by_ids(sb, responder_user_ids)
        for resp in resp_result.data or []:
            driver_user = responder_users.get(str(resp.get("driver_user_id")), {})
            responders.append(SosResponderItem(
                id=str(resp.get("id", "")),
                driver_user_id=str(resp.get("driver_user_id")) if resp.get("driver_user_id") else None,
                driver_name=driver_user.get("full_name"),
                driver_phone=driver_user.get("phone_number"),
                notified_at=_iso_or_none(resp.get("notified_at")),
                responded=resp.get("responded", False),
                responded_at=_iso_or_none(resp.get("responded_at")),
            ))
    except Exception:
        pass

    _write_sos_admin_log(
        sb,
        _user,
        action="sos_view",
        sos_session_id=session_id,
        ride_id=session.ride_id,
    )

    emergency_contacts = []
    if session.customer_user_id:
        try:
            contacts_result = (
                sb.table("emergency_contacts")
                .select("*")
                .eq("user_id", session.customer_user_id)
                .order("created_at", desc=True)
                .execute()
            )
            emergency_contacts = _normalize_emergency_contacts(contacts_result.data or [])
        except Exception:
            emergency_contacts = []

    return SosSessionDetailResponse(
        **session.model_dump(),
        alert_radius_km=float(session_row.get("alert_radius_km") or 15.0),
        emergency_contacts=emergency_contacts,
        responders=responders,
    )


@router.post("/admin/sessions/{session_id}/resolve", response_model=SosSessionDetailResponse)
@router.patch("/admin/sessions/{session_id}/resolve", response_model=SosSessionDetailResponse)
def resolve_sos_session(
    session_id: str,
    body: Optional[ResolveSosBody] = None,
    _user=Depends(require_sos_access),
):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")

    now = datetime.now(timezone.utc).isoformat()
    resolution_notes = _clean_text(body.resolution_notes if body else None)
    try:
        sb = get_supabase()
        result = (
            sb.table("sos_sessions")
            .update({
                "is_active": False,
                "cancelled_at": now,
                "resolved_at": now,
                "resolved_by": admin_id,
                "resolution_notes": resolution_notes,
            })
            .eq("id", session_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="SOS session not found")

    _write_sos_admin_log(
        sb,
        _user,
        action="sos_resolved",
        sos_session_id=session_id,
        ride_id=(result.data[0] or {}).get("ride_id") if isinstance(result.data, list) and result.data else None,
        resolution_notes=resolution_notes,
    )

    try:
        from app.services.live_location.router import emit_live_location_event
        emit_live_location_event("session_manually_stopped", sos_session_id=session_id)
    except Exception:
        pass

    return get_sos_session_detail(session_id, _user)


# ── Shorthand: active sessions ───────────────────────────────────────────────


@router.get("/sessions/active", response_model=SosSessionListResponse)
def list_active_sos_sessions(_user=Depends(require_sos_access)):
    """Convenience alias: returns only currently active SOS sessions."""
    return list_sos_sessions(status="active", search=None, limit=50, offset=0, _user=_user)


# ── Responders list ──────────────────────────────────────────────────────────


@router.get("/session/{session_id}/responders", response_model=SosResponderListResponse)
def get_sos_responders(session_id: str, _user=Depends(require_sos_access)):
    """Return the list of nearby drivers who were notified for a driver-SOS session."""
    sb = get_supabase()
    try:
        resp_result = (
            sb.table("sos_driver_alerts")
            .select("*, users(full_name, phone_number)")
            .eq("sos_session_id", session_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    responders = []
    responder_user_ids = [str(resp.get("driver_user_id")) for resp in (resp_result.data or []) if resp.get("driver_user_id")]
    user_map = _query_users_by_ids(sb, responder_user_ids)
    for resp in resp_result.data or []:
        driver_user = user_map.get(str(resp.get("driver_user_id")), {})
        responders.append(SosResponderItem(
            id=str(resp.get("id", "")),
            driver_user_id=str(resp.get("driver_user_id")) if resp.get("driver_user_id") else None,
            driver_name=driver_user.get("full_name"),
            driver_phone=driver_user.get("phone_number"),
            notified_at=_iso_or_none(resp.get("notified_at")),
            responded=resp.get("responded", False),
            responded_at=_iso_or_none(resp.get("responded_at")),
        ))

    return SosResponderListResponse(
        session_id=session_id,
        notified_count=len(responders),
        responders=responders,
    )


@router.get("/admin/sessions/{session_id}/emergency-contacts", response_model=List[EmergencyContactItem])
def get_sos_emergency_contacts(session_id: str, _user=Depends(require_sos_access)):
    sb = get_supabase()
    try:
        session_result = (
            sb.table("sos_sessions")
            .select("*")
            .eq("id", session_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not session_result.data:
        raise HTTPException(status_code=404, detail="SOS session not found")

    session = _get_sos_session_item(sb, session_result.data)
    if not session.customer_user_id:
        return []

    try:
        contacts_result = (
            sb.table("emergency_contacts")
            .select("*")
            .eq("user_id", session.customer_user_id)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return _normalize_emergency_contacts(contacts_result.data or [])


# ── Mobile location push for SOS ─────────────────────────────────────────────


@router.post("/session/{session_id}/location", status_code=200)
def push_sos_location(
    session_id: str,
    body: SosLocationBody,
    _user: dict = Depends(get_current_user),
):
    """
    Mobile-app endpoint: push a real-time GPS update for an active SOS session.
    Updates last_latitude/longitude/heading on the session and broadcasts
    a WebSocket event to all connected admin sessions.
    """
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    try:
        result = (
            sb.table("sos_sessions")
            .update({
                "last_latitude": body.latitude,
                "last_longitude": body.longitude,
                "last_heading": body.heading,
                "last_location_update": now,
            })
            .eq("id", session_id)
            .eq("is_active", True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Active SOS session not found")

    # Broadcast to admin WebSocket connections (best-effort)
    ws_payload = {
        "session_id": session_id,
        "latitude": body.latitude,
        "longitude": body.longitude,
        "heading": body.heading,
        "speed": body.speed,
        "accuracy": body.accuracy,
        "last_update": now,
    }
    try:
        from app.services.ws.router import manager as ws_manager
        asyncio.get_event_loop().create_task(ws_manager.broadcast("sos_location_update", ws_payload))
    except RuntimeError:
        pass

    try:
        from app.services.live_location.router import emit_live_location_event
        emit_live_location_event("location_updated", sos_session_id=session_id)
    except Exception:
        pass

    return {"session_id": session_id, "updated_at": now}


# ── Public SOS tracking (no auth required) ──────────────────────────────────


@router.get("/track/{token}", include_in_schema=False)
def public_track_sos(token: str):
    """
    Public JSON endpoint for polling the current SOS location.
    Used as a WebSocket fallback and by the public HTML tracking page.
    No authentication required — the token is the only credential.
    """
    sb = get_supabase()
    try:
        result = (
            sb.table("sos_sessions")
            .select(
                "id, is_active, triggered_at, last_latitude, last_longitude, "
                "last_heading, last_location_update, triggered_by_driver, ride_id"
            )
            .eq("tracking_token", token)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Tracking session not found")

    r = result.data
    return {
        "session_id": r.get("id"),
        "is_active": r.get("is_active"),
        "triggered_at": r.get("triggered_at"),
        "triggered_by_driver": r.get("triggered_by_driver"),
        "ride_id": r.get("ride_id"),
        "latitude": r.get("last_latitude"),
        "longitude": r.get("last_longitude"),
        "heading": r.get("last_heading"),
        "last_update": r.get("last_location_update"),
    }


@router.get("/track/{token}/map", response_class=HTMLResponse, include_in_schema=False)
def public_sos_map_page(token: str):
    """
    Public HTML page that shows real-time SOS location on a Leaflet map.
    Accessible without authentication — the token serves as the access credential.
    """
    api_url = f"{settings.PUBLIC_BASE_URL}/api/v1/sos/track/{token}"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>SOS Live Tracking</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #1a1a2e; color: #eee; }}
    #header {{
      background: #c0392b; padding: 12px 20px;
      display: flex; align-items: center; gap: 12px;
    }}
    #header h1 {{ font-size: 1.1rem; font-weight: 700; color: #fff; }}
    #status-badge {{
      margin-left: auto; background: #e74c3c; color: #fff;
      padding: 4px 10px; border-radius: 999px; font-size: 0.75rem; font-weight: 600;
    }}
    #map {{ height: calc(100vh - 100px); width: 100%; }}
    #footer {{
      background: #111; padding: 8px 20px;
      font-size: 0.75rem; color: #888; display: flex; gap: 20px;
    }}
  </style>
</head>
<body>
  <div id="header">
    <span style="font-size:1.6rem">🆘</span>
    <h1>AlboTax SOS Live Tracking</h1>
    <span id="status-badge">ACTIVE</span>
  </div>
  <div id="map"></div>
  <div id="footer">
    <span id="last-update">Last update: —</span>
    <span id="coords">Position: —</span>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const API_URL = "{api_url}";
    const map = L.map("map").setView([0, 20], 5);
    L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      attribution: "© OpenStreetMap contributors"
    }}).addTo(map);

    const redIcon = L.divIcon({{
      html: '<div style="width:18px;height:18px;background:#e74c3c;border:3px solid #fff;border-radius:50%;box-shadow:0 2px 6px rgba(0,0,0,.5)"></div>',
      className: "", iconSize: [18, 18], iconAnchor: [9, 9]
    }});

    let marker = null;
    let firstLoad = true;

    async function refresh() {{
      try {{
        const res = await fetch(API_URL);
        if (res.status === 404) {{
          document.getElementById("status-badge").textContent = "RESOLVED";
          document.getElementById("status-badge").style.background = "#27ae60";
          return;
        }}
        const d = await res.json();
        if (!d.is_active) {{
          document.getElementById("status-badge").textContent = "RESOLVED";
          document.getElementById("status-badge").style.background = "#27ae60";
        }}
        if (d.latitude && d.longitude) {{
          const latlng = [d.latitude, d.longitude];
          if (!marker) {{
            marker = L.marker(latlng, {{ icon: redIcon }}).addTo(map)
              .bindPopup("SOS location").openPopup();
          }} else {{
            marker.setLatLng(latlng);
          }}
          if (firstLoad) {{ map.setView(latlng, 15); firstLoad = false; }}
          document.getElementById("coords").textContent =
            `Position: ${{d.latitude.toFixed(5)}}, ${{d.longitude.toFixed(5)}}`;
        }}
        if (d.last_update) {{
          document.getElementById("last-update").textContent =
            "Last update: " + new Date(d.last_update).toLocaleTimeString();
        }}
      }} catch(e) {{ console.error(e); }}
    }}

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)

