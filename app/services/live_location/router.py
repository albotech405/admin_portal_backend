from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState

from app.core.dependencies import require_role, resolve_admin_from_token
from app.core.supabase import get_supabase

router = APIRouter(prefix="/live-location/admin", tags=["live-location"])
logger = logging.getLogger(__name__)

ACTIVE_RIDE_STATUSES = {"in_progress", "driver_en_route", "arrived"}
ENDED_RIDE_STATUSES = {"completed", "cancelled", "cancelled_by_driver", "cancelled_by_customer"}
DEFAULT_STALE_AFTER_SECONDS = 120
RIDE_SHARE_TTL_SECONDS = 45 * 60


class CoordinatePoint(BaseModel):
    latitude: float
    longitude: float
    heading: Optional[float] = None
    speed: Optional[float] = None
    accuracy: Optional[float] = None
    timestamp: Optional[str] = None


class LocationAnchor(BaseModel):
    name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class ParticipantPayload(BaseModel):
    participant_type: Literal["driver", "customer"]
    name: Optional[str] = None
    phone: Optional[str] = None
    source: Literal["trip_tracking", "manual_live_share", "sos"]
    status: Literal["active", "stale", "ended", "manually_stopped", "expired"]
    is_live: bool
    started_at: Optional[str] = None
    expires_at: Optional[str] = None
    stopped_at: Optional[str] = None
    stop_reason: Optional[str] = None
    last_updated_at: Optional[str] = None
    waiting_for_first_update: bool = False
    point: Optional[CoordinatePoint] = None


class SessionParticipants(BaseModel):
    driver: Optional[ParticipantPayload] = None
    customer: Optional[ParticipantPayload] = None


class LiveLocationSession(BaseModel):
    id: str
    type: Literal["ride", "sos"]
    source: Literal["trip_tracking", "manual_live_share", "sos"]
    status: Literal["active", "stale", "ended", "manually_stopped", "expired"]
    is_live: bool
    started_at: Optional[str] = None
    expires_at: Optional[str] = None
    ended_at: Optional[str] = None
    stopped_at: Optional[str] = None
    stop_reason: Optional[str] = None
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS
    ride_id: Optional[str] = None
    sos_session_id: Optional[str] = None
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    pickup: Optional[LocationAnchor] = None
    destination: Optional[LocationAnchor] = None
    stops: List[LocationAnchor] = Field(default_factory=list)
    route_path: List[CoordinatePoint] = Field(default_factory=list)
    last_location_timestamp: Optional[str] = None
    waiting_for_first_update: bool = False
    stream_error_code: Optional[str] = None
    end_reason: Optional[str] = None
    permission_revoked_at: Optional[str] = None
    last_known_city: Optional[str] = None
    last_known_zone: Optional[str] = None
    participants: SessionParticipants = Field(default_factory=SessionParticipants)


class SessionListResponse(BaseModel):
    sessions: List[LiveLocationSession]
    total: int
    limit: int
    offset: int


class AuditViewBody(BaseModel):
    action: Literal["live_location_view"] = "live_location_view"
    entity_viewed: str
    session_id: str
    session_type: Literal["trip_tracking", "sos_tracking"]
    source_surface: Optional[Literal["dashboard", "ride_detail", "sos_detail", "search_history"]] = None
    ride_id: Optional[str] = None
    sos_session_id: Optional[str] = None


