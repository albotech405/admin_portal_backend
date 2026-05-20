from typing import Optional, List
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


def require_admin(user: dict = Depends(get_current_user)):
    if user.get("role") == "service_role":
        return user

    # The auth UUID is stored in users.supabase_uid (users.id is the app-internal PK)
    auth_id = user["id"]
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
    except Exception as exc:
        logger.error("Admin DB lookup failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error during admin check")

    if not row or not row.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    if not row.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin account disabled")

    # Replace auth UUID with the internal users-table PK so downstream queries work
    user["id"] = row["id"]
    user["admin_role"] = row.get("admin_role")
    return user


def require_role(*allowed_roles: str):
    """
    Factory that returns a FastAPI dependency enforcing one of the allowed roles.
    super_admin always passes regardless of what roles are listed.

    Usage:
        @router.post("/sensitive")
        def endpoint(_user=Depends(require_role("finance", "super_admin"))):
    """
    def _dep(user: dict = Depends(require_admin)):
        if user.get("role") == "service_role":
            return user
        role = user.get("admin_role")
        if role == "super_admin":
            return user
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of: {', '.join(allowed_roles)}",
            )
        return user
    return _dep

