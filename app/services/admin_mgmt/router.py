from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime, timezone
from uuid import uuid4
import logging

from app.core.dependencies import require_admin, get_current_user
from app.core.supabase import get_supabase

router = APIRouter(prefix="/admin/mgmt", tags=["admin-management"])
logger = logging.getLogger(__name__)

# ── Role constants ────────────────────────────────────────────────────────────

VALID_ROLES = {"super_admin", "operations", "finance", "support", "readonly"}


# ── Models ────────────────────────────────────────────────────────────────────

class AdminUserItem(BaseModel):
    id: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    admin_role: Optional[str] = None
    is_active: bool = True
    is_admin: bool = True
    two_fa_enabled: bool = False
    created_at: str
    updated_at: str
    last_login_at: Optional[str] = None


class AdminUserListResponse(BaseModel):
    admins: List[AdminUserItem]
    total: int


class CreateAdminBody(BaseModel):
    email: str
    full_name: str
    phone_number: Optional[str] = None
    admin_role: str = "readonly"
    password: str


class UpdateAdminBody(BaseModel):
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    admin_role: Optional[str] = None
    is_active: Optional[bool] = None


class Verify2FABody(BaseModel):
    totp_code: str


class SessionInfoResponse(BaseModel):
    id: str
    admin_user_id: str
    admin_email: Optional[str] = None
    admin_role: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: str
    expires_at: str
    is_active: bool


class IpAllowlistItem(BaseModel):
    id: str
    ip_cidr: str
    label: Optional[str] = None
    created_by: Optional[str] = None
    created_at: str