class StreamManager:
    def __init__(self) -> None:
        self._connections: Dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self,
        websocket: WebSocket,
        admin_user_id: str,
        admin_role: str,
        session_type_filter: Optional[str] = None,
    ) -> str:
        await websocket.accept()
        connection_id = str(uuid4())
        async with self._lock:
            self._connections[connection_id] = {
                "websocket": websocket,
                "admin_user_id": admin_user_id,
                "admin_role": admin_role,
                "session_type_filter": session_type_filter,
            }
        return connection_id

    async def disconnect(self, connection_id: str) -> None:
        async with self._lock:
            self._connections.pop(connection_id, None)

    async def broadcast(self, event: str, data: dict[str, Any], session_type: Optional[str] = None) -> None:
        async with self._lock:
            snapshot = list(self._connections.items())

        dead: list[str] = []
        payload = json.dumps({"event": event, "data": data})
        for connection_id, entry in snapshot:
            websocket = entry["websocket"]
            filter_type = entry.get("session_type_filter")
            if filter_type and session_type and filter_type != session_type:
                continue
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text(payload)
            except Exception:
                dead.append(connection_id)

        if dead:
            async with self._lock:
                for connection_id in dead:
                    self._connections.pop(connection_id, None)


stream_manager = StreamManager()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _iso(value: Any) -> Optional[str]:
    parsed = _parse_dt(value)
    if not parsed:
        return value if isinstance(value, str) else None
    return parsed.astimezone(timezone.utc).isoformat()


