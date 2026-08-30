"""Pure RBAC/policy helpers with no I/O beyond the filesystem checks.

Scopes follow two shapes:

* ``toolset:<name>`` grants a Hermes toolset.
* ``task:run`` grants task execution (checked by the MCP layer, not here).
"""

from __future__ import annotations

import os


def _toolset_scopes(client_cfg) -> set[str]:
    return {
        scope.split(":", 1)[1]
        for scope in client_cfg.scopes
        if scope.startswith("toolset:")
    }


def allowed_toolsets(client_cfg, requested: list[str]) -> list[str]:
    """Return the requested toolsets that the client is allowed to use.

    The caller is responsible for enforcing strictness: if any requested
    toolset is dropped here, the request should be rejected. An empty
    ``requested`` list yields no toolsets.
    """
    allowed = _toolset_scopes(client_cfg)
    return [toolset for toolset in (requested or []) if toolset in allowed]


def validate_workdir(client_cfg, workdir) -> str | None:
    """Return the real path of ``workdir`` if it is a real dir inside a root.

    Returns ``None`` when the directory does not exist, is not a directory, or
    resolves outside every allowed workdir root (including via symlinks).
    """
    if not workdir:
        return None

    roots = [os.path.realpath(os.path.expanduser(root)) for root in client_cfg.workdirs]
    target = os.path.realpath(os.path.expanduser(str(workdir)))

    if not os.path.isdir(target):
        return None

    for root in roots:
        try:
            if os.path.commonpath([root, target]) == root:
                return target
        except ValueError:
            continue
    return None


def validate_model(client_cfg, model) -> bool:
    """Return whether ``model`` is allowed for this client.

    An empty models allowlist means any model may be used, but ``None`` is
    never allowed.
    """
    if model is None:
        return False
    if not client_cfg.models:
        return True
    return model in client_cfg.models


def requires_task_approval(client_cfg, toolsets: list[str]) -> bool:
    """A client requires approval only when configured to, and only for terminal."""
    return bool(client_cfg.requires_approval) and "terminal" in (toolsets or [])
