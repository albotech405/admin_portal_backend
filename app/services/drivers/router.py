import mimetypes
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from typing import Any, Optional, List
from datetime import date, datetime, timezone
from uuid import uuid4
from app.core.dependencies import require_admin, require_role
from app.core.config import settings
from app.core.supabase import call_rpc, first_row, get_supabase, rpc_error_to_http_exception
from app.services.notifications.router import send_push_to_users
from app.services.audit.router import write_audit_log

router = APIRouter(prefix="/drivers", tags=["drivers"])


DRIVER_DOCUMENT_BUCKET = "driver-documents"
DRIVER_DOCUMENT_URL_TTL_SECONDS = 60 * 60
KYC_DOCUMENT_ALLOWED_ROLES = ("operations", "super_admin")
REQUIRED_DRIVER_DOCUMENT_TYPES = [
    "national_id",
    "selfie_with_id",
    "drivers_license",
    "vehicle_registration",
    "insurance",
    "profile_photo",
    "vehicle_photo_front",
    "vehicle_photo_back",
    "vehicle_photo_left",
    "vehicle_photo_right",
]
require_driver_document_access = require_role(*KYC_DOCUMENT_ALLOWED_ROLES)


class DriverAdminListItem(BaseModel):
    id: str
    user_id: str
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    license_number: str
    license_expiry: str
    vehicle_type: Optional[str] = None
    verification_status: str
    is_online: bool = False
    rating: float = 0.0
    total_trips: int = 0
    credit_balance: float = 0.0
    submitted_at: Optional[str] = None
    activation_date: Optional[str] = None
    verification_feedback: Optional[str] = None
    is_suspended: bool = False
    created_at: str
    has_customer_profile: bool = False
    linked_customer_user_id: Optional[str] = None
    platform_roles: List[str] = Field(default_factory=lambda: ["driver"])
    linked_customer: Optional[dict] = None
    document_count: int = 0
    required_document_count: int = len(REQUIRED_DRIVER_DOCUMENT_TYPES)
    document_types_present: List[str] = Field(default_factory=list)
    missing_document_types: List[str] = Field(default_factory=list)
    document_access_error: Optional[str] = None
    documents: List["DriverDocumentResponse"] = Field(default_factory=list)


class VehicleDetailsResponse(BaseModel):
    id: str
    vehicle_type: str
    license_plate: str
    make: str
    model: str
    year: int
    color: str
    passenger_capacity: Optional[int] = None
    has_air_conditioning: Optional[bool] = None
    provides_helmet: Optional[bool] = None


class DriverDocumentResponse(BaseModel):
    id: str
    document_type: str
    file_url: Optional[str] = None
    open_url: Optional[str] = None
    download_url: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_extension: Optional[str] = None
    uploaded_at: Optional[str] = None
    rejection_reason: Optional[str] = None
    storage_status: str = "available"
    status: str


class DriverDetailResponse(DriverAdminListItem):
    vehicle: Optional[VehicleDetailsResponse] = None


