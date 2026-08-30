from datetime import UTC, datetime

import pytest

from hermes_mcp_gateway.db import Database


def make_db(tmp_path):
    return Database(str(tmp_path / "gateway.db"))


def test_task_crud_and_timestamps(tmp_path):
    db = make_db(tmp_path)

    db.create_task("t1", "client-a", "do things", "file,web", "model-x", "/tmp/work")
    task = db.get_task("t1")

    assert task["id"] == "t1"
    assert task["client_id"] == "client-a"
    assert task["prompt"] == "do things"
    assert task["toolsets"] == "file,web"
    assert task["model"] == "model-x"
    assert task["workdir"] == "/tmp/work"
    assert task["status"] == "pending"
    assert datetime.fromisoformat(task["created_at"]).tzinfo is not None
    assert task["started_at"] is None
    assert task["finished_at"] is None

    db.update_task(
        "t1", status="done", exit_code=0, session_id="sess-1", started_at=_now()
    )
    task = db.get_task("t1")
    assert task["status"] == "done"
    assert task["exit_code"] == 0
    assert task["session_id"] == "sess-1"
    assert datetime.fromisoformat(task["started_at"]).tzinfo is not None


def test_update_task_rejects_unknown_field(tmp_path):
    db = make_db(tmp_path)
    db.create_task("t1", "client-a", "p", "", None, "/tmp")
    with pytest.raises(ValueError):
        db.update_task("t1", bogus_field=1)


def test_list_tasks_filters_by_client(tmp_path):
    db = make_db(tmp_path)
    for i in range(3):
        db.create_task(f"a{i}", "client-a", f"p{i}", "", None, "/tmp")
    for i in range(2):
        db.create_task(f"b{i}", "client-b", f"p{i}", "", None, "/tmp")

    assert len(db.list_tasks()) == 5
    assert {t["id"] for t in db.list_tasks(client_id="client-a")} == {"a0", "a1", "a2"}
    assert len(db.list_tasks(client_id="client-b", limit=1)) == 1


def test_approvals_flow(tmp_path):
    db = make_db(tmp_path)
    db.create_approval("t1")

    pending = db.pending_approvals()
    assert len(pending) == 1
    assert pending[0]["task_id"] == "t1"
    assert pending[0]["decision"] == "pending"

    db.decide_approval("t1", "approved", "alice@example.com")
    assert db.pending_approvals() == []

    approval = db.get_approval("t1")
    assert approval["decision"] == "approved"
    assert approval["approver"] == "alice@example.com"
    assert datetime.fromisoformat(approval["decided_at"]).tzinfo is not None


def test_token_issue_audit(tmp_path):
    db = make_db(tmp_path)
    db.log_token_issue("j1", "client-a", "task:run toolset:file", _now(), _now())

    issues = db.list_token_issues()
    assert len(issues) == 1
    assert issues[0]["id"] == "j1"
    assert issues[0]["client_id"] == "client-a"
    assert issues[0]["scopes"] == "task:run toolset:file"


def _now():
    return datetime.now(UTC).isoformat()
