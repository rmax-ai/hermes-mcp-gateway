"""Human-in-the-loop task gating (a thin queue over the DB functions).

The MCP layer calls :func:`gate` before submitting a task. When approval is
required, the task is parked as a pending approval row and not spawned; the
operator CLI (later phase) decides it via ``decide_approval`` and then
re-submits the task for execution.
"""

from __future__ import annotations

from . import policy


def gate(client_cfg, db, task_id: str, toolsets: list[str]) -> bool:
    """Return ``True`` if the task may run now, ``False`` if it is queued.

    When approval is required a pending approval row is created for ``task_id``
    and the caller must not spawn the task.
    """
    if policy.requires_task_approval(client_cfg, toolsets):
        db.create_approval(task_id)
        return False
    return True
