"""Pydantic response envelopes shared by API modules.

These models describe the public error and pagination contracts only. HTTP exception handling and
request-id middleware are intentionally kept in the application layer.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AnyUrl, BaseModel, ConfigDict, Field


class ProblemResponse(BaseModel):
    """RFC 7807-style error response used by the HTTP API."""

    model_config = ConfigDict(extra="forbid")

    type: AnyUrl
    title: str
    status: Annotated[int, Field(ge=400, le=599)]
    detail: str
    code: str
    request_id: str = Field(min_length=1)
    instance: str | None = None


class ValidationErrorItem(BaseModel):
    """A single request-field validation failure."""

    model_config = ConfigDict(extra="forbid")

    field: str
    message: str
    code: str | None = None


class ValidationProblemResponse(ProblemResponse):
    """Problem response containing one or more field validation failures."""

    errors: list[ValidationErrorItem]


class PageMeta(BaseModel):
    """Cursor-pagination metadata shared by every list envelope.

    ``next_cursor`` is required but nullable, matching the contract: the last
    page always carries an explicit ``null``.
    """

    model_config = ConfigDict(extra="forbid")

    limit: Annotated[int, Field(ge=1, le=100)]
    next_cursor: str | None
    has_next: bool


# Names matching the schemas in openapi.yaml are useful to callers that work
# directly with the contract, while the explicit *Response names make their
# HTTP purpose clear inside Python code.
Problem = ProblemResponse
ValidationProblem = ValidationProblemResponse
FieldError = ValidationErrorItem
