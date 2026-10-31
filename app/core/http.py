"""HTTP middleware shared by the FastAPI application."""

from __future__ import annotations

import re
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _is_safe_request_id(value: str) -> bool:
    """Accept only bounded, header-safe request IDs supplied by a client."""

    return bool(_REQUEST_ID_PATTERN.fullmatch(value))


def generate_request_id() -> str:
    """Generate a non-sensitive request ID suitable for response headers/logs."""

    return f"req_{secrets.token_urlsafe(16)}"


def request_id_for(request: Request) -> str:
    """Return the middleware-provided ID, with a safe fallback for direct calls."""

    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str) and _is_safe_request_id(request_id):
        return request_id
    request_id = generate_request_id()
    request.state.request_id = request_id
    return request_id


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Propagate a safe client ID or generate one for every request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        supplied_request_id = request.headers.get(REQUEST_ID_HEADER)
        request_id = (
            supplied_request_id
            if supplied_request_id and _is_safe_request_id(supplied_request_id)
            else generate_request_id()
        )
        request.state.request_id = request_id

        try:
            response = await call_next(request)
        except Exception as exc:
            # ServerErrorMiddleware handles unhandled exceptions outside the
            # user middleware stack. Handle the same safe fallback here so the
            # correlation header is still attached to the 500 response.
            from app.core.errors import unexpected_error_handler

            response = await unexpected_error_handler(request, exc)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
