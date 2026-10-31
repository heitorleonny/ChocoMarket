import base64
import hashlib
import hmac
import json

import pytest

from app.core.auth import extract_bearer_token, validate_bearer_token
from app.core.errors import UnauthorizedError
from app.core.security import JWTConfigurationError, JWTSettings, decode_jwt


SECRET = "a-test-secret-kept-outside-source-configuration"
SETTINGS = JWTSettings(
    secret_key=SECRET,
    issuer="https://issuer.example.test",
    audience="chocomarket-api",
)


def _segment(value: object) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()


def _token(*, claims: dict, algorithm: str = "HS256", secret: str = SECRET) -> str:
    header = _segment({"alg": algorithm, "typ": "JWT"})
    payload = _segment(claims)
    signing_input = f"{header}.{payload}".encode()
    digest = getattr(hashlib, {"HS256": "sha256", "HS384": "sha384", "HS512": "sha512"}[algorithm])
    signature = hmac.new(secret.encode(), signing_input, digest).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{header}.{payload}.{encoded_signature}"


def _valid_claims(**overrides: object) -> dict:
    claims = {
        "sub": "user-123",
        "iss": SETTINGS.issuer,
        "aud": SETTINGS.audience,
        "exp": 2_000_000_000,
    }
    claims.update(overrides)
    return claims


def test_jwt_settings_load_required_values_from_environment() -> None:
    settings = JWTSettings.from_env(
        {
            "CHOCOMARKET_JWT_SECRET": SECRET,
            "CHOCOMARKET_JWT_ISSUER": SETTINGS.issuer,
            "CHOCOMARKET_JWT_AUDIENCE": SETTINGS.audience,
            "CHOCOMARKET_JWT_ALGORITHM": "HS384",
            "CHOCOMARKET_JWT_LEEWAY_SECONDS": "5",
        }
    )

    assert settings.algorithm == "HS384"
    assert settings.leeway_seconds == 5
    assert SECRET not in repr(settings)


def test_jwt_settings_require_secret_issuer_and_audience() -> None:
    with pytest.raises(JWTConfigurationError):
        JWTSettings.from_env({})


def test_decode_jwt_accepts_valid_signature_and_registered_claims() -> None:
    claims = _valid_claims()

    decoded = decode_jwt(_token(claims=claims), SETTINGS, now=1_900_000_000)

    assert decoded == claims


def test_decode_jwt_rejects_tampered_signature() -> None:
    token = _token(claims=_valid_claims())
    tampered = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"

    with pytest.raises(UnauthorizedError):
        decode_jwt(tampered, SETTINGS, now=1_900_000_000)


@pytest.mark.parametrize(
    "claims",
    [
        _valid_claims(exp=1_899_999_999),
        _valid_claims(iss="other-issuer"),
        _valid_claims(aud="other-audience"),
        _valid_claims(sub=""),
    ],
)
def test_decode_jwt_rejects_invalid_registered_claims(claims: dict) -> None:
    with pytest.raises(UnauthorizedError):
        decode_jwt(_token(claims=claims), SETTINGS, now=1_900_000_000)


def test_decode_jwt_rejects_algorithm_substitution_and_none() -> None:
    hs384_token = _token(claims=_valid_claims(), algorithm="HS384")
    none_token = f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(_valid_claims())}."

    with pytest.raises(UnauthorizedError):
        decode_jwt(hs384_token, SETTINGS, now=1_900_000_000)
    with pytest.raises(UnauthorizedError):
        decode_jwt(none_token, SETTINGS, now=1_900_000_000)


def test_extract_bearer_token_requires_exact_bearer_scheme() -> None:
    token = _token(claims=_valid_claims())

    assert extract_bearer_token(f"Bearer {token}") == token
    assert extract_bearer_token(f"bearer {token}") == token

    for header in (None, "", "Basic token", "Bearer", f"Bearer  {token}"):
        with pytest.raises(UnauthorizedError):
            extract_bearer_token(header)


def test_validate_bearer_token_validates_header_and_jwt() -> None:
    claims = _valid_claims()

    assert validate_bearer_token(
        f"Bearer {_token(claims=claims)}", SETTINGS, now=1_900_000_000
    ) == claims


def test_jwt_settings_reject_unsafe_algorithm_and_leeway_configuration() -> None:
    base_environment = {
        "CHOCOMARKET_JWT_SECRET": SECRET,
        "CHOCOMARKET_JWT_ISSUER": SETTINGS.issuer,
        "CHOCOMARKET_JWT_AUDIENCE": SETTINGS.audience,
    }

    with pytest.raises(JWTConfigurationError, match="algorithm"):
        JWTSettings.from_env({**base_environment, "CHOCOMARKET_JWT_ALGORITHM": "none"})
    with pytest.raises(JWTConfigurationError, match="negative"):
        JWTSettings.from_env({**base_environment, "CHOCOMARKET_JWT_LEEWAY_SECONDS": "-1"})
    with pytest.raises(JWTConfigurationError, match="non-negative"):
        JWTSettings.from_env({**base_environment, "CHOCOMARKET_JWT_LEEWAY_SECONDS": "not-an-integer"})


def test_jwt_settings_do_not_accept_blank_required_environment_values() -> None:
    with pytest.raises(JWTConfigurationError, match="required JWT"):
        JWTSettings.from_env(
            {
                "CHOCOMARKET_JWT_SECRET": "   ",
                "CHOCOMARKET_JWT_ISSUER": SETTINGS.issuer,
                "CHOCOMARKET_JWT_AUDIENCE": SETTINGS.audience,
            }
        )
