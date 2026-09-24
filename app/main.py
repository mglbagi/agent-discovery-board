import asyncio
from contextlib import asynccontextmanager

# .env is loaded as a side effect of importing the app package (see app/__init__.py)
# before this or any other app.* module executes.
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.api.routes.discovery import router as discovery_router
from app.api.routes.health import router as health_router
from app.api.routes.listings import router as listings_router
from app.api.routes.site_meta import router as site_meta_router
from app.core.constants import SERVICE_NAME, SERVICE_VERSION
from app.core.db import close_db
from app.core.maintenance import startup_maintenance
from app.core.errors import ERROR_CODES, error_codes_manifest, install_error_handlers
from app.core.limits import MaxBodySizeMiddleware
from app.core.request_logging import ErrorRequestLoggingMiddleware
from app.mcp_server import MCP_PATH, McpPathNormalizer, create_server

APP_TITLE = SERVICE_NAME
APP_VERSION = SERVICE_VERSION

APP_DESCRIPTION = (
    "A directory of AI agent services, built for agents: structured JSON, stable codes, no prose-only responses. "
    "Free, no accounts. Editing, deactivating and heartbeating a listing are signed with the submitter's wallet "
    "(EIP-191 personal_sign); the exact signing spec is in /.well-known/agent-card.json under "
    "capabilities.extensions[].params.signingSpec.\n\n"
    "Every error response has the same shape - `error_code` (stable), `message`, `detail`, and `next_actions` "
    "(a list of `{method, path, required_fields, description}`) - and these error codes:\n\n"
    + "\n".join(f"- `{code}` ({spec.http_status}): {spec.description}" for code, spec in ERROR_CODES.items())
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the DB connection pool, create/migrate the schema, and purge expired test
    # listings, once, here. Best-effort: if the database is briefly unreachable at boot
    # the service still starts, and the first request that needs the DB retries the same
    # initialization (see app/core/db.py's lazy _ensure_schema).
    await asyncio.to_thread(startup_maintenance)
    # A mounted sub-app's own lifespan doesn't run, so the MCP session manager is
    # started here for the lifetime of the service instead.
    async with mcp_server.session_manager.run():
        yield
    close_db()


# Second, additive access path (see app/mcp_server.py): the same search/browse
# behavior as GET /listings, exposed as an MCP tool. The REST endpoints above are
# unchanged and remain the primary path.
mcp_server = create_server()

app = FastAPI(title=APP_TITLE, version=APP_VERSION, description=APP_DESCRIPTION, lifespan=lifespan)

install_error_handlers(app)

app.include_router(health_router)
app.include_router(listings_router)
app.include_router(discovery_router)
app.include_router(site_meta_router)
app.mount(MCP_PATH, mcp_server.streamable_http_app())
app.add_middleware(McpPathNormalizer)  # innermost: only rewrites exactly "/mcp" -> "/mcp/"


def _openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    schema["info"]["x-error-codes"] = error_codes_manifest()
    schema["info"]["x-signing-spec"] = "/.well-known/agent-card.json -> capabilities.extensions[].params.signingSpec"
    app.openapi_schema = schema
    return schema


app.openapi = _openapi  # type: ignore[method-assign]

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
