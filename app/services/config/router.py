from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional
from app.core.dependencies import require_admin
from app.core.supabase import get_supabase
from app.services.audit.router import write_audit_log

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
        before = get_app_toggles(_user)

        if body.active_request_resume_enabled is not None:
            _set_config_value(sb, "active_request_resume_enabled", str(body.active_request_resume_enabled).lower())

        if body.driver_offer_update_enabled is not None:
            _set_config_value(sb, "driver_offer_update_enabled", str(body.driver_offer_update_enabled).lower())

        if body.stale_request_alert_threshold_minutes is not None:
            _set_config_value(sb, "stale_request_alert_threshold_minutes", str(body.stale_request_alert_threshold_minutes))

        after = get_app_toggles(_user)

        write_audit_log(
            sb=sb,
            admin_user=_user,
            action_type="app_toggles_updated",
            entity_type="app_config",
            entity_id="app-toggles",
            summary="Admin updated app configuration toggles",
            before_state=before.model_dump(),
            after_state=after.model_dump(),
        )

        return after
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Operating-area discovery ─────────────────────────────────────────────
#
# Backend-owned country -> city -> operating-area hierarchy (public.service_areas,
# see sql/20260827_service_areas.sql) so the Admin Frontend can discover
# configured markets/cities/areas without any location ever being hardcoded in
# frontend code. Read-only: adding a future country/city/area is a config-row
# insert, never a frontend deploy. Also consumed by GET /analytics/admin/heatmap's
# optional service_area_id param (app/services/analytics/router.py).


class ServiceAreaItem(BaseModel):
    id: str
    country_code: str
    country_name: str
    city: str
    area_name: str
    is_active: bool = True
    north: float
    south: float
    east: float
    west: float


class ServiceAreasResponse(BaseModel):
    areas: List[ServiceAreaItem] = []


@router.get("/admin/service-areas", response_model=ServiceAreasResponse)
def get_service_areas(
    include_inactive: bool = Query(False, description="When true, include areas with is_active=false. Defaults to active-only."),
    _user=Depends(require_admin),
):
    """
    List configured operating areas (country/city/area hierarchy + bbox), for
    the Admin Frontend's Heatmap location selector and similar UI.

    Defaults to active areas only -- pass include_inactive=true to also see
    disabled areas (e.g. for a future admin-management view). A single direct
    table read, no joins, no aggregation -- this data is small and rarely
    changes, unlike Heatmap's own RPC-backed aggregation over ride/driver
    activity tables.
    """
    try:
        sb = get_supabase()
        query = sb.table("service_areas").select(
            "id, country_code, country_name, city, area_name, is_active, north, south, east, west"
        )
        if not include_inactive:
            query = query.eq("is_active", True)
        rows = query.execute().data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return ServiceAreasResponse(areas=[ServiceAreaItem(**row) for row in rows])
