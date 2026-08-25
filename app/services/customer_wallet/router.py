from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone, timedelta
import logging

from app.core.dependencies import require_admin
from app.core.supabase import call_rpc, first_row, get_supabase, rpc_missing as _rpc_missing

router = APIRouter(tags=["customer-wallet"])
logger = logging.getLogger(__name__)

CUSTOMER_REFERRAL_DAILY_CAP = 10  # per Admin_APIs_Implementations.md: 10 referrals/referrer/UTC-day


# ── Wallet rules ─────────────────────────────────────────────────────────────

class CustomerWalletRuleItem(BaseModel):
    id: str
    is_active: bool
    referrer_bonus_cdf: float
    referred_bonus_cdf: float
    first_ride_bonus_cdf: float
    max_wallet_usage_percentage: Optional[float] = None
    expiration_days: Optional[int] = None
    created_at: Optional[str] = None


class CustomerWalletRulesResponse(BaseModel):
    rules: List[CustomerWalletRuleItem]


@router.get("/customer-wallet/admin/rules", response_model=CustomerWalletRulesResponse)
def list_customer_wallet_rules(_user=Depends(require_admin)):
    """Read-only view of the active (and historical) customer wallet reward rules.

    Reads the same customer_wallet_rules table the mobile-app backend writes to and
    pays reward amounts from â this admin backend does not maintain its own copy of
    these values.
    """
    try:
        sb = get_supabase()
        result = sb.table("customer_wallet_rules").select("*").order("created_at", desc=True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    rules = [CustomerWalletRuleItem(**{k: v for k, v in r.items() if k in CustomerWalletRuleItem.model_fields}) for r in (result.data or [])]
    return CustomerWalletRulesResponse(rules=rules)


# ── Wallet metrics ───────────────────────────────────────────────────────────

class CustomerWalletMetricsResponse(BaseModel):
    total_wallet_balance_cdf: float
    customers_with_balance: int
    total_credits_issued_cdf: float
    total_first_ride_bonus_cdf: float
    total_referral_credit_cdf: float
    total_change_credit_cdf: float
    total_promo_credit_cdf: float
    transaction_count: int
    first_ride_bonus_count: int
    referral_credit_count: int
    change_credit_count: int


# PostgREST has no server-side SUM()/aggregate without a custom Postgres RPC
# function, which would need its own SQL migration against the shared production
# schema. Until that RPC exists, these metrics are summed client-side from a
# capped row fetch. The cap is a safety net against unbounded growth, not a
# correctness guarantee â metrics will silently under-report once transaction
# volume exceeds it. Replace with a real DB-side aggregate RPC when that's
# authored; do not raise this cap as a substitute for the real fix.
_METRICS_ROW_CAP = 50_000


@router.get("/customer-wallet/admin/metrics", response_model=CustomerWalletMetricsResponse)
def get_customer_wallet_metrics(_user=Depends(require_admin)):
    """Aggregate customer wallet reward metrics.

    Prefers the get_customer_wallet_metrics Postgres RPC (true server-side SUM/COUNT,
    see sql/20260825_customer_wallet_metrics_rpc.sql) so totals are always correct
    regardless of table size. Falls back to a capped client-side summation (see
    _METRICS_ROW_CAP) only if that migration hasn't been applied yet â that fallback
    path can silently under-report once transaction volume exceeds the cap.
    """
    sb = get_supabase()
    admin_id = (_user or {}).get("id")

    if admin_id:
        try:
            result = first_row(call_rpc("get_customer_wallet_metrics", {"p_admin_id": admin_id}))
            if result:
                return CustomerWalletMetricsResponse(**result)
        except Exception as e:
            if not _rpc_missing(e):
                raise HTTPException(status_code=500, detail=str(e))
            logger.warning(
                "get_customer_wallet_metrics RPC not found â falling back to capped "
                "client-side aggregation. Apply sql/20260825_customer_wallet_metrics_rpc.sql."
            )

    try:
        balances = sb.table("users").select("id, wallet_balance_cdf").gt("wallet_balance_cdf", 0).limit(_METRICS_ROW_CAP).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    total_balance = sum((float(r.get("wallet_balance_cdf") or 0) for r in (balances.data or [])), 0.0)
    customers_with_balance = len(balances.data or [])

    try:
        txns = (
            sb.table("customer_wallet_transactions")
            .select("amount_cdf, type, source")
            .limit(_METRICS_ROW_CAP)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    rows = txns.data or []
    total_credits = 0.0
    first_ride_total = 0.0
    referral_total = 0.0
    change_total = 0.0
    promo_total = 0.0
    first_ride_count = 0
    referral_count = 0
    change_count = 0

    for r in rows:
        if r.get("type") != "credit":
            continue
        amount = float(r.get("amount_cdf") or 0)
        total_credits += amount
        source = r.get("source")
        if source == "first_ride":
            first_ride_total += amount
            first_ride_count += 1
        elif source == "referral":
            referral_total += amount
            referral_count += 1
        elif source == "change":
            change_total += amount
            change_count += 1
        elif source == "promo":
            promo_total += amount

    return CustomerWalletMetricsResponse(
        total_wallet_balance_cdf=total_balance,
        customers_with_balance=customers_with_balance,
        total_credits_issued_cdf=total_credits,
        total_first_ride_bonus_cdf=first_ride_total,
        total_referral_credit_cdf=referral_total,
        total_change_credit_cdf=change_total,
        total_promo_credit_cdf=promo_total,
        transaction_count=len(rows),
        first_ride_bonus_count=first_ride_count,
        referral_credit_count=referral_count,
        change_credit_count=change_count,
    )


# ── Wallet transactions (ledger) ─────────────────────────────────────────────

class CustomerWalletTransactionItem(BaseModel):
    id: str
    user_id: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    type: str
    amount_cdf: float
    balance_after_cdf: Optional[float] = None
    source: Optional[str] = None
    ride_id: Optional[str] = None
    reference_id: Optional[str] = None
    created_at: str


class CustomerWalletTransactionListResponse(BaseModel):
    transactions: List[CustomerWalletTransactionItem]
    total: int


@router.get("/customer-wallet/admin/transactions", response_model=CustomerWalletTransactionListResponse)
def list_customer_wallet_transactions(
    user_id: Optional[str] = Query(None),
    source: Optional[str] = Query(None, description="first_ride | referral | change | promo | ride_discount"),
    type: Optional[str] = Query(None, description="credit | debit"),
    ride_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    """Read-only audit list of customer wallet transactions.

    Distinguishes reward source (first_ride / referral / change / promo) so admins
    can tell a welcome bonus apart from a referral payout or driver-issued change
    credit without cross-referencing another screen.
    """
    try:
        sb = get_supabase()
        query = sb.table("customer_wallet_transactions").select(
            "*, users(full_name, phone_number)", count="exact"
        )
        if user_id:
            query = query.eq("user_id", user_id)
        if source:
            query = query.eq("source", source)
        if type:
            query = query.eq("type", type)
        if ride_id:
            query = query.eq("ride_id", ride_id)
        result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    items = []
    for r in result.data or []:
        user_info = r.pop("users", None) or {}
        items.append(CustomerWalletTransactionItem(
            **{k: v for k, v in r.items() if k in CustomerWalletTransactionItem.model_fields},
            customer_name=user_info.get("full_name"),
            customer_phone=user_info.get("phone_number"),
        ))

    return CustomerWalletTransactionListResponse(transactions=items, total=result.count or len(items))


# ── Driver-issued change credit (subset of the ledger above, source=change) ──

class ChangeCreditItem(BaseModel):
    id: str
    ride_id: Optional[str] = None
    reference_id: Optional[str] = None
    customer_id: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    amount_cdf: float
    balance_after_cdf: Optional[float] = None
    created_at: str


class ChangeCreditListResponse(BaseModel):
    transactions: List[ChangeCreditItem]
    total: int
    max_allowed_cdf: float = 5000.0


@router.get("/customer-wallet/admin/change-credits", response_model=ChangeCreditListResponse)
def list_change_credits(
    ride_id: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    """Driver-issued customer change credit, for reconciliation.

    Filters the same customer_wallet_transactions ledger to source='change' and
    resolves the ride's driver for each row so Finance/Operations can reconcile
    "cash due -> change requested -> wallet credit" without a second query per row.
    The 5,000 CDF per-ride cap is the mobile-app backend's own business rule
    (MAX_DRIVER_CHANGE_CREDIT_CDF) â this endpoint reports it, it does not enforce it.
    """
    try:
        sb = get_supabase()
        query = sb.table("customer_wallet_transactions").select(
            "*, users(full_name, phone_number)", count="exact"
        ).eq("source", "change")
        if ride_id:
            query = query.eq("ride_id", ride_id)
        if customer_id:
            query = query.eq("user_id", customer_id)
        result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    rows = result.data or []
    ride_ids = [r["ride_id"] for r in rows if r.get("ride_id")]
    driver_by_ride: dict = {}
    if ride_ids:
        try:
            rides_result = (
                sb.table("rides")
                .select("id, driver_id, driver_profiles(user_id, users(full_name))")
                .in_("id", ride_ids)
                .execute()
            )
            for row in rides_result.data or []:
                dp = row.get("driver_profiles") or {}
                driver_users = (dp.get("users") or {}) if isinstance(dp, dict) else {}
                driver_by_ride[row["id"]] = {
                    "driver_id": row.get("driver_id"),
                    "driver_name": driver_users.get("full_name"),
                }
        except Exception:
            driver_by_ride = {}

    items = []
    for r in rows:
        user_info = r.pop("users", None) or {}
        ride_driver = driver_by_ride.get(r.get("ride_id"), {})
        items.append(ChangeCreditItem(
            id=r["id"],
            ride_id=r.get("ride_id"),
            reference_id=r.get("reference_id"),
            customer_id=r["user_id"],
            customer_name=user_info.get("full_name"),
            customer_phone=user_info.get("phone_number"),
            driver_id=ride_driver.get("driver_id"),
            driver_name=ride_driver.get("driver_name"),
            amount_cdf=r.get("amount_cdf") or 0,
            balance_after_cdf=r.get("balance_after_cdf"),
            created_at=r["created_at"],
        ))

    return ChangeCreditListResponse(transactions=items, total=result.count or len(items))


# ── Customer referrals ───────────────────────────────────────────────────────

class ReferralItem(BaseModel):
    id: str
    referrer_user_id: str
    referrer_name: Optional[str] = None
    referrer_phone: Optional[str] = None
    referred_user_id: Optional[str] = None
    referred_name: Optional[str] = None
    referred_phone: Optional[str] = None
    status: Optional[str] = None
    review_status: Optional[str] = None
    review_reason: Optional[str] = None
    reward_given: Optional[bool] = None
    referrer_reward_cdf: Optional[float] = None
    referred_reward_cdf: Optional[float] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class ReferralListResponse(BaseModel):
    referrals: List[ReferralItem]
    total: int


@router.get("/customer-wallet/admin/referrals", response_model=ReferralListResponse)
def list_customer_referrals(
    status: Optional[str] = Query(None, description="pending | completed"),
    referrer_user_id: Optional[str] = Query(None),
    referred_user_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    """Customer referral list with reward state, for admin oversight of the referral
    program. Read-only â does not modify reward-issuance state."""
    try:
        sb = get_supabase()
        query = sb.table("referrals").select(
            "*, referrer:referrer_user_id(full_name, phone_number), "
            "referred:referred_user_id(full_name, phone_number)",
            count="exact",
        )
        if status:
            query = query.eq("status", status)
        if referrer_user_id:
            query = query.eq("referrer_user_id", referrer_user_id)
        if referred_user_id:
            query = query.eq("referred_user_id", referred_user_id)
        result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    items = []
    for r in result.data or []:
        referrer = r.pop("referrer", None) or {}
        referred = r.pop("referred", None) or {}
        items.append(ReferralItem(
            **{k: v for k, v in r.items() if k in ReferralItem.model_fields},
            referrer_name=referrer.get("full_name"),
            referrer_phone=referrer.get("phone_number"),
            referred_name=referred.get("full_name"),
            referred_phone=referred.get("phone_number"),
        ))

    return ReferralListResponse(referrals=items, total=result.count or len(items))


class ReferralFraudSignalsResponse(BaseModel):
    needs_review: List[ReferralItem]
    daily_cap_exceeded: List[dict]
    duplicate_phone_hash_referrals: List[dict]


@router.get("/customer-wallet/admin/referrals/fraud-signals", response_model=ReferralFraudSignalsResponse)
def get_customer_referral_fraud_signals(_user=Depends(require_admin)):
    """Fraud-review views over the existing referral fraud flags.

    Does not implement new fraud detection â surfaces what the mobile-app backend
    already flagged (status='flagged', review_status='needs_review') plus two
    belt-and-suspenders audit checks (daily referral cap, rewarded-phone duplicates)
    against the same unique-index-protected data.
    """
    sb = get_supabase()

    try:
        flagged = (
            sb.table("referrals")
            .select(
                "*, referrer:referrer_user_id(full_name, phone_number), "
                "referred:referred_user_id(full_name, phone_number)"
            )
            .eq("review_status", "needs_review")
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    needs_review = []
    for r in flagged.data or []:
        referrer = r.pop("referrer", None) or {}
        referred = r.pop("referred", None) or {}
        needs_review.append(ReferralItem(
            **{k: v for k, v in r.items() if k in ReferralItem.model_fields},
            referrer_name=referrer.get("full_name"),
            referrer_phone=referrer.get("phone_number"),
            referred_name=referred.get("full_name"),
            referred_phone=referred.get("phone_number"),
        ))

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    daily_cap_exceeded: List[dict] = []
    try:
        recent = (
            sb.table("referrals")
            .select("referrer_user_id")
            .gte("created_at", since)
            .limit(_METRICS_ROW_CAP)
            .execute()
        )
        counts: dict = {}
        for row in recent.data or []:
            rid = row.get("referrer_user_id")
            if rid:
                counts[rid] = counts.get(rid, 0) + 1
        daily_cap_exceeded = [
            {"referrer_user_id": rid, "referrals_last_24h": count}
            for rid, count in counts.items()
            if count > CUSTOMER_REFERRAL_DAILY_CAP
        ]
    except Exception:
        daily_cap_exceeded = []

    duplicate_phone_hash_referrals: List[dict] = []
    try:
        # referrals.referred_user_id is UNIQUE (one referral row per referred user) and
        # users.phone_number is UNIQUE, so grouping by the *current* referred user's phone
        # can never find a duplicate. The real signal is referred_phone_number_hash â
        # captured at referral-creation time, not FK-constrained â which can legitimately
        # repeat if the same phone number attempted the referral flow more than once
        # (e.g. delete + re-signup, or a blocked/retried attempt).
        hashed = (
            sb.table("referrals")
            .select("id, referred_phone_number_hash")
            .not_.is_("referred_phone_number_hash", "null")
            .limit(_METRICS_ROW_CAP)
            .execute()
        )
        hash_to_ids: dict = {}
        for row in hashed.data or []:
            h = row.get("referred_phone_number_hash")
            if h:
                hash_to_ids.setdefault(h, []).append(row["id"])
        duplicate_phone_hash_referrals = [
            {"referred_phone_number_hash": h, "referral_ids": ids}
            for h, ids in hash_to_ids.items()
            if len(ids) > 1
        ]
    except Exception:
        duplicate_phone_hash_referrals = []

    return ReferralFraudSignalsResponse(
        needs_review=needs_review,
        daily_cap_exceeded=daily_cap_exceeded,
        duplicate_phone_hash_referrals=duplicate_phone_hash_referrals,
    )


# ── Driver referrals ─────────────────────────────────────────────────────────

class DriverReferralItem(BaseModel):
    id: str
    referrer_driver_id: str
    referrer_name: Optional[str] = None
    referrer_phone: Optional[str] = None
    referred_driver_id: Optional[str] = None
    referred_name: Optional[str] = None
    referred_phone: Optional[str] = None
    status: Optional[str] = None
    review_status: Optional[str] = None
    review_reason: Optional[str] = None
    completed_rides: Optional[int] = None
    required_rides: Optional[int] = None
    reward_given: Optional[bool] = None
    referrer_reward_cdf: Optional[float] = None
    referred_reward_cdf: Optional[float] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    reward_paid_at: Optional[str] = None


class DriverReferralListResponse(BaseModel):
    referrals: List[DriverReferralItem]
    total: int


@router.get("/driver-referrals/admin/list", response_model=DriverReferralListResponse)
def list_driver_referrals(
    status: Optional[str] = Query(None, description="pending | completed"),
    referrer_driver_id: Optional[str] = Query(None),
    referred_driver_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    """Driver referral list with reward + ride-progress state.

    Rewards (10,000 CDF referrer / 5,000 CDF referred driver, paid at the referred
    driver's 10th completed ride) are a separate, higher bar than the customer
    referral program â kept on its own endpoint so the two are never conflated.

    Joins via referrer_user_id/referred_user_id (both NOT NULL) rather than
    referrer_driver_id/referred_driver_id (referred_driver_id is nullable â a
    referral row can exist before the referred person's driver profile is created)
    so name/phone are never silently dropped for a not-yet-onboarded referred driver.
    """
    try:
        sb = get_supabase()
        query = sb.table("driver_referrals").select(
            "*, referrer:referrer_user_id(full_name, phone_number), "
            "referred:referred_user_id(full_name, phone_number)",
            count="exact",
        )
        if status:
            query = query.eq("status", status)
        if referrer_driver_id:
            query = query.eq("referrer_driver_id", referrer_driver_id)
        if referred_driver_id:
            query = query.eq("referred_driver_id", referred_driver_id)
        result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    items = []
    for r in result.data or []:
        referrer = r.pop("referrer", None) or {}
        referred = r.pop("referred", None) or {}
        items.append(DriverReferralItem(
            **{k: v for k, v in r.items() if k in DriverReferralItem.model_fields},
            referrer_name=referrer.get("full_name"),
            referrer_phone=referrer.get("phone_number"),
            referred_name=referred.get("full_name"),
            referred_phone=referred.get("phone_number"),
        ))

    return DriverReferralListResponse(referrals=items, total=result.count or len(items))


class DriverReferralFraudSignalsResponse(BaseModel):
    needs_review: List[DriverReferralItem]


@router.get("/driver-referrals/admin/fraud-signals", response_model=DriverReferralFraudSignalsResponse)
def get_driver_referral_fraud_signals(_user=Depends(require_admin)):
    """Driver-referral fraud review queue.

    The driver-referral fraud check is narrower than the customer side (same-phone-
    already-rewarded only â no device-fingerprint or daily-rate-cap equivalent on
    this path today), so this surfaces exactly that one flag rather than inventing
    additional checks this admin backend has no authority to define.
    """
    try:
        sb = get_supabase()
        result = (
            sb.table("driver_referrals")
            .select(
                "*, referrer:referrer_user_id(full_name, phone_number), "
                "referred:referred_user_id(full_name, phone_number)"
            )
            .eq("review_status", "needs_review")
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    items = []
    for r in result.data or []:
        referrer = r.pop("referrer", None) or {}
        referred = r.pop("referred", None) or {}
        items.append(DriverReferralItem(
            **{k: v for k, v in r.items() if k in DriverReferralItem.model_fields},
            referrer_name=referrer.get("full_name"),
            referrer_phone=referrer.get("phone_number"),
            referred_name=referred.get("full_name"),
            referred_phone=referred.get("phone_number"),
        ))

    return DriverReferralFraudSignalsResponse(needs_review=items)


# ── Welcome bonuses (customer + driver, kept distinct from referral stats) ──

class WelcomeBonusMetricsResponse(BaseModel):
    customer_welcome_bonus_count: int
    customer_welcome_bonus_total_cdf: float
    driver_welcome_bonus_count: int
    driver_welcome_bonus_total_cdf: float


@router.get("/customer-wallet/admin/welcome-bonus-metrics", response_model=WelcomeBonusMetricsResponse)
def get_welcome_bonus_metrics(_user=Depends(require_admin)):
    """Welcome-bonus spend, reported separately from referral totals.

    Customer welcome bonus (source='first_ride' in customer_wallet_transactions) and
    driver welcome bonus (reference_type='driver_welcome_bonus' in wallet_transactions)
    are mutually exclusive with referral rewards by construction upstream â this just
    sums each ledger's own rows, it doesn't re-derive the eligibility rule.

    Prefers the get_welcome_bonus_metrics Postgres RPC (true server-side SUM/COUNT,
    see sql/20260825_customer_wallet_metrics_rpc.sql); falls back to a capped
    client-side sum if that migration hasn't been applied yet.
    """
    sb = get_supabase()
    admin_id = (_user or {}).get("id")

    if admin_id:
        try:
            result = first_row(call_rpc("get_welcome_bonus_metrics", {"p_admin_id": admin_id}))
            if result:
                return WelcomeBonusMetricsResponse(**result)
        except Exception as e:
            if not _rpc_missing(e):
                raise HTTPException(status_code=500, detail=str(e))
            logger.warning(
                "get_welcome_bonus_metrics RPC not found â falling back to capped "
                "client-side aggregation. Apply sql/20260825_customer_wallet_metrics_rpc.sql."
            )

    try:
        customer_rows = (
            sb.table("customer_wallet_transactions")
            .select("amount_cdf")
            .eq("source", "first_ride")
            .eq("type", "credit")
            .limit(_METRICS_ROW_CAP)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    customer_total = sum((float(r.get("amount_cdf") or 0) for r in (customer_rows.data or [])), 0.0)

    try:
        driver_rows = (
            sb.table("wallet_transactions")
            .select("amount")
            .eq("reference_type", "driver_welcome_bonus")
            .limit(_METRICS_ROW_CAP)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    driver_total = sum((float(r.get("amount") or 0) for r in (driver_rows.data or [])), 0.0)

    return WelcomeBonusMetricsResponse(
        customer_welcome_bonus_count=len(customer_rows.data or []),
        customer_welcome_bonus_total_cdf=customer_total,
        driver_welcome_bonus_count=len(driver_rows.data or []),
        driver_welcome_bonus_total_cdf=driver_total,
    )
