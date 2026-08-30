"""Operator CLI tests.

Drives the CLI verb functions directly (no subprocess to the CLI itself) against
a tmp config, a tmp ``Database`` and a fake-hermes ``TaskExecutor``, reusing the
fake-hermes shim pattern from :mod:`test_executor`.
"""

import hashlib
import io
import time

import pytest
import yaml

from hermes_mcp_gateway import cli
from hermes_mcp_gateway.config import load_config
from hermes_mcp_gateway.db import Database
from hermes_mcp_gateway.executor import TaskExecutor

SIGNING_KEY = "cli-test-signing-key-words-only-zzzz-not-real"

FAKE_HERMES = """#!/usr/bin/env python3
import sys
import time


def arg(name):
    try:
        i = sys.argv.index(name)
        return sys.argv[i + 1]
    except (ValueError, IndexError):
        return None


prompt = arg("-q") or ""
print("fake-hermes-output")
print("session_id: fake-session-123", file=sys.stderr)
if "sleep" in prompt:
    time.sleep(30)
sys.exit(0)
"""


def write_fake(tmp_path, name="fake-hermes"):
    path = tmp_path / name
    path.write_text(FAKE_HERMES)
    path.chmod(0o755)
    return path


def client_def(client_id, scopes, *, requires_approval=False, workdirs=None):
    return {
        "client_id": client_id,
        "secret_hash": "sha256:" + "0" * 64,
        "scopes": scopes,
        "requires_approval": requires_approval,
        "max_concurrency": 1,
        "max_duration_s": 60,
        "max_turns": 5,
        "workdirs": workdirs if workdirs is not None else ["/tmp"],
        "models": [],
    }


