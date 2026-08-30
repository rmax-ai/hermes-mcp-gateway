"""MCP tool handlers exposing governed Hermes task execution.

Every tool returns a JSON string (``json.dumps``) so the MCP client receives a
single text blob it can parse. Policy decisions live in :mod:`policy`; the
handlers here only glue authentication context, the audit store and the task
executor together.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import policy
from .context import get_auth_context
from .executor import FINISHED_STATES

OUTPUT_LIMIT = 4000


def err_json(code: str, **extra) -> str:
    """Render a tool-level error as ``{"error": <code>, ...}``."""
    payload = {"error": code}
    payload.update(extra)
    return json.dumps(payload)


def run_task(
    client_cfg,
    scopes: list[str],
    db,
    executor,
    *,
    prompt: str,
    toolsets: list[str] | None = None,
    model: str | None = None,
    workdir: str | None = None,
    max_duration_s: int | None = None,
) -> str:
    """Validate policy for a task and either submit it or queue approval.

    Returns ``{"task_id", "status"}`` on success or ``{"error", ...}``.
    """
    if "task:run" not in scopes:
        return err_json("forbidden", missing_scopes=["task:run"])

    requested = list(toolsets or [])
    resolved = policy.allowed_toolsets(client_cfg, requested)
    missing = [toolset for toolset in requested if toolset not in resolved]
    if missing:
        return err_json(
            "forbidden",
            missing_scopes=sorted({f"toolset:{name}" for name in missing}),
        )

    requested_workdir = workdir or (client_cfg.workdirs[0] if client_cfg.workdirs else None)
    resolved_workdir = policy.validate_workdir(client_cfg, requested_workdir)
    if resolved_workdir is None:
        return err_json("invalid_workdir", workdir=requested_workdir)

    if model is not None and not policy.validate_model(client_cfg, model):
        return err_json("invalid_model", model=model)

    duration = client_cfg.max_duration_s
    if max_duration_s is not None:
        duration = min(int(max_duration_s), client_cfg.max_duration_s)
    duration = max(1, duration)

    task_id = uuid.uuid4().hex

    if policy.requires_task_approval(client_cfg, resolved):
        db.create_task(
            task_id,
            client_cfg.client_id,
            prompt,
            ",".join(resolved),
            model,
            resolved_workdir,
            status="pending_approval",
        )
        db.create_approval(task_id)
        return json.dumps({"task_id": task_id, "status": "pending_approval"})

    db.create_task(
        task_id,
        client_cfg.client_id,
        prompt,
        ",".join(resolved),
        model,
        resolved_workdir,
        status="pending",
    )
    executor.submit(
        client_cfg,
        task_id,
        prompt,
        resolved,
        model,
        resolved_workdir,
        max_duration_s=duration,
        max_turns=client_cfg.max_turns,
    )
    return json.dumps({"task_id": task_id, "status": "submitted"})


def task_payload(db, executor, task_id: str) -> dict | None:
    """Merge the task's DB row with its ``status.json`` into one view."""
    task = db.get_task(task_id)
    if task is None:
        return None

    state = executor.read_status(task_id) or {}

    def pick(key: str):
        value = state.get(key)
        if value in (None, ""):
            value = task.get(key)
        return value

    return {
        "task_id": task_id,
        "status": state.get("status") or task["status"],
        "exit_code": state.get("exit_code") if state.get("exit_code") is not None else task["exit_code"],
        "session_id": pick("session_id"),
        "created_at": task["created_at"],
        "started_at": pick("started_at"),
        "finished_at": pick("finished_at"),
        "error": pick("error"),
    }


def _read_output(task: dict, executor, task_id: str, limit: int = OUTPUT_LIMIT) -> tuple[str | None, bool]:
    """Read (and truncate) a finished task's stdout, when it exists."""
    path = task.get("output_path") or str(executor.task_dir(task_id) / "stdout.txt")
    try:
        data = Path(path).read_text(errors="replace")
    except OSError:
        return None, False
    if len(data) > limit:
        return data[:limit], True
    return data, False


