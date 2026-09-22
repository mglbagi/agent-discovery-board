import logging
from contextlib import asynccontextmanager

# .env is loaded as a side effect of importing the app package (see app/__init__.py)
# before this or any other app.* module executes.
from fastapi import FastAPI

from app.api.routes.discovery import router as discovery_router
from app.api.routes.health import router as health_router
from app.api.routes.listings import router as listings_router
from app.core.db import close_db, init_db
from app.core.limits import MaxBodySizeMiddleware
from app.core.request_logging import ErrorRequestLoggingMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the DB connection pool and create the schema once, here, rather than on
    # every request. Best-effort: if the database is briefly unreachable at boot the
    # service still starts, and the first request that needs the DB retries the same
    # initialization (see app/core/db.py's lazy _ensure_schema).
    try:
        init_db()
    except Exception:  # noqa: BLE001
        logging.getLogger("app.db").exception("Database init at startup failed; will retry on first use")
    yield
    close_db()


app = FastAPI(title="Agent Discovery Board", version="0.1.0", lifespan=lifespan)

app.include_router(health_router)
app.include_router(listings_router)
app.include_router(discovery_router)

# This service charges nothing for its own endpoints (no x402 payment middleware) -
# the only x402 client code here is outbound, in app/core/score_client.py, paying the
# separate verification service for trust-score badges.

# Added last so it wraps everything else (Starlette applies middleware
# outermost-last-added-first): oversized requests are rejected before any body
# parsing happens.
app.add_middleware(MaxBodySizeMiddleware)

# Logs method/path/client/User-Agent for any non-2xx/3xx response, so things like
# 404s and 429s are diagnosable directly from Render's logs.
app.add_middleware(ErrorRequestLoggingMiddleware)
