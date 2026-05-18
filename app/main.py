from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.core.config import settings
from app.services.admin.router import router as admin_router
from app.services.wallet.router import router as wallet_router
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

app = FastAPI(title=settings.APP_NAME, version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


app.include_router(wallet_router, prefix=settings.API_V1_PREFIX)
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
app.include_router(admin_router, prefix=settings.API_V1_PREFIX)


@app.get("/health")
def health():
    return {"status": "ok"}
