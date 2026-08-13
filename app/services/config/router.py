from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
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


class ExchangeRateResponse(BaseModel):
    """Matches the ExchangeRate interface the admin portal expects."""
    id: Optional[str] = None
    rate_cdf_per_usd: float
    source: str = "manual"
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    set_by: Optional[str] = None
    created_at: Optional[str] = None


class SetExchangeRateBody(BaseModel):
    rate_cdf_per_usd: float = Field(..., gt=0)
    source: str = "manual"
    effective_from: Optional[str] = None


# Used only if the table has no rows yet, so the Dashboard and Finance screens render
# a sane figure instead of erroring on a fresh install.
_DEFAULT_CDF_PER_USD = 2800.0


def _missing_table_detail(exc: Exception, what: str) -> str:
    """Turn PostgREST's schema-cache miss into an actionable operator message.

    Deliberately does not fall back to a default rate: silently serving an invented
    figure for a currency conversion would be worse than surfacing the misconfiguration.
    """
    message = str(exc)
    if "PGRST205" in message or "schema cache" in message:
        return (
            f"The {what} storage table is missing. Apply "
            "sql/20260813_app_config_and_exchange_rates.sql in the Supabase SQL editor."
        )
    return message


@router.get("/admin/exchange-rate", response_model=ExchangeRateResponse)
def get_exchange_rate(_user=Depends(require_admin)):
    """Return the currently effective USD→CDF rate (the newest row by effective_from)."""
    try:
        sb = get_supabase()
        result = (
            sb.table("exchange_rates")
            .select("*")
            .order("effective_from", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_missing_table_detail(e, "exchange rate"))

    rows = result.data or []
    if not rows:
        return ExchangeRateResponse(
            rate_cdf_per_usd=_DEFAULT_CDF_PER_USD,
            source="manual",
        )

    row = rows[0]
    return ExchangeRateResponse(
        id=str(row["id"]) if row.get("id") else None,
        rate_cdf_per_usd=float(row["rate_cdf_per_usd"]),
        source=row.get("source") or "manual",
        effective_from=row.get("effective_from"),
        effective_to=row.get("effective_to"),
        set_by=row.get("set_by"),
        created_at=row.get("created_at"),
    )


@router.put("/admin/exchange-rate", response_model=ExchangeRateResponse)
def set_exchange_rate(body: SetExchangeRateBody, _user=Depends(require_admin)):
    """Record a new rate.

    Rates are append-only: the previous row keeps its history and is closed off with an
    effective_to, so past conversions stay auditable rather than being overwritten.
    """
    sb = get_supabase()
    effective_from = body.effective_from or datetime.now(timezone.utc).isoformat()

    try:
        previous = (
            sb.table("exchange_rates")
            .select("id")
            .is_("effective_to", "null")
            .order("effective_from", desc=True)
            .limit(1)
            .execute()
        )
        for row in previous.data or []:
            sb.table("exchange_rates").update({"effective_to": effective_from}).eq("id", row["id"]).execute()

        inserted = (
            sb.table("exchange_rates")
            .insert({
                "rate_cdf_per_usd": body.rate_cdf_per_usd,
                "source": body.source if body.source in ("live", "manual") else "manual",
                "effective_from": effective_from,
                "set_by": (_user or {}).get("sub") if isinstance(_user, dict) else None,
            })
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_missing_table_detail(e, "exchange rate"))

    rows = inserted.data or []
    if not rows:
        raise HTTPException(status_code=500, detail="Exchange rate was not saved.")

    row = rows[0]
    return ExchangeRateResponse(
        id=str(row["id"]) if row.get("id") else None,
        rate_cdf_per_usd=float(row["rate_cdf_per_usd"]),
        source=row.get("source") or "manual",
        effective_from=row.get("effective_from"),
        effective_to=row.get("effective_to"),
        set_by=row.get("set_by"),
        created_at=row.get("created_at"),
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
