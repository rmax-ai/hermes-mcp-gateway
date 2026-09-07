"""Application assembly and the server entry point."""

from __future__ import annotations

import argparse
import sys

from . import telemetry
from .auth import resolve_signing_key
from .config import ConfigError, load_config
from .db import Database
from .executor import TaskExecutor
from .server import build_gateway_app


def create_app(cfg, db, executor, *, signing_key: str | None = None):
    """Build the gateway Starlette app from already-constructed components."""
    if signing_key is None:
        signing_key = resolve_signing_key(cfg.auth.signing_secret_env)
    return build_gateway_app(cfg, db, executor, signing_key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-mcp-gateway",
        description="Operate the Hermes MCP Gateway.",
    )
    parser.add_argument("--config", help="path to the gateway YAML config file")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and exit (does not start the server)",
    )
    parser.add_argument("command", nargs="?", default=None, help="'serve' (the default)")

    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print("config OK")
        return 0

    if args.command not in (None, "serve"):
        print(f"error: unknown command {args.command!r} (not implemented yet)", file=sys.stderr)
        return 2

    telemetry.init("hermes-mcp-gateway")
    try:
        signing_key = resolve_signing_key(cfg.auth.signing_secret_env)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    db = Database(cfg.hermes.db_path)
    executor = TaskExecutor(cfg, db)
    app = create_app(cfg, db, executor, signing_key=signing_key)

    import uvicorn

    uvicorn.run(app, host=cfg.server.bind, port=cfg.server.port, log_level="info")
    return 0
