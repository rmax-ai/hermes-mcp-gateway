"""Console entry point and operator CLI.

``main`` owns argument parsing: bare invocation and ``serve`` start the gateway
exactly as before, while the operator verbs (``clients``, ``approvals``,
``tasks``, ``token``) read and act on the same config/DB/executor pieces
without going through the HTTP layer.

Verb functions are plain (no subprocess, no argparse coupling) so tests can
drive them directly with a temp config, a tmp ``Database`` and a fake-hermes
executor.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import UTC, datetime

from .app_factory import create_app
from .auth import issue_token, resolve_signing_key, validate_token
from .config import ConfigError, load_config
from .db import Database
from .executor import TaskExecutor

DEV_TOKEN_ENV = "HERMES_MCP_GATEWAY_ALLOW_DEV_TOKEN"
APPROVER = "cli"


class CliError(Exception):
    """A user-facing operator error (rendered as ``error: ...``, exit 1)."""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _iso_epoch(timestamp) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=UTC).isoformat()


def _cell(value) -> str:
    return "" if value is None else str(value)


def _table(headers: list[str], rows: list[list]) -> str:
    lines = ["\t".join(headers)]
    lines.extend("\t".join(_cell(v) for v in row) for row in rows)
    return "\n".join(lines)


def _load_cfg(config_path: str | None):
    try:
        return load_config(config_path)
    except ConfigError as exc:
        raise CliError(str(exc)) from exc


def _find_client(cfg, client_id: str):
    for client in cfg.clients:
        if client.client_id == client_id:
            return client
    raise CliError(f"unknown client {client_id!r}")


# -- verb functions (no argparse, no subprocess) -------------------------------


def hash_client_secret(secret: str) -> str:
    """Return ``sha256:<hex>`` of a client secret."""
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def cmd_clients_list(cfg) -> str:
    headers = [
        "client_id",
        "scopes",
        "requires_approval",
        "max_concurrency",
        "max_duration_s",
        "workdirs",
        "models",
    ]
    rows = [
        [
            client.client_id,
            " ".join(client.scopes),
            "true" if client.requires_approval else "false",
            client.max_concurrency,
            client.max_duration_s,
            ",".join(client.workdirs),
            ",".join(client.models),
        ]
        for client in cfg.clients
    ]
    return _table(headers, rows)


def cmd_approvals_pending(db) -> str:
    headers = ["task_id", "client_id", "created_at", "prompt"]
    rows = []
    for approval in db.pending_approvals():
        task = db.get_task(approval["task_id"])
        if task is None:
            continue
        prompt = task.get("prompt") or ""
        rows.append([task["id"], task["client_id"], task["created_at"], prompt[:60]])
    return _table(headers, rows)


def cmd_approvals_approve(cfg, db, executor, task_id: str) -> str:
    """Approve a pending task, spawn it, and wait for it to finish.

    The executor runs tasks on daemon threads; a short-lived CLI process would
    otherwise exit and kill the thread mid-task. Joining here keeps the task's
    final state (and stdout) recorded before the operator command returns.
    """
    task = db.get_task(task_id)
    if task is None:
        raise CliError(f"unknown task {task_id!r}")

    if task["status"] != "pending_approval":
        print(
            f"warning: task {task_id} is {task['status']} (not pending approval); nothing to do",
            file=sys.stderr,
        )
        return ""

    client = _find_client(cfg, task["client_id"])
    db.decide_approval(task_id, "approved", APPROVER)
    toolsets = [name for name in (task["toolsets"] or "").split(",") if name]

    db.update_task(task_id, status="running")
    thread = executor.submit(
        client,
        task_id,
        task["prompt"],
        toolsets,
        task["model"],
        task["workdir"],
        max_duration_s=client.max_duration_s,
        max_turns=client.max_turns,
    )
    thread.join()

    final = db.get_task(task_id) or {}
    return _table(
        ["task_id", "client_id", "status"],
        [[task_id, client.client_id, final.get("status", "running")]],
    )


def cmd_approvals_deny(db, task_id: str, reason: str | None) -> str:
    """Deny a pending task; no-op with a warning if not pending."""
    task = db.get_task(task_id)
    if task is None:
        raise CliError(f"unknown task {task_id!r}")

    if task["status"] != "pending_approval":
        print(
            f"warning: task {task_id} is {task['status']} (not pending approval); nothing to do",
            file=sys.stderr,
        )
        return ""

    db.decide_approval(task_id, "denied", APPROVER)
    error = reason or "denied by operator"
    db.update_task(task_id, status="denied", error=error, finished_at=_utcnow())
    return _table(["task_id", "status", "error"], [[task_id, "denied", error]])


def cmd_tasks_list(cfg, db, client_id: str | None, limit: int) -> str:
    if client_id is not None:
        _find_client(cfg, client_id)
    headers = ["id", "client_id", "status", "model", "created_at", "finished_at", "session_id"]
    rows = [
        [
            task["id"],
            task["client_id"],
            task["status"],
            task["model"],
            task["created_at"],
            task["finished_at"],
            task["session_id"],
        ]
        for task in db.list_tasks(client_id=client_id, limit=limit)
    ]
    return _table(headers, rows)


def cmd_tasks_show(db, task_id: str) -> str:
    task = db.get_task(task_id)
    if task is None:
        raise CliError(f"unknown task {task_id!r}")

    approval = db.get_approval(task_id) or {}
    fields = [
        ("id", task.get("id")),
        ("client_id", task.get("client_id")),
        ("prompt", task.get("prompt")),
        ("toolsets", task.get("toolsets")),
        ("model", task.get("model")),
        ("workdir", task.get("workdir")),
        ("status", task.get("status")),
        ("created_at", task.get("created_at")),
        ("started_at", task.get("started_at")),
        ("finished_at", task.get("finished_at")),
        ("exit_code", task.get("exit_code")),
        ("session_id", task.get("session_id")),
        ("output_path", task.get("output_path")),
        ("error", task.get("error")),
        ("approval_decision", approval.get("decision")),
        ("approver", approval.get("approver")),
        ("decided_at", approval.get("decided_at")),
    ]
    return _table(["field", "value"], [[name, value] for name, value in fields])


def cmd_tasks_cancel(db, executor, task_id: str) -> str:
    task = db.get_task(task_id)
    if task is None:
        raise CliError(f"unknown task {task_id!r}")

    if task["status"] != "running":
        print(f"warning: task {task_id} is {task['status']}; nothing to cancel", file=sys.stderr)
        return ""

    executor.cancel(task_id)
    return _table(["task_id", "status"], [[task_id, "cancelled"]])


def cmd_token_issue(cfg, db, client_id: str, scopes: list[str] | None) -> str:
    if os.environ.get(DEV_TOKEN_ENV) != "1":
        raise CliError(
            f"token issue is a dev helper and is disabled; set {DEV_TOKEN_ENV}=1 to enable"
        )

    client = _find_client(cfg, client_id)
    if scopes:
        for scope in scopes:
            if scope not in client.scopes:
                raise CliError(f"scope {scope!r} is not available to client {client_id!r}")
        granted = list(scopes)
    else:
        granted = list(client.scopes)

    signing_key = resolve_signing_key(cfg.auth.signing_secret_env)
    token = issue_token(client.client_id, granted, signing_key, cfg.auth.issuer, cfg.auth.token_ttl_s)
    claims = validate_token(token, signing_key, cfg.auth.issuer)
    db.log_token_issue(
        claims["jti"],
        client.client_id,
        claims["scope"],
        _iso_epoch(claims["iat"]),
        _iso_epoch(claims["exp"]),
    )
    return token


# -- argparse wiring ----------------------------------------------------------


def _read_secret(value: str) -> str:
    """Resolve a secret argument: ``-`` reads one stdin line without echoing."""
    if value != "-":
        return value
    line = sys.stdin.readline()
    return line.rstrip("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-mcp-gateway",
        description="Operate the Hermes MCP Gateway.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "commands:\n"
            "  serve                                   run the gateway server (the default)\n"
            "  clients list                            list configured clients and policy\n"
            "  clients hash <secret|- >                sha256 of a client secret\n"
            "  approvals pending                       tasks waiting for approval\n"
            "  approvals approve <task_id>             approve and spawn a pending task\n"
            "  approvals deny <task_id> [reason]       deny a pending task\n"
            "  tasks list [--client ID] [--limit N]    list tasks, newest first\n"
            "  tasks show <task_id>                    task row + approval state + output path\n"
            "  tasks cancel <task_id>                  cancel a running task\n"
            "  token issue <client_id> [scopes...]     dev-only token issuance (env-gated)\n"
            "\n"
            "Every command reads --config like serve does."
        ),
    )
    parser.add_argument("--config", help="path to the gateway YAML config file")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and exit (does not start the server)",
    )

    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.add_parser("serve", help="run the gateway server (the default)")

    clients = sub.add_parser("clients", help="inspect configured clients")
    clients_sub = clients.add_subparsers(dest="clients_command", metavar="command", required=True)
    clients_sub.add_parser("list", help="list configured clients and their policy")
    clients_hash = clients_sub.add_parser("hash", help="print sha256 of a client secret")
    clients_hash.add_argument("secret", help="secret to hash; '-' reads a line from stdin")

    approvals = sub.add_parser("approvals", help="task-level human-in-the-loop approvals")
    approvals_sub = approvals.add_subparsers(
        dest="approvals_command", metavar="command", required=True
    )
    approvals_sub.add_parser("pending", help="list tasks waiting for approval")
    approve = approvals_sub.add_parser("approve", help="approve and spawn a pending task")
    approve.add_argument("task_id")
    deny = approvals_sub.add_parser("deny", help="deny a pending task")
    deny.add_argument("task_id")
    deny.add_argument("reason", nargs="?", default=None, help="why the task was denied")

    tasks = sub.add_parser("tasks", help="inspect and cancel tasks")
    tasks_sub = tasks.add_subparsers(dest="tasks_command", metavar="command", required=True)
    tasks_list = tasks_sub.add_parser("list", help="list tasks, newest first")
    tasks_list.add_argument("--client", dest="client_id", help="filter by client id")
    tasks_list.add_argument("--limit", type=int, default=None, help="max rows (default 100)")
    tasks_show = tasks_sub.add_parser("show", help="show one task with approval state")
    tasks_show.add_argument("task_id")
    tasks_cancel = tasks_sub.add_parser("cancel", help="cancel a running task")
    tasks_cancel.add_argument("task_id")

    token = sub.add_parser("token", help="token inspection and dev issuance")
    token_sub = token.add_subparsers(dest="token_command", metavar="command", required=True)
    token_issue = token_sub.add_parser("issue", help="dev helper: issue a token for a client")
    token_issue.add_argument("client_id")
    token_issue.add_argument("scopes", nargs="*", help="scopes (default: the client's scopes)")

    return parser


def _dispatch(args) -> int:
    command = args.command
    if command in (None, "serve"):
        cfg = _load_cfg(args.config)
        if args.check:
            print("config OK")
            return 0
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

    if command == "clients":
        if args.clients_command == "hash":
            print(hash_client_secret(_read_secret(args.secret)))
            return 0
        cfg = _load_cfg(args.config)
        print(cmd_clients_list(cfg))
        return 0

    if command == "approvals":
        cfg = _load_cfg(args.config)
        db = Database(cfg.hermes.db_path)
        if args.approvals_command == "pending":
            print(cmd_approvals_pending(db))
            return 0
        if args.approvals_command == "approve":
            executor = TaskExecutor(cfg, db)
            output = cmd_approvals_approve(cfg, db, executor, args.task_id)
            if output:
                print(output)
            return 0
        # deny
        output = cmd_approvals_deny(db, args.task_id, args.reason)
        if output:
            print(output)
        return 0

    if command == "tasks":
        cfg = _load_cfg(args.config)
        db = Database(cfg.hermes.db_path)
        if args.tasks_command == "list":
            limit = args.limit if args.limit is not None else 100
            print(cmd_tasks_list(cfg, db, args.client_id, limit))
            return 0
        if args.tasks_command == "show":
            print(cmd_tasks_show(db, args.task_id))
            return 0
        executor = TaskExecutor(cfg, db)
        output = cmd_tasks_cancel(db, executor, args.task_id)
        if output:
            print(output)
        return 0

    if command == "token":
        cfg = _load_cfg(args.config)
        db = Database(cfg.hermes.db_path)
        print(cmd_token_issue(cfg, db, args.client_id, args.scopes or None))
        return 0

    print(f"error: unknown command {command!r}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
