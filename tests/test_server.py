"""End-to-end tests for the HTTP layer and MCP tools.

These tests drive a real ``create_app`` instance through Starlette's
``TestClient`` (which runs the app lifespan, including the MCP session
manager) against a fake ``hermes`` executable.
"""

import base64
import hashlib
import json
import time

from starlette.testclient import TestClient

from hermes_mcp_gateway import auth as auth_mod
from hermes_mcp_gateway.app_factory import create_app
from hermes_mcp_gateway.config import (
    AuthConfig,
    ClientConfig,
    GatewayConfig,
    HermesConfig,
    ServerConfig,
)
from hermes_mcp_gateway.db import Database
from hermes_mcp_gateway.executor import FINISHED_STATES, TaskExecutor

SIGNING_KEY = "test-signing-key-abcdefghij-0123456789"
ISSUER = "hermes-mcp-gateway"

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
with open("argv.txt", "w") as f:
    f.write(chr(10).join(sys.argv))
print("fake-hermes-output")
print("session_id: fake-session-123", file=sys.stderr)
if "exit-nonzero" in prompt:
    sys.exit(1)
if "sleep" in prompt:
    time.sleep(30)
sys.exit(0)
"""


def write_fake(tmp_path, name="fake-hermes", body=FAKE_HERMES):
    path = tmp_path / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def make_client(
    client_id,
    secret,
    scopes,
    *,
    requires_approval=False,
    workdirs=None,
    models=None,
):
    return ClientConfig(
        client_id=client_id,
        secret_hash="sha256:" + hashlib.sha256(secret.encode()).hexdigest(),
        scopes=scopes,
        max_duration_s=60,
        requires_approval=requires_approval,
        workdirs=workdirs or ["/tmp"],
        models=models if models is not None else [],
    )


def make_app(tmp_path, clients):
    cfg = GatewayConfig(
        server=ServerConfig(),
        hermes=HermesConfig(
            task_dir=str(tmp_path / "tasks"),
            db_path=str(tmp_path / "gw.db"),
        ),
        auth=AuthConfig(),
        clients=clients,
    )
    cfg.hermes.bin = str(write_fake(tmp_path))

    db = Database(cfg.hermes.db_path)
    executor = TaskExecutor(cfg, db)
    app = create_app(cfg, db, executor, signing_key=SIGNING_KEY)
    return cfg, db, executor, app


def get_token(client, client_id, secret, scope=None):
    data = {"grant_type": "client_credentials"}
    if scope is not None:
        data["scope"] = scope
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    response = client.post(
        "/token",
        data=data,
        headers={"Authorization": f"Basic {basic}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sse_payload(body):
    lines = []
    for line in body.splitlines():
        if line.startswith("data:"):
            lines.append(line[len("data:"):].lstrip())
    return "\n".join(lines)


class Rpc:
    """Tiny JSON-RPC-over-TestClient driver for the MCP Streamable HTTP API."""

    def __init__(self, client, token):
        self.client = client
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.session_id = None
        self._next_id = 0

    def _post(self, payload):
        headers = dict(self.headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        response = self.client.post("/mcp", headers=headers, content=json.dumps(payload))
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        return response

    def initialize(self):
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            }
        )
        assert response.status_code == 200, response.text
        self.session_id = response.headers["mcp-session-id"]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, method, params):
        self._next_id += 1
        response = self._post(
            {"jsonrpc": "2.0", "id": self._next_id + 100, "method": method, "params": params}
        )
        assert response.status_code == 200, response.text
        return json.loads(_sse_payload(response.text))["result"]

    def call_tool(self, name, arguments):
        result = self.call("tools/call", {"name": name, "arguments": arguments})
        assert result["isError"] is False, result
        return json.loads(result["content"][0]["text"])


def wait_for_status(rpc, task_id, states, timeout=10.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = rpc.call_tool("task_status", {"task_id": task_id})
        if last.get("status") in states:
            return last
        time.sleep(0.05)
    return last


# -- plain HTTP routes --------------------------------------------------------


def test_healthz(tmp_path):
    _cfg, _db, _ex, app = make_app(tmp_path, [make_client("c1", "sec-c1-word", ["task:run"])])
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_well_known_documents(tmp_path):
    _cfg, _db, _ex, app = make_app(tmp_path, [make_client("c1", "sec-c1-word", ["task:run"])])
    with TestClient(app) as client:
        oas = client.get("/.well-known/oauth-authorization-server")
        opr = client.get("/.well-known/oauth-protected-resource")

    assert oas.status_code == 200
    assert oas.json()["token_endpoint"].endswith("/token")
    assert "task:run" in oas.json()["scopes_supported"]
    assert "client_credentials" in oas.json()["grant_types_supported"]

    assert opr.status_code == 200
    assert opr.json()["resource"].endswith("/mcp")
    assert opr.json()["scopes_supported"] == oas.json()["scopes_supported"]


def test_token_basic_and_post(tmp_path):
    secret = "sec-c1-word"
    _cfg, _db, _ex, app = make_app(
        tmp_path, [make_client("c1", secret, ["task:run", "toolset:file"])]
    )
    with TestClient(app) as client:
        basic = get_token(client, "c1", secret)

        post = client.post(
            "/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "c1",
                "client_secret": secret,
            },
        )
        assert post.status_code == 200
        post_json = post.json()

    for body in (basic, post_json):
        assert body["access_token"]
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] > 0
        assert body["scope"].split() == ["task:run", "toolset:file"]

    # Audit trail records one row per issued token with the right client.
    issues = _db.list_token_issues()
    assert len(issues) == 2
    assert {i["client_id"] for i in issues} == {"c1"}


def test_token_wrong_secret(tmp_path):
    _cfg, _db, _ex, app = make_app(tmp_path, [make_client("c1", "sec-c1-word", ["task:run"])])
    with TestClient(app) as client:
        basic = base64.b64encode(b"c1:wrong-secret-word").decode()
        response = client.post(
            "/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
        )
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


def test_token_unsupported_grant(tmp_path):
    _cfg, _db, _ex, app = make_app(tmp_path, [make_client("c1", "sec-c1-word", ["task:run"])])
    with TestClient(app) as client:
        response = client.post(
            "/token",
            data={"grant_type": "password", "client_id": "c1", "client_secret": "sec-c1-word"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"


def test_token_scope_narrowing(tmp_path):
    secret = "sec-c1-word"
    _cfg, _db, _ex, app = make_app(
        tmp_path,
        [make_client("c1", secret, ["task:run", "toolset:file", "toolset:web"])],
    )
    with TestClient(app) as client:
        body = get_token(client, "c1", secret, scope="task:run toolset:file")
    assert sorted(body["scope"].split()) == ["task:run", "toolset:file"]


def test_mcp_without_token(tmp_path):
    _cfg, _db, _ex, app = make_app(tmp_path, [make_client("c1", "sec-c1-word", ["task:run"])])
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={"Content-Type": "application/json"},
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        )
    assert response.status_code == 401
    www_authenticate = response.headers["www-authenticate"]
    assert 'error="invalid_token"' in www_authenticate
    assert "/.well-known/oauth-protected-resource" in www_authenticate


def test_mcp_forged_and_expired_token(tmp_path):
    _cfg, _db, _ex, app = make_app(tmp_path, [make_client("c1", "sec-c1-word", ["task:run"])])
    forged = auth_mod.issue_token(
        "c1", ["task:run"], "wrong-signing-key-words-only-zzzz", ISSUER, 300
    )
    expired = auth_mod.issue_token("c1", ["task:run"], SIGNING_KEY, ISSUER, -10)
    unknown = auth_mod.issue_token("ghost-client", ["task:run"], SIGNING_KEY, ISSUER, 300)

    with TestClient(app) as client:
        for bad_token in (forged, expired, unknown):
            response = client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {bad_token}",
                    "Content-Type": "application/json",
                },
                content=json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                ),
            )
            assert response.status_code == 401
            assert 'error="invalid_token"' in response.headers["www-authenticate"]


# -- MCP tool policy branches -------------------------------------------------


def _init_rpc(client, client_id, secret, scope=None):
    body = get_token(client, client_id, secret, scope=scope)
    rpc = Rpc(client, body["access_token"])
    rpc.initialize()
    return rpc


def test_task_run_without_task_run_scope(tmp_path):
    secret = "sec-c2-word"
    _cfg, _db, _ex, app = make_app(
        tmp_path, [make_client("c2", secret, ["toolset:file"])]
    )
    with TestClient(app) as client:
        rpc = _init_rpc(client, "c2", secret)
        result = rpc.call_tool("task_run", {"prompt": "hello"})
    assert result["error"] == "forbidden"
    assert result["missing_scopes"] == ["task:run"]


def test_task_run_toolsets_outside_scopes(tmp_path):
    secret = "sec-c3-word"
    _cfg, _db, _ex, app = make_app(
        tmp_path,
        [make_client("c3", secret, ["task:run", "toolset:file"], workdirs=[str(tmp_path)])],
    )
    with TestClient(app) as client:
        rpc = _init_rpc(client, "c3", secret)
        result = rpc.call_tool("task_run", {"prompt": "hello", "toolsets": ["terminal"]})
    assert result["error"] == "forbidden"
    assert result["missing_scopes"] == ["toolset:terminal"]


def test_task_run_invalid_workdir(tmp_path):
    secret = "sec-c4-word"
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _cfg, _db, _ex, app = make_app(
        tmp_path,
        [make_client("c4", secret, ["task:run", "toolset:file"], workdirs=[str(allowed)])],
    )
    with TestClient(app) as client:
        rpc = _init_rpc(client, "c4", secret)
        result = rpc.call_tool("task_run", {"prompt": "hello", "workdir": str(outside)})
    assert result["error"] == "invalid_workdir"
    assert result["workdir"] == str(outside)


def test_task_run_invalid_model(tmp_path):
    secret = "sec-c5-word"
    _cfg, _db, _ex, app = make_app(
        tmp_path,
        [
            make_client(
                "c5",
                secret,
                ["task:run", "toolset:file"],
                workdirs=[str(tmp_path)],
                models=["model-allowed"],
            )
        ],
    )
    with TestClient(app) as client:
        rpc = _init_rpc(client, "c5", secret)
        result = rpc.call_tool("task_run", {"prompt": "hello", "model": "model-denied"})
    assert result["error"] == "invalid_model"
    assert result["model"] == "model-denied"


def test_task_run_happy_path_and_result(tmp_path):
    secret = "sec-c1-word"
    _cfg, db, _ex, app = make_app(
        tmp_path,
        [make_client("c1", secret, ["task:run", "toolset:file"], workdirs=[str(tmp_path)])],
    )
    with TestClient(app) as client:
        rpc = _init_rpc(client, "c1", secret)
        submitted = rpc.call_tool("task_run", {"prompt": "hello", "toolsets": ["file"]})
        assert submitted["status"] == "submitted"
        task_id = submitted["task_id"]

        payload = wait_for_status(rpc, task_id, FINISHED_STATES)
        assert payload["status"] == "done"
        assert payload["session_id"] == "fake-session-123"

        result = rpc.call_tool("task_result", {"task_id": task_id})
        assert result["status"] == "done"
        assert "fake-hermes-output" in result["output"]

    assert db.get_task(task_id)["status"] == "done"


def test_task_list_and_status_are_scoped(tmp_path):
    secret_a = "sec-ca-word"
    secret_b = "sec-cb-word"
    _cfg, _db, _ex, app = make_app(
        tmp_path,
        [
            make_client("ca", secret_a, ["task:run", "toolset:file"], workdirs=[str(tmp_path)]),
            make_client("cb", secret_b, ["task:run", "toolset:file"], workdirs=[str(tmp_path)]),
        ],
    )
    with TestClient(app) as client:
        rpc_a = _init_rpc(client, "ca", secret_a)
        rpc_b = _init_rpc(client, "cb", secret_b)

        submitted = rpc_a.call_tool("task_run", {"prompt": "hello", "toolsets": ["file"]})
        task_id = submitted["task_id"]
        wait_for_status(rpc_a, task_id, FINISHED_STATES)

        # B cannot see or read A's task, and its own list is empty.
        assert rpc_b.call_tool("task_status", {"task_id": task_id}) == {"error": "not_found"}
        assert rpc_b.call_tool("task_result", {"task_id": task_id}) == {"error": "not_found"}
        assert rpc_b.call_tool("task_list", {})["tasks"] == []

        # A's list contains the task (newest first).
        ids = [t["task_id"] for t in rpc_a.call_tool("task_list", {})["tasks"]]
        assert task_id in ids


def test_task_cancel_enforcement(tmp_path):
    secret_a = "sec-ca-word"
    secret_b = "sec-cb-word"
    _cfg, db, executor, app = make_app(
        tmp_path,
        [
            make_client("ca", secret_a, ["task:run", "toolset:file"], workdirs=[str(tmp_path)]),
            make_client("cb", secret_b, ["task:run", "toolset:file"], workdirs=[str(tmp_path)]),
        ],
    )
    with TestClient(app) as client:
        rpc_a = _init_rpc(client, "ca", secret_a)
        rpc_b = _init_rpc(client, "cb", secret_b)

        submitted = rpc_a.call_tool("task_run", {"prompt": "sleep please", "toolsets": ["file"]})
        task_id = submitted["task_id"]

        # Wait until the runner has recorded a pid (it is actually running).
        deadline = time.time() + 10
        while time.time() < deadline:
            state = executor.read_status(task_id) or {}
            if state.get("status") == "running" and state.get("pid"):
                break
            time.sleep(0.05)

        # B cannot cancel A's task.
        assert rpc_b.call_tool("task_cancel", {"task_id": task_id}) == {"error": "not_found"}

        # A can, and the task transitions to cancelled.
        cancelled = rpc_a.call_tool("task_cancel", {"task_id": task_id})
        assert cancelled["task_id"] == task_id
        assert cancelled["status"] == "cancelled"

    assert db.get_task(task_id)["status"] == "cancelled"


def test_requires_approval_queues_without_spawn(tmp_path):
    secret = "sec-c6-word"
    _cfg, db, executor, app = make_app(
        tmp_path,
        [
            make_client(
                "c6",
                secret,
                ["task:run", "toolset:terminal"],
                requires_approval=True,
                workdirs=[str(tmp_path)],
            )
        ],
    )
    with TestClient(app) as client:
        rpc = _init_rpc(client, "c6", secret)
        result = rpc.call_tool("task_run", {"prompt": "hello", "toolsets": ["terminal"]})
        assert result["status"] == "pending_approval"
        task_id = result["task_id"]

    task = db.get_task(task_id)
    assert task["status"] == "pending_approval"
    assert db.get_approval(task_id)["decision"] == "pending"
    # The optimistic pre-status.json must not exist: nothing was spawned.
    assert not executor.status_path(task_id).exists()
