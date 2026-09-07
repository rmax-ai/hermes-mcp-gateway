"""Telemetry tests: shim-level (synthetic, no network) + span-point integration.

The integration tests install fake ``opentelemetry`` modules into ``sys.modules``
and reload the vendored shim so every instrumented decision point records spans
into an in-process capture list. No real SDK, no exporter, no network.
"""

from __future__ import annotations

import contextvars
import hashlib
import importlib
import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from hermes_mcp_gateway import tools as tools_mod
from hermes_mcp_gateway.app_factory import create_app
from hermes_mcp_gateway.config import (
    AuthConfig,
    ClientConfig,
    GatewayConfig,
    HermesConfig,
    ServerConfig,
)
from hermes_mcp_gateway.db import Database
from hermes_mcp_gateway.executor import TaskExecutor

SHIM_PATH = Path(__file__).parents[1] / "src" / "hermes_mcp_gateway" / "telemetry.py"
SIGNING_KEY = "test-signing-key-abcdefghij-0123456789"

# -- fake hermes executable (same contract as test_executor/test_server) ----------

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


# -- fake OpenTelemetry SDK --------------------------------------------------------

_capture: list = []
_current: contextvars.ContextVar = contextvars.ContextVar("fake_otel_current", default=None)


class FakeSpan:
    def __init__(self, name, attributes=None):
        self.name = name
        self.attributes = dict(attributes or {})
        self.parent = _current.get()
        self.ended = False

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def end(self):
        self.ended = True


class _SpanContext:
    def __init__(self, tracer, name, attributes=None):
        self.tracer = tracer
        self.name = name
        self.attributes = attributes

    def __enter__(self):
        self.span = FakeSpan(self.name, self.attributes)
        _capture.append(self.span)
        self.token = _current.set(self.span)
        return self.span

    def __exit__(self, exc_type, exc, tb):
        _current.reset(self.token)
        self.span.end()
        return False


class FakeTracer:
    def start_as_current_span(self, name, attributes=None):
        return _SpanContext(self, name, attributes)


