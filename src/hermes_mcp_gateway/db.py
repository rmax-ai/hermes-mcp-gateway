"""SQLite audit store for tasks, approvals, and token issuance.

Uses a connection-per-call pattern so a single :class:`Database` instance is
safe to share across threads without a lock. All timestamps are UTC ISO 8601.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    toolsets TEXT NOT NULL,
    model TEXT,
    workdir TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    session_id TEXT,
    output_path TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    task_id TEXT PRIMARY KEY,
    decision TEXT,
    approver TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS token_issues (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

_TASK_COLUMNS = frozenset(
    {
        "client_id",
        "prompt",
        "toolsets",
        "model",
        "workdir",
        "status",
        "started_at",
        "finished_at",
        "exit_code",
        "session_id",
        "output_path",
        "error",
    }
)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    # -- tasks -------------------------------------------------------------
    def create_task(
        self,
        task_id: str,
        client_id: str,
        prompt: str,
        toolsets: str,
        model: str | None,
        workdir: str | None,
        status: str = "pending",
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO tasks (id, client_id, prompt, toolsets, model, workdir, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, client_id, prompt, toolsets, model, workdir, status, _utcnow()),
            )

    def update_task(self, task_id: str, **fields) -> dict | None:
        unknown = set(fields) - _TASK_COLUMNS
        if unknown:
            raise ValueError(f"unknown task field(s): {', '.join(sorted(unknown))}")
        if not fields:
            return self.get_task(task_id)

        assignments = ", ".join(f"{name} = ?" for name in fields)
        params = list(fields.values()) + [task_id]
        with closing(self._connect()) as conn, conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", params)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, client_id: str | None = None, limit: int = 100) -> list[dict]:
        if client_id is not None:
            sql = "SELECT * FROM tasks WHERE client_id = ? ORDER BY created_at DESC LIMIT ?"
            params = (client_id, limit)
        else:
            sql = "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # -- approvals ---------------------------------------------------------
    def create_approval(self, task_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO approvals (task_id, decision) VALUES (?, 'pending')",
                (task_id,),
            )

    def decide_approval(self, task_id: str, decision: str, approver: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE approvals SET decision = ?, approver = ?, decided_at = ? WHERE task_id = ?",
                (decision, approver, _utcnow(), task_id),
            )

    def get_approval(self, task_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM approvals WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def pending_approvals(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE decision = 'pending' ORDER BY task_id"
            ).fetchall()
        return [dict(row) for row in rows]

    # -- token audit ---------------------------------------------------------
    def log_token_issue(
        self, token_id: str, client_id: str, scopes: str, issued_at: str, expires_at: str
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO token_issues (id, client_id, scopes, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_id, client_id, scopes, issued_at, expires_at),
            )

    def list_token_issues(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM token_issues ORDER BY issued_at DESC").fetchall()
        return [dict(row) for row in rows]
