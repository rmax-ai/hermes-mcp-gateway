import os

from hermes_mcp_gateway import policy
from hermes_mcp_gateway.config import ClientConfig


def make_client(**overrides):
    base = {
        "client_id": "test-client",
        "secret_hash": "sha256:" + "a" * 64,
        "scopes": ["task:run", "toolset:file", "toolset:web"],
        "max_duration_s": 300,
    }
    base.update(overrides)
    return ClientConfig(**base)


def test_allowed_toolsets_subset():
    client = make_client()
    assert policy.allowed_toolsets(client, ["file"]) == ["file"]


def test_allowed_toolsets_drops_disallowed():
    client = make_client()
    assert policy.allowed_toolsets(client, ["file", "terminal"]) == ["file"]


def test_allowed_toolsets_empty_request():
    client = make_client()
    assert policy.allowed_toolsets(client, []) == []


def test_workdir_allowed(tmp_path):
    client = make_client(workdirs=[str(tmp_path)])
    subdir = tmp_path / "repo"
    subdir.mkdir()
    assert policy.validate_workdir(client, subdir) == os.path.realpath(subdir)


def test_workdir_outside_root(tmp_path):
    client = make_client(workdirs=[str(tmp_path / "allowed")])
    (tmp_path / "allowed").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert policy.validate_workdir(client, outside) is None


def test_workdir_symlink_escape(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    link = allowed / "escape"
    link.symlink_to(outside)

    client = make_client(workdirs=[str(allowed)])
    assert policy.validate_workdir(client, link) is None


def test_workdir_missing(tmp_path):
    client = make_client(workdirs=[str(tmp_path)])
    assert policy.validate_workdir(client, tmp_path / "does-not-exist") is None


def test_model_allowlist():
    client = make_client(models=["model-a", "model-b"])
    assert policy.validate_model(client, "model-a") is True
    assert policy.validate_model(client, "model-c") is False
    assert policy.validate_model(client, None) is False


def test_model_any_when_allowlist_empty():
    client = make_client(models=[])
    assert policy.validate_model(client, "anything-at-all") is True
    assert policy.validate_model(client, None) is False


def test_approval_required_for_terminal():
    client = make_client(requires_approval=True)
    assert policy.requires_task_approval(client, ["terminal"]) is True
    assert policy.requires_task_approval(client, ["file"]) is False
    assert policy.requires_task_approval(client, []) is False


def test_approval_not_required_without_flag():
    client = make_client(requires_approval=False)
    assert policy.requires_task_approval(client, ["terminal"]) is False
