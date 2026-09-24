import os

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import build_error_body

# No user-supplied JSON Schema or regex ever gets executed by this service (that
# risk belongs to the verification service, not this one), so there's no ReDoS
# surface here — this cap exists purely as ordinary request-size hygiene.
MAX_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(64 * 1024)))


class MaxBodySizeMiddleware:
    """Rejects requests whose body exceeds MAX_BODY_BYTES, checking Content-Length
    upfront and also enforcing the cap while the body is actually read (in case
    Content-Length is missing or understates the true size)."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                too_big = int(content_length) > self.max_bytes
            except ValueError:
                too_big = False
            if too_big:
                body = build_error_body(
                    code="body_too_large",
                    detail="Request body too large",
                    method=scope.get("method", ""),
                    path=scope.get("path", ""),
                )
                await JSONResponse(body, status_code=413)(scope, receive, send)
                return

        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise ValueError("Request body exceeded the maximum allowed size")
            return message

        await self.app(scope, limited_receive, send)