def task_status_json(client_id: str, db, executor, task_id: str) -> str:
    task = db.get_task(task_id)
    if task is None or task["client_id"] != client_id:
        return err_json("not_found")
    return json.dumps(task_payload(db, executor, task_id))


def task_result_json(client_id: str, db, executor, task_id: str) -> str:
    task = db.get_task(task_id)
    if task is None or task["client_id"] != client_id:
        return err_json("not_found")

    payload = task_payload(db, executor, task_id)
    output = None
    truncated = False
    if payload and payload["status"] in FINISHED_STATES:
        output, truncated = _read_output(task, executor, task_id)
    payload["output"] = output
    payload["truncated"] = truncated
    return json.dumps(payload)


def task_list_json(client_id: str, db, executor, limit: int = 50) -> str:
    limit = max(1, min(int(limit) if limit is not None else 50, 1000))
    rows = db.list_tasks(client_id=client_id, limit=limit)
    tasks = [
        {
            "task_id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "session_id": row["session_id"],
        }
        for row in rows
    ]
    return json.dumps({"tasks": tasks})


def task_cancel_json(client_id: str, db, executor, task_id: str) -> str:
    task = db.get_task(task_id)
    if task is None or task["client_id"] != client_id:
        return err_json("not_found")
    result = executor.cancel(task_id)
    status = (result or {}).get("status") or task["status"]
    return json.dumps({"task_id": task_id, "status": status})


def build_mcp_server(db, executor) -> MCPServer:
    """Register the gateway's MCP tools on a fresh :class:`MCPServer`."""
    mcp = MCPServer(
        name="hermes-mcp-gateway",
        title="Hermes MCP Gateway",
        version="0.1.0",
        description="Governed one-shot Hermes Agent task execution.",
    )

    @mcp.tool(description="Run a one-shot Hermes agent task.")
    def task_run(
        prompt: str,
        toolsets: list[str] | None = None,
        model: str | None = None,
        workdir: str | None = None,
        max_duration_s: int | None = None,
    ) -> str:
        try:
            ctx = get_auth_context()
            if ctx is None:
                return err_json("unauthenticated")
            client_cfg, scopes = ctx
            return run_task(
                client_cfg,
                scopes,
                db,
                executor,
                prompt=prompt,
                toolsets=toolsets,
                model=model,
                workdir=workdir,
                max_duration_s=max_duration_s,
            )
        except Exception as exc:  # noqa: BLE001 - surface a short message, never an uncaught raise
            return err_json(str(exc)[:200])

    @mcp.tool(description="Return the status of one of the caller's tasks.")
    def task_status(task_id: str) -> str:
        try:
            ctx = get_auth_context()
            if ctx is None:
                return err_json("unauthenticated")
            return task_status_json(ctx[0].client_id, db, executor, task_id)
        except Exception as exc:  # noqa: BLE001
            return err_json(str(exc)[:200])

    @mcp.tool(description="Return a task's status plus its (truncated) output.")
    def task_result(task_id: str) -> str:
        try:
            ctx = get_auth_context()
            if ctx is None:
                return err_json("unauthenticated")
            return task_result_json(ctx[0].client_id, db, executor, task_id)
        except Exception as exc:  # noqa: BLE001
            return err_json(str(exc)[:200])

    @mcp.tool(description="List the caller's tasks, newest first.")
    def task_list(limit: int = 50) -> str:
        try:
            ctx = get_auth_context()
            if ctx is None:
                return err_json("unauthenticated")
            return task_list_json(ctx[0].client_id, db, executor, limit)
        except Exception as exc:  # noqa: BLE001
            return err_json(str(exc)[:200])

    @mcp.tool(description="Cancel one of the caller's running tasks.")
    def task_cancel(task_id: str) -> str:
        try:
            ctx = get_auth_context()
            if ctx is None:
                return err_json("unauthenticated")
            return task_cancel_json(ctx[0].client_id, db, executor, task_id)
        except Exception as exc:  # noqa: BLE001
            return err_json(str(exc)[:200])

    return mcp
