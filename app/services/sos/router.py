from __future__ import annotations

import asyncio
import secrets
import logging
from typing import Optional, List
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.core.config import settings
from app.core.dependencies import require_admin, get_current_user
from app.core.supabase import get_supabase
from app.services.notifications.router import send_push_to_users

router = APIRouter(prefix="/sos", tags=["sos"])
logger = logging.getLogger(__name__)


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
    last_heading: Optional[float] = None
    cancelled_at: Optional[str] = None
    ride_id: Optional[str] = None
    triggered_by_driver: bool = False
    responder_count: int = 0
    tracking_url: Optional[str] = None


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
    last_heading: Optional[float] = None
    last_location_update: Optional[str] = None
    cancelled_at: Optional[str] = None
    ride_id: Optional[str] = None
    triggered_by_driver: bool = False
    alert_radius_km: float = 15.0
    responders: List[SosResponderItem] = []
    tracking_url: Optional[str] = None


class SosResponderListResponse(BaseModel):
    session_id: str
    notified_count: int
    responders: List[SosResponderItem] = []


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
            last_heading=r.get("last_heading"),
            tracking_url=(
                f"{settings.PUBLIC_BASE_URL}/api/v1/sos/track/{r['tracking_token']}/map"
                if r.get("tracking_token") else None
            ),
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
        last_heading=r.get("last_heading"),
        last_location_update=r.get("last_location_update"),
        cancelled_at=r.get("cancelled_at"),
        ride_id=r.get("ride_id"),
        triggered_by_driver=r.get("triggered_by_driver", False),
        alert_radius_km=r.get("alert_radius_km", 15.0),
        responders=responders,
        tracking_url=(
            f"{settings.PUBLIC_BASE_URL}/api/v1/sos/track/{r['tracking_token']}/map"
            if r.get("tracking_token") else None
        ),
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

    try:
        from app.services.live_location.router import emit_live_location_event
        emit_live_location_event("session_manually_stopped", sos_session_id=session_id)
    except Exception:
        pass

    return {
        "message": "SOS session resolved",
        "session_id": session_id,
        "resolution_notes": body.resolution_notes if body else None,
    }


# ── Shorthand: active sessions ───────────────────────────────────────────────


@router.get("/sessions/active", response_model=SosSessionListResponse)
def list_active_sos_sessions(_user=Depends(require_admin)):
    """Convenience alias: returns only currently active SOS sessions."""
    sb = get_supabase()
    try:
        result = (
            sb.table("sos_sessions")
            .select("*, users(full_name, phone_number)")
            .eq("is_active", True)
            .order("triggered_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    sessions = []
    for r in result.data or []:
        user_info = r.pop("users", {}) or {}
        responder_count = 0
        try:
            resp_res = (
                sb.table("sos_driver_alerts")
                .select("id", count="exact")
                .eq("sos_session_id", r.get("id"))
                .execute()
            )
            responder_count = len(resp_res.data or [])
        except Exception:
            pass

        sessions.append(SosSessionItem(
            id=r.get("id"),
            user_id=r.get("user_id"),
            full_name=user_info.get("full_name"),
            phone_number=user_info.get("phone_number"),
            is_active=r.get("is_active", True),
            triggered_at=r.get("triggered_at"),
            expires_at=r.get("expires_at"),
            last_latitude=r.get("last_latitude"),
            last_longitude=r.get("last_longitude"),
            last_heading=r.get("last_heading"),
            cancelled_at=r.get("cancelled_at"),
            ride_id=r.get("ride_id"),
            triggered_by_driver=r.get("triggered_by_driver", False),
            responder_count=responder_count,
            tracking_url=(
                f"{settings.PUBLIC_BASE_URL}/api/v1/sos/track/{r['tracking_token']}/map"
                if r.get("tracking_token") else None
            ),
        ))

    return SosSessionListResponse(sessions=sessions, total=len(sessions))


# ── Responders list ──────────────────────────────────────────────────────────


@router.get("/session/{session_id}/responders", response_model=SosResponderListResponse)
def get_sos_responders(session_id: str, _user=Depends(require_admin)):
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

    return SosResponderListResponse(
        session_id=session_id,
        notified_count=len(responders),
        responders=responders,
    )


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

