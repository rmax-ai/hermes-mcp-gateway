"""Runs one-shot Hermes agent turns as governed background tasks.

``status.json`` under ``<task_dir>/<task_id>/`` and the SQLite audit store are
the source of truth, so tasks survive gateway restarts and can be reconciled.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

SESSION_ID_RE = re.compile(r"session_id:\s*([^\s]+)")
FINISHED_STATES = {"done", "failed", "timeout", "denied", "cancelled"}


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class TaskExecutor:
    def __init__(self, cfg, db) -> None:
        self.cfg = cfg
        self.db = db
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        for client in cfg.clients:
            self._semaphores[client.client_id] = threading.BoundedSemaphore(
                max(1, client.max_concurrency)
            )
        self.reconcile()

    # -- filesystem helpers ---------------------------------------------------
    def task_dir(self, task_id: str) -> Path:
        return Path(self.cfg.hermes.task_dir) / task_id

    def status_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "status.json"

    def _read_status(self, path) -> dict | None:
        try:
            state = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return None
        return state if isinstance(state, dict) else None

    def _write_status(self, path, state: dict) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        os.replace(tmp, path)

    def _status_dict(self, state: dict) -> dict:
        return {
            "status": state.get("status"),
            "exit_code": state.get("exit_code"),
            "session_id": state.get("session_id"),
            "output_path": state.get("output_path"),
        }

    def _extract_session_id(self, task_dir: Path) -> str | None:
        for name in ("stderr.txt", "stdout.txt"):
            try:
                text = (task_dir / name).read_text(errors="replace")
            except OSError:
                continue
            match = SESSION_ID_RE.search(text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _kill_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # -- lifecycle --------------------------------------------------------------
    def reconcile(self) -> None:
        """Mark running tasks left behind over an hour ago as failed."""
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        for task in self.db.list_tasks(limit=100000):
            if task.get("status") != "running" or not task.get("started_at"):
                continue
            try:
                started = datetime.fromisoformat(task["started_at"])
            except ValueError:
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if started < cutoff:
                self.db.update_task(
                    task["id"],
                    status="failed",
                    finished_at=_utcnow(),
                    error="orphaned (gateway restarted)",
                )

    def run(
        self,
        client_cfg,
        task_id: str,
        prompt: str,
        toolsets: list[str],
        model: str | None,
        workdir: str,
        max_duration_s: int | None = None,
        max_turns: int | None = None,
    ) -> dict:
        """Synchronous runner used by tests and by the submit thread."""
        toolsets = list(toolsets or [])
        max_duration_s = max_duration_s if max_duration_s is not None else client_cfg.max_duration_s
        max_turns = max_turns if max_turns is not None else client_cfg.max_turns

        td = self.task_dir(task_id)
        td.mkdir(parents=True, exist_ok=True)
        sf = self.status_path(task_id)
        stdout_file = td / "stdout.txt"
        stderr_file = td / "stderr.txt"
        output_path = str(stdout_file)

        existing = self._read_status(sf)
        if existing and existing.get("status") in FINISHED_STATES:
            return self._status_dict(existing)

        client_id = client_cfg.client_id
        started_at = _utcnow()
        running = {
            "task_id": task_id,
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "exit_code": None,
            "session_id": None,
            "pid": None,
            "output_path": output_path,
        }
        self._write_status(sf, running)

        if self.db.get_task(task_id) is None:
            self.db.create_task(
                task_id,
                client_id,
                prompt,
                ",".join(toolsets),
                model,
                workdir,
                status="running",
            )
        self.db.update_task(task_id, status="running", started_at=started_at)

        with self._semaphore(client_cfg):
            return self._execute(
                task_id=task_id,
                client_id=client_id,
                prompt=prompt,
                toolsets=toolsets,
                model=model,
                workdir=workdir,
                max_duration_s=max_duration_s,
                max_turns=max_turns,
                td=td,
                sf=sf,
                stdout_file=stdout_file,
                stderr_file=stderr_file,
                output_path=output_path,
                started_at=started_at,
            )

    def _execute(
        self,
        *,
        task_id: str,
        client_id: str,
        prompt: str,
        toolsets: list[str],
        model: str | None,
        workdir: str,
        max_duration_s: int,
        max_turns: int,
        td: Path,
        sf: Path,
        stdout_file: Path,
        stderr_file: Path,
        output_path: str,
        started_at: str,
    ) -> dict:
        cmd = [self.cfg.hermes.bin, "-p", self.cfg.hermes.profile, "chat", "-q", prompt]
        if toolsets:
            cmd += ["-t", ",".join(toolsets)]
        if model:
            cmd += ["-m", model]
        cmd += ["--source", f"mcp:{client_id}", "--max-turns", str(max_turns), "-Q"]

        timed_out = False
        try:
            with open(stdout_file, "wb") as out, open(stderr_file, "wb") as err:
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
                running = self._read_status(sf) or {}
                running["pid"] = proc.pid
                self._write_status(sf, running)
            try:
                proc.wait(timeout=max_duration_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_group(proc.pid)
                proc.wait()
        except (OSError, subprocess.SubprocessError) as exc:
            finished_at = _utcnow()
            failed = {
                "task_id": task_id,
                "status": "failed",
                "started_at": started_at,
                "finished_at": finished_at,
                "exit_code": None,
                "session_id": None,
                "pid": None,
                "output_path": output_path,
                "error": str(exc),
            }
            self._write_status(sf, failed)
            self.db.update_task(task_id, status="failed", finished_at=finished_at, error=str(exc))
            return self._status_dict(failed)

        exit_code = proc.returncode
        session_id = self._extract_session_id(td)

        current = self._read_status(sf)
        if current and current.get("status") == "cancelled":
            self.db.update_task(task_id, status="cancelled", finished_at=_utcnow())
            return self._status_dict(current)

        if timed_out:
            status = "timeout"
        elif exit_code == 0:
            status = "done"
        else:
            status = "failed"

        final = {
            "task_id": task_id,
            "status": status,
            "started_at": started_at,
            "finished_at": _utcnow(),
            "exit_code": exit_code,
            "session_id": session_id,
            "pid": proc.pid,
            "output_path": output_path,
        }
        self._write_status(sf, final)
        self.db.update_task(
            task_id,
            status=status,
            finished_at=final["finished_at"],
            exit_code=exit_code,
            session_id=session_id,
            output_path=output_path,
        )
        return self._status_dict(final)

    def submit(
        self,
        client_cfg,
        task_id: str,
        prompt: str,
        toolsets: list[str],
        model: str | None,
        workdir: str,
        max_duration_s: int | None = None,
        max_turns: int | None = None,
    ) -> threading.Thread:
        thread = threading.Thread(
            target=self.run,
            args=(client_cfg, task_id, prompt, toolsets, model, workdir, max_duration_s, max_turns),
            name=f"hermes-exec-{task_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def cancel(self, task_id: str) -> dict | None:
        """Kill a running task's process group and mark it cancelled."""
        sf = self.status_path(task_id)
        state = self._read_status(sf)
        if state is None or state.get("status") != "running":
            return state

        pid = state.get("pid")
        if pid:
            self._kill_group(pid)

        cancelled = {**state, "status": "cancelled", "finished_at": _utcnow()}
        self._write_status(sf, cancelled)
        self.db.update_task(task_id, status="cancelled", finished_at=cancelled["finished_at"])
        return cancelled

    def _semaphore(self, client_cfg) -> threading.BoundedSemaphore:
        if client_cfg.client_id not in self._semaphores:
            self._semaphores[client_cfg.client_id] = threading.BoundedSemaphore(
                max(1, client_cfg.max_concurrency)
            )
        return self._semaphores[client_cfg.client_id]
