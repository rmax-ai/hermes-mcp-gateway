"""Per-request authenticated principal carried through context variables.

The auth middleware sets the principal for the duration of an ``/mcp`` request
and MCP tool handlers read it back via :func:`get_auth_context`. A
:class:`contextvars.ContextVar` is used because the MCP SDK may run synchronous
tool handlers in worker threads; ``anyio.to_thread.run_sync`` copies the
current context into those threads, so the value set here stays visible to the
handlers.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

from .config import ClientConfig

_auth_context: contextvars.ContextVar[tuple[ClientConfig, list[str]] | None] = (
    contextvars.ContextVar("hermes_mcp_gateway_auth_context", default=None)
)


def set_auth_context(client_cfg: ClientConfig, scopes: list[str]) -> None:
    """Set the authenticated principal for the current context."""
    _auth_context.set((client_cfg, list(scopes)))


def get_auth_context() -> tuple[ClientConfig, list[str]] | None:
    """Return the current ``(ClientConfig, scopes)`` principal, if any."""
    return _auth_context.get()


@contextmanager
def auth_scope(client_cfg: ClientConfig, scopes: list[str]) -> Iterator[None]:
    """Set the principal for a scoped block, restoring the previous value after."""
    token = _auth_context.set((client_cfg, list(scopes)))
    try:
        yield
    finally:
        _auth_context.reset(token)
