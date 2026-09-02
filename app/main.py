import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.core.config import settings
from app.services.admin.router import router as admin_router
from app.services.wallet.router import router as wallet_router
from app.services.customer_wallet.router import router as customer_wallet_router
from app.services.drivers.router import router as drivers_router
from app.services.rides.router import router as rides_router
from app.services.disputes.router import router as disputes_router
from app.services.analytics.router import router as analytics_router
from app.services.audit.router import router as audit_router
from app.services.config.router import router as config_router
from app.services.customers.router import router as customers_router
from app.services.notifications.router import router as notifications_router
from app.services.payments.router import router as payments_router
from app.services.pricing.router import router as pricing_router
from app.services.sos.router import router as sos_router
from app.services.support.router import router as support_router
from app.services.admin_mgmt.router import router as admin_mgmt_router
from app.services.users.router import router as users_router
from app.services.ws.router import router as ws_router
from app.services.admin_ui.router import router as admin_ui_router
from app.services.live_location.router import router as live_location_router
from app.core.scheduler import start_scheduler, stop_scheduler
from app.core.migrations import run_migrations

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Apply pending sql/*.sql migrations, then start the background scheduler,
    for the lifetime of the app.

    Migrations run first and are allowed to raise: an exception raised before
    `yield` in this lifespan context manager prevents uvicorn from ever entering
    the "serving" state -- the process exits instead. Intentional (see
    app/core/migrations.py): the app must never serve traffic against a schema
    it failed to bring up to date.

    Replaces the deprecated @app.on_event("startup"/"shutdown") hooks, which are
    scheduled for removal in a future FastAPI release.
    """
    if settings.AUTO_MIGRATE_ENABLED:
        run_migrations()
    else:
        logger.warning("[migrations] AUTO_MIGRATE_ENABLED=false; skipping migration runner")
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan,
)

# Enhance the auto-generated HTTPBearer security scheme with a description
from fastapi.openapi.utils import get_openapi

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    # FastAPI names the scheme "HTTPBearer" from the HTTPBearer dependency
    schemes.setdefault("HTTPBearer", {}).update({
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Paste your Supabase JWT access token (without 'Bearer ' prefix)",
    })
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi

_IS_PRODUCTION = settings.ENVIRONMENT.strip().lower() in {"production", "prod"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Localhost is convenient in development, but combined with allow_credentials it
    # lets anything served from a developer machine call the admin API, so it is not
    # enabled in production. Add real deploy URLs via ADMIN_FRONTEND_ORIGINS.
    allow_origin_regex=None if _IS_PRODUCTION else r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    # str(exc) on an unhandled error routinely carries table names, SQL fragments and
    # connection details. Log it server-side; return an opaque message in production.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    detail = "Internal server error" if _IS_PRODUCTION else str(exc)
    return JSONResponse(status_code=500, content={"detail": detail})


app.include_router(wallet_router, prefix=settings.API_V1_PREFIX)
app.include_router(customer_wallet_router, prefix=settings.API_V1_PREFIX)
app.include_router(drivers_router, prefix=settings.API_V1_PREFIX)
app.include_router(rides_router, prefix=settings.API_V1_PREFIX)
app.include_router(disputes_router, prefix=settings.API_V1_PREFIX)
app.include_router(analytics_router, prefix=settings.API_V1_PREFIX)
app.include_router(audit_router, prefix=settings.API_V1_PREFIX)
app.include_router(config_router, prefix=settings.API_V1_PREFIX)
app.include_router(customers_router, prefix=settings.API_V1_PREFIX)
app.include_router(notifications_router, prefix=settings.API_V1_PREFIX)
app.include_router(payments_router, prefix=settings.API_V1_PREFIX)
app.include_router(pricing_router, prefix=settings.API_V1_PREFIX)
app.include_router(sos_router, prefix=settings.API_V1_PREFIX)
app.include_router(support_router, prefix=settings.API_V1_PREFIX)
app.include_router(admin_mgmt_router, prefix=settings.API_V1_PREFIX)
app.include_router(users_router, prefix=settings.API_V1_PREFIX)
app.include_router(admin_router, prefix=settings.API_V1_PREFIX)
app.include_router(ws_router, prefix=settings.API_V1_PREFIX)
app.include_router(live_location_router, prefix=settings.API_V1_PREFIX)

# Admin UI pages — no /api/v1 prefix, served at /admin/...
app.include_router(admin_ui_router)


@app.get("/health")
def health():
    return {"status": "ok"}
