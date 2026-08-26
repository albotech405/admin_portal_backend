"""
Auto-migration runner: applies sql/*.sql files against DATABASE_URL on startup.

Design:
  - Every file in sql/ is tracked in a schema_migrations ledger table
    (filename PRIMARY KEY, applied_at). A file is applied at most once, ever.
  - The 21 files that predate this feature were already manually applied via the
    Supabase SQL editor across prior sessions -- they are NOT re-run. On first
    boot with an empty ledger, BOOTSTRAP_APPLIED_FILENAMES is seeded into the
    ledger as already-applied, without executing their SQL. Only genuinely new
    files (added after this feature shipped) are ever executed by this runner.
  - Each file runs in its own transaction; the ledger INSERT for that file happens
    in the SAME transaction as the file's own SQL, then COMMIT. This makes "ran
    the SQL" and "recorded it" atomic -- a crash between the two is impossible,
    so a file can never be silently skipped or silently re-run on the next boot.
  - A Postgres session-scoped advisory lock (pg_advisory_lock, held for the whole
    connection, released automatically on connection close even on crash)
    serializes concurrent instances booting at the same time. Session-scoped
    (not pg_advisory_xact_lock) because the lock must span ALL pending files in
    one boot, but each file commits its own transaction independently -- no
    single transaction spans the whole run for a transaction-scoped lock to
    attach to.

Failure handling: any exception here propagates out of run_migrations() and out
of lifespan() before `yield`, which stops FastAPI/uvicorn from starting -- the
app must never serve traffic against a schema it failed to bring up to date.

Requires DATABASE_URL to be a DIRECT Postgres connection string, not a
transaction-mode pooler (pgbouncer transaction pooling silently breaks
session-scoped advisory locks and multi-statement sessions). Confirmed with the
project owner (2026-08-26) that DATABASE_URL is a direct connection.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg2

from app.core.config import settings

logger = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent.parent.parent / "sql"

# Arbitrary fixed int64 key for pg_advisory_lock, scoped to this app's migration
# runner. Stable and documented so it's obviously not colliding with any other
# subsystem's advisory lock usage (none currently exists in this codebase).
MIGRATION_LOCK_KEY = 771_820_260

# Files that existed in sql/ before this auto-migration feature shipped and were
# already manually applied via the Supabase SQL editor across prior sessions.
# Seeded into the ledger as already-applied on first boot (empty ledger) -- their
# SQL is never executed by this runner. Do not add to this list after initial
# deploy of this feature; new files just get picked up and run normally.
BOOTSTRAP_APPLIED_FILENAMES: frozenset[str] = frozenset({
    "20260507_admin_control_panel_rpc.sql",
    "20260508_pricing_audit_log.sql",
    "20260520_device_tokens_and_notifications.sql",
    "20260524_wallet_transactions_id_default.sql",
    "20260526_admin_sessions_refresh_token.sql",
    "20260526_live_location_tracking.sql",
    "20260609_sos_alert_notification_metadata.sql",
    "20260609_sos_resolution_metadata.sql",
    "20260609_sos_route_history.sql",
    "20260702_admin_log_refresh.sql",
    "20260702_approve_driver_rpc_refresh.sql",
    "20260702_approve_wallet_topup_rpc_refresh.sql",
    "20260702_assert_admin_refresh.sql",
    "20260702_notification_helpers_refresh.sql",
    "20260702_notifications_metadata_refresh.sql",
    "20260813_app_config_and_exchange_rates.sql",
    "20260825_customer_wallet_metrics_rpc.sql",
    "20260825_heatmap_rpc.sql",
    "20260825_notification_stats_rpc.sql",
    "20260826_heatmap_v2_indexes.sql",
    "20260826_heatmap_v2_viewport_zoom.sql",
})

_CREATE_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


# ── Pure logic (unit-testable, no I/O) ──────────────────────────────────────

def _discover_sql_files(sql_dir: Path) -> list[str]:
    """Sorted filenames of *.sql files directly in sql_dir. Lexicographic sort
    is chronological given this repo's YYYYMMDD_*.sql naming convention."""
    if not sql_dir.is_dir():
        return []
    return sorted(p.name for p in sql_dir.glob("*.sql") if p.is_file())


def _pending_migrations(all_files: list[str], applied_files: set[str]) -> list[str]:
    """Filenames not yet in the ledger, in the same sorted order as all_files."""
    return [f for f in all_files if f not in applied_files]


def _files_to_seed(all_files: list[str], ledger_is_empty: bool) -> list[str]:
    """Which filenames to bootstrap-seed as already-applied. Only on a genuinely
    empty ledger (first boot of this feature); only files both on disk and in
    the hardcoded bootstrap list -- guards against seeding a filename that was
    renamed/deleted since the list was frozen, and against ever re-seeding after
    the first successful boot (a non-empty ledger means bootstrapping already
    happened)."""
    if not ledger_is_empty:
        return []
    return [f for f in all_files if f in BOOTSTRAP_APPLIED_FILENAMES]


# ── I/O (requires a live psycopg2 connection; not unit-tested in this repo) ─

def run_migrations() -> None:
    """Entry point called from app.main's lifespan, before start_scheduler().
    Raises on any failure -- callers must let it propagate so FastAPI refuses
    to start rather than serving against a stale/broken schema."""
    all_files = _discover_sql_files(SQL_DIR)
    if not all_files:
        logger.info("[migrations] No files found in %s; nothing to do", SQL_DIR)
        return

    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        conn.autocommit = True  # lock + ledger bootstrap run outside any migration's own transaction
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s);", (MIGRATION_LOCK_KEY,))
        try:
            _ensure_ledger_table(conn)
            applied = _load_applied_filenames(conn)

            seed = _files_to_seed(all_files, ledger_is_empty=(len(applied) == 0))
            if seed:
                logger.info("[migrations] Bootstrapping ledger with %d pre-existing file(s)", len(seed))
                _seed_bootstrap(conn, seed)
                applied |= set(seed)

            pending = _pending_migrations(all_files, applied)
            if not pending:
                logger.info("[migrations] No pending migrations (%d already applied)", len(applied))
                return

            logger.info("[migrations] Applying %d pending migration(s): %s", len(pending), pending)
            for filename in pending:
                _apply_one(conn, filename)
                logger.info("[migrations] Applied %s", filename)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s);", (MIGRATION_LOCK_KEY,))
    finally:
        conn.close()


def _ensure_ledger_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_CREATE_LEDGER_SQL)


def _load_applied_filenames(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM public.schema_migrations;")
        return {row[0] for row in cur.fetchall()}


def _seed_bootstrap(conn, filenames: list[str]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO public.schema_migrations (filename) VALUES (%s) ON CONFLICT (filename) DO NOTHING;",
            [(f,) for f in filenames],
        )


def _apply_one(conn, filename: str) -> None:
    """Run one sql/*.sql file and record it in the ledger, atomically.

    conn.autocommit is toggled off for the duration of this function so the
    file's SQL and the ledger INSERT commit or roll back together -- the
    guarantee that a crash mid-file can never leave the ledger and the actual
    schema state disagreeing with each other.

    The whole file is sent to execute() as one opaque string, not split on ';'
    -- several existing files use $$-quoted PL/pgSQL function bodies containing
    semicolons, which naive splitting would corrupt.
    """
    sql_text = (SQL_DIR / filename).read_text(encoding="utf-8")
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)
            cur.execute(
                "INSERT INTO public.schema_migrations (filename) VALUES (%s);",
                (filename,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True
