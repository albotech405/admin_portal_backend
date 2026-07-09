import logging
from typing import Any, Optional

import httpx
from fastapi import HTTPException
from supabase import create_client, Client

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: Optional[Client] = None


def get_supabase() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    return _client


def get_supabase_admin() -> Client:
    """Return the service-role Supabase client (same as get_supabase; alias for clarity)."""
    return get_supabase()


def revoke_user_sessions(supabase_uid: str) -> None:
    """
    Call the Supabase GoTrue admin API to sign out all active sessions (refresh tokens)
    for a user. Failures are logged but not re-raised — invalidating our own
    admin_sessions table is the authoritative action.
    """
    try:
        resp = httpx.post(
            f"{settings.SUPABASE_URL}/auth/v1/admin/users/{supabase_uid}/logout",
            headers={
                "apikey": settings.SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}",
            },
            timeout=10,
        )
        if resp.status_code not in (200, 204, 404):
            logger.warning(
                "Supabase session revocation returned %s for uid %s: %s",
                resp.status_code, supabase_uid, resp.text[:200],
            )
    except Exception as exc:
        logger.warning("Could not revoke Supabase sessions for uid %s: %s", supabase_uid, exc)


def call_rpc(function_name: str, params: dict[str, Any]) -> Any:
    response = get_supabase().rpc(function_name, params).execute()
    return response.data


def rpc_error_to_http_exception(exc: Exception) -> HTTPException:
    message = str(exc)
    lower_message = message.lower()

    if "admin access required" in lower_message:
        return HTTPException(status_code=403, detail="Admin access required")
    if "admin_id is required" in lower_message:
        return HTTPException(status_code=400, detail="admin_id is required")
    if "permission denied" in lower_message:
        return HTTPException(status_code=403, detail=message)

    return HTTPException(status_code=500, detail=message)


def first_row(data: Any) -> Any:
    if isinstance(data, list):
        return data[0] if data else None
    return data
