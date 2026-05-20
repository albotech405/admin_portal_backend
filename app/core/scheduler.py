"""
Background job scheduler using APScheduler.

Jobs:
  1. daily_earnings_summary   — runs every evening (19:00 UTC)
  2. inactive_driver_reengagement — runs daily at 08:00 UTC
  3. rating_milestone_check   — runs daily at 09:00 UTC
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.supabase import get_supabase
from app.services.notifications.router import send_push_to_users

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None


# ── Job implementations ────────────────────────────────────────────────────


def job_daily_earnings_summary() -> None:
    """Send each active driver a summary of their earnings for the day."""
    logger.info("[scheduler] Running daily_earnings_summary")
    try:
        sb = get_supabase()
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()

        # Fetch all completed rides today
        rides_result = (
            sb.table("rides")
            .select("driver_id, price")
            .eq("status", "completed")
            .gte("completed_at", today_start)
            .execute()
        )
        rides = rides_result.data or []

        # Aggregate per driver_profile_id
        earnings: dict = {}
        for r in rides:
            did = r.get("driver_id")
            if not did:
                continue
            if did not in earnings:
                earnings[did] = {"total": 0.0, "count": 0}
            earnings[did]["total"] += float(r.get("price") or 0)
            earnings[did]["count"] += 1

        if not earnings:
            return

        # Resolve driver_profile_id → user_id
        dp_result = (
            sb.table("driver_profiles")
            .select("id, user_id")
            .in_("id", list(earnings.keys()))
            .execute()
        )
        for dp in dp_result.data or []:
            user_id = dp.get("user_id")
            if not user_id:
                continue
            data = earnings[dp["id"]]
            send_push_to_users(
                [user_id],
                "Daily Earnings Summary",
                f"Today you earned {data['total']:.0f} CDF across {data['count']} ride(s). Keep it up!",
                notification_type="earnings",
                persist=True,
            )
    except Exception as exc:
        logger.error("[scheduler] daily_earnings_summary failed: %s", exc)


def job_inactive_driver_reengagement() -> None:
    """Notify drivers who have been offline for 7+ days."""
    logger.info("[scheduler] Running inactive_driver_reengagement")
    try:
        sb = get_supabase()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        result = (
            sb.table("driver_profiles")
            .select("user_id, updated_at")
            .eq("is_online", False)
            .eq("verification_status", "approved")
            .eq("is_suspended", False)
            .lte("updated_at", cutoff)
            .execute()
        )
        user_ids = [r["user_id"] for r in (result.data or []) if r.get("user_id")]
        if not user_ids:
            return

        # Rate-limit: skip drivers who already received this within 7 days
        notif_cutoff = cutoff
        notif_result = (
            sb.table("notifications")
            .select("user_id")
            .in_("user_id", user_ids)
            .eq("notification_type", "reengagement")
            .gte("created_at", notif_cutoff)
            .execute()
        )
        already_notified = {r["user_id"] for r in (notif_result.data or [])}
        targets = [uid for uid in user_ids if uid not in already_notified]

        for uid in targets:
            send_push_to_users(
                [uid],
                "We miss you!",
                "Come back online to start earning again. Riders are waiting!",
                notification_type="reengagement",
                persist=True,
            )
    except Exception as exc:
        logger.error("[scheduler] inactive_driver_reengagement failed: %s", exc)


def job_rating_milestone_check() -> None:
    """Notify drivers who have hit 4.5+ rating with 50+ trips."""
    logger.info("[scheduler] Running rating_milestone_check")
    try:
        sb = get_supabase()

        result = (
            sb.table("driver_profiles")
            .select("id, user_id, rating, total_rides")
            .gte("rating", 4.5)
            .gte("total_rides", 50)
            .execute()
        )

        if not result.data:
            return

        # Only notify once per driver (check for prior milestone notification)
        user_ids = [r["user_id"] for r in result.data if r.get("user_id")]
        if not user_ids:
            return

        notif_result = (
            sb.table("notifications")
            .select("user_id")
            .in_("user_id", user_ids)
            .eq("notification_type", "rating_milestone")
            .execute()
        )
        already_notified = {r["user_id"] for r in (notif_result.data or [])}

        targets = [r["user_id"] for r in result.data if r.get("user_id") and r["user_id"] not in already_notified]
        for uid in targets:
            send_push_to_users(
                [uid],
                "Congratulations!",
                "You've reached Gold Driver status with a 4.5+ rating and 50+ trips. Keep up the great work!",
                notification_type="rating_milestone",
                persist=True,
            )
    except Exception as exc:
        logger.error("[scheduler] rating_milestone_check failed: %s", exc)


# ── Scheduler lifecycle ────────────────────────────────────────────────────


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")

    _scheduler.add_job(
        job_daily_earnings_summary,
        CronTrigger(hour=19, minute=0),
        id="daily_earnings_summary",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        job_inactive_driver_reengagement,
        CronTrigger(hour=8, minute=0),
        id="inactive_driver_reengagement",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        job_rating_milestone_check,
        CronTrigger(hour=9, minute=0),
        id="rating_milestone_check",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()
    logger.info("[scheduler] APScheduler started with %d jobs", len(_scheduler.get_jobs()))


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] APScheduler stopped")
