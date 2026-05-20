from typing import Optional, List
import logging
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

    1. Try Supabase auth API — works for ES256 user JWTs (the format Supabase now issues).
    2. Fall back to local HS256 decode for service_role tokens signed internally.
    """
    # --- Primary: ask Supabase to validate the token ---
    try:
        sb = get_supabase()
        resp = sb.auth.get_user(token)
        if resp and resp.user:
            return {"id": resp.user.id, "role": "authenticated"}
        logger.warning("auth.get_user returned no user for token")
    except Exception as exc:
        logger.warning("auth.get_user failed: %s", exc)

    # --- Fallback: local decode for HS256 service_role tokens ---
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

    sb = get_supabase()
    result = (
        sb.table("users")
        .select("is_admin, is_active, admin_role")
        .eq("id", user["id"])
        .maybe_single()
        .execute()
    )
    if not result.data or not result.data.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    if not result.data.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin account disabled")

    # Attach admin_role to the user dict for downstream use
    user["admin_role"] = result.data.get("admin_role")
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

