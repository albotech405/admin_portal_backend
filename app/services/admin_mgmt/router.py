from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime, timezone
from uuid import uuid4

from app.core.dependencies import require_admin, get_current_user
from app.core.supabase import get_supabase

router = APIRouter(prefix="/admin/mgmt", tags=["admin-management"])

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


def _require_super_admin(user: dict, sb):
    """Raise 403 if the calling admin is not super_admin."""
    row = sb.table("users").select("admin_role").eq("id", user["id"]).maybe_single().execute()
    if not row.data or row.data.get("admin_role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")


# ── Admin profile (self) ──────────────────────────────────────────────────────

@router.get("/me", response_model=AdminUserItem)
def get_my_profile(_user=Depends(require_admin)):
    sb = get_supabase()
    data = _get_admin_profile(sb, _user["id"])
    return AdminUserItem(**{k: data[k] for k in AdminUserItem.model_fields if k in data})


@router.post("/me/record-login")
def record_admin_login(request: Request, _user=Depends(require_admin)):
    """Called by frontend immediately after successful auth to record login time + session."""
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    ip = request.client.host if request.client else None

    try:
        # Update last_login_at on the user row
        sb.table("users").update({"last_login_at": now}).eq("id", _user["id"]).execute()

        # Check IP allowlist (if any entries exist, enforce it)
        allowlist_result = sb.table("admin_ip_allowlist").select("ip_cidr").execute()
        allowed_ips = [r["ip_cidr"] for r in (allowlist_result.data or [])]
        if allowed_ips and ip and ip not in allowed_ips:
            # Also check CIDR prefixes (simple prefix match for /24 etc.)
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

        # Create a session record (30-min expiry enforced at frontend; stored for audit)
        from datetime import timedelta
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        session_id = str(uuid4())
        admin_email = None
        try:
            ur = sb.table("users").select("email, admin_role").eq("id", _user["id"]).maybe_single().execute()
            if ur.data:
                admin_email = ur.data.get("email")
        except Exception:
            pass

        sb.table("admin_sessions").insert({
            "id": session_id,
            "admin_user_id": _user["id"],
            "admin_email": admin_email,
            "ip_address": ip,
            "created_at": now,
            "expires_at": expires_at,
            "is_active": True,
        }).execute()

        return {"session_id": session_id, "expires_at": expires_at}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/me/invalidate-session")
def invalidate_session(session_id: str = Query(...), _user=Depends(require_admin)):
    """Mark a session as inactive (logout)."""
    sb = get_supabase()
    try:
        sb.table("admin_sessions").update({"is_active": False}).eq("id", session_id).eq("admin_user_id", _user["id"]).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"message": "Session invalidated"}


# ── 2FA ──────────────────────────────────────────────────────────────────────

@router.get("/me/2fa/status")
def get_2fa_status(_user=Depends(require_admin)):
    """Return whether 2FA is currently enabled for this admin."""
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
    sb = get_supabase()
    try:
        sb.table("users").update({"two_fa_enabled": True}).eq("id", _user["id"]).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"message": "2FA enabled"}


@router.post("/me/2fa/disable")
def disable_2fa(_user=Depends(require_admin)):
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
    sb = get_supabase()
    try:
        row = sb.table("users").select("admin_role, full_name, email, two_fa_enabled").eq("id", _user["id"]).maybe_single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not row.data:
        raise HTTPException(status_code=404, detail="Admin not found")
    return row.data
