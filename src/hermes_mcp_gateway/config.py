"""YAML configuration loading for the Hermes MCP Gateway.

The config file location is taken from the ``HERMES_MCP_GATEWAY_CONFIG``
environment variable, falling back to ``~/.hermes/mcp-gateway/config.yaml``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = "/home/rmax-10/.hermes/mcp-gateway/config.yaml"

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SECRET_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ConfigError(ValueError):
    """Raised when the gateway configuration is invalid."""


@dataclass
class ServerConfig:
    bind: str = "127.0.0.1"
    port: int = 8778


@dataclass
class HermesConfig:
    bin: str = "hermes"
    profile: str = "mcp-worker"
    task_dir: str = "/home/rmax-10/.hermes/mcp-gateway/tasks"
    db_path: str = "/home/rmax-10/.hermes/mcp-gateway/gateway.db"
    timeout_s: int = 1800


@dataclass
class AuthConfig:
    issuer: str = "hermes-mcp-gateway"
    signing_secret_env: str = "HERMES_MCP_GATEWAY_SIGNING_KEY"
    token_ttl_s: int = 600


@dataclass
class ClientConfig:
    client_id: str
    secret_hash: str
    scopes: list[str]
    max_duration_s: int
    requires_approval: bool = False
    max_concurrency: int = 1
    max_turns: int = 60
    workdirs: list[str] = field(default_factory=lambda: ["/home/rmax-10/src"])
    models: list[str] = field(default_factory=lambda: ["deepseek-v4-flash"])


@dataclass
class GatewayConfig:
    server: ServerConfig
    hermes: HermesConfig
    auth: AuthConfig
    clients: list[ClientConfig]


def load_config(path: str | os.PathLike[str] | None = None) -> GatewayConfig:
    """Load and validate the gateway YAML configuration."""
    if path is None:
        path = os.environ.get("HERMES_MCP_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)

    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise ConfigError("config root must be a mapping")

    _reject_unknown(raw, ("server", "hermes", "auth", "clients"), "config root")

    server = _parse_server(raw.get("server") or {}, "server")
    hermes = _parse_hermes(raw.get("hermes") or {}, "hermes")
    auth = _parse_auth(raw.get("auth") or {}, "auth")

    if "clients" not in raw:
        raise ConfigError("config root requires a 'clients' list")
    clients_raw = raw["clients"]
    if not isinstance(clients_raw, list):
        raise ConfigError("'clients' must be a list")
    clients = [_parse_client(c, f"clients[{index}]") for index, c in enumerate(clients_raw)]

    return GatewayConfig(server=server, hermes=hermes, auth=auth, clients=clients)


def _reject_unknown(mapping: Mapping, allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _require_mapping(value, where: str) -> dict:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a mapping")
    return dict(value)


def _as_int(value, where: str, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{where} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} must be an integer") from exc
    if minimum is not None and number < minimum:
        raise ConfigError(f"{where} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ConfigError(f"{where} must be <= {maximum}")
    return number


def _as_str_list(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{where} must be a non-empty list of strings")
    return list(value)


def _parse_server(raw, where: str) -> ServerConfig:
    raw = _require_mapping(raw, where)
    _reject_unknown(raw, ("bind", "port"), where)
    return ServerConfig(
        bind=raw.get("bind", "127.0.0.1"),
        port=_as_int(raw.get("port", 8778), f"{where}.port", minimum=1, maximum=65535),
    )


def _parse_hermes(raw, where: str) -> HermesConfig:
    raw = _require_mapping(raw, where)
    _reject_unknown(raw, ("bin", "profile", "task_dir", "db_path", "timeout_s"), where)
    return HermesConfig(
        bin=raw.get("bin", "hermes"),
        profile=raw.get("profile", "mcp-worker"),
        task_dir=str(raw.get("task_dir", "/home/rmax-10/.hermes/mcp-gateway/tasks")),
        db_path=str(raw.get("db_path", "/home/rmax-10/.hermes/mcp-gateway/gateway.db")),
        timeout_s=_as_int(raw.get("timeout_s", 1800), f"{where}.timeout_s", minimum=1),
    )


def _parse_auth(raw, where: str) -> AuthConfig:
    raw = _require_mapping(raw, where)
    _reject_unknown(raw, ("issuer", "signing_secret_env", "token_ttl_s"), where)
    return AuthConfig(
        issuer=raw.get("issuer", "hermes-mcp-gateway"),
        signing_secret_env=raw.get("signing_secret_env", "HERMES_MCP_GATEWAY_SIGNING_KEY"),
        token_ttl_s=_as_int(raw.get("token_ttl_s", 600), f"{where}.token_ttl_s", minimum=1),
    )


def _parse_client(raw, where: str) -> ClientConfig:
    raw = _require_mapping(raw, where)
    allowed = (
        "client_id",
        "secret_hash",
        "scopes",
        "requires_approval",
        "max_concurrency",
        "max_duration_s",
        "max_turns",
        "workdirs",
        "models",
    )
    _reject_unknown(raw, allowed, where)

    client_id = raw.get("client_id")
    if not isinstance(client_id, str) or not CLIENT_ID_RE.match(client_id):
        raise ConfigError(f"{where}.client_id must match ^[A-Za-z0-9._-]{{1,64}}$")

    secret_hash = raw.get("secret_hash")
    if not isinstance(secret_hash, str) or not SECRET_HASH_RE.match(secret_hash):
        raise ConfigError(f"{where}.secret_hash must look like 'sha256:<64 hex chars>'")

    scopes = raw.get("scopes")
    if not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) and s for s in scopes):
        raise ConfigError(f"{where}.scopes must be a non-empty list of strings")

    if "max_duration_s" not in raw:
        raise ConfigError(f"{where}.max_duration_s is required")

    requires_approval = raw.get("requires_approval", False)
    if not isinstance(requires_approval, bool):
        raise ConfigError(f"{where}.requires_approval must be a boolean")

    workdirs = raw.get("workdirs", ["/home/rmax-10/src"])
    if not isinstance(workdirs, list) or not all(isinstance(w, str) for w in workdirs):
        raise ConfigError(f"{where}.workdirs must be a list of strings")

    models = raw.get("models", ["deepseek-v4-flash"])
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise ConfigError(f"{where}.models must be a list of strings")

    return ClientConfig(
        client_id=client_id,
        secret_hash=secret_hash,
        scopes=list(scopes),
        max_duration_s=_as_int(raw["max_duration_s"], f"{where}.max_duration_s", minimum=1),
        requires_approval=requires_approval,
        max_concurrency=_as_int(raw.get("max_concurrency", 1), f"{where}.max_concurrency", minimum=1),
        max_turns=_as_int(raw.get("max_turns", 60), f"{where}.max_turns", minimum=1),
        workdirs=list(workdirs),
        models=list(models),
    )
