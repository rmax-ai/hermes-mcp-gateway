"""Client-secret verification and short-lived JWT issuance/validation.

HS256 JWTs carry the client id (``sub``), space-joined scopes (``scope``), the
issuer (``iss``), issued-at/expiry timestamps and a random ``jti``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid

import jwt


class AuthError(Exception):
    """Authentication/authorization failure with a machine-readable code."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


def resolve_signing_key(signing_secret_env: str = "HERMES_MCP_GATEWAY_SIGNING_KEY") -> str:
    """Read the JWT signing key from the environment.

    Refuse to run without a key, or with one too short to be safe.
    """
    key = os.environ.get(signing_secret_env)
    if not key:
        raise RuntimeError(
            f"signing key environment variable {signing_secret_env!r} is not set"
        )
    if len(key) < 32:
        raise RuntimeError(
            f"signing key from {signing_secret_env!r} must be at least 32 characters"
        )
    return key


def verify_client_secret(client_cfg, secret: str) -> bool:
    """Compare ``sha256(secret)`` with the hex digest in ``client_cfg.secret_hash``."""
    if not secret:
        return False
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    expected = client_cfg.secret_hash.split(":", 1)[1]
    return hmac.compare_digest(digest, expected)


def issue_token(client_id: str, scopes: list[str], signing_key: str, issuer: str, ttl_s: int) -> str:
    """Mint an HS256 JWT for the given client."""
    now = int(time.time())
    claims = {
        "sub": client_id,
        "scope": " ".join(scopes),
        "iss": issuer,
        "iat": now,
        "exp": now + int(ttl_s),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, signing_key, algorithm="HS256")


def validate_token(token: str | None, signing_key: str, issuer: str) -> dict:
    """Validate a token and return its claims, or raise :class:`AuthError`."""
    if not token:
        raise AuthError("missing_token", "token is missing")

    try:
        return jwt.decode(
            token,
            signing_key,
            algorithms=["HS256"],
            issuer=issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token_expired", "token has expired") from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthError("wrong_issuer", "token issuer does not match") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid_token", "token is invalid") from exc
