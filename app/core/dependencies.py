from typing import Optional, List, Sequence
import logging
import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from app.core.config import settings
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)

# Role hierarchy: higher index = more permissive override
ROLE_HIERARCHY = ["readonly", "support", "finance", "operations", "super_admin"]


def _verify_token(token: str) -> dict:
    """
    Verify a bearer token.

    1. Call Supabase /auth/v1/user directly (handles ES256 user JWTs, no client state mutation).
    2. Fall back to local HS256 decode for service_role tokens signed internally.
    """
    # --- Primary: direct HTTP call to Supabase auth API ---
    try:
        resp = httpx.get(
            f"{settings.SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": settings.SUPABASE_KEY,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {"id": data["id"], "role": "authenticated"}
        logger.warning("Supabase /auth/v1/user returned %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Supabase auth HTTP call failed: %s", exc)

    # --- Fallback: local HS256 decode for service_role tokens ---
    try:
        payload = jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        if payload.get("role") == "service_role":
            return {"id": payload.get("sub"), "role": "service_role"}
    except JWTError as exc:
        logger.warning("local JWT decode failed: %s", exc)

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No token provided")
    return _verify_token(credentials.credentials)


def _load_admin_row(auth_id: str) -> Optional[dict]:
    sb = get_supabase()
    try:
        result = (
            sb.table("users")
            .select("id, is_admin, is_active, admin_role")
            .eq("supabase_uid", auth_id)
            .limit(1)
            .execute()
        )
        row = result.data[0] if result.data else None
        # Fallback: some admin rows were inserted with id = auth_uuid and no supabase_uid
        if not row:
            result2 = (
                sb.table("users")
                .select("id, is_admin, is_active, admin_role")
                .eq("id", auth_id)
                .limit(1)
                .execute()
            )
            row = result2.data[0] if result2.data else None
    except Exception as exc:
        logger.error("Admin DB lookup failed for auth id %s: %s", auth_id, exc)
        return None

    return row


def enforce_admin_access(user: dict, allowed_roles: Optional[Sequence[str]] = None) -> dict:
    if user.get("role") == "service_role":
        user.setdefault("admin_role", "super_admin")
        return user

    auth_id = user["id"]
    row = _load_admin_row(auth_id)

    if not row or not row.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    if not row.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin account disabled")

    role = row.get("admin_role")
    if allowed_roles and role != "super_admin" and role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires one of: {', '.join(allowed_roles)}",
        )

    # Replace auth UUID with the internal users-table PK so downstream queries work
    user["id"] = row["id"]
    user["admin_role"] = role
    return user


def resolve_admin_from_token(token: str, allowed_roles: Optional[Sequence[str]] = None) -> dict:
    return enforce_admin_access(_verify_token(token), allowed_roles=allowed_roles)


def require_admin(user: dict = Depends(get_current_user)):
    return enforce_admin_access(user)


def require_role(*allowed_roles: str):
    """
    Factory that returns a FastAPI dependency enforcing one of the allowed roles.
    super_admin always passes regardless of what roles are listed.

    Usage:
        @router.post("/sensitive")
        def endpoint(_user=Depends(require_role("finance", "super_admin"))):
    """
    def _dep(user: dict = Depends(require_admin)):
        return enforce_admin_access(user, allowed_roles=allowed_roles)
    return _dep

