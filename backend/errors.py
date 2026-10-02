"""Consistent API error envelope + exception handlers (BS-043).

Every error the API returns uses the single envelope documented in api.md::

    { "error": { "code": "...", "message": "...", "field": "..." } }

``field`` is present only for validation errors (it names the offending input).
The documented ``code`` ↔ HTTP-status pairs are:

    | 422 | VALIDATION_ERROR   | request failed schema/range validation |
    | 404 | NOT_FOUND          | resource (e.g. trip id) does not exist  |
    | 429 | RATE_LIMITED       | client exceeded the backend rate limit  |
    | 503 | NO_PLAN_AVAILABLE  | no feasible plan even after fallbacks   |
    | 500 | INTERNAL_ERROR     | unexpected server error                 |

Routes raise a typed :class:`AppError` (or let FastAPI raise a validation error);
:func:`register_exception_handlers` turns each into the envelope so no handler has
to build it by hand. Registered once on the app in ``main.py``.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# Error codes (api.md). Kept as constants so routes/tests reference one source.
VALIDATION_ERROR = "VALIDATION_ERROR"
NOT_FOUND = "NOT_FOUND"
RATE_LIMITED = "RATE_LIMITED"
NO_PLAN_AVAILABLE = "NO_PLAN_AVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"

# Map for HTTP errors raised outside our typed AppError (e.g. an unmatched route,
# or a plain HTTPException) so they still come out in the documented vocabulary.
_STATUS_TO_CODE = {
    status.HTTP_422_UNPROCESSABLE_ENTITY: VALIDATION_ERROR,
    status.HTTP_404_NOT_FOUND: NOT_FOUND,
    status.HTTP_429_TOO_MANY_REQUESTS: RATE_LIMITED,
    status.HTTP_503_SERVICE_UNAVAILABLE: NO_PLAN_AVAILABLE,
    status.HTTP_500_INTERNAL_SERVER_ERROR: INTERNAL_ERROR,
}


def error_body(code: str, message: str, field: str | None = None) -> dict:
    """Build the api.md envelope; ``field`` is included only when provided."""
    error: dict[str, str] = {"code": code, "message": message}
    if field is not None:
        error["field"] = field
    return {"error": error}


class AppError(Exception):
    """An error that maps directly to the API envelope + an HTTP status.

    Raise a subclass from a route (or service) and the registered handler renders
    ``{error:{code,message,field}}`` with :attr:`status_code`.
    """

    code: str = INTERNAL_ERROR
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class NotFoundError(AppError):
    """A requested resource does not exist (→ 404 NOT_FOUND)."""

    code = NOT_FOUND
    status_code = status.HTTP_404_NOT_FOUND


class RateLimitedError(AppError):
    """The client exceeded the backend's rate limit (→ 429 RATE_LIMITED)."""

    code = RATE_LIMITED
    status_code = status.HTTP_429_TOO_MANY_REQUESTS


class NoPlanAvailableError(AppError):
    """No feasible plan could be produced even after fallbacks (→ 503)."""

    code = NO_PLAN_AVAILABLE
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE


class ValidationError(AppError):
    """A request value failed a semantic check beyond the schema (→ 422).

    Used for checks the Pydantic schema can't express — e.g. an origin city that exists as a
    non-empty string but can't be resolved to a real location — so the client gets a precise,
    field-tagged message instead of a generic failure.
    """

    code = VALIDATION_ERROR
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


def _validation_field(loc: tuple) -> str | None:
    """Derive the offending field name from a Pydantic error ``loc``.

    Drops the leading request-part marker ("body"/"query"/"path") and joins the
    rest with dots (e.g. ("body","preferences","culture") → "preferences.culture").
    Returns ``None`` for a whole-body/model-level error with no specific field.
    """
    parts = [str(p) for p in loc if p not in ("body", "query", "path")]
    return ".".join(parts) if parts else None


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    first = errors[0] if errors else {}
    field = _validation_field(tuple(first.get("loc", ())))
    message = first.get("msg", "Invalid request")
    logger.info("validation error on %s: field=%s msg=%s", request.url.path, field, message)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=error_body(VALIDATION_ERROR, message, field),
    )


async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    logger.info("app error on %s: code=%s msg=%s", request.url.path, exc.code, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message, exc.field),
    )


async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # A plain HTTPException raised by FastAPI/Starlette (unmatched route, etc.).
    # If someone already passed an envelope-shaped detail, keep it; otherwise wrap.
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        code = str(detail["code"])
        message = str(detail.get("message", ""))
    else:
        code = _STATUS_TO_CODE.get(exc.status_code, "ERROR")
        message = detail if isinstance(detail, str) else "Request failed"
    return JSONResponse(status_code=exc.status_code, content=error_body(code, message))


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internals; log the real exception for operators.
    logger.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_body(INTERNAL_ERROR, "An unexpected error occurred."),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install the envelope handlers on ``app`` (call once at startup)."""
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
