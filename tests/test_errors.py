import pytest
from pydantic import ValidationError as PydanticValidationError

from app.common.responses import ProblemResponse, ValidationProblemResponse
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    InternalServerError,
    NotFoundError,
    UnauthorizedError,
    ValidationDomainError,
    to_problem_response,
)


@pytest.mark.parametrize(
    ("error_type", "status", "code"),
    [
        (UnauthorizedError, 401, "UNAUTHORIZED"),
        (ForbiddenError, 403, "FORBIDDEN"),
        (NotFoundError, 404, "NOT_FOUND"),
        (ConflictError, 409, "CONFLICT"),
        (ValidationDomainError, 422, "VALIDATION_ERROR"),
        (InternalServerError, 500, "INTERNAL_ERROR"),
    ],
)
def test_domain_errors_map_to_contract_fields(error_type, status, code) -> None:
    response = to_problem_response(error_type(request_id="req_test"))

    assert isinstance(response, ProblemResponse)
    assert response.status == status
    assert response.code == code
    assert response.request_id == "req_test"
    assert str(response.type).startswith("https://api.chocomarket.com/problems/")


def test_validation_error_maps_field_errors_to_validation_problem() -> None:
    error = ValidationDomainError(
        request_id="req_validation",
        errors=[
            {"field": "email", "message": "Invalid email.", "code": "email"}
        ],
    )

    response = error.to_problem_response()

    assert isinstance(response, ValidationProblemResponse)
    assert response.errors[0].field == "email"
    assert response.errors[0].code == "email"


def test_problem_response_rejects_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        ProblemResponse(
            type="https://api.chocomarket.com/problems/conflict",
            title="Conflict",
            status=409,
            detail="Conflict.",
            code="CONFLICT",
            request_id="req_test",
            unexpected="not allowed",
        )


def test_mapping_requires_request_id_when_error_does_not_have_one() -> None:
    with pytest.raises(ValueError, match="request_id is required"):
        NotFoundError().to_problem_response()