def _to_iso8601(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _is_absolute_url(value: Optional[str]) -> bool:
    return bool(value and value.startswith(("http://", "https://")))


def _guess_mime_type(file_name: Optional[str], fallback: Optional[str]) -> Optional[str]:
    if fallback:
        return fallback
    if not file_name:
        return None
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed


def _normalize_storage_path(document_row: dict[str, Any]) -> Optional[str]:
    for key in ("storage_path", "file_path", "path", "object_path", "storage_key", "file_url"):
        raw_value = document_row.get(key)
        if not raw_value or _is_absolute_url(raw_value):
            continue
        path = str(raw_value).strip().lstrip("/")
        if not path:
            continue
        prefixes = (
            f"{DRIVER_DOCUMENT_BUCKET}/",
            f"public/{DRIVER_DOCUMENT_BUCKET}/",
            f"sign/{DRIVER_DOCUMENT_BUCKET}/",
            f"object/public/{DRIVER_DOCUMENT_BUCKET}/",
            f"object/sign/{DRIVER_DOCUMENT_BUCKET}/",
        )
        for prefix in prefixes:
            if path.startswith(prefix):
                return path[len(prefix):]
        return path
    return None


def _extract_file_name(document_row: dict[str, Any], storage_path: Optional[str]) -> Optional[str]:
    for key in ("file_name", "filename", "original_file_name", "original_filename", "name"):
        value = document_row.get(key)
        if value:
            return str(value)
    raw_url = document_row.get("file_url")
    if _is_absolute_url(raw_url):
        path = urlparse(str(raw_url)).path
        if path:
            return PurePosixPath(path).name or None
    if storage_path:
        return PurePosixPath(storage_path).name or None
    return None


def _resolve_signed_url(supabase_client: Any, storage_path: str) -> Optional[str]:
    response = supabase_client.storage.from_(DRIVER_DOCUMENT_BUCKET).create_signed_url(
        storage_path,
        DRIVER_DOCUMENT_URL_TTL_SECONDS,
    )
    if isinstance(response, str):
        signed_url = response
    else:
        signed_url = response.get("signedURL") or response.get("signedUrl") or response.get("signed_url")
    if not signed_url:
        return None
    if signed_url.startswith(("http://", "https://")):
        return signed_url
    if signed_url.startswith("/storage/v1/"):
        return f"{settings.SUPABASE_URL.rstrip('/')}{signed_url}"
    return f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/{signed_url.lstrip('/')}"


def _build_document_response(document_row: dict[str, Any], supabase_client: Any) -> DriverDocumentResponse:
    raw_file_url = document_row.get("file_url")
    storage_path = _normalize_storage_path(document_row)
    file_name = _extract_file_name(document_row, storage_path)
    mime_type = _guess_mime_type(file_name, document_row.get("mime_type"))
    file_extension = document_row.get("file_extension")
    if not file_extension and file_name and "." in file_name:
        file_extension = file_name.rsplit(".", 1)[-1].lower()

    file_url = raw_file_url if _is_absolute_url(raw_file_url) else None
    storage_status = "available"
    if not file_url and storage_path:
        try:
            file_url = _resolve_signed_url(supabase_client, storage_path)
            if not file_url:
                storage_status = "missing"
        except Exception as exc:
            error_text = str(exc).lower()
            storage_status = "missing" if "not found" in error_text or "no such" in error_text else "unavailable"
    elif not file_url:
        storage_status = "missing"

    return DriverDocumentResponse(
        id=str(document_row.get("id", "")),
        document_type=str(document_row.get("document_type", "unknown")),
        status=str(document_row.get("status", "pending")),
        file_url=file_url,
        open_url=file_url,
        download_url=file_url,
        file_name=file_name,
        mime_type=mime_type,
        file_extension=str(file_extension) if file_extension is not None else None,
        uploaded_at=_to_iso8601(document_row.get("uploaded_at") or document_row.get("created_at")),
        rejection_reason=document_row.get("rejection_reason"),
        storage_status=storage_status,
    )


def _build_document_payload(document_rows: list[dict[str, Any]], supabase_client: Any) -> tuple[list[DriverDocumentResponse], Optional[str]]:
    documents: list[DriverDocumentResponse] = []
    access_error: Optional[str] = None
    for row in document_rows or []:
        try:
            documents.append(_build_document_response(row, supabase_client))
        except Exception as exc:
            access_error = "One or more documents could not be prepared for admin viewing."
            documents.append(
                DriverDocumentResponse(
                    id=str(row.get("id", "")),
                    document_type=str(row.get("document_type", "unknown")),
                    status=str(row.get("status", "pending")),
                    file_name=_extract_file_name(row, _normalize_storage_path(row)),
                    mime_type=row.get("mime_type"),
                    file_extension=row.get("file_extension"),
                    uploaded_at=_to_iso8601(row.get("uploaded_at") or row.get("created_at")),
                    rejection_reason=row.get("rejection_reason"),
                    storage_status="unavailable",
                )
            )
    return documents, access_error


def _document_summary(document_rows: list[dict[str, Any]], supabase_client: Any) -> tuple[list[DriverDocumentResponse], dict[str, Any]]:
    documents, access_error = _build_document_payload(document_rows, supabase_client)
    present_types: list[str] = []
    seen_types: set[str] = set()
    for document in documents:
        document_type = document.document_type
        if document_type and document_type not in seen_types:
            seen_types.add(document_type)
            present_types.append(document_type)

    missing_types = [doc_type for doc_type in REQUIRED_DRIVER_DOCUMENT_TYPES if doc_type not in seen_types]
    return documents, {
        "document_count": len(documents),
        "required_document_count": len(REQUIRED_DRIVER_DOCUMENT_TYPES),
        "document_types_present": present_types,
        "missing_document_types": missing_types,
        "document_access_error": access_error,
    }


def _load_customer_profile_user_ids(supabase_client: Any, user_ids: list[str]) -> set[str]:
    if not user_ids:
        return set()

    try:
        result = (
            supabase_client.table("customer_profiles")
            .select("user_id")
            .in_("user_id", user_ids)
            .execute()
        )
        return {str(row.get("user_id")) for row in (result.data or []) if row.get("user_id")}
    except Exception:
        return set()


def _driver_identity_fields(user_id: Optional[str], user_info: dict[str, Any], customer_profile_user_ids: set[str]) -> dict[str, Any]:
    linked_customer_user_id = str(user_id) if user_id else None
    has_customer_profile = bool(
        linked_customer_user_id and (
            linked_customer_user_id in customer_profile_user_ids or user_info.get("role") == "customer"
        )
    )
    platform_roles = ["driver"]
    if has_customer_profile:
        platform_roles.append("customer")

    linked_customer = None
    if has_customer_profile and linked_customer_user_id:
        linked_customer = {
            "id": linked_customer_user_id,
            "status": "active" if user_info.get("is_active", True) else "suspended",
        }

    return {
        "has_customer_profile": has_customer_profile,
        "linked_customer_user_id": linked_customer_user_id if has_customer_profile else None,
        "platform_roles": platform_roles,
        "linked_customer": linked_customer,
    }


class DriverProfileFullResponse(DriverDetailResponse):
    """Full driver profile returned to both admin and driver-facing lookups.
    Includes verification_feedback so drivers can see the exact rejection reason."""
    verification_feedback: Optional[str] = None


class DriverAdminListResponse(BaseModel):
    drivers: List[DriverAdminListItem]
    total: int


class CreateDriverRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100)
    phone_number: str = Field(..., min_length=7, max_length=20)
    email: Optional[str] = None
    license_number: str = Field(..., min_length=3, max_length=30)
    license_expiry: date
    vehicle_type: str = Field(..., pattern=r"^(car|moto|tuk_tuk|van|suv)$")

    @field_validator("phone_number")
    @classmethod
    def phone_digits_only(cls, v: str) -> str:
        if not re.match(r"^\+?[\d\s\-]{7,20}$", v):
            raise ValueError("Invalid phone number format")
        return v


