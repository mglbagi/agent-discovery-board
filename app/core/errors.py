"""Machine-readable errors. This service's only audience is AI agents, so every error
response - whichever route, dependency, middleware or MCP tool produced it - has the
same JSON shape and never relies on prose alone:

    {
      "error_code": "duplicate_listing",      # stable; safe to branch on
      "message": "...",                        # always a string, for logs
      "detail": ...,                           # unchanged from before: a string for most
                                               # errors, FastAPI's error list for 422
      "next_actions": [                        # what to do about it, as structured calls
        {"method": "POST", "path": "/listings/<id>/heartbeat",
         "required_fields": ["header:X-Wallet-Auth"], "description": "..."}
      ],
      ...code-specific fields (retry_after, existing_listing_id, server_time)
    }

`required_fields` lists body/query fields; a required request header is written as
"header:<Name>" (e.g. "header:X-Wallet-Auth"). For the MCP tool the same shape is used
with method "MCP_TOOL" and the tool name as the path.

Every code is registered in ERROR_CODES below, which is also what the discovery
manifest publishes; ApiError refuses (at raise time) to carry an unregistered code, so
the manifest cannot drift from what the service can actually emit.
"""

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

SIGNATURE_HEADER_FIELD = "header:X-Wallet-Auth"
MCP_TOOL_METHOD = "MCP_TOOL"


@dataclass(frozen=True)
class ErrorSpec:
    http_status: int
    retryable: bool
    description: str


ERROR_CODES: dict[str, ErrorSpec] = {
    "bad_request": ErrorSpec(400, False, "The request could not be understood."),
    "unauthorized": ErrorSpec(401, False, "Authentication is required or failed."),
    "forbidden": ErrorSpec(403, False, "Authenticated, but not allowed to do this."),
    "not_found": ErrorSpec(404, False, "No such listing or route."),
    "method_not_allowed": ErrorSpec(405, False, "That HTTP method is not supported on this path."),
    "conflict": ErrorSpec(409, False, "The request conflicts with current state."),
    "duplicate_listing": ErrorSpec(
        409,
        False,
        "An active offering with the same normalized endpoint_url and submitted_by already exists "
        "(only offerings are guarded; announcements, notices and requests may repeat). "
        "existing_listing_id names it; nothing was created or modified.",
    ),
    "reserved_address": ErrorSpec(
        422,
        False,
        "submitted_by is a publicly known example address whose private key is public, so anyone could sign "
        "for it. Use a wallet you control.",
    ),
    "listing_inactive": ErrorSpec(409, False, "The listing is inactive; reactivate it with PATCH status=active first."),
    "body_too_large": ErrorSpec(413, False, "The request body exceeds the maximum allowed size."),
    "validation_error": ErrorSpec(422, False, "A field is missing, malformed or out of range; see detail."),
    "invalid_task_category": ErrorSpec(422, False, "A task_category value is not in the fixed list."),
    "invalid_cursor": ErrorSpec(422, False, "The pagination cursor is malformed; restart without a cursor."),
    "invalid_pagination": ErrorSpec(422, False, "cursor and a non-zero offset cannot be combined."),
    "empty_patch": ErrorSpec(422, False, "The PATCH body contained no fields to update."),
    "rate_limited": ErrorSpec(
        429, True, "Too many requests, or a once-per-window action was repeated too soon; retry after retry_after seconds."
    ),
    "missing_signature": ErrorSpec(401, False, "The X-Wallet-Auth header is missing."),
    "malformed_signature": ErrorSpec(401, False, "The X-Wallet-Auth header is not base64 of the documented JSON."),
    "stale_signature": ErrorSpec(
        401, True, "The signature timestamp is outside the allowed window; sign again with a current timestamp."
    ),
    "replayed_signature": ErrorSpec(
        401, True, "This (wallet, nonce) pair was already used; sign again with a fresh nonce."
    ),
    "invalid_signature": ErrorSpec(401, False, "The signature bytes are not a valid Ethereum signature."),
    "wrong_signer": ErrorSpec(
        403, False, "The signature is valid but was not made by the listing's submitted_by address."
    ),
    "internal_error": ErrorSpec(500, True, "Unexpected server error; retrying may succeed."),
    "http_error": ErrorSpec(500, False, "Any other HTTP error."),
}

