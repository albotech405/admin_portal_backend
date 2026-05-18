from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.core.dependencies import require_admin
from app.core.supabase import get_supabase

router = APIRouter(prefix="/config", tags=["config"])

# Default values for app configuration toggles
_DEFAULT_TOGGLES = {
    "active_request_resume_enabled": "true",
    "driver_offer_update_enabled": "true",
    "stale_request_alert_threshold_minutes": "10",
}


class AppConfigResponse(BaseModel):
    active_request_resume_enabled: bool = True
    driver_offer_update_enabled: bool = True
    stale_request_alert_threshold_minutes: int = 10


class UpdateAppConfigBody(BaseModel):
    active_request_resume_enabled: Optional[bool] = None
    driver_offer_update_enabled: Optional[bool] = None
    stale_request_alert_threshold_minutes: Optional[int] = None


def _get_config_value(sb, key: str, default: str) -> str:
    """Get a single config value from the app_config table."""
    try:
        result = (
            sb.table("app_config")
            .select("value")
            .eq("key", key)
            .maybe_single()
            .execute()
        )
        if result.data and result.data.get("value") is not None:
            return str(result.data["value"])
    except Exception:
        pass
    return default


def _set_config_value(sb, key: str, value: str):
    """Upsert a config value into the app_config table."""
    try:
        # Check if key exists
        existing = (
            sb.table("app_config")
            .select("id")
            .eq("key", key)
            .maybe_single()
            .execute()
        )
        if existing.data:
            sb.table("app_config").update({"value": value}).eq("key", key).execute()
        else:
            sb.table("app_config").insert({"key": key, "value": value}).execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update config key '{key}': {str(e)}",
        )


@router.get("/admin/app-toggles", response_model=AppConfigResponse)
def get_app_toggles(_user=Depends(require_admin)):
    try:
        sb = get_supabase()

        resume_enabled = _get_config_value(sb, "active_request_resume_enabled", _DEFAULT_TOGGLES["active_request_resume_enabled"])
        offer_update_enabled = _get_config_value(sb, "driver_offer_update_enabled", _DEFAULT_TOGGLES["driver_offer_update_enabled"])
        stale_threshold = _get_config_value(sb, "stale_request_alert_threshold_minutes", _DEFAULT_TOGGLES["stale_request_alert_threshold_minutes"])

        return AppConfigResponse(
            active_request_resume_enabled=resume_enabled.lower() == "true",
            driver_offer_update_enabled=offer_update_enabled.lower() == "true",
            stale_request_alert_threshold_minutes=int(stale_threshold),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/admin/app-toggles", response_model=AppConfigResponse)
def update_app_toggles(body: UpdateAppConfigBody, _user=Depends(require_admin)):
    try:
        sb = get_supabase()

        if body.active_request_resume_enabled is not None:
            _set_config_value(sb, "active_request_resume_enabled", str(body.active_request_resume_enabled).lower())

        if body.driver_offer_update_enabled is not None:
            _set_config_value(sb, "driver_offer_update_enabled", str(body.driver_offer_update_enabled).lower())

        if body.stale_request_alert_threshold_minutes is not None:
            _set_config_value(sb, "stale_request_alert_threshold_minutes", str(body.stale_request_alert_threshold_minutes))

        # Return updated state
        return get_app_toggles(_user)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
