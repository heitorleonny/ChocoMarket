"""Password hashing and strict JWT verification helpers.

JWT validation is deliberately kept separate from user lookup and FastAPI
security dependencies.  This module verifies only cryptographic and registered
claims; active-user and role checks belong to the authentication dependencies.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.errors import UnauthorizedError

ph = PasswordHasher()


class JWTConfigurationError(ValueError):
    """Raised when required JWT settings are absent or unsafe."""


@dataclass(frozen=True, slots=True)
class JWTSettings:
    """Environment-backed settings used to validate signed access tokens.

    ``secret_key`` is excluded from the representation so it cannot be exposed
    accidentally by a debug log.  There is intentionally no code default for
    the secret, issuer, or audience.
    """

    secret_key: str = field(repr=False)
    issuer: str
    audience: str
    algorithm: str = "HS256"
    leeway_seconds: int = 0

    SUPPORTED_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})

    def __post_init__(self) -> None:
        if not self.secret_key.strip():
            raise JWTConfigurationError("JWT secret must be configured via the environment")
        if not self.issuer.strip():
            raise JWTConfigurationError("JWT issuer must be configured via the environment")
        if not self.audience.strip():
            raise JWTConfigurationError("JWT audience must be configured via the environment")
        if self.algorithm not in self.SUPPORTED_ALGORITHMS:
            raise JWTConfigurationError(
                "JWT algorithm must be one of HS256, HS384, or HS512"
            )
        if self.leeway_seconds < 0:
            raise JWTConfigurationError("JWT leeway cannot be negative")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "JWTSettings":
        """Load JWT settings from process configuration without insecure defaults."""

        env = os.environ if environ is None else environ

        def read(*names: str) -> str | None:
            for name in names:
                value = env.get(name)
                if value is not None:
                    return value
            return None

        secret = read("CHOCOMARKET_JWT_SECRET", "JWT_SECRET_KEY")
        issuer = read("CHOCOMARKET_JWT_ISSUER", "JWT_ISSUER")
        audience = read("CHOCOMARKET_JWT_AUDIENCE", "JWT_AUDIENCE")
        algorithm = read("CHOCOMARKET_JWT_ALGORITHM", "JWT_ALGORITHM") or "HS256"
        leeway_text = read("CHOCOMARKET_JWT_LEEWAY_SECONDS", "JWT_LEEWAY_SECONDS") or "0"

        missing = [
            name
            for name, value in (
                ("CHOCOMARKET_JWT_SECRET", secret),
                ("CHOCOMARKET_JWT_ISSUER", issuer),
                ("CHOCOMARKET_JWT_AUDIENCE", audience),
            )
            if value is None or not value.strip()
        ]
        if missing:
            raise JWTConfigurationError(
                "Missing required JWT environment setting(s): " + ", ".join(missing)
            )

        try:
            leeway_seconds = int(leeway_text)
        except (TypeError, ValueError) as exc:
            raise JWTConfigurationError(
                "JWT leeway must be a non-negative integer"
            ) from exc

        return cls(
            secret_key=secret,
            issuer=issuer,
            audience=audience,
            algorithm=algorithm,
            leeway_seconds=leeway_seconds,
        )


def get_jwt_settings() -> JWTSettings:
    """Read the current process environment for JWT validation settings."""

    return JWTSettings.from_env()


def _invalid_token() -> UnauthorizedError:
    """Return one safe error for every token parsing and validation failure."""

    return UnauthorizedError(detail="Authentication is required or invalid.")


_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _decode_base64url(segment: str) -> bytes:
    if not segment or not _BASE64URL_RE.fullmatch(segment):
        raise ValueError("invalid base64url segment")
    try:
        padding = "=" * (-len(segment) % 4)
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64url segment") from exc


def _decode_json_object(segment: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_decode_base64url(segment).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid JSON object") from exc
    if not isinstance(decoded, dict):
        raise ValueError("JWT segment must be a JSON object")
    return decoded


def _numeric_date(value: Any, claim: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{claim} must be a numeric date")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{claim} must be a finite numeric date")
    return result


def decode_jwt(
    token: str,
    settings: JWTSettings | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify an HMAC JWT and return its claims.

    Only explicitly supported HMAC algorithms are accepted.  The configured
    algorithm is compared to the token header before signature verification,
    preventing algorithm substitution and ``none``-algorithm tokens.
    """

    active_settings = settings or get_jwt_settings()
    if not isinstance(token, str):
        raise _invalid_token()

    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise _invalid_token()

    try:
        header = _decode_json_object(parts[0])
        claims = _decode_json_object(parts[1])
        signature = _decode_base64url(parts[2])

        if header.get("alg") != active_settings.algorithm:
            raise ValueError("unexpected JWT algorithm")
        if header.get("alg") not in JWTSettings.SUPPORTED_ALGORITHMS:
            raise ValueError("unsupported JWT algorithm")

        digest_name = {
            "HS256": "sha256",
            "HS384": "sha384",
            "HS512": "sha512",
        }[active_settings.algorithm]
        expected_signature = hmac.new(
            active_settings.secret_key.encode("utf-8"),
            f"{parts[0]}.{parts[1]}".encode("ascii"),
            getattr(hashlib, digest_name),
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError("invalid JWT signature")

        subject = claims.get("sub")
        issuer = claims.get("iss")
        audience = claims.get("aud")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("JWT subject is required")
        if issuer != active_settings.issuer:
            raise ValueError("invalid JWT issuer")
        if isinstance(audience, str):
            audience_values = [audience]
        elif isinstance(audience, list) and all(isinstance(item, str) for item in audience):
            audience_values = audience
        else:
            raise ValueError("invalid JWT audience")
        if active_settings.audience not in audience_values:
            raise ValueError("invalid JWT audience")

        expiration = _numeric_date(claims.get("exp"), "exp")
        current_time = time.time() if now is None else now
        if expiration <= current_time - active_settings.leeway_seconds:
            raise ValueError("JWT has expired")
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
        raise _invalid_token()

    return claims


# Explicit alias for callers that use validation terminology.
validate_jwt = decode_jwt


def hash_user_password(password: str) -> str:
    return ph.hash(password)


def verify_user_password(password_hash: str, password_attempt: str) -> bool:
    try:
        return ph.verify(password_hash, password_attempt)
    except VerifyMismatchError:
        return False
