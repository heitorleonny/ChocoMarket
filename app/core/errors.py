"""Domain errors and their standardized problem-response mapping.

The exceptions in this module are transport-neutral. FastAPI exception handlers
and request-id middleware are intentionally implemented separately, so services
can raise these errors without depending on request objects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.common.responses import (
    ProblemResponse,
    ValidationErrorItem,
    ValidationProblemResponse,
)
from app.core.http import request_id_for

PROBLEM_BASE_URL = "https://api.chocomarket.com/problems/"
_LOGGER = logging.getLogger(__name__)


class DomainError(Exception):
    """Base exception for expected, safe-to-return domain failures."""

    status_code: int = 500
    default_title: str = "Internal Server Error"
    default_code: str = "INTERNAL_ERROR"
    default_problem_type: str = "internal-error"
    default_detail: str = "An unexpected error occurred."

    def __init__(
        self,
        detail: str | None = None,
        *,
        request_id: str | None = None,
        title: str | None = None,
        code: str | None = None,
        problem_type: str | None = None,
        instance: str | None = None,
        errors: Sequence[ValidationErrorItem | Mapping[str, Any]] | None = None,
    ) -> None:
        self.detail = detail or self.default_detail
        self.request_id = request_id
        self.title = title or self.default_title
        self.code = code or self.default_code
        self.problem_type = problem_type or self.default_problem_type
        self.instance = instance
        self.errors = [
            item if isinstance(item, ValidationErrorItem)
            else ValidationErrorItem.model_validate(item)
            for item in (errors or [])
        ]
        super().__init__(self.detail)

    @property
    def type_uri(self) -> str:
        """Return the stable URI identifying this problem category."""

        return f"{PROBLEM_BASE_URL}{self.problem_type}"

    def to_problem_response(
        self, *, request_id: str | None = None
    ) -> ProblemResponse | ValidationProblemResponse:
        """Map this error to the public response model.

        The request ID is normally supplied by the request middleware. Keeping
        it as an explicit mapping argument avoids making domain services aware
        of HTTP request state while still enforcing the required contract field.
        """

        resolved_request_id = request_id or self.request_id
        if not resolved_request_id:
            raise ValueError("request_id is required to build a problem response")

        common = {
            "type": self.type_uri,
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "code": self.code,
            "request_id": resolved_request_id,
            "instance": self.instance,
        }
        if self.errors or self.code == "VALIDATION_ERROR":
            return ValidationProblemResponse(**common, errors=self.errors)
        return ProblemResponse(**common)


class UnauthorizedError(DomainError):
    status_code = 401
    default_title = "Unauthorized"
    default_code = "UNAUTHORIZED"
    default_problem_type = "unauthorized"
    default_detail = "Authentication is required or invalid."


class ForbiddenError(DomainError):
    status_code = 403
    default_title = "Forbidden"
    default_code = "FORBIDDEN"
    default_problem_type = "forbidden"
    default_detail = "You do not have permission to perform this operation."


class NotFoundError(DomainError):
    status_code = 404
    default_title = "Not Found"
    default_code = "NOT_FOUND"
    default_problem_type = "not-found"
    default_detail = "The requested resource was not found."


class ConflictError(DomainError):
    status_code = 409
    default_title = "Conflict"
    default_code = "CONFLICT"
    default_problem_type = "conflict"
    default_detail = "The request conflicts with the current resource state."


class ValidationDomainError(DomainError):
    status_code = 422
    default_title = "Validation Error"
    default_code = "VALIDATION_ERROR"
    default_problem_type = "validation-error"
    default_detail = "One or more fields are invalid."


class InternalServerError(DomainError):
    status_code = 500
    default_title = "Internal Server Error"
    default_code = "INTERNAL_ERROR"
    default_problem_type = "internal-error"
    default_detail = "An unexpected error occurred."


def to_problem_response(
    error: DomainError, *, request_id: str | None = None
) -> ProblemResponse | ValidationProblemResponse:
    """Map a domain error to its contract response without handling HTTP."""

    return error.to_problem_response(request_id=request_id)


# Short aliases keep call sites readable while retaining the explicit name for
# the validation error, which avoids confusion with framework exceptions.
ValidationError = ValidationDomainError


_HTTP_PROBLEM_DETAILS = {
    400: ("Bad Request", "BAD_REQUEST", "bad-request"),
    401: ("Unauthorized", "UNAUTHORIZED", "unauthorized"),
    403: ("Forbidden", "FORBIDDEN", "forbidden"),
    404: ("Not Found", "NOT_FOUND", "not-found"),
    405: ("Method Not Allowed", "METHOD_NOT_ALLOWED", "method-not-allowed"),
    409: ("Conflict", "CONFLICT", "conflict"),
    422: ("Validation Error", "VALIDATION_ERROR", "validation-error"),
    429: ("Too Many Requests", "TOO_MANY_REQUESTS", "too-many-requests"),
    500: ("Internal Server Error", "INTERNAL_ERROR", "internal-error"),
}


def _rollback_request_session(request: Request) -> None:
    """Rollback a request-bound session when one is available."""

    db = getattr(request.state, "db", None)
    rollback = getattr(db, "rollback", None)
    if callable(rollback):
        rollback()


def _response(
    problem: ProblemResponse | ValidationProblemResponse,
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Serialize a public problem with the contract media type."""

    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type="application/problem+json",
        headers=dict(headers or {}),
    )