def _add_seconds(value: Any, seconds: int) -> Optional[str]:
    parsed = _parse_dt(value)
    if not parsed:
        return None
    return (parsed + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()


def _point_from_anchor(value: Any) -> Optional[LocationAnchor]:
    if not isinstance(value, dict):
        return None
    return LocationAnchor(
        name=value.get("name") or value.get("label") or value.get("address"),
        latitude=value.get("latitude") or value.get("lat"),
        longitude=value.get("longitude") or value.get("lng"),
    )


def _stops_from_payload(value: Any) -> List[LocationAnchor]:
    if not isinstance(value, list):
        return []
    return [stop for stop in (_point_from_anchor(item) for item in value) if stop]


def _pick_city_zone(*values: Any) -> tuple[Optional[str], Optional[str]]:
    for value in values:
        if isinstance(value, dict):
            city = value.get("city") or value.get("city_name")
            zone = value.get("zone") or value.get("zone_name") or value.get("district")
            if city or zone:
                return city, zone
    return None, None


def _derive_status(
    *,
    now: datetime,
    last_updated_at: Optional[Any],
    expires_at: Optional[Any],
    ended_at: Optional[Any],
    stopped_at: Optional[Any],
    stale_after_seconds: int,
) -> str:
    stopped_dt = _parse_dt(stopped_at)
    ended_dt = _parse_dt(ended_at)
    expires_dt = _parse_dt(expires_at)
    last_dt = _parse_dt(last_updated_at)

    if stopped_dt:
        return "manually_stopped"
    if ended_dt:
        return "ended"
    if expires_dt and now > expires_dt:
        return "expired"
    if last_dt and (now - last_dt).total_seconds() > stale_after_seconds:
        return "stale"
    return "active"


def _is_live(status: str) -> bool:
    return status == "active"


def _session_sort_key(session: LiveLocationSession) -> tuple[int, str]:
    priority = 0 if session.type == "sos" else 1
    timestamp = session.last_location_timestamp or session.started_at or ""
    return (priority, timestamp)


def _matches_search(session: LiveLocationSession, search: Optional[str]) -> bool:
    if not search:
        return True
    needle = search.strip().lower()
    haystack = [
        session.id,
        session.ride_id,
        session.sos_session_id,
        session.customer_name,
        session.customer_phone,
        session.driver_name,
        session.driver_phone,
    ]
    return any(needle in str(value).lower() for value in haystack if value)


def _matches_history_filters(
    session: LiveLocationSession,
    status: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    city: Optional[str],
    zone: Optional[str],
) -> bool:
    if status and session.status != status:
        return False

    if city and (session.last_known_city or "").lower() != city.lower():
        return False
    if zone and (session.last_known_zone or "").lower() != zone.lower():
        return False

    started_at = _parse_dt(session.started_at)
    if date_from:
        from_dt = _parse_dt(date_from)
        if from_dt and started_at and started_at < from_dt:
            return False
    if date_to:
        to_dt = _parse_dt(date_to)
        if to_dt and started_at and started_at > to_dt:
            return False
    return True


def _record_admin_log(
    admin_user: dict,
    *,
    entity_viewed: str,
    session_id: str,
    session_type: str,
    source_surface: Optional[str] = None,
    ride_id: Optional[str] = None,
    sos_session_id: Optional[str] = None,
    participants_present: Optional[list[str]] = None,
) -> None:
    try:
        sb = get_supabase()
        sb.table("admin_logs").insert({
            "action": "live_location_view",
            "admin_id": admin_user.get("id"),
            "target_id": session_id,
            "target_table": "live_location_sessions",
            "metadata": {
                "admin_role": admin_user.get("admin_role"),
                "entity_viewed": entity_viewed,
                "session_id": session_id,
                "session_type": session_type,
                "ride_id": ride_id,
                "sos_session_id": sos_session_id,
                "source_surface": source_surface,
                "participants_present": participants_present or [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }).execute()
    except Exception as exc:
        logger.warning("Failed to write live-location admin log: %s", exc)


def _query_ride_rows(active_only: bool, fetch_limit: int) -> List[dict[str, Any]]:
    sb = get_supabase()
    query = sb.table("rides").select(
        "id, ride_request_id, customer_id, driver_id, customer_name, customer_phone, "
        "driver_name, driver_phone, picking_point, destination, stops, status, started_at, "
        "completed_at, cancelled_at, cancelled_by, cancellation_reason, created_at"
    )
    statuses = list(ACTIVE_RIDE_STATUSES if active_only else ENDED_RIDE_STATUSES)
    return query.in_("status", statuses).order("created_at", desc=True).limit(fetch_limit).execute().data or []


def _query_ride_location_rows(ride_ids: List[str]) -> Dict[str, Dict[str, dict[str, Any]]]:
    if not ride_ids:
        return {}
    sb = get_supabase()
    rows = (
        sb.table("ride_location_updates")
        .select("ride_id, role, latitude, longitude, heading, speed, accuracy, updated_at")
        .in_("ride_id", ride_ids)
        .execute()
        .data
        or []
    )
    by_ride: Dict[str, Dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_ride.setdefault(row["ride_id"], {})[row["role"]] = row
    return by_ride


def _query_sos_rows(active_only: bool, fetch_limit: int) -> List[dict[str, Any]]:
    sb = get_supabase()
    query = sb.table("sos_sessions").select("*, users(full_name, phone_number)").order("triggered_at", desc=True).limit(fetch_limit)
    if active_only:
        query = query.eq("is_active", True)
    else:
        query = query.eq("is_active", False)
    return query.execute().data or []


def _query_rides_by_ids(ride_ids: List[str]) -> Dict[str, dict[str, Any]]:
    if not ride_ids:
        return {}
    sb = get_supabase()
    rows = (
        sb.table("rides")
        .select(
            "id, ride_request_id, customer_id, driver_id, customer_name, customer_phone, "
            "driver_name, driver_phone, picking_point, destination, stops, status, started_at, "
            "completed_at, cancelled_at, cancelled_by, cancellation_reason, created_at"
        )
        .in_("id", ride_ids)
        .execute()
        .data
        or []
    )
    return {row["id"]: row for row in rows}


def _participant_payload(
    *,
    participant_type: Literal["driver", "customer"],
    source: Literal["trip_tracking", "manual_live_share", "sos"],
    name: Optional[str],
    phone: Optional[str],
    started_at: Optional[str],
    expires_at: Optional[str],
    stopped_at: Optional[str],
    stop_reason: Optional[str],
    ended_at: Optional[str],
    last_updated_at: Optional[str],
    point_row: Optional[dict[str, Any]],
    stale_after_seconds: int,
    now: datetime,
) -> ParticipantPayload:
    status = _derive_status(
        now=now,
        last_updated_at=last_updated_at,
        expires_at=expires_at,
        ended_at=ended_at,
        stopped_at=stopped_at,
        stale_after_seconds=stale_after_seconds,
    )
    point = None
    if point_row:
        point = CoordinatePoint(
            latitude=point_row.get("latitude"),
            longitude=point_row.get("longitude"),
            heading=point_row.get("heading"),
            speed=point_row.get("speed"),
            accuracy=point_row.get("accuracy"),
            timestamp=_iso(point_row.get("updated_at") or point_row.get("timestamp")),
        )
    return ParticipantPayload(
        participant_type=participant_type,
        name=name,
        phone=phone,
        source=source,
        status=status,
        is_live=_is_live(status),
        started_at=started_at,
        expires_at=expires_at,
        stopped_at=stopped_at,
        stop_reason=stop_reason,
        last_updated_at=last_updated_at,
        waiting_for_first_update=point is None,
        point=point,
    )


def _build_ride_session(ride: dict[str, Any], location_rows: Dict[str, dict[str, Any]]) -> LiveLocationSession:
    now = datetime.now(timezone.utc)
    started_at = _iso(ride.get("started_at") or ride.get("created_at"))
    completed_at = _iso(ride.get("completed_at"))
    cancelled_at = _iso(ride.get("cancelled_at"))
    expires_at = _add_seconds(ride.get("started_at") or ride.get("created_at"), RIDE_SHARE_TTL_SECONDS)
    end_reason = ride.get("cancellation_reason") if ride.get("status") != "completed" else "ride_completed"

    driver_row = location_rows.get("driver")
    customer_row = location_rows.get("customer")
    last_location_timestamp = max(
        [timestamp for timestamp in [_iso((driver_row or {}).get("updated_at")), _iso((customer_row or {}).get("updated_at"))] if timestamp],
        default=None,
    )

    driver_participant = None
    if ride.get("driver_id") or driver_row:
        driver_participant = _participant_payload(
            participant_type="driver",
            source="trip_tracking",
            name=ride.get("driver_name"),
            phone=ride.get("driver_phone"),
            started_at=started_at,
            expires_at=expires_at,
            stopped_at=None,
            stop_reason=None,
            ended_at=completed_at or cancelled_at,
            last_updated_at=_iso((driver_row or {}).get("updated_at")),
            point_row=driver_row,
            stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
            now=now,
        )

    customer_participant = None
    if customer_row:
        customer_participant = _participant_payload(
            participant_type="customer",
            source="manual_live_share",
            name=ride.get("customer_name"),
            phone=ride.get("customer_phone"),
            started_at=started_at,
            expires_at=expires_at,
            stopped_at=None,
            stop_reason=None,
            ended_at=completed_at or cancelled_at,
            last_updated_at=_iso(customer_row.get("updated_at")),
            point_row=customer_row,
            stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
            now=now,
        )

    if ride.get("status") in ENDED_RIDE_STATUSES:
        status = "ended"
    else:
        status = _derive_status(
            now=now,
            last_updated_at=last_location_timestamp,
            expires_at=expires_at,
            ended_at=None,
            stopped_at=None,
            stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
        )

    city, zone = _pick_city_zone(ride.get("picking_point"), ride.get("destination"))
    return LiveLocationSession(
        id=f"ride:{ride['id']}",
        type="ride",
        source="trip_tracking",
        status=status,
        is_live=_is_live(status),
        started_at=started_at,
        expires_at=expires_at,
        ended_at=completed_at or cancelled_at,
        stopped_at=None,
        stop_reason=None,
        stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
        ride_id=ride.get("id"),
        sos_session_id=None,
        customer_id=ride.get("customer_id"),
        customer_name=ride.get("customer_name"),
        customer_phone=ride.get("customer_phone"),
        driver_id=ride.get("driver_id"),
        driver_name=ride.get("driver_name"),
        driver_phone=ride.get("driver_phone"),
        pickup=_point_from_anchor(ride.get("picking_point")),
        destination=_point_from_anchor(ride.get("destination")),
        stops=_stops_from_payload(ride.get("stops")),
        route_path=[],
        last_location_timestamp=last_location_timestamp,
        waiting_for_first_update=driver_participant is not None and driver_participant.waiting_for_first_update,
        end_reason=end_reason,
        last_known_city=city,
        last_known_zone=zone,
        participants=SessionParticipants(driver=driver_participant, customer=customer_participant),
    )


def _build_sos_session(sos_row: dict[str, Any], linked_ride: Optional[dict[str, Any]], ride_locations: Dict[str, dict[str, Any]]) -> LiveLocationSession:
    now = datetime.now(timezone.utc)
    user_info = sos_row.get("users") or {}
    started_at = _iso(sos_row.get("triggered_at"))
    expires_at = _iso(sos_row.get("expires_at"))
    stopped_at = _iso(sos_row.get("cancelled_at"))
    last_updated_at = _iso(sos_row.get("last_location_update"))
    stop_reason = "sos_manually_stopped" if stopped_at else None

    trigger_point = None
    if sos_row.get("last_latitude") is not None and sos_row.get("last_longitude") is not None:
        trigger_point = {
            "latitude": sos_row.get("last_latitude"),
            "longitude": sos_row.get("last_longitude"),
            "heading": sos_row.get("last_heading"),
            "speed": None,
            "accuracy": None,
            "timestamp": last_updated_at,
        }

    driver_name = linked_ride.get("driver_name") if linked_ride else None
    driver_phone = linked_ride.get("driver_phone") if linked_ride else None
    driver_id = linked_ride.get("driver_id") if linked_ride else None
    customer_name = linked_ride.get("customer_name") if linked_ride else user_info.get("full_name")
    customer_phone = linked_ride.get("customer_phone") if linked_ride else user_info.get("phone_number")
    customer_id = linked_ride.get("customer_id") if linked_ride else sos_row.get("user_id")

    driver_row = ride_locations.get("driver")
    customer_row = ride_locations.get("customer")
    driver_participant = None
    customer_participant = None

    if sos_row.get("triggered_by_driver"):
        driver_participant = _participant_payload(
            participant_type="driver",
            source="sos",
            name=driver_name or user_info.get("full_name"),
            phone=driver_phone or user_info.get("phone_number"),
            started_at=started_at,
            expires_at=expires_at,
            stopped_at=stopped_at,
            stop_reason=stop_reason,
            ended_at=None if sos_row.get("is_active") else stopped_at,
            last_updated_at=last_updated_at,
            point_row=trigger_point,
            stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
            now=now,
        )
        if customer_row:
            customer_participant = _participant_payload(
                participant_type="customer",
                source="manual_live_share",
                name=customer_name,
                phone=customer_phone,
                started_at=started_at,
                expires_at=expires_at,
                stopped_at=stopped_at,
                stop_reason=stop_reason,
                ended_at=None if sos_row.get("is_active") else stopped_at,
                last_updated_at=_iso(customer_row.get("updated_at")),
                point_row=customer_row,
                stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
                now=now,
            )
    else:
        customer_participant = _participant_payload(
            participant_type="customer",
            source="sos",
            name=customer_name,
            phone=customer_phone,
            started_at=started_at,
            expires_at=expires_at,
            stopped_at=stopped_at,
            stop_reason=stop_reason,
            ended_at=None if sos_row.get("is_active") else stopped_at,
            last_updated_at=last_updated_at,
            point_row=trigger_point,
            stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
            now=now,
        )
        if driver_row:
            driver_participant = _participant_payload(
                participant_type="driver",
                source="trip_tracking",
                name=driver_name,
                phone=driver_phone,
                started_at=started_at,
                expires_at=expires_at,
                stopped_at=stopped_at,
                stop_reason=stop_reason,
                ended_at=None if sos_row.get("is_active") else stopped_at,
                last_updated_at=_iso(driver_row.get("updated_at")),
                point_row=driver_row,
                stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
                now=now,
            )

    if not sos_row.get("is_active") and stopped_at:
        status = "manually_stopped"
        ended_at = stopped_at
    else:
        ended_at = None
        status = _derive_status(
            now=now,
            last_updated_at=last_updated_at,
            expires_at=expires_at,
            ended_at=ended_at,
            stopped_at=stopped_at,
            stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
        )

    city, zone = _pick_city_zone(
        (linked_ride or {}).get("picking_point"),
        (linked_ride or {}).get("destination"),
    )
    return LiveLocationSession(
        id=sos_row.get("id"),
        type="sos",
        source="sos",
        status=status,
        is_live=_is_live(status),
        started_at=started_at,
        expires_at=expires_at,
        ended_at=ended_at,
        stopped_at=stopped_at,
        stop_reason=stop_reason,
        stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
        ride_id=sos_row.get("ride_id"),
        sos_session_id=sos_row.get("id"),
        customer_id=customer_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        driver_id=driver_id,
        driver_name=driver_name,
        driver_phone=driver_phone,
        pickup=_point_from_anchor((linked_ride or {}).get("picking_point")),
        destination=_point_from_anchor((linked_ride or {}).get("destination")),
        stops=_stops_from_payload((linked_ride or {}).get("stops")),
        route_path=[],
        last_location_timestamp=last_updated_at,
        waiting_for_first_update=trigger_point is None,
        end_reason="sos_resolved" if stopped_at else None,
        last_known_city=city,
        last_known_zone=zone,
        participants=SessionParticipants(driver=driver_participant, customer=customer_participant),
    )


def _collect_sessions(*, include_history: bool, type_filter: Optional[str], search: Optional[str]) -> List[LiveLocationSession]:
    fetch_limit = 250
    sessions: List[LiveLocationSession] = []

    if type_filter in (None, "ride"):
        ride_rows = _query_ride_rows(active_only=not include_history, fetch_limit=fetch_limit)
        ride_locations = _query_ride_location_rows([row["id"] for row in ride_rows])
        for ride in ride_rows:
            session = _build_ride_session(ride, ride_locations.get(ride["id"], {}))
            if _matches_search(session, search):
                sessions.append(session)

    if type_filter in (None, "sos"):
        sos_rows = _query_sos_rows(active_only=not include_history, fetch_limit=fetch_limit)
        linked_rides = _query_rides_by_ids([row.get("ride_id") for row in sos_rows if row.get("ride_id")])
        ride_locations = _query_ride_location_rows(list(linked_rides.keys()))
        for sos_row in sos_rows:
            ride_id = sos_row.get("ride_id")
            session = _build_sos_session(sos_row, linked_rides.get(ride_id), ride_locations.get(ride_id, {}))
            if _matches_search(session, search):
                sessions.append(session)

    sessions.sort(key=_session_sort_key)
    sessions.reverse()
    return sessions


def _get_session_by_session_id(session_id: str) -> Optional[LiveLocationSession]:
    if session_id.startswith("ride:"):
        return _get_session_for_ride(session_id.split(":", 1)[1])
    return _get_session_for_sos(session_id)


def _get_session_for_ride(ride_id: str) -> Optional[LiveLocationSession]:
    ride_rows = [row for row in _query_ride_rows(active_only=True, fetch_limit=250) if row.get("id") == ride_id]
    if not ride_rows:
        ride_rows = [row for row in _query_ride_rows(active_only=False, fetch_limit=250) if row.get("id") == ride_id]
    if not ride_rows:
        return None
    locations = _query_ride_location_rows([ride_id])
    return _build_ride_session(ride_rows[0], locations.get(ride_id, {}))


def _get_session_for_sos(sos_session_id: str) -> Optional[LiveLocationSession]:
    all_rows = _query_sos_rows(active_only=True, fetch_limit=250) + _query_sos_rows(active_only=False, fetch_limit=250)
    for row in all_rows:
        if row.get("id") == sos_session_id:
            linked_rides = _query_rides_by_ids([row.get("ride_id")] if row.get("ride_id") else [])
            ride_id = row.get("ride_id")
            ride_locations = _query_ride_location_rows([ride_id] if ride_id else [])
            return _build_sos_session(row, linked_rides.get(ride_id), ride_locations.get(ride_id, {}))
    return None


async def _broadcast_session_event(event: str, *, ride_id: Optional[str] = None, sos_session_id: Optional[str] = None) -> None:
    session = _get_session_for_ride(ride_id) if ride_id else _get_session_for_sos(sos_session_id or "")
    session_type = "ride" if ride_id else "sos"
    if session is None:
        payload = {"session_id": f"ride:{ride_id}" if ride_id else sos_session_id, "type": session_type}
        await stream_manager.broadcast("session_removed", payload, session_type=session_type)
        return
    payload = {"session": session.model_dump(mode="json")}
    await stream_manager.broadcast(event, payload, session_type=session.type)


def emit_live_location_event(event: str, *, ride_id: Optional[str] = None, sos_session_id: Optional[str] = None) -> None:
    try:
        asyncio.get_event_loop().create_task(
            _broadcast_session_event(event, ride_id=ride_id, sos_session_id=sos_session_id)
        )
    except RuntimeError:
        return


@router.get("/sessions", response_model=SessionListResponse)
def list_live_location_sessions(
    status: Optional[Literal["active", "stale", "ended", "manually_stopped", "expired"]] = Query(None),
    type: Optional[Literal["ride", "sos"]] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source_surface: Optional[Literal["dashboard", "ride_detail", "sos_detail", "search_history"]] = Query(None),
    admin_user: dict = Depends(require_role("operations")),
):
    sessions = _collect_sessions(include_history=False, type_filter=type, search=search)
    allowed_statuses = {status} if status else {"active", "stale"}
    filtered = [session for session in sessions if session.status in allowed_statuses]
    paged = filtered[offset: offset + limit]
    for session in paged:
        _record_admin_log(
            admin_user,
            entity_viewed=f"{session.type}:{session.ride_id or session.sos_session_id}",
            session_id=session.id,
            session_type="trip_tracking" if session.type == "ride" else "sos_tracking",
            source_surface=source_surface or "dashboard",
            ride_id=session.ride_id,
            sos_session_id=session.sos_session_id,
            participants_present=[name for name, payload in session.participants.model_dump().items() if payload],
        )
    return SessionListResponse(sessions=paged, total=len(filtered), limit=limit, offset=offset)


@router.get("/sessions/history", response_model=SessionListResponse)
def list_live_location_history(
    type: Optional[Literal["ride", "sos"]] = Query(None),
    status: Optional[Literal["stale", "ended", "manually_stopped", "expired"]] = Query(None),
    search: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    zone: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source_surface: Optional[Literal["dashboard", "ride_detail", "sos_detail", "search_history"]] = Query(None),
    admin_user: dict = Depends(require_role("operations")),
):
    sessions = _collect_sessions(include_history=True, type_filter=type, search=search)
    filtered = [
        session for session in sessions
        if _matches_history_filters(session, status, date_from, date_to, city, zone)
    ]
    paged = filtered[offset: offset + limit]
    for session in paged:
        _record_admin_log(
            admin_user,
            entity_viewed=f"{session.type}:{session.ride_id or session.sos_session_id}",
            session_id=session.id,
            session_type="trip_tracking" if session.type == "ride" else "sos_tracking",
            source_surface=source_surface or "search_history",
            ride_id=session.ride_id,
            sos_session_id=session.sos_session_id,
            participants_present=[name for name, payload in session.participants.model_dump().items() if payload],
        )
    return SessionListResponse(sessions=paged, total=len(filtered), limit=limit, offset=offset)


@router.get("/sessions/{session_id}", response_model=LiveLocationSession)
def get_live_location_session(
    session_id: str,
    source_surface: Optional[Literal["dashboard", "ride_detail", "sos_detail", "search_history"]] = Query(None),
    admin_user: dict = Depends(require_role("operations")),
):
    session = _get_session_by_session_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Live-location session not found")
    _record_admin_log(
        admin_user,
        entity_viewed=f"{session.type}:{session.ride_id or session.sos_session_id}",
        session_id=session.id,
        session_type="trip_tracking" if session.type == "ride" else "sos_tracking",
        source_surface=source_surface,
        ride_id=session.ride_id,
        sos_session_id=session.sos_session_id,
        participants_present=[name for name, payload in session.participants.model_dump().items() if payload],
    )
    return session


@router.get("/rides/{ride_id}", response_model=LiveLocationSession)
def get_live_location_for_ride(
    ride_id: str,
    source_surface: Optional[Literal["dashboard", "ride_detail", "sos_detail", "search_history"]] = Query(None),
    admin_user: dict = Depends(require_role("operations")),
):
    session = _get_session_for_ride(ride_id)
    if not session:
        raise HTTPException(status_code=404, detail="Ride live-location session not found")
    _record_admin_log(
        admin_user,
        entity_viewed=f"ride:{ride_id}",
        session_id=session.id,
        session_type="trip_tracking",
        source_surface=source_surface or "ride_detail",
        ride_id=ride_id,
        participants_present=[name for name, payload in session.participants.model_dump().items() if payload],
    )
    return session


@router.get("/sos/{sos_session_id}", response_model=LiveLocationSession)
def get_live_location_for_sos(
    sos_session_id: str,
    source_surface: Optional[Literal["dashboard", "ride_detail", "sos_detail", "search_history"]] = Query(None),
    admin_user: dict = Depends(require_role("operations")),
):
    session = _get_session_for_sos(sos_session_id)
    if not session:
        raise HTTPException(status_code=404, detail="SOS live-location session not found")
    _record_admin_log(
        admin_user,
        entity_viewed=f"sos:{sos_session_id}",
        session_id=session.id,
        session_type="sos_tracking",
        source_surface=source_surface or "sos_detail",
        ride_id=session.ride_id,
        sos_session_id=sos_session_id,
        participants_present=[name for name, payload in session.participants.model_dump().items() if payload],
    )
    return session


@router.post("/audit-view")
def audit_live_location_view(
    body: AuditViewBody,
    admin_user: dict = Depends(require_role("operations")),
):
    _record_admin_log(
        admin_user,
        entity_viewed=body.entity_viewed,
        session_id=body.session_id,
        session_type=body.session_type,
        source_surface=body.source_surface,
        ride_id=body.ride_id,
        sos_session_id=body.sos_session_id,
    )
    return {"status": "ok"}


@router.websocket("/ws")
async def live_location_admin_ws(
    websocket: WebSocket,
    token: str = Query(...),
    type: Optional[Literal["ride", "sos"]] = Query(None),
) -> None:
    try:
        admin_user = resolve_admin_from_token(token, allowed_roles=("operations",))
    except HTTPException:
        await websocket.close(code=4403)
        return

    connection_id = await stream_manager.connect(
        websocket,
        admin_user_id=admin_user["id"],
        admin_role=admin_user.get("admin_role") or "operations",
        session_type_filter=type,
    )

    snapshot = _collect_sessions(include_history=False, type_filter=type, search=None)
    snapshot = [session for session in snapshot if session.status in {"active", "stale"}]
    await websocket.send_text(json.dumps({
        "event": "session_snapshot",
        "data": {"sessions": [session.model_dump(mode="json") for session in snapshot]},
    }))

    _record_admin_log(
        admin_user,
        entity_viewed=f"stream:{type or 'all'}",
        session_id=f"stream:{type or 'all'}",
        session_type="trip_tracking" if type == "ride" else "sos_tracking" if type == "sos" else "trip_tracking",
        source_surface="dashboard",
    )

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await stream_manager.disconnect(connection_id)