def _install_fake_sdk(monkeypatch):
    state = {"providers": [], "exporters": [], "processors": [], "flushes": 0}

    class FakeExporter:
        def __init__(self):
            state["exporters"].append(self)

    class FakeProcessor:
        def __init__(self, exporter):
            self.exporter = exporter
            state["processors"].append(self)

    class FakeResource:
        @staticmethod
        def create(attributes):
            return attributes

    class FakeProvider:
        def __init__(self, resource):
            self.resource = resource
            self.processors = []
            state["providers"].append(self)

        def add_span_processor(self, processor):
            self.processors.append(processor)

        def force_flush(self):
            state["flushes"] += 1

    trace = types.ModuleType("opentelemetry.trace")
    trace.set_tracer_provider = lambda provider: None
    trace.get_tracer = lambda name=None, **kwargs: FakeTracer()
    root = types.ModuleType("opentelemetry")
    root.trace = trace
    exporter_module = types.ModuleType("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    exporter_module.OTLPSpanExporter = FakeExporter
    resource_module = types.ModuleType("opentelemetry.sdk.resources")
    resource_module.Resource = FakeResource
    trace_module = types.ModuleType("opentelemetry.sdk.trace")
    trace_module.TracerProvider = FakeProvider
    export_module = types.ModuleType("opentelemetry.sdk.trace.export")
    export_module.BatchSpanProcessor = FakeProcessor
    modules = {
        "opentelemetry": root,
        "opentelemetry.trace": trace,
        "opentelemetry.exporter": types.ModuleType("opentelemetry.exporter"),
        "opentelemetry.exporter.otlp": types.ModuleType("opentelemetry.exporter.otlp"),
        "opentelemetry.exporter.otlp.proto": types.ModuleType(
            "opentelemetry.exporter.otlp.proto"
        ),
        "opentelemetry.exporter.otlp.proto.http": types.ModuleType(
            "opentelemetry.exporter.otlp.proto.http"
        ),
        "opentelemetry.exporter.otlp.proto.http.trace_exporter": exporter_module,
        "opentelemetry.sdk": types.ModuleType("opentelemetry.sdk"),
        "opentelemetry.sdk.resources": resource_module,
        "opentelemetry.sdk.trace": trace_module,
        "opentelemetry.sdk.trace.export": export_module,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return state


@pytest.fixture
def fake_telemetry(monkeypatch):
    """Enable the vendored shim against the fake SDK and reload it in place."""
    monkeypatch.setenv("TELEMETRY_ENABLED", "1")
    _install_fake_sdk(monkeypatch)
    telemetry_mod = importlib.import_module("hermes_mcp_gateway.telemetry")
    importlib.reload(telemetry_mod)
    assert telemetry_mod.init("hermes-mcp-gateway-test") is True
    _capture.clear()
    yield telemetry_mod
    monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    importlib.reload(telemetry_mod)
    _capture.clear()


# -- shim-level tests ---------------------------------------------------------------


def load_shim(monkeypatch, enabled):
    if enabled:
        monkeypatch.setenv("TELEMETRY_ENABLED", "1")
    else:
        monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    name = f"telemetry_test_{id(monkeypatch)}"
    spec = importlib.util.spec_from_file_location(name, SHIM_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_disabled_init_does_not_import_or_construct_sdk(monkeypatch):
    telemetry = load_shim(monkeypatch, enabled=False)
    imported = []
    real_import = __import__

    def tracking_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", tracking_import)
    assert telemetry.init("hermes-mcp-gateway") is False
    assert telemetry.enabled() is False
    assert imported == []


def test_enabled_init_installs_exporter_and_flushes(monkeypatch):
    telemetry = load_shim(monkeypatch, enabled=True)
    registered = []
    monkeypatch.setattr(telemetry.atexit, "register", registered.append)
    state = _install_fake_sdk(monkeypatch)

    assert telemetry.init("hermes-mcp-gateway") is True
    assert len(state["providers"]) == 1
    assert len(state["exporters"]) == 1
    assert len(state["processors"]) == 1
    assert registered == [telemetry.flush]
    telemetry.flush()
    assert state["flushes"] == 1


# -- span-point integration ---------------------------------------------------------


def test_http_and_token_spans_on_token_success(fake_telemetry, tmp_path):
    secret = "s3cr3t-0123456789abcdef"
    client = make_client("alice", secret, ["task:run", "toolset:file"])
    _, _, _, app = make_app(tmp_path, [client])

    with TestClient(app) as tc:
        response = tc.post(
            "/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "alice",
                "client_secret": secret,
            },
        )
    assert response.status_code == 200

    http_spans = [s for s in _capture if s.name == "hermes-mcp-gateway.http"]
    assert any(
        s.attributes.get("http.path") == "/token"
        and s.attributes.get("http.method") == "POST"
        and s.attributes.get("http.status_code") == 200
        for s in http_spans
    )
    token_spans = [s for s in _capture if s.name == "hermes-mcp-gateway.token"]
    assert len(token_spans) == 1
    assert token_spans[0].attributes.get("decision") == "ok"
    assert token_spans[0].attributes.get("scopes") == "task:run toolset:file"


def test_http_and_token_spans_on_failed_token(fake_telemetry, tmp_path):
    secret = "s3cr3t-0123456789abcdef"
    client = make_client("alice", secret, ["task:run"])
    _, _, _, app = make_app(tmp_path, [client])

    with TestClient(app) as tc:
        response = tc.post(
            "/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "alice",
                "client_secret": "wrong-secret-0000000000000000",
            },
        )
    assert response.status_code == 401
    token_spans = [s for s in _capture if s.name == "hermes-mcp-gateway.token"]
    assert token_spans and token_spans[0].attributes.get("decision") == "invalid_client"


def test_auth_rejected_span_on_unauthenticated_mcp(fake_telemetry, tmp_path):
    client = make_client("alice", "s3cr3t-0123456789abcdef", ["task:run"])
    _, _, _, app = make_app(tmp_path, [client])

    with TestClient(app) as tc:
        response = tc.get("/mcp")
    assert response.status_code == 401

    auth_spans = [s for s in _capture if s.name == "hermes-mcp-gateway.auth"]
    assert len(auth_spans) == 1
    assert auth_spans[0].attributes.get("decision") == "rejected"
    assert auth_spans[0].attributes.get("reason") == "missing_token"


def test_policy_denied_span_when_toolset_not_granted(fake_telemetry, tmp_path):
    client = make_client("alice", "s3cr3t-0123456789abcdef", ["task:run"])
    _, db, executor, _ = make_app(tmp_path, [client])

    result = tools_mod.run_task(
        client,
        ["task:run"],
        db,
        executor,
        prompt="do something",
        toolsets=["file"],
    )
    assert "forbidden" in result

    policy_spans = [s for s in _capture if s.name == "hermes-mcp-gateway.policy"]
    assert len(policy_spans) == 1
    assert policy_spans[0].attributes.get("decision") == "denied"
    assert policy_spans[0].attributes.get("reason") == "missing_toolsets"
    assert policy_spans[0].attributes.get("missing_scopes") == "toolset:file"


def test_submitted_and_task_spans_with_outcome(fake_telemetry, tmp_path):
    client = make_client(
        "alice",
        "s3cr3t-0123456789abcdef",
        ["task:run", "toolset:file"],
        workdirs=[str(tmp_path)],
    )
    _, db, executor, _ = make_app(tmp_path, [client])

    result = tools_mod.run_task(
        client,
        ["task:run", "toolset:file"],
        db,
        executor,
        prompt="hi",
        toolsets=["file"],
        workdir=str(tmp_path),
    )
    payload = json.loads(result)
    assert payload["status"] == "submitted"
    task_id = payload["task_id"]

    deadline = time.time() + 5.0
    while time.time() < deadline:
        task = db.get_task(task_id)
        if task and task["status"] in ("done", "failed", "timeout"):
            break
        time.sleep(0.05)
    assert task is not None
    assert task["status"] == "done"

    policy_spans = [s for s in _capture if s.name == "hermes-mcp-gateway.policy"]
    assert policy_spans and policy_spans[0].attributes.get("decision") == "submitted"
    assert policy_spans[0].attributes.get("task_id") == task_id

    task_spans = [s for s in _capture if s.name == "hermes-mcp-gateway.task"]
    assert len(task_spans) == 1
    attrs = task_spans[0].attributes
    assert attrs.get("task_id") == task_id
    assert attrs.get("client_id") == "alice"
    assert attrs.get("status") == "done"
    assert attrs.get("exit_code") == 0
    assert attrs.get("session_id") == "fake-session-123"


def test_approval_pending_span(fake_telemetry, tmp_path):
    client = make_client(
        "alice",
        "s3cr3t-0123456789abcdef",
        ["task:run", "toolset:terminal"],
        requires_approval=True,
        workdirs=[str(tmp_path)],
    )
    _, db, executor, _ = make_app(tmp_path, [client])

    result = tools_mod.run_task(
        client,
        ["task:run", "toolset:terminal"],
        db,
        executor,
        prompt="run things",
        toolsets=["terminal"],
        workdir=str(tmp_path),
    )
    payload = json.loads(result)
    assert payload["status"] == "pending_approval"

    policy_spans = [s for s in _capture if s.name == "hermes-mcp-gateway.policy"]
    assert policy_spans
    assert policy_spans[0].attributes.get("decision") == "pending_approval"
    assert policy_spans[0].attributes.get("task_id") == payload["task_id"]
