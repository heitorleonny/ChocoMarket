"""Validate HTTP responses against the hand-written ``openapi.yaml``."""

from __future__ import annotations

import re
from datetime import datetime
from functools import cache
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.main import load_contract

CONTRACT_URI = "urn:chocomarket:openapi"
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})

FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("uuid", raises=ValueError)
def _is_uuid(value: object) -> bool:
    return not isinstance(value, str) or str(UUID(value)) == value.lower()


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    # RFC 3339 requires the "T" separator and an explicit offset.
    return "T" in value and datetime.fromisoformat(value).tzinfo is not None


@FORMAT_CHECKER.checks("uri")
def _is_uri(value: object) -> bool:
    if not isinstance(value, str):
        return True
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


@FORMAT_CHECKER.checks("uri-reference")
def _is_uri_reference(value: object) -> bool:
    return not isinstance(value, str) or (" " not in value and bool(value))


@FORMAT_CHECKER.checks("email")
def _is_email(value: object) -> bool:
    return not isinstance(value, str) or bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


@cache
def _registry() -> Registry:
    resource = Resource.from_contents(load_contract(), default_specification=DRAFT202012)
    return Registry().with_resource(CONTRACT_URI, resource)


def _resolve(node: dict[str, Any]) -> dict[str, Any]:
    """Follow local ``$ref`` pointers inside the contract document."""

    while "$ref" in node:
        pointer = node["$ref"].removeprefix("#/").split("/")
        target: Any = load_contract()
        for part in pointer:
            target = target[part]
        node = target
    return node


def validator_for(schema: dict[str, Any]) -> Draft202012Validator:
    """Build a validator whose local refs resolve against the contract."""

    if "$ref" in schema and schema["$ref"].startswith("#/"):
        schema = {"$ref": f"{CONTRACT_URI}{schema['$ref']}"}
    return Draft202012Validator(schema, registry=_registry(), format_checker=FORMAT_CHECKER)


def component_validator(name: str) -> Draft202012Validator:
    return validator_for({"$ref": f"#/components/schemas/{name}"})


def contract_operations() -> dict[tuple[str, str], dict[str, Any]]:
    """Return ``{(METHOD, normalized path): operation}`` for the contract."""

    operations: dict[tuple[str, str], dict[str, Any]] = {}
    for path, item in load_contract()["paths"].items():
        for method, operation in item.items():
            if method in HTTP_METHODS:
                operations[(method.upper(), normalize_path(path))] = operation
    return operations


def normalize_path(path: str) -> str:
    """Make ``/v1/users/{userId}`` and ``/v1/users/{user_id}`` comparable."""

    return re.sub(r"\{[^}]+\}", "{}", path)


def assert_response_matches(response: Any, method: str, path_template: str) -> Any:
    """Assert status, media type, headers and body against the contract.

    Returns the decoded JSON body, or ``None`` for responses without content.
    """

    operation = contract_operations().get((method.upper(), normalize_path(path_template)))
    assert operation is not None, f"{method} {path_template} is not in openapi.yaml"
    status = str(response.status_code)
    assert status in operation["responses"], (
        f"{method} {path_template} returned undocumented status {status}: {response.text}"
    )
    spec = _resolve(operation["responses"][status])

    for header_name, header_spec in spec.get("headers", {}).items():
        assert header_name in response.headers, f"missing {header_name} header"
        header_schema = _resolve(header_spec)["schema"]
        errors = list(validator_for(header_schema).iter_errors(response.headers[header_name]))
        assert not errors, f"{header_name} header: {errors[0].message}"

    content = spec.get("content")
    if not content:
        assert response.content == b"", "response without documented content has a body"
        return None

    media_type = response.headers["content-type"].split(";")[0].strip()
    assert media_type in content, f"media type {media_type} not documented for {status}"
    body = response.json()
    errors = sorted(
        validator_for(content[media_type]["schema"]).iter_errors(body),
        key=lambda error: list(error.absolute_path),
    )
    assert not errors, (
        f"{method} {path_template} {status} violates the contract at "
        f"{list(errors[0].absolute_path)}: {errors[0].message}"
    )
    return body