class AddIpBody(BaseModel):
    ip_cidr: str
    label: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_admin_profile(sb, user_id: str) -> dict:
    """Return admin user row or raise 404."""
    result = (
        sb.table("users")
        .select("id, email, full_name, phone_number, is_admin, is_active, admin_role, two_fa_enabled, created_at, updated_at, last_login_at")
        .eq("id", user_id)
        .eq("is_admin", True)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Admin user not found")
    return result.data


def _require_user_id(user: dict):
    """Raise 403 if the token has no real user ID (e.g. service_role machine tokens)."""
    if not user.get("id"):
        raise HTTPException(status_code=403, detail="This endpoint requires a user-scoped token")


def _require_super_admin(user: dict, sb):
    """Raise 403 if the calling admin is not super_admin."""
    if user.get("role") == "service_role":
        return  # service_role tokens have full access
    row = sb.table("users").select("admin_role").eq("id", user["id"]).maybe_single().execute()
    if not row.data or row.data.get("admin_role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")


def _is_missing_admin_id_column_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "admin_id" in message and ("schema cache" in message or "column" in message)


_ADMIN_SESSION_MODERN_COLUMNS = {
    "admin_id",
    "logged_in_at",
    "logged_out_at",
    "user_agent",
    "last_refreshed_at",
}


def _missing_admin_session_columns(exc: Exception) -> set[str]:
    message = str(exc).lower()
    if "schema cache" not in message and "column" not in message:
        return set()
    return {column for column in _ADMIN_SESSION_MODERN_COLUMNS if column in message}


def _legacy_admin_session_payload(payload: dict, missing_columns: Optional[set[str]] = None) -> dict:
    legacy_payload = dict(payload)
    columns_to_strip = _ADMIN_SESSION_MODERN_COLUMNS if missing_columns else _ADMIN_SESSION_MODERN_COLUMNS
    for column in columns_to_strip:
        legacy_payload.pop(column, None)
    return legacy_payload


def _insert_admin_session(sb, payload: dict) -> None:
    try:
        sb.table("admin_sessions").insert(payload).execute()
    except Exception as exc:
        missing_columns = _missing_admin_session_columns(exc)
        if not missing_columns:
            raise
        legacy_payload = _legacy_admin_session_payload(payload, missing_columns)
        sb.table("admin_sessions").insert(legacy_payload).execute()


def _update_admin_sessions_for_admin(sb, admin_user_id: str, updates: dict, *, session_id: Optional[str] = None, active_only: bool = False) -> None:
    query = sb.table("admin_sessions").update(updates)
    if session_id:
        query = query.eq("id", session_id)
    if active_only:
        query = query.eq("is_active", True)
    try:
        query.eq("admin_id", admin_user_id).execute()
    except Exception as exc:
        missing_columns = _missing_admin_session_columns(exc)
        if not missing_columns:
            raise
        fallback_query = sb.table("admin_sessions").update(_legacy_admin_session_payload(updates, missing_columns))
        if session_id:
            fallback_query = fallback_query.eq("id", session_id)
        if active_only:
            fallback_query = fallback_query.eq("is_active", True)
        fallback_query.eq("admin_user_id", admin_user_id).execute()


def _invalidate_all_sessions(sb, admin_user_id: str) -> None:
    """
    Invalidate all active admin_sessions rows for admin_user_id and revoke
    Supabase refresh tokens. Failures are non-fatal — the user account is
    already disabled in the users table.
    """
    from app.core.supabase import revoke_user_sessions

    now_str = datetime.now(timezone.utc).isoformat()
    try:
        _update_admin_sessions_for_admin(sb, admin_user_id, {
            "is_active": False,
            "logged_out_at": now_str,
        }, active_only=True)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to invalidate admin_sessions for %s: %s", admin_user_id, exc)

    try:
        uid_row = sb.table("users").select("supabase_uid").eq("id", admin_user_id).maybe_single().execute()
        supabase_uid = uid_row.data.get("supabase_uid") if uid_row.data else None
        if supabase_uid:
            revoke_user_sessions(supabase_uid)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to revoke Supabase sessions for %s: %s", admin_user_id, exc)


# ── Admin profile (self) ──────────────────────────────────────────────────────

@router.get("/me", response_model=AdminUserItem)
def get_my_profile(_user=Depends(require_admin)):
    _require_user_id(_user)
    sb = get_supabase()
    data = _get_admin_profile(sb, _user["id"])
    return AdminUserItem(**{k: data[k] for k in AdminUserItem.model_fields if k in data})


@router.post("/me/record-login")
def record_admin_login(request: Request, _user=Depends(require_admin)):
    """Called by frontend immediately after successful auth to record login time + session."""
    _require_user_id(_user)
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    # Update last_login_at best-effort.
    try:
        sb.table("users").update({"last_login_at": now}).eq("id", _user["id"]).execute()
    except Exception as exc:
        logger.warning("Failed to update last_login_at for %s: %s", _user["id"], exc)

    # Check IP allowlist (if any entries exist, enforce it).
    try:
        allowlist_result = sb.table("admin_ip_allowlist").select("ip_cidr").execute()
        allowed_ips = [r["ip_cidr"] for r in (allowlist_result.data or [])]
    except Exception as exc:
        logger.warning("Failed to load admin IP allowlist: %s", exc)
        allowed_ips = []

    if allowed_ips and ip and ip not in allowed_ips:
        def _in_allowlist(client_ip: str, entries: list) -> bool:
            for cidr in entries:
                if "/" not in cidr:
                    if client_ip == cidr:
                        return True
                else:
                    prefix = cidr.split("/")[0].rsplit(".", 1)[0]
                    if client_ip.startswith(prefix):
                        return True
            return False

        if not _in_allowlist(ip, allowed_ips):
            raise HTTPException(status_code=403, detail="IP address not in allowlist")

    session_id = str(uuid4())
    admin_email = None
    try:
        ur = sb.table("users").select("email, admin_role").eq("id", _user["id"]).maybe_single().execute()
        if ur.data:
            admin_email = ur.data.get("email")
    except Exception as exc:
        logger.warning("Failed to load admin email for session record %s: %s", _user["id"], exc)

    try:
        _insert_admin_session(sb, {
            "id": session_id,
            # Canonical columns (new schema)
            "admin_id": _user["id"],
            "logged_in_at": now,
            "user_agent": user_agent,
            # Legacy columns kept during migration window
            "admin_user_id": _user["id"],
            "admin_email": admin_email,
            "ip_address": ip,
            "created_at": now,
            "is_active": True,
        })
    except Exception as exc:
        logger.warning("Failed to record admin session for %s: %s", _user["id"], exc)
        return {"session_id": None, "status": "degraded"}

    return {"session_id": session_id, "status": "ok"}


@router.post("/me/invalidate-session")
def invalidate_session(session_id: str = Query(...), _user=Depends(require_admin)):
    """Mark a session as inactive (logout). Idempotent — safe to call multiple times."""
    _require_user_id(_user)
    sb = get_supabase()
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        _update_admin_sessions_for_admin(sb, _user["id"], {
            "is_active": False,
            "logged_out_at": now_str,
        }, session_id=session_id)
    except Exception:
        pass  # Idempotent: session may not exist or already be inactive
    return {"message": "Session invalidated"}


# ── 2FA ──────────────────────────────────────────────────────────────────────

@router.get("/me/2fa/status")
def get_2fa_status(_user=Depends(require_admin)):
    """Return whether 2FA is currently enabled for this admin."""
    _require_user_id(_user)
    sb = get_supabase()
    try:
        row = sb.table("users").select("two_fa_enabled").eq("id", _user["id"]).maybe_single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    enabled = row.data.get("two_fa_enabled", False) if row.data else False
    return {"two_fa_enabled": enabled}


@router.post("/me/2fa/enable")
def enable_2fa(_user=Depends(require_admin)):
    """
    Enable 2FA for this admin. Supabase Auth manages the actual TOTP secret via
    supabase.auth.mfa.enroll() on the frontend. This endpoint just marks the flag.
    """
    _require_user_id(_user)
    sb = get_supabase()
    try:
        sb.table("users").update({"two_fa_enabled": True}).eq("id", _user["id"]).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"message": "2FA enabled"}