class CreateDriverResponse(BaseModel):
    id: str
    full_name: str
    phone_number: str
    role: str
    verification_status: str
    otp_sent: bool


_DRIVER_FIELDS = {f for f in DriverAdminListItem.model_fields if f not in ("full_name", "phone_number", "total_trips")}


@router.post(
    "/admin/create",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateDriverResponse,
    dependencies=[Depends(require_role("operations", "super_admin"))],
)
def create_driver(body: CreateDriverRequest, _user: dict = Depends(require_role("operations", "super_admin"))):
    sb = get_supabase()

    # 1. Check phone uniqueness
    try:
        phone_check = sb.table("users").select("id").eq("phone_number", body.phone_number).limit(1).execute()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
    if phone_check.data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Phone number already registered")

    # 2. Create Supabase Auth user (phone-only, no password — OTP login)
    try:
        from gotrue.types import AdminUserAttributes
        auth_result = sb.auth.admin.create_user(
            AdminUserAttributes(phone=body.phone_number, phone_confirm=True)
        )
        auth_uid = auth_result.user.id
    except Exception as exc:
        msg = str(exc).lower()
        if "already registered" in msg or "already been registered" in msg:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Phone number already registered")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    # 3. Insert into users table
    new_user_id = str(uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        sb.table("users").insert({
            "id": new_user_id,
            "supabase_uid": auth_uid,
            "full_name": body.full_name,
            "phone_number": body.phone_number,
            "email": body.email,
            "role": "driver",
            "is_active": True,
            "created_at": now_iso,
            "updated_at": now_iso,
        }).execute()
    except Exception as exc:
        try:
            sb.auth.admin.delete_user(auth_uid)
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    # 4. Insert into driver_profiles
    try:
        sb.table("driver_profiles").insert({
            "user_id": new_user_id,
            "license_number": body.license_number,
            "license_expiry": body.license_expiry.isoformat(),
            "vehicle_type": body.vehicle_type,
            "verification_status": "pending",
            "is_online": False,
            "rating": 0,
            "total_trips": 0,
            "created_at": now_iso,
        }).execute()
    except Exception as exc:
        # users row was created; try to clean up auth but leave users row for data integrity
        try:
            sb.auth.admin.delete_user(auth_uid)
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    # 5. Trigger OTP — sends SMS via Twilio; non-fatal if it fails
    otp_sent = True
    try:
        sb.auth.sign_in_with_otp({"phone": body.phone_number})
    except Exception as otp_exc:
        import logging as _logging
        _logging.getLogger(__name__).warning("OTP send failed for %s: %s", body.phone_number, otp_exc)
        otp_sent = False

    # 6. Audit log
    write_audit_log(
        sb=sb,
        admin_user=_user,
        action_type="manual_user_create",
        entity_type="users",
        entity_id=new_user_id,
        summary=f"Admin manually created driver: {body.full_name} ({body.phone_number})",
        after_state={
            "full_name": body.full_name,
            "phone_number": body.phone_number,
            "role": "driver",
            "license_number": body.license_number,
            "vehicle_type": body.vehicle_type,
        },
    )

    return CreateDriverResponse(
        id=new_user_id,
        full_name=body.full_name,
        phone_number=body.phone_number,
        role="driver",
        verification_status="pending",
        otp_sent=otp_sent,
    )


@router.get("/admin/list", response_model=DriverAdminListResponse)
def list_drivers(
    verification_status: Optional[str] = Query(None),
    _user=Depends(require_driver_document_access),
):
    try:
        sb = get_supabase()
        query = sb.table("driver_profiles").select(
            "*, users(full_name, phone_number, role, is_active), driver_documents(*)"
        )
        if verification_status:
            query = query.eq("verification_status", verification_status)
        result = query.order("created_at", desc=True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    customer_profile_user_ids = _load_customer_profile_user_ids(
        sb,
        [str(row.get("user_id")) for row in (result.data or []) if row.get("user_id")],
    )

    drivers = []
    for r in result.data or []:
        user_info = r.pop("users", {}) or {}
        document_rows = r.pop("driver_documents", []) or []
        row_fields = {k: v for k, v in r.items() if k in _DRIVER_FIELDS}
        documents, document_summary = _document_summary(document_rows, sb)
        identity_fields = _driver_identity_fields(r.get("user_id"), user_info, customer_profile_user_ids)
        drivers.append(DriverAdminListItem(
            **row_fields,
            full_name=user_info.get("full_name"),
            phone_number=user_info.get("phone_number"),
            total_trips=r.get("total_rides", 0) or 0,
            **identity_fields,
            documents=documents,
            **document_summary,
        ))

    return DriverAdminListResponse(drivers=drivers, total=len(drivers))


@router.get("/by-user/{user_id}", response_model=DriverProfileFullResponse)
def get_driver_by_user(user_id: str, _user=Depends(require_driver_document_access)):
    """Fetch a driver's full profile by their user_id (auth uid)."""
    try:
        sb = get_supabase()
        result = (
            sb.table("driver_profiles")
            .select("*, users(full_name, phone_number, role, is_active), vehicle_details(*), driver_documents(*)")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Driver profile not found for this user")

    row = result.data
    user_info = row.pop("users", {}) or {}
    vehicle_data = row.pop("vehicle_details", None)
    if isinstance(vehicle_data, list):
        vehicle_data = vehicle_data[0] if vehicle_data else None
    document_rows = row.pop("driver_documents", []) or []
    base_fields = {k: v for k, v in row.items() if k in _DRIVER_FIELDS and k != "verification_feedback"}
    documents, document_summary = _document_summary(document_rows, sb)
    identity_fields = _driver_identity_fields(
        row.get("user_id"),
        user_info,
        _load_customer_profile_user_ids(sb, [str(row.get("user_id"))]) if row.get("user_id") else set(),
    )

    write_audit_log(
        sb=sb,
        admin_user=_user,
        action_type="driver_documents_viewed",
        entity_type="driver_profiles",
        entity_id=str(row.get("id")),
        summary=f"Admin viewed driver documents for user {user_id}",
        after_state={
            "document_count": document_summary["document_count"],
            "signed_url_ttl_seconds": DRIVER_DOCUMENT_URL_TTL_SECONDS,
        },
    )

    return DriverProfileFullResponse(
        **base_fields,
        verification_feedback=row.get("verification_feedback"),
        full_name=user_info.get("full_name"),
        phone_number=user_info.get("phone_number"),
        total_trips=row.get("total_rides", 0) or 0,
        **identity_fields,
        vehicle=VehicleDetailsResponse(**vehicle_data) if vehicle_data else None,
        documents=documents,
        **document_summary,
    )


@router.get("/{driver_id}", response_model=DriverProfileFullResponse)
def get_driver_detail(driver_id: str, _user=Depends(require_driver_document_access)):
    try:
        sb = get_supabase()
        result = (
            sb.table("driver_profiles")
            .select("*, users(full_name, phone_number, role, is_active), vehicle_details(*), driver_documents(*)")
            .eq("id", driver_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Driver not found")

    row = result.data
    user_info = row.pop("users", {}) or {}
    vehicle_data = row.pop("vehicle_details", None)
    if isinstance(vehicle_data, list):
        vehicle_data = vehicle_data[0] if vehicle_data else None
    document_rows = row.pop("driver_documents", []) or []
    base_fields = {k: v for k, v in row.items() if k in _DRIVER_FIELDS and k != "verification_feedback"}
    documents, document_summary = _document_summary(document_rows, sb)
    identity_fields = _driver_identity_fields(
        row.get("user_id"),
        user_info,
        _load_customer_profile_user_ids(sb, [str(row.get("user_id"))]) if row.get("user_id") else set(),
    )

    write_audit_log(
        sb=sb,
        admin_user=_user,
        action_type="driver_documents_viewed",
        entity_type="driver_profiles",
        entity_id=driver_id,
        summary=f"Admin viewed driver documents for driver {driver_id}",
        after_state={
            "document_count": document_summary["document_count"],
            "signed_url_ttl_seconds": DRIVER_DOCUMENT_URL_TTL_SECONDS,
        },
    )

    return DriverProfileFullResponse(
        **base_fields,
        verification_feedback=row.get("verification_feedback"),
        full_name=user_info.get("full_name"),
        phone_number=user_info.get("phone_number"),
        total_trips=row.get("total_rides", 0) or 0,
        **identity_fields,
        vehicle=VehicleDetailsResponse(**vehicle_data) if vehicle_data else None,
        documents=documents,
        **document_summary,
    )


@router.patch("/{driver_id}/activate")
def activate_driver(driver_id: str, _user=Depends(require_admin)):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")

    try:
        result = first_row(
            call_rpc("approve_driver", {"p_driver_id": driver_id, "p_admin_id": admin_id})
        )
    except Exception as e:
        raise rpc_error_to_http_exception(e)
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")
    return result


@router.patch("/{driver_id}/deactivate")
def deactivate_driver(
    driver_id: str,
    feedback: Optional[str] = Query(None),
    _user=Depends(require_admin),
):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")

    try:
        result = first_row(
            call_rpc(
                "reject_driver",
                {
                    "p_driver_id": driver_id,
                    "p_reason": feedback,
                    "p_admin_id": admin_id,
                },
            )
        )
    except Exception as e:
        raise rpc_error_to_http_exception(e)
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")
    return result


class RejectDriverBody(BaseModel):
    feedback: Optional[str] = None
    reason: Optional[str] = None


@router.patch("/{driver_id}/reject")
def reject_driver(driver_id: str, body: RejectDriverBody, _user=Depends(require_admin)):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")
    reason = body.feedback or body.reason
    try:
        result = first_row(
            call_rpc("reject_driver", {"p_driver_id": driver_id, "p_reason": reason, "p_admin_id": admin_id})
        )
    except Exception as e:
        raise rpc_error_to_http_exception(e)
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")
    return result


class SuspendDriverBody(BaseModel):
    reason: str
    end_date: Optional[str] = None
    appeal_url: Optional[str] = None


@router.patch("/{driver_id}/suspend")
def suspend_driver(driver_id: str, body: SuspendDriverBody, _user=Depends(require_admin)):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")
    try:
        result = first_row(
            call_rpc(
                "reject_driver",
                {"p_driver_id": driver_id, "p_reason": body.reason, "p_admin_id": admin_id},
            )
        )
    except Exception as e:
        raise rpc_error_to_http_exception(e)
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")

    # Push notification to driver
    try:
        sb = get_supabase()
        dp = sb.table("driver_profiles").select("user_id").eq("id", driver_id).maybe_single().execute()
        if dp.data and dp.data.get("user_id"):
            msg = f"Your driver account has been suspended. Reason: {body.reason}"
            send_push_to_users(
                [dp.data["user_id"]], "Account Suspended", msg,
                notification_type="driver_update", persist=False,
            )
    except Exception:
        pass

    return result


@router.patch("/{driver_id}/unsuspend")
def unsuspend_driver(driver_id: str, _user=Depends(require_admin)):
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")
    try:
        result = first_row(
            call_rpc("approve_driver", {"p_driver_id": driver_id, "p_admin_id": admin_id})
        )
    except Exception as e:
        raise rpc_error_to_http_exception(e)
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")

    # Push notification to driver
    try:
        sb = get_supabase()
        dp = sb.table("driver_profiles").select("user_id").eq("id", driver_id).maybe_single().execute()
        if dp.data and dp.data.get("user_id"):
            send_push_to_users(
                [dp.data["user_id"]],
                "Account Reinstated",
                "Your driver account has been reinstated. You can go online and accept rides again.",
                notification_type="driver_update",
                persist=False,
            )
    except Exception:
        pass

    return result


@router.patch("/{driver_id}/block")
def block_driver(driver_id: str, _user=Depends(require_admin)):
    """Block driver if wallet balance is zero or negative (calls block_driver_if_no_balance RPC)."""
    admin_id = _user.get("id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Admin token must include a user id")
    try:
        result = first_row(
            call_rpc(
                "block_driver_if_no_balance",
                {"p_driver_id": driver_id, "p_admin_id": admin_id},
            )
        )
    except Exception as e:
        raise rpc_error_to_http_exception(e)
    if not result:
        raise HTTPException(status_code=404, detail="Driver not found")
    return result


class CategoryUpdateBody(BaseModel):
    category: str


@router.patch("/{driver_id}/category")
def update_driver_category(driver_id: str, body: CategoryUpdateBody, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        sb.table("driver_profiles").update({"vehicle_type": body.category}).eq("id", driver_id).execute()
        result = sb.table("driver_profiles").select(
            "*, users(full_name, phone_number)"
        ).eq("id", driver_id).maybe_single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.data:
        raise HTTPException(status_code=404, detail="Driver not found")
    return result.data


class DocumentRejectBody(BaseModel):
    reason: str


@router.patch("/{driver_id}/documents/{document_id}/approve")
def approve_driver_document(driver_id: str, document_id: str, _user=Depends(require_driver_document_access)):
    """Approve an individual driver document."""
    try:
        sb = get_supabase()
        result = (
            sb.table("driver_documents")
            .update({"status": "approved"})
            .eq("id", document_id)
            .eq("driver_id", driver_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.data:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"message": "Document approved", "document_id": document_id}


@router.patch("/{driver_id}/documents/{document_id}/reject")
def reject_driver_document(driver_id: str, document_id: str, body: DocumentRejectBody, _user=Depends(require_driver_document_access)):
    """Reject an individual driver document with a reason."""
    try:
        sb = get_supabase()
        result = (
            sb.table("driver_documents")
            .update({"status": "rejected", "rejection_reason": body.reason})
            .eq("id", document_id)
            .eq("driver_id", driver_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.data:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"message": "Document rejected", "document_id": document_id, "reason": body.reason}


@router.delete("/{driver_id}")
def delete_driver(driver_id: str, _user=Depends(require_admin)):
    try:
        sb = get_supabase()
        sb.table("driver_profiles").delete().eq("id", driver_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"message": "Driver deleted"}


@router.get("/{driver_id}/trips")
def get_driver_trips(
    driver_id: str,
    limit: int = Query(50),
    offset: int = Query(0),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("rides")
            .select("*")
            .eq("driver_id", driver_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"trips": result.data or [], "total": len(result.data or [])}


@router.get("/{driver_id}/earnings")
def get_driver_earnings(
    driver_id: str,
    limit: int = Query(50),
    offset: int = Query(0),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("wallet_transactions")
            .select("*")
            .eq("driver_id", driver_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        balance_result = (
            sb.table("driver_profiles")
            .select("credit_balance")
            .eq("id", driver_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    balance = (balance_result.data or {}).get("credit_balance", 0)
    return {"transactions": result.data or [], "current_balance": balance}


@router.get("/{driver_id}/ratings")
def get_driver_ratings(
    driver_id: str,
    limit: int = Query(50),
    offset: int = Query(0),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("ride_ratings")
            .select("*")
            .eq("driver_id", driver_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        profile = (
            sb.table("driver_profiles")
            .select("rating, total_rides")
            .eq("id", driver_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "ratings": result.data or [],
        "average_rating": (profile.data or {}).get("rating", 0),
        "total_trips": (profile.data or {}).get("total_rides", 0),
    }


@router.get("/{driver_id}/compliance")
def get_driver_compliance(driver_id: str, _user=Depends(require_driver_document_access)):
    try:
        sb = get_supabase()
        docs = sb.table("driver_documents").select("*").eq("driver_id", driver_id).execute()
        profile = (
            sb.table("driver_profiles")
            .select("license_number, license_expiry, verification_status")
            .eq("id", driver_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    documents, document_summary = _document_summary(docs.data or [], sb)
    return {
        "documents": [document.model_dump() for document in documents],
        "license_number": (profile.data or {}).get("license_number"),
        "license_expiry": (profile.data or {}).get("license_expiry"),
        "verification_status": (profile.data or {}).get("verification_status"),
        **document_summary,
    }


@router.get("/{driver_id}/activity")
def get_driver_activity(
    driver_id: str,
    limit: int = Query(50),
    _user=Depends(require_admin),
):
    try:
        sb = get_supabase()
        rides = (
            sb.table("rides")
            .select("id, status, created_at, completed_at, cancelled_at")
            .eq("driver_id", driver_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"activity": rides.data or []}
