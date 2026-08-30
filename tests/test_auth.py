import hashlib

import pytest

from hermes_mcp_gateway import auth
from hermes_mcp_gateway.auth import AuthError
from hermes_mcp_gateway.config import ClientConfig

KEY = "not-a-real-secret-this-is-only-for-tests"
ISSUER = "hermes-mcp-gateway"


def make_client(secret_hash="sha256:" + "a" * 64):
    return ClientConfig(
        client_id="test-client",
        secret_hash=secret_hash,
        scopes=["task:run", "toolset:file"],
        max_duration_s=60,
    )


def test_verify_client_secret():
    digest = hashlib.sha256(b"correct-horse-battery-staple").hexdigest()
    client = make_client(secret_hash="sha256:" + digest)

    assert auth.verify_client_secret(client, "correct-horse-battery-staple") is True
    assert auth.verify_client_secret(client, "wrong-secret") is False
    assert auth.verify_client_secret(client, "") is False


def test_jwt_roundtrip():
    token = auth.issue_token("test-client", ["task:run", "toolset:file"], KEY, ISSUER, 300)
    claims = auth.validate_token(token, KEY, ISSUER)

    assert claims["sub"] == "test-client"
    assert claims["scope"] == "task:run toolset:file"
    assert claims["iss"] == ISSUER
    assert claims["jti"]


def test_expired_token():
    token = auth.issue_token("test-client", ["task:run"], KEY, ISSUER, -10)
    with pytest.raises(AuthError) as excinfo:
        auth.validate_token(token, KEY, ISSUER)
    assert excinfo.value.code == "token_expired"


def test_tampered_token():
    token = auth.issue_token("test-client", ["task:run"], KEY, ISSUER, 300)
    # Corrupt the signature segment so the payload/signature no longer match.
    # Flip the *first* signature character: the final base64url character only
    # contributes four significant bits, so replacing it can leave the decoded
    # signature unchanged and make this test flaky.
    header, payload, signature = token.split(".")
    first = "A" if not signature.startswith("A") else "B"
    tampered = f"{header}.{payload}.{first}{signature[1:]}"
    with pytest.raises(AuthError) as excinfo:
        auth.validate_token(tampered, KEY, ISSUER)
    assert excinfo.value.code == "invalid_token"


def test_wrong_issuer():
    token = auth.issue_token("test-client", ["task:run"], KEY, "some-other-issuer", 300)
    with pytest.raises(AuthError) as excinfo:
        auth.validate_token(token, KEY, ISSUER)
    assert excinfo.value.code == "wrong_issuer"


def test_missing_token():
    with pytest.raises(AuthError) as excinfo:
        auth.validate_token(None, KEY, ISSUER)
    assert excinfo.value.code == "missing_token"


def test_signing_key_resolution(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_GATEWAY_SIGNING_KEY", raising=False)
    with pytest.raises(RuntimeError):
        auth.resolve_signing_key("HERMES_MCP_GATEWAY_SIGNING_KEY")

    monkeypatch.setenv("HERMES_MCP_GATEWAY_SIGNING_KEY", "too-short")
    with pytest.raises(RuntimeError):
        auth.resolve_signing_key("HERMES_MCP_GATEWAY_SIGNING_KEY")

    monkeypatch.setenv("HERMES_MCP_GATEWAY_SIGNING_KEY", KEY)
    assert auth.resolve_signing_key("HERMES_MCP_GATEWAY_SIGNING_KEY") == KEY
