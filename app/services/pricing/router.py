from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timezone
from uuid import uuid4

from app.core.dependencies import require_admin
from app.core.supabase import get_supabase

router = APIRouter()

# ── Request / Response models ──────────────────────────────────────────────


class VehiclePricingItem(BaseModel):
    """Per-vehicle-type pricing row from pricing_config."""
    id: str
    vehicle_type: str
    time_band: str  # "standard" | "night"
    category: Optional[str] = None  # null for base rates
    base_fare_usd: float
    per_km_usd: float
    per_min_usd: float
    minimum_fare_usd: float = 0.0
    bid_floor_usd: Optional[float] = None
    is_active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class VehiclePricingConfig(BaseModel):
    """Aggregated pricing for one vehicle type (car / motorcycle)."""
    vehicle_type: str
    base_fare: float
    per_km: float
    minimum_fare: float
    night_multiplier: float
    is_active: bool


class UpdateVehiclePricingBody(BaseModel):
    base_fare: Optional[float] = None
    per_km: Optional[float] = None
    minimum_fare: Optional[float] = None
    night_multiplier: Optional[float] = None
    is_active: Optional[bool] = None


class GlobalPricingConfig(BaseModel):
    vat_rate: float = 0.16
    day_multiplier: float = 1.00
    evening_multiplier: float = 1.10
    commission_rate: float = 0.05


class UpdateGlobalPricingBody(BaseModel):
    vat_rate: Optional[float] = None
    day_multiplier: Optional[float] = None
    evening_multiplier: Optional[float] = None
    commission_rate: Optional[float] = None


class CategoryMultiplierItem(BaseModel):
    category: str  # "standard" | "premium" | "lady_driver"
    multiplier: float
    description: str
    is_active: bool


class UpdateCategoryMultiplierBody(BaseModel):
    multiplier: Optional[float] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class FareSimulateRequest(BaseModel):
    vehicle_type: str = "car"
    distance_km: float = 5.0
    time_band: str = "day"  # "day" | "evening" | "night"
    category: str = "standard"


class FareSimulateStep(BaseModel):
    step: str
    label: str
    value: float


class FareSimulateResponse(BaseModel):
    steps: List[FareSimulateStep]
    final_price: float
    price_excluding_vat: float
    commission_amount: float
    vat_amount: float
    driver_net: float


class CategoryMetricsItem(BaseModel):
    category: str
    ride_volume: int
    average_fare: Optional[float] = None
    active_drivers: int
    total_requests: int
    completed_trips: int
    conversion_rate: Optional[float] = None  # completed / requests


class PricingConfigResponse(BaseModel):
    vehicles: List[VehiclePricingConfig]
    global_config: GlobalPricingConfig
    category_multipliers: List[CategoryMultiplierItem]


class PricingAuditLogItem(BaseModel):
    id: str
    admin_id: str
    admin_name: Optional[str] = None
    change_type: str  # "vehicle_pricing" | "global_config" | "category_multiplier"
    change_summary: str
    previous_values: Optional[dict] = None
    new_values: Optional[dict] = None
    created_at: str


# ── Helpers ─────────────────────────────────────────────────────────────────


def _get_time_multiplier(time_band: str, global_config: GlobalPricingConfig) -> float:
    """Map time band string to multiplier."""
    if time_band == "day":
        return global_config.day_multiplier
    elif time_band == "evening":
        return global_config.evening_multiplier
    elif time_band == "night":
        return 1.0  # night uses per-vehicle night_multiplier, applied separately
    return 1.0