def _http_problem(
    status_code: int,
    detail: str,
    request_id: str,
) -> ProblemResponse:
    title, code, problem_type = _HTTP_PROBLEM_DETAILS.get(
        status_code,
        ("HTTP Error", "HTTP_ERROR", "http-error"),
    )
    return ProblemResponse(
        type=f"{PROBLEM_BASE_URL}{problem_type}",
        title=title,
        status=status_code,
        detail=detail,
        code=code,
        request_id=request_id,
    )


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """Render an expected domain failure without exposing internal state."""

    problem = exc.to_problem_response(request_id=request_id_for(request))
    headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, UnauthorizedError) else None
    return _response(problem, headers=headers)


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render FastAPI/Pydantic input errors in the public validation envelope."""

    errors: list[ValidationErrorItem] = []
    for error in exc.errors():
        location = error.get("loc", ())
        field = ".".join(str(part) for part in location) or "request"
        errors.append(
            ValidationErrorItem(
                field=field,
                message=str(error.get("msg", "Invalid value.")),
                code=str(error["type"]) if error.get("type") else None,
            )
        )

    problem = ValidationProblemResponse(
        type=f"{PROBLEM_BASE_URL}validation-error",
        title="Validation Error",
        status=422,
        detail="One or more fields are invalid.",
        code="VALIDATION_ERROR",
        request_id=request_id_for(request),
        errors=errors,
    )
    return _response(problem)


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Convert FastAPI and Starlette HTTP exceptions to Problem_Response."""

    status_code = exc.status_code
    detail = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
    problem = _http_problem(status_code, detail, request_id_for(request))
    headers = exc.headers or {}
    return _response(problem, headers=headers)


async def integrity_error_handler(request: Request, _exc: IntegrityError) -> JSONResponse:
    """Hide database-specific constraint details behind a safe conflict."""

    _rollback_request_session(request)
    error = ConflictError(
        detail="The request conflicts with existing data.",
        request_id=request_id_for(request),
    )
    return _response(error.to_problem_response())


async def unexpected_error_handler(request: Request, _exc: Exception) -> JSONResponse:
    """Return a generic 500 and log only its correlation ID."""

    _rollback_request_session(request)
    request_id = request_id_for(request)
    _LOGGER.error(
        "Unhandled application error",
        extra={"request_id": request_id},
        exc_info=False,
    )
    error = InternalServerError(request_id=request_id)
    return _response(error.to_problem_response())


def register_exception_handlers(app: FastAPI) -> None:
    """Register the application's uniform error handlers."""

    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