def write_config(tmp_path, clients):
    data = {
        "server": {"bind": "127.0.0.1", "port": 8878},
        "hermes": {
            "bin": "hermes",
            "profile": "mcp-worker",
            "task_dir": str(tmp_path / "tasks"),
            "db_path": str(tmp_path / "gateway.db"),
            "timeout_s": 60,
        },
        "auth": {
            "issuer": "hermes-mcp-gateway",
            "signing_secret_env": "HERMES_MCP_GATEWAY_SIGNING_KEY",
            "token_ttl_s": 600,
        },
        "clients": clients,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def make_env(tmp_path, clients):
    cfg = load_config(write_config(tmp_path, clients))
    cfg.hermes.bin = str(write_fake(tmp_path))
    db = Database(cfg.hermes.db_path)
    executor = TaskExecutor(cfg, db)
    return cfg, db, executor


def wait_task_status(executor, db, task_id, states, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = db.get_task(task_id)
        if task and task["status"] in states:
            return task
        time.sleep(0.05)
    return db.get_task(task_id)


# -- clients ------------------------------------------------------------------


def test_clients_list(tmp_path):
    cfg, _db, _ex = make_env(
        tmp_path,
        [
            client_def("research", ["task:run", "toolset:file", "toolset:web"]),
            client_def("terminal-ops", ["task:run", "toolset:terminal"], requires_approval=True),
        ],
    )
    out = cli.cmd_clients_list(cfg)
    lines = out.splitlines()

    assert lines[0].split("\t")[0] == "client_id"
    assert "research" in out
    assert "terminal-ops" in out
    assert "task:run toolset:file toolset:web" in out
    assert "task:run toolset:terminal" in out
    assert "false" in out and "true" in out


def test_clients_hash():
    secret = "correct-horse-battery-staple"
    expected = "sha256:" + hashlib.sha256(secret.encode()).hexdigest()
    assert cli.hash_client_secret(secret) == expected


def test_clients_hash_stdin_mode(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("secret-from-stdin\n"))
    assert cli._read_secret("-") == "secret-from-stdin"
    assert cli._read_secret("literal") == "literal"
    expected = "sha256:" + hashlib.sha256(b"secret-from-stdin").hexdigest()
    assert cli.hash_client_secret("secret-from-stdin") == expected


# -- approvals ----------------------------------------------------------------


def test_approvals_approve_spawns_and_runs(tmp_path, capsys):
    cfg, db, ex = make_env(
        tmp_path,
        [
            client_def(
                "apv",
                ["task:run", "toolset:terminal"],
                requires_approval=True,
                workdirs=[str(tmp_path)],
            )
        ],
    )
    db.create_task(
        "t1", "apv", "run a terminal command", "terminal", None, str(tmp_path),
        status="pending_approval",
    )
    db.create_approval("t1")

    pending = cli.cmd_approvals_pending(db)
    assert "t1" in pending
    assert "apv" in pending
    assert "run a terminal command" in pending

    out = cli.cmd_approvals_approve(cfg, db, ex, "t1")
    assert out.splitlines()[0].split("\t") == ["task_id", "client_id", "status"]
    assert "t1" in out and "done" in out

    task = wait_task_status(ex, db, "t1", {"done"})
    assert task["status"] == "done"
    assert task["session_id"] == "fake-session-123"

    approval = db.get_approval("t1")
    assert approval["decision"] == "approved"
    assert approval["approver"] == "cli"

    # Approving an already-approved task is a no-op with a warning.
    assert cli.cmd_approvals_approve(cfg, db, ex, "t1") == ""
    assert "nothing to do" in capsys.readouterr().err


def test_approvals_deny_with_and_without_reason(tmp_path):
    _cfg, db, _ex = make_env(
        tmp_path,
        [
            client_def(
                "apv",
                ["task:run", "toolset:terminal"],
                requires_approval=True,
                workdirs=[str(tmp_path)],
            )
        ],
    )
    db.create_task(
        "t2", "apv", "danger", "terminal", None, str(tmp_path), status="pending_approval"
    )
    db.create_approval("t2")

    out = cli.cmd_approvals_deny(db, "t2", "no shell today")
    assert "t2" in out and "denied" in out

    task = db.get_task("t2")
    assert task["status"] == "denied"
    assert task["error"] == "no shell today"
    assert db.get_approval("t2")["decision"] == "denied"

    db.create_task(
        "t3", "apv", "danger again", "terminal", None, str(tmp_path), status="pending_approval"
    )
    db.create_approval("t3")
    cli.cmd_approvals_deny(db, "t3", None)
    assert db.get_task("t3")["status"] == "denied"
    assert db.get_task("t3")["error"] == "denied by operator"


# -- tasks --------------------------------------------------------------------


def test_tasks_list_and_show(tmp_path):
    cfg, db, _ex = make_env(
        tmp_path,
        [
            client_def("c1", ["task:run", "toolset:file"]),
            client_def("c2", ["task:run", "toolset:file"]),
        ],
    )
    db.create_task("t-c1", "c1", "prompt-1", "file", "m-1", "/tmp", status="pending")
    db.create_task("t-c2", "c2", "prompt-2", "file", "m-2", "/tmp", status="done")

    out_all = cli.cmd_tasks_list(cfg, db, None, 100)
    assert out_all.splitlines()[0].split("\t")[0] == "id"
    assert "t-c1" in out_all and "t-c2" in out_all

    out_c1 = cli.cmd_tasks_list(cfg, db, "c1", 100)
    assert "t-c1" in out_c1 and "t-c2" not in out_c1

    shown = cli.cmd_tasks_show(db, "t-c1")
    assert "t-c1" in shown
    assert "prompt-1" in shown
    assert "output_path" in shown
    assert "approval_decision" in shown

    with pytest.raises(cli.CliError):
        cli.cmd_tasks_list(cfg, db, "nobody", 100)


def test_tasks_cancel_running(tmp_path):
    cfg, db, ex = make_env(
        tmp_path,
        [client_def("c1", ["task:run", "toolset:file"], workdirs=[str(tmp_path)])],
    )
    client = cfg.clients[0]
    db.create_task("t-c", "c1", "sleep please", "file", None, str(tmp_path), status="pending")
    ex.submit(client, "t-c", "sleep please", ["file"], None, str(tmp_path))

    deadline = time.time() + 10
    while time.time() < deadline:
        state = ex.read_status("t-c") or {}
        if state.get("status") == "running" and state.get("pid"):
            break
        time.sleep(0.05)

    out = cli.cmd_tasks_cancel(db, ex, "t-c")
    assert "t-c" in out and "cancelled" in out

    task = wait_task_status(ex, db, "t-c", {"cancelled"})
    assert task["status"] == "cancelled"


def test_tasks_cancel_finished_is_noop(tmp_path, capsys):
    _cfg, db, ex = make_env(tmp_path, [client_def("c1", ["task:run", "toolset:file"])])
    db.create_task("t-d", "c1", "p", "file", None, "/tmp", status="done")

    assert cli.cmd_tasks_cancel(db, ex, "t-d") == ""
    assert "nothing to cancel" in capsys.readouterr().err


# -- token issue --------------------------------------------------------------


def test_token_issue_requires_env_gate(tmp_path, monkeypatch):
    cfg, db, _ex = make_env(tmp_path, [client_def("c1", ["task:run", "toolset:file"])])
    monkeypatch.delenv(cli.DEV_TOKEN_ENV, raising=False)
    with pytest.raises(cli.CliError):
        cli.cmd_token_issue(cfg, db, "c1", None)


def test_token_issue_mints_and_audits(tmp_path, monkeypatch):
    cfg, db, _ex = make_env(tmp_path, [client_def("c1", ["task:run", "toolset:file"])])
    monkeypatch.setenv(cli.DEV_TOKEN_ENV, "1")
    monkeypatch.setenv("HERMES_MCP_GATEWAY_SIGNING_KEY", SIGNING_KEY)

    token = cli.cmd_token_issue(cfg, db, "c1", None)
    assert token and token.count(".") == 2

    narrowed = cli.cmd_token_issue(cfg, db, "c1", ["task:run"])
    assert narrowed and narrowed != token

    scopes = {issue["scopes"] for issue in db.list_token_issues()}
    assert scopes == {"task:run", "task:run toolset:file"}
    assert {issue["client_id"] for issue in db.list_token_issues()} == {"c1"}


def test_token_issue_unknown_client(tmp_path, monkeypatch):
    cfg, db, _ex = make_env(tmp_path, [client_def("c1", ["task:run"])])
    monkeypatch.setenv(cli.DEV_TOKEN_ENV, "1")
    monkeypatch.setenv("HERMES_MCP_GATEWAY_SIGNING_KEY", SIGNING_KEY)
    with pytest.raises(cli.CliError):
        cli.cmd_token_issue(cfg, db, "nobody", None)


# -- error exits --------------------------------------------------------------


def test_unknown_task_raises(tmp_path):
    cfg, db, ex = make_env(tmp_path, [client_def("c1", ["task:run", "toolset:file"])])
    with pytest.raises(cli.CliError):
        cli.cmd_tasks_show(db, "missing")
    with pytest.raises(cli.CliError):
        cli.cmd_approvals_deny(db, "missing", None)
    with pytest.raises(cli.CliError):
        cli.cmd_approvals_approve(cfg, db, ex, "missing")


def test_unknown_task_nonzero_via_main(tmp_path, capsys):
    cfg_path = write_config(tmp_path, [client_def("c1", ["task:run"])])
    rc = cli.main(["--config", str(cfg_path), "tasks", "show", "missing"])
    assert rc == 1
    assert "unknown task" in capsys.readouterr().err