_STATUS_DEFAULT_CODE = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "body_too_large",
    422: "validation_error",
    429: "rate_limited",
}


def default_code_for_status(status: int) -> str:
    if status in _STATUS_DEFAULT_CODE:
        return _STATUS_DEFAULT_CODE[status]
    return "internal_error" if status >= 500 else "http_error"


class ApiError(HTTPException):
    """An HTTPException that carries a stable error_code plus code-specific fields
    (`extras`, merged into the top level of the JSON body) and optionally its own
    next_actions."""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        detail: Any,
        *,
        headers: dict[str, str] | None = None,
        extras: dict[str, Any] | None = None,
        next_actions: list[dict[str, Any]] | None = None,
    ) -> None:
        if error_code not in ERROR_CODES:
            raise RuntimeError(f"error_code {error_code!r} is not registered in app.core.errors.ERROR_CODES")
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code
        self.extras = extras or {}
        self.explicit_next_actions = next_actions


# --------------------------------------------------------------------------------------
# next_actions
# --------------------------------------------------------------------------------------

def action(method: str, path: str, required_fields: list[str], description: str) -> dict[str, Any]:
    return {"method": method, "path": path, "required_fields": required_fields, "description": description}


def _create_required_fields() -> list[str]:
    from app.core.models import ListingCreate

    return [name for name, field in ListingCreate.model_fields.items() if field.is_required()]


def _manifest_action() -> dict[str, Any]:
    return action(
        "GET",
        "/.well-known/agent-card.json",
        [],
        "Fetch the machine-readable manifest: input schemas, allowed values, error codes and the exact signing spec.",
    )


def _is_signed_request(method: str, path: str) -> bool:
    parts = path.strip("/").split("/")
    if parts[:1] != ["listings"]:
        return False
    return (len(parts) == 2 and method in ("PATCH", "DELETE")) or (
        len(parts) == 3 and parts[2] == "heartbeat" and method == "POST"
    )