def _compute_fare_pipeline(
    base_fare: float,
    per_km: float,
    distance_km: float,
    minimum_fare: float,
    time_multiplier: float,
    night_multiplier: float,
    time_band: str,
    category_multiplier: float,
    vat_rate: float,
    commission_rate: float,
) -> FareSimulateResponse:
    """
    Compute the full pricing pipeline:
    1. base_price        = base_fare + (distance_km × per_km)
    2. time_adjusted     = base_price × time_multiplier
    3. floored_price     = max(time_adjusted, minimum_fare)
    4. night_adjusted    = floored_price × night_multiplier (only for night band)
    5. category_adjusted = night_adjusted × category_multiplier
    6. final_price       = category_adjusted × (1 + vat_rate)
    """
    base_price = base_fare + (distance_km * per_km)

    if time_band == "night":
        time_adjusted = base_price  # night uses night_multiplier instead
        night_adjusted = time_adjusted * night_multiplier
    else:
        time_adjusted = base_price * time_multiplier
        night_adjusted = time_adjusted

    floored_price = max(night_adjusted, minimum_fare)
    category_adjusted = floored_price * category_multiplier
    final_price = category_adjusted * (1 + vat_rate)

    price_excluding_vat = final_price / (1 + vat_rate)
    commission_amount = price_excluding_vat * commission_rate
    vat_amount = final_price - price_excluding_vat
    driver_net = price_excluding_vat - commission_amount

    steps = [
        FareSimulateStep(step="base_price", label=f"Base price ({base_fare} + {distance_km}km × {per_km})", value=round(base_price, 4)),
        FareSimulateStep(step="time_adjusted", label=f"Time-adjusted (×{time_multiplier if time_band != 'night' else 1.0})", value=round(time_adjusted, 4)),
    ]
    if time_band == "night":
        steps.append(FareSimulateStep(step="night_adjusted", label=f"Night-adjusted (×{night_multiplier})", value=round(night_adjusted, 4)))
    steps.append(FareSimulateStep(step="floored_price", label=f"Floored (min {minimum_fare})", value=round(floored_price, 4)))
    steps.append(FareSimulateStep(step="category_adjusted", label=f"Category-adjusted (×{category_multiplier})", value=round(category_adjusted, 4)))
    steps.append(FareSimulateStep(step="final_price", label=f"Final (×{1 + vat_rate} VAT)", value=round(final_price, 4)))

    return FareSimulateResponse(
        steps=steps,
        final_price=round(final_price, 2),
        price_excluding_vat=round(price_excluding_vat, 4),
        commission_amount=round(commission_amount, 4),
        vat_amount=round(vat_amount, 4),
        driver_net=round(driver_net, 4),
    )


