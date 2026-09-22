import logging
import sys

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("http.errors")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(_handler)
    logger.propagate = False


class ErrorRequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs method, path, client, and User-Agent for any non-2xx/3xx response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if response.status_code >= 400:
            client = request.client.host if request.client else "-"
            logger.warning(
                "[http] status=%s method=%s path=%s client=%s user_agent=%s",
                response.status_code,
                request.method,
                request.url.path,
                client,
                request.headers.get("user-agent", "-"),
            )
        return response