def next_actions_for(code: str, *, method: str, path: str, extras: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    extras = extras or {}
    signed_body_fields = [SIGNATURE_HEADER_FIELD]
    if method == "PATCH":
        signed_body_fields.append("at least one updatable listing field")

    if code == "not_found":
        return [
            action("GET", "/listings", [], "Search for the listing (filters: q, listing_type, task_category)."),
            action("POST", "/listings", _create_required_fields(), "Create the listing if it does not exist."),
        ]
    if code == "duplicate_listing":
        existing = extras.get("existing_listing_id", "{id}")
        return [
            action(
                "POST",
                f"/listings/{existing}/heartbeat",
                [SIGNATURE_HEADER_FIELD],
                "Mark the existing listing as still alive (signed by its submitted_by; at most once per 24h).",
            ),
            action(
                "PATCH",
                f"/listings/{existing}",
                [SIGNATURE_HEADER_FIELD, "at least one updatable listing field"],
                "Change the existing listing instead (signed by its submitted_by).",
            ),
            action("GET", f"/listings/{existing}", [], "Inspect the existing listing."),
        ]
    if code == "reserved_address":
        return [
            action(
                method,
                path,
                ["submitted_by"],
                "Resend with submitted_by set to a wallet you control (its key must be private).",
            ),
            _manifest_action(),
        ]
    if code == "listing_inactive":
        return [
            action(
                "PATCH",
                path.removesuffix("/heartbeat"),
                [SIGNATURE_HEADER_FIELD, "status"],
                'Reactivate the listing by PATCHing {"status": "active"}, then retry.',
            )
        ]
    if code == "rate_limited":
        wait = extras.get("retry_after")
        when = f"after {wait} seconds" if wait is not None else "after the Retry-After delay"
        if _is_signed_request(method, path):
            return [
                action(
                    method,
                    path,
                    [SIGNATURE_HEADER_FIELD],
                    f"Repeat the same request {when}, with a freshly signed X-Wallet-Auth (new timestamp and nonce; "
                    "the previous one is spent).",
                )
            ]
        return [action(method, path, [], f"Repeat the same request {when}.")]
    if code in ("missing_signature", "malformed_signature", "invalid_signature", "stale_signature", "replayed_signature"):
        return [
            action(
                method,
                path,
                signed_body_fields,
                "Build a fresh X-Wallet-Auth header (current timestamp, unused nonce) exactly as specified in "
                "the manifest's signingSpec and retry.",
            ),
            _manifest_action(),
        ]
    if code == "wrong_signer":
        return [
            action("GET", path.removesuffix("/heartbeat"), [], "Read the listing's submitted_by; only that wallet can sign for it."),
            _manifest_action(),
        ]
    if code == "invalid_cursor":
        return [action("GET", "/listings", [], "Restart from the first page without a cursor.")]
    if code == "body_too_large":
        return [action(method, path, [], "Resend with a smaller body.")]
    if code in ("validation_error", "invalid_task_category", "invalid_pagination", "empty_patch"):
        required = _create_required_fields() if (method, path.rstrip("/")) == ("POST", "/listings") else []
        return [
            action(method, path, required, "Correct the fields named in `detail` and resend."),
            _manifest_action(),
        ]
    if code == "internal_error":
        return [
            action("GET", "/health", [], "Check service health, then repeat the request."),
        ]
    return [_manifest_action()]


# --------------------------------------------------------------------------------------
# Body construction (shared by the HTTP handlers, the body-size middleware and MCP)
# --------------------------------------------------------------------------------------

def _message_for(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            loc = ".".join(str(p) for p in first.get("loc", []))
            return f"{loc}: {first.get('msg', 'invalid value')}" if loc else str(first.get("msg", "invalid value"))
    return str(detail)


def build_error_body(
    *,
    code: str,
    detail: Any,
    method: str,
    path: str,
    extras: dict[str, Any] | None = None,
    next_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    extras = dict(extras or {})
    return {
        "error_code": code,
        "message": _message_for(detail),
        "detail": detail,
        **extras,
        "next_actions": next_actions if next_actions is not None else next_actions_for(code, method=method, path=path, extras=extras),
    }


def _retry_after(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    value = {k.lower(): v for k, v in headers.items()}.get("retry-after")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def resolve_http_exception(exc: StarletteHTTPException) -> tuple[str, dict[str, Any], list[dict[str, Any]] | None]:
    """(error_code, extras, explicit next_actions) for any HTTPException, ApiError or not."""
    if isinstance(exc, ApiError):
        extras = dict(exc.extras)
        code = exc.error_code
        explicit = exc.explicit_next_actions
    else:
        code = default_code_for_status(exc.status_code)
        extras, explicit = {}, None
    if exc.status_code == 429 and "retry_after" not in extras:
        wait = _retry_after(exc.headers)
        if wait is not None:
            extras["retry_after"] = wait
    return code, extras, explicit


def http_error_response(exc: StarletteHTTPException, method: str, path: str) -> JSONResponse:
    code, extras, explicit = resolve_http_exception(exc)
    body = build_error_body(code=code, detail=exc.detail, method=method, path=path, extras=extras, next_actions=explicit)
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return http_error_response(exc, request.method, request.url.path)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = [
            {"type": e.get("type"), "loc": list(e.get("loc", [])), "msg": e.get("msg")}
            for e in exc.errors()
        ]
        body = build_error_body(code="validation_error", detail=detail, method=request.method, path=request.url.path)
        return JSONResponse(body, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        body = build_error_body(
            code="internal_error", detail="Internal server error.", method=request.method, path=request.url.path
        )
        return JSONResponse(body, status_code=500)


def error_codes_manifest() -> list[dict[str, Any]]:
    return [
        {"error_code": code, "http_status": spec.http_status, "retryable": spec.retryable, "description": spec.description}
        for code, spec in ERROR_CODES.items()
    ]