def _log_audit(
    sb,
    admin_id: str,
    change_type: str,
    change_summary: str,
    previous_values: Optional[dict] = None,
    new_values: Optional[dict] = None,
):
    """Write an entry to the pricing_audit_log table."""
    try:
        sb.table("pricing_audit_log").insert({
            "id": str(uuid4()),
            "admin_id": admin_id,
            "change_type": change_type,
            "change_summary": change_summary,
            "previous_values": previous_values,
            "new_values": new_values,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        pass  # Non-critical; audit failures should not block the response


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/admin/pricing/config", response_model=PricingConfigResponse)
def get_pricing_config(_user=Depends(require_admin)):
    """
    Get the full pricing configuration:
    - Per-vehicle pricing (car, motorcycle)
    - Global time-band config (stored in app_config)
    - Category multipliers (stored in app_config)
    """
    sb = get_supabase()

    try:
        # 1. Fetch per-vehicle pricing from pricing_config table
        result = sb.table("pricing_config").select("*").order("vehicle_type").execute()
        rows = result.data or []

        # Aggregate into per-vehicle configs
        vehicle_map: dict[str, dict] = {}
        for row in rows:
            vt = row.get("vehicle_type", "")
            if vt not in vehicle_map:
                vehicle_map[vt] = {
                    "vehicle_type": vt,
                    "base_fare": 0.0,
                    "per_km": 0.0,
                    "minimum_fare": 0.0,
                    "night_multiplier": 1.0,
                    "is_active": True,
                }
            tb = row.get("time_band", "standard")
            if tb == "standard":
                vehicle_map[vt]["base_fare"] = float(row.get("base_fare_usd", 0))
                vehicle_map[vt]["per_km"] = float(row.get("per_km_usd", 0))
                vehicle_map[vt]["minimum_fare"] = float(row.get("minimum_fare_usd", 0) or 0)
            elif tb == "night":
                # night_multiplier = night base / standard base
                std_fare = vehicle_map[vt].get("base_fare", 1.0)
                night_fare = float(row.get("base_fare_usd", 0))
                if std_fare > 0:
                    vehicle_map[vt]["night_multiplier"] = round(night_fare / std_fare, 4)
            vehicle_map[vt]["is_active"] = row.get("is_active", True)

        vehicles = [VehiclePricingConfig(**v) for v in vehicle_map.values()]

        # 2. Fetch global config from app_config
        global_config = GlobalPricingConfig()
        try:
            config_result = sb.table("app_config").select("*").execute()
            config_map = {c["key"]: c["value"] for c in (config_result.data or [])}
            if "vat_rate" in config_map:
                global_config.vat_rate = float(config_map["vat_rate"])
            if "day_multiplier" in config_map:
                global_config.day_multiplier = float(config_map["day_multiplier"])
            if "evening_multiplier" in config_map:
                global_config.evening_multiplier = float(config_map["evening_multiplier"])
            if "commission_rate" in config_map:
                global_config.commission_rate = float(config_map["commission_rate"])
        except Exception:
            pass  # Use defaults

        # 3. Fetch category multipliers from app_config
        category_multipliers = [
            CategoryMultiplierItem(category="standard", multiplier=1.00, description="Default for cars + all motorcycles", is_active=True),
            CategoryMultiplierItem(category="premium", multiplier=1.25, description="Cars only — newer cars, AC required, vehicle year ≥ 2015", is_active=True),
            CategoryMultiplierItem(category="lady_driver", multiplier=1.15, description="Cars only, female drivers", is_active=True),
        ]
        try:
            if "category_multiplier_standard" in config_map:
                category_multipliers[0].multiplier = float(config_map["category_multiplier_standard"])
            if "category_multiplier_premium" in config_map:
                category_multipliers[1].multiplier = float(config_map["category_multiplier_premium"])
            if "category_multiplier_lady_driver" in config_map:
                category_multipliers[2].multiplier = float(config_map["category_multiplier_lady_driver"])
            if "category_standard_active" in config_map:
                category_multipliers[0].is_active = config_map["category_standard_active"].lower() == "true"
            if "category_premium_active" in config_map:
                category_multipliers[1].is_active = config_map["category_premium_active"].lower() == "true"
            if "category_lady_driver_active" in config_map:
                category_multipliers[2].is_active = config_map["category_lady_driver_active"].lower() == "true"
        except Exception:
            pass

        return PricingConfigResponse(
            vehicles=vehicles,
            global_config=global_config,
            category_multipliers=category_multipliers,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch pricing config: {str(e)}")


@router.put("/admin/pricing/vehicle/{vehicle_type}", response_model=VehiclePricingConfig)
def update_vehicle_pricing(
    vehicle_type: str,
    body: UpdateVehiclePricingBody,
    _user=Depends(require_admin),
):
    """
    Update per-vehicle pricing config.
    Updates both 'standard' and 'night' time-band rows in pricing_config.
    """
    sb = get_supabase()
    admin_id = _user.get("id", "unknown")

    try:
        # Fetch current values for audit
        current_rows = sb.table("pricing_config").select("*").eq("vehicle_type", vehicle_type).execute()
        current_data = current_rows.data or []
        previous_values = {r["time_band"]: {k: float(v) if isinstance(v, (int, float)) else v for k, v in r.items() if k in ("base_fare_usd", "per_km_usd", "per_min_usd", "minimum_fare_usd", "is_active")} for r in current_data}

        now = datetime.now(timezone.utc).isoformat()

        # Update standard time-band row
        standard_row = None
        night_row = None
        for r in current_data:
            if r.get("time_band") == "standard":
                standard_row = r
            elif r.get("time_band") == "night":
                night_row = r

        # Upsert standard row
        standard_update = {"updated_at": now}
        if body.base_fare is not None:
            standard_update["base_fare_usd"] = body.base_fare
        if body.per_km is not None:
            standard_update["per_km_usd"] = body.per_km
        if body.minimum_fare is not None:
            standard_update["minimum_fare_usd"] = body.minimum_fare
        if body.is_active is not None:
            standard_update["is_active"] = body.is_active

        if standard_row:
            sb.table("pricing_config").update(standard_update).eq("id", standard_row["id"]).execute()
        else:
            standard_update.update({
                "id": str(uuid4()),
                "vehicle_type": vehicle_type,
                "time_band": "standard",
                "per_min_usd": 0,
                "bid_floor_usd": 0,
                "created_at": now,
            })
            sb.table("pricing_config").insert(standard_update).execute()

        # Upsert night row (if night_multiplier provided, compute night base_fare)
        if body.night_multiplier is not None or body.base_fare is not None:
            std_base = body.base_fare if body.base_fare is not None else (float(standard_row.get("base_fare_usd", 0)) if standard_row else 0)
            night_mult = body.night_multiplier if body.night_multiplier is not None else 1.0
            night_base = round(std_base * night_mult, 4)

            night_update = {
                "base_fare_usd": night_base,
                "updated_at": now,
            }
            if body.per_km is not None:
                night_update["per_km_usd"] = body.per_km
            if body.minimum_fare is not None:
                night_update["minimum_fare_usd"] = body.minimum_fare
            if body.is_active is not None:
                night_update["is_active"] = body.is_active

            if night_row:
                sb.table("pricing_config").update(night_update).eq("id", night_row["id"]).execute()
            else:
                night_update.update({
                    "id": str(uuid4()),
                    "vehicle_type": vehicle_type,
                    "time_band": "night",
                    "per_min_usd": 0,
                    "bid_floor_usd": 0,
                    "created_at": now,
                })
                sb.table("pricing_config").insert(night_update).execute()

        # Build response
        new_std = sb.table("pricing_config").select("*").eq("vehicle_type", vehicle_type).eq("time_band", "standard").maybe_single().execute()
        new_night = sb.table("pricing_config").select("*").eq("vehicle_type", vehicle_type).eq("time_band", "night").maybe_single().execute()

        std_data = new_std.data or {}
        night_data = new_night.data or {}

        result = VehiclePricingConfig(
            vehicle_type=vehicle_type,
            base_fare=float(std_data.get("base_fare_usd", 0)),
            per_km=float(std_data.get("per_km_usd", 0)),
            minimum_fare=float(std_data.get("minimum_fare_usd", 0) or 0),
            night_multiplier=round(
                float(night_data.get("base_fare_usd", 0)) / float(std_data.get("base_fare_usd", 1))
                if float(std_data.get("base_fare_usd", 1)) > 0 else 1.0, 4
            ),
            is_active=std_data.get("is_active", True),
        )

        # Audit log
        new_values = {
            "standard": {k: float(v) if isinstance(v, (int, float)) else v for k, v in std_data.items() if k in ("base_fare_usd", "per_km_usd", "per_min_usd", "minimum_fare_usd", "is_active")},
            "night": {k: float(v) if isinstance(v, (int, float)) else v for k, v in night_data.items() if k in ("base_fare_usd", "per_km_usd", "per_min_usd", "minimum_fare_usd", "is_active")},
        }
        _log_audit(sb, admin_id, "vehicle_pricing", f"Updated {vehicle_type} pricing", previous_values, new_values)

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update vehicle pricing: {str(e)}")


@router.put("/admin/pricing/global", response_model=GlobalPricingConfig)
def update_global_pricing(
    body: UpdateGlobalPricingBody,
    _user=Depends(require_admin),
):
    """
    Update global time-band pricing config (vat_rate, day_multiplier, evening_multiplier, commission_rate).
    Stored in app_config table.
    """
    sb = get_supabase()
    admin_id = _user.get("id", "unknown")

    try:
        # Fetch previous values
        config_result = sb.table("app_config").select("*").execute()
        config_map = {c["key"]: c["value"] for c in (config_result.data or [])}
        previous_values = {
            "vat_rate": config_map.get("vat_rate", "0.16"),
            "day_multiplier": config_map.get("day_multiplier", "1.00"),
            "evening_multiplier": config_map.get("evening_multiplier", "1.10"),
            "commission_rate": config_map.get("commission_rate", "0.05"),
        }

        updates = {}
        if body.vat_rate is not None:
            updates["vat_rate"] = str(body.vat_rate)
        if body.day_multiplier is not None:
            updates["day_multiplier"] = str(body.day_multiplier)
        if body.evening_multiplier is not None:
            updates["evening_multiplier"] = str(body.evening_multiplier)
        if body.commission_rate is not None:
            updates["commission_rate"] = str(body.commission_rate)

        for key, value in updates.items():
            existing = sb.table("app_config").select("id").eq("key", key).maybe_single().execute()
            if existing.data:
                sb.table("app_config").update({"value": value}).eq("key", key).execute()
            else:
                sb.table("app_config").insert({"key": key, "value": value}).execute()

        # Audit log
        _log_audit(sb, admin_id, "global_config", "Updated global pricing config", previous_values, updates)

        # Return updated config
        config_result = sb.table("app_config").select("*").execute()
        config_map = {c["key"]: c["value"] for c in (config_result.data or [])}

        return GlobalPricingConfig(
            vat_rate=float(config_map.get("vat_rate", 0.16)),
            day_multiplier=float(config_map.get("day_multiplier", 1.00)),
            evening_multiplier=float(config_map.get("evening_multiplier", 1.10)),
            commission_rate=float(config_map.get("commission_rate", 0.05)),
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update global pricing: {str(e)}")


@router.put("/admin/pricing/category/{category}", response_model=CategoryMultiplierItem)
def update_category_multiplier(
    category: str,
    body: UpdateCategoryMultiplierBody,
    _user=Depends(require_admin),
):
    """
    Update a category multiplier (standard / premium / lady_driver).
    Stored in app_config table.
    """
    if category not in ("standard", "premium", "lady_driver"):
        raise HTTPException(status_code=400, detail=f"Invalid category: {category}. Must be one of: standard, premium, lady_driver")

    sb = get_supabase()
    admin_id = _user.get("id", "unknown")

    try:
        # Fetch previous values
        multiplier_key = f"category_multiplier_{category}"
        active_key = f"category_{category}_active"

        config_result = sb.table("app_config").select("*").execute()
        config_map = {c["key"]: c["value"] for c in (config_result.data or [])}
        previous_values = {
            "multiplier": config_map.get(multiplier_key, "1.00"),
            "is_active": config_map.get(active_key, "true"),
        }

        updates = {}
        if body.multiplier is not None:
            updates[multiplier_key] = str(body.multiplier)
        if body.is_active is not None:
            updates[active_key] = str(body.is_active).lower()
        if body.description is not None:
            desc_key = f"category_{category}_description"
            updates[desc_key] = body.description

        for key, value in updates.items():
            existing = sb.table("app_config").select("id").eq("key", key).maybe_single().execute()
            if existing.data:
                sb.table("app_config").update({"value": value}).eq("key", key).execute()
            else:
                sb.table("app_config").insert({"key": key, "value": value}).execute()

        # Audit log
        _log_audit(sb, admin_id, "category_multiplier", f"Updated {category} category multiplier", previous_values, updates)

        # Return updated config
        config_result = sb.table("app_config").select("*").execute()
        config_map = {c["key"]: c["value"] for c in (config_result.data or [])}

        descriptions = {
            "standard": "Default for cars + all motorcycles",
            "premium": "Cars only — newer cars, AC required, vehicle year ≥ 2015",
            "lady_driver": "Cars only, female drivers",
        }

        return CategoryMultiplierItem(
            category=category,
            multiplier=float(config_map.get(multiplier_key, 1.00)),
            description=config_map.get(f"category_{category}_description", descriptions.get(category, "")),
            is_active=config_map.get(active_key, "true").lower() == "true",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update category multiplier: {str(e)}")


@router.post("/admin/pricing/simulate", response_model=FareSimulateResponse)
def simulate_fare(
    body: FareSimulateRequest,
    _user=Depends(require_admin),
):
    """
    Simulate a fare for arbitrary distance + time + vehicle + category.
    Returns the full pipeline breakdown.
    """
    sb = get_supabase()

    try:
        # 1. Get vehicle pricing
        std_result = sb.table("pricing_config").select("*").eq("vehicle_type", body.vehicle_type).eq("time_band", "standard").maybe_single().execute()
        night_result = sb.table("pricing_config").select("*").eq("vehicle_type", body.vehicle_type).eq("time_band", "night").maybe_single().execute()

        if not std_result.data:
            raise HTTPException(status_code=404, detail=f"No pricing config found for vehicle type: {body.vehicle_type}")

        std = std_result.data
        night = night_result.data or {}

        base_fare = float(std.get("base_fare_usd", 0))
        per_km = float(std.get("per_km_usd", 0))
        minimum_fare = float(std.get("minimum_fare_usd", 0) or 0)
        night_base = float(night.get("base_fare_usd", 0))
        night_multiplier = round(night_base / base_fare, 4) if base_fare > 0 else 1.0

        # 2. Get global config
        config_result = sb.table("app_config").select("*").execute()
        config_map = {c["key"]: c["value"] for c in (config_result.data or [])}

        vat_rate = float(config_map.get("vat_rate", 0.16))
        day_multiplier = float(config_map.get("day_multiplier", 1.00))
        evening_multiplier = float(config_map.get("evening_multiplier", 1.10))
        commission_rate = float(config_map.get("commission_rate", 0.05))

        # 3. Get category multiplier
        cat_key = f"category_multiplier_{body.category}"
        category_multiplier = float(config_map.get(cat_key, 1.00))

        # 4. Determine time multiplier
        time_multiplier = _get_time_multiplier(body.time_band, GlobalPricingConfig(
            vat_rate=vat_rate,
            day_multiplier=day_multiplier,
            evening_multiplier=evening_multiplier,
            commission_rate=commission_rate,
        ))

        # 5. Compute pipeline
        return _compute_fare_pipeline(
            base_fare=base_fare,
            per_km=per_km,
            distance_km=body.distance_km,
            minimum_fare=minimum_fare,
            time_multiplier=time_multiplier,
            night_multiplier=night_multiplier,
            time_band=body.time_band,
            category_multiplier=category_multiplier,
            vat_rate=vat_rate,
            commission_rate=commission_rate,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to simulate fare: {str(e)}")


@router.get("/admin/pricing/metrics", response_model=List[CategoryMetricsItem])
def get_pricing_metrics(
    days: int = Query(7, ge=1, le=90, description="Number of days to look back"),
    _user=Depends(require_admin),
):
    """
    Per-category metrics dashboard:
    - Ride volume by category
    - Average fare
    - Active drivers per category
    - Requests per category
    - Conversion rate
    """
    sb = get_supabase()

    try:
        categories = ["standard", "premium", "lady_driver"]
        metrics: List[CategoryMetricsItem] = []

        for category in categories:
            # Count completed rides in this category
            ride_result = sb.table("rides").select("id, accepted_price", count="exact").eq("category", category).eq("status", "completed").gte("created_at", f"now() - interval '{days} days'").execute()
            ride_count = ride_result.count or 0
            rides = ride_result.data or []
            avg_fare = round(sum(float(r.get("accepted_price", 0) or 0) for r in rides) / ride_count, 2) if ride_count > 0 else None

            # Count active drivers for this category
            if category == "standard":
                # Standard includes ALL approved drivers
                driver_result = sb.table("driver_profiles").select("id", count="exact").eq("verification_status", "approved").execute()
            else:
                driver_result = sb.table("driver_profiles").select("id", count="exact").eq("verification_status", "approved").eq("category", category).execute()
            active_drivers = driver_result.count or 0

            # Count total ride requests in this category
            request_result = sb.table("ride_requests").select("id", count="exact").eq("category", category).gte("created_at", f"now() - interval '{days} days'").execute()
            total_requests = request_result.count or 0

            # Conversion rate
            conversion_rate = round(ride_count / total_requests * 100, 1) if total_requests > 0 else None

            metrics.append(CategoryMetricsItem(
                category=category,
                ride_volume=ride_count,
                average_fare=avg_fare,
                active_drivers=active_drivers,
                total_requests=total_requests,
                completed_trips=ride_count,
                conversion_rate=conversion_rate,
            ))

        return metrics

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch pricing metrics: {str(e)}")


@router.get("/admin/pricing/audit-log", response_model=List[PricingAuditLogItem])
def get_pricing_audit_log(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    """Get the pricing audit log."""
    sb = get_supabase()

    try:
        result = sb.table("pricing_audit_log").select("*").order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        items = result.data or []

        # Enrich with admin names
        admin_ids = list(set(item.get("admin_id", "") for item in items if item.get("admin_id")))
        admin_map = {}
        if admin_ids:
            try:
                admin_result = sb.table("users").select("id, full_name").in_("id", admin_ids).execute()
                for u in (admin_result.data or []):
                    admin_map[u["id"]] = u.get("full_name", "")
            except Exception:
                pass

        return [
            PricingAuditLogItem(
                id=item["id"],
                admin_id=item.get("admin_id", ""),
                admin_name=admin_map.get(item.get("admin_id", ""), ""),
                change_type=item.get("change_type", ""),
                change_summary=item.get("change_summary", ""),
                previous_values=item.get("previous_values"),
                new_values=item.get("new_values"),
                created_at=item.get("created_at", ""),
            )
            for item in items
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch pricing audit log: {str(e)}")
