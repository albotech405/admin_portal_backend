"""
WebSocket router for real-time admin updates.

Admins connect once as:
  wss://<host>/api/v1/ws/<admin_user_id>?token=<jwt>

The shared `manager` singleton is imported by other routers (rides, sos)
to broadcast location events to every connected admin.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.dependencies import _verify_token

router = APIRouter(prefix="/ws", tags=["websocket"])
logger = logging.getLogger(__name__)


class ConnectionManager:
    """Thread-safe (asyncio) registry of active admin WebSocket connections."""

    def __init__(self) -> None:
        # admin_user_id (str) → WebSocket
        self._connections: Dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, admin_user_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            # Cleanly close any stale connection for the same admin
            old = self._connections.get(admin_user_id)
            if old is not None:
                try:
                    await old.close()
                except Exception:
                    pass
            self._connections[admin_user_id] = websocket
        logger.info("WS connected: admin %s  (total=%d)", admin_user_id, len(self._connections))

    async def disconnect(self, admin_user_id: str) -> None:
        async with self._lock:
            self._connections.pop(admin_user_id, None)
        logger.info("WS disconnected: admin %s  (total=%d)", admin_user_id, len(self._connections))

    async def broadcast(self, event: str, data: Dict[str, Any]) -> None:
        """Send an event payload to every connected admin."""
        if not self._connections:
            return
        payload = json.dumps({"event": event, "data": data})
        dead: list[str] = []
        async with self._lock:
            snapshot = list(self._connections.items())
        for uid, ws in snapshot:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(payload)
            except Exception:
                dead.append(uid)
        if dead:
            async with self._lock:
                for uid in dead:
                    self._connections.pop(uid, None)


# Module-level singleton — import this from other routers
manager = ConnectionManager()


@router.websocket("/{admin_user_id}")
async def admin_websocket(
    admin_user_id: str,
    websocket: WebSocket,
    token: str = Query(..., description="Admin JWT bearer token"),
) -> None:
    """
    Persistent WebSocket connection for an admin user.
    Authenticates via the `token` query parameter (same JWT used for REST calls).
    Emits JSON frames:
      {"event": "<event_name>", "data": {...}}

    Events:
      driver_location_update   — driver moved on an active ride
      customer_location_update — customer moved on an active ride
      sos_location_update      — distressed driver moved during SOS session
      sos_triggered            — new SOS session started
    """
    # Authenticate before accepting the connection
    try:
        _verify_token(token)
    except Exception:
        await websocket.close(code=4001)
        return

    await manager.connect(admin_user_id, websocket)
    try:
        while True:
            # Keep-alive: clients may send pings; we just drain them
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(admin_user_id)
