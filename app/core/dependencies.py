from typing import Optional, List
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from app.core.config import settings
from app.core.supabase import get_supabase

bearer_scheme = HTTPBearer(auto_error=False)

_JWT_SECRET = settings.SUPABASE_JWT_SECRET

# Role hierarchy: higher index = more permissive override
ROLE_HIERARCHY = ["readonly", "support", "finance", "operations", "super_admin"]


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No token provided")

    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            _JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {e}")

    user_id = payload.get("sub")
    role = payload.get("role", "authenticated")

    if not user_id and role != "service_role":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing sub")

    return {"id": user_id, "role": role}


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