@router.post("/me/2fa/disable")
def disable_2fa(_user=Depends(require_admin)):
    _require_user_id(_user)
    sb = get_supabase()
    try:
        sb.table("users").update({"two_fa_enabled": False}).eq("id", _user["id"]).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"message": "2FA disabled"}


# ── Admin user management (super_admin only) ──────────────────────────────────

@router.get("/users", response_model=AdminUserListResponse)
def list_admin_users(_user=Depends(require_admin)):
    sb = get_supabase()
    _require_super_admin(_user, sb)
    try:
        result = (
            sb.table("users")
            .select("id, email, full_name, phone_number, is_admin, is_active, admin_role, two_fa_enabled, created_at, updated_at, last_login_at")
            .eq("is_admin", True)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    admins = []
    for r in result.data or []:
        admins.append(AdminUserItem(**{k: r[k] for k in AdminUserItem.model_fields if k in r}))

    return AdminUserListResponse(admins=admins, total=len(admins))


@router.post("/users", status_code=201)
def create_admin_user(body: CreateAdminBody, _user=Depends(require_admin)):
    """Create a new admin user via Supabase Auth + set is_admin + admin_role."""
    sb = get_supabase()
    _require_super_admin(_user, sb)

    if body.admin_role not in VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}")

    from app.core.supabase import get_supabase_admin
    try:
        admin_sb = get_supabase_admin()
        # Create the Supabase Auth user
        auth_result = admin_sb.auth.admin.create_user({
            "email": body.email,
            "password": body.password,
            "email_confirm": True,
        })
        new_user_id = auth_result.user.id if auth_result.user else None
        if not new_user_id:
            raise HTTPException(status_code=500, detail="Failed to create auth user")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auth user creation failed: {e}")

    now = datetime.now(timezone.utc).isoformat()
    try:
        sb.table("users").upsert({
            "id": new_user_id,
            "supabase_uid": new_user_id,
            "email": body.email,
            "full_name": body.full_name,
            "phone_number": body.phone_number,
            "is_admin": True,
            "is_active": True,
            "admin_role": body.admin_role,
            "two_fa_enabled": False,
            "created_at": now,
            "updated_at": now,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Profile creation failed: {e}")

    return {"id": new_user_id, "message": "Admin user created"}


@router.patch("/users/{target_user_id}")
def update_admin_user(target_user_id: str, body: UpdateAdminBody, _user=Depends(require_admin)):
    sb = get_supabase()
    _require_super_admin(_user, sb)

    if body.admin_role is not None and body.admin_role not in VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}")

    updates: dict = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if body.full_name is not None:
        updates["full_name"] = body.full_name
    if body.phone_number is not None:
        updates["phone_number"] = body.phone_number
    if body.admin_role is not None:
        updates["admin_role"] = body.admin_role
    if body.is_active is not None:
        updates["is_active"] = body.is_active

    try:
        result = sb.table("users").update(updates).eq("id", target_user_id).eq("is_admin", True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Admin user not found")

    # When disabling an account, immediately invalidate all active sessions
    if body.is_active is False:
        _invalidate_all_sessions(sb, target_user_id)

    return {"message": "Admin user updated"}


@router.delete("/users/{target_user_id}")
def disable_admin_user(target_user_id: str, _user=Depends(require_admin)):
    """Disable (not hard-delete) an admin user. Cannot disable yourself."""
    sb = get_supabase()
    _require_super_admin(_user, sb)

    if target_user_id == _user["id"]:
        raise HTTPException(status_code=400, detail="Cannot disable your own account")

    try:
        result = sb.table("users").update({
            "is_active": False,
            "is_admin": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", target_user_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Admin user not found")

    _invalidate_all_sessions(sb, target_user_id)

    return {"message": "Admin user disabled"}


# ── IP Allowlist ──────────────────────────────────────────────────────────────

@router.get("/ip-allowlist")
def list_ip_allowlist(_user=Depends(require_admin)):
    sb = get_supabase()
    _require_super_admin(_user, sb)
    try:
        result = sb.table("admin_ip_allowlist").select("*").order("created_at", desc=True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"entries": result.data or []}


@router.post("/ip-allowlist", status_code=201)
def add_ip_allowlist(body: AddIpBody, _user=Depends(require_admin)):
    sb = get_supabase()
    _require_super_admin(_user, sb)
    try:
        entry_id = str(uuid4())
        sb.table("admin_ip_allowlist").insert({
            "id": entry_id,
            "ip_cidr": body.ip_cidr,
            "label": body.label,
            "created_by": _user["id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"id": entry_id, "message": "IP added to allowlist"}


@router.delete("/ip-allowlist/{entry_id}")
def remove_ip_allowlist(entry_id: str, _user=Depends(require_admin)):
    sb = get_supabase()
    _require_super_admin(_user, sb)
    try:
        result = sb.table("admin_ip_allowlist").delete().eq("id", entry_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.data:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"message": "IP removed from allowlist"}


# ── Sessions (super_admin audit) ──────────────────────────────────────────────

@router.get("/sessions")
def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    sb = get_supabase()
    _require_super_admin(_user, sb)
    try:
        result = (
            sb.table("admin_sessions")
            .select("*")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        count_result = sb.table("admin_sessions").select("id", count="exact").execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"sessions": result.data or [], "total": count_result.count or 0}


# ── Role-info endpoint (used by frontend to gate UI) ─────────────────────────

@router.get("/role")
def get_my_role(_user=Depends(require_admin)):
    """Return current admin's role. Frontend calls this once after login."""
    _require_user_id(_user)
    sb = get_supabase()
    try:
        row = sb.table("users").select("admin_role, full_name, email, two_fa_enabled").eq("id", _user["id"]).maybe_single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not row.data:
        raise HTTPException(status_code=404, detail="Admin not found")
    return row.data
