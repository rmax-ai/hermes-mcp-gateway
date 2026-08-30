import threading
import time
from pathlib import Path

from hermes_mcp_gateway.config import (
    AuthConfig,
    ClientConfig,
    GatewayConfig,
    HermesConfig,
    ServerConfig,
)
from hermes_mcp_gateway.db import Database
from hermes_mcp_gateway.executor import TaskExecutor

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
    f.write("\n".join(sys.argv))
print("fake-hermes-output")
print("session_id: fake-session-123", file=sys.stderr)
if "exit-nonzero" in prompt:
    sys.exit(1)
if "sleep" in prompt:
    time.sleep(30)
sys.exit(0)
"""

FAILING_HERMES = "#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n"


def write_fake(tmp_path, name="fake-hermes", body=FAKE_HERMES):
    path = tmp_path / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def make_executor(tmp_path, **client_overrides):
    cfg = GatewayConfig(
        server=ServerConfig(),
        hermes=HermesConfig(
            task_dir=str(tmp_path / "tasks"),
            db_path=str(tmp_path / "gateway.db"),
        ),
        auth=AuthConfig(),
        clients=[
            ClientConfig(
                client_id="test-client",
                secret_hash="sha256:" + "0" * 64,
                scopes=["task:run"],
                max_duration_s=60,
                workdirs=[str(tmp_path)],
                models=[],
                **client_overrides,
            )
        ],
    )
    cfg.hermes.bin = str(write_fake(tmp_path))

    db = Database(cfg.hermes.db_path)
    executor = TaskExecutor(cfg, db)
    return cfg, cfg.clients[0], executor


def wait_for_pid(executor, task_id, timeout=5.0):
    sf = executor.status_path(task_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = executor._read_status(sf)
        if state and state.get("pid"):
            return state
        time.sleep(0.05)
    return None


def test_happy_path(tmp_path):
    _cfg, client, ex = make_executor(tmp_path)
    result = ex.run(client, "t1", "hello", [], None, str(tmp_path))

    assert result["status"] == "done"
    assert result["exit_code"] == 0
    assert result["session_id"] == "fake-session-123"
    assert Path(result["output_path"]).exists()
    assert "fake-hermes-output" in Path(result["output_path"]).read_text()

    task = ex.db.get_task("t1")
    assert task["status"] == "done"
    assert task["session_id"] == "fake-session-123"
    assert task["exit_code"] == 0


def test_nonzero_exit(tmp_path):
    _cfg, client, ex = make_executor(tmp_path)
    result = ex.run(client, "t2", "exit-nonzero", [], None, str(tmp_path))

    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert ex.db.get_task("t2")["status"] == "failed"


def test_timeout_kill(tmp_path):
    _cfg, client, ex = make_executor(tmp_path)
    result = ex.run(client, "t3", "sleep please", [], None, str(tmp_path), max_duration_s=1)

    assert result["status"] == "timeout"
    assert result["exit_code"] is not None
    assert ex.db.get_task("t3")["status"] == "timeout"


def test_cancel(tmp_path):
    _cfg, client, ex = make_executor(tmp_path)

    holder = {}

    def worker():
        holder["result"] = ex.run(client, "t4", "sleep please", [], None, str(tmp_path))

    thread = threading.Thread(target=worker)
    thread.start()

    assert wait_for_pid(ex, "t4") is not None
    ex.cancel("t4")
    thread.join(timeout=10)

    assert not thread.is_alive()
    state = ex._read_status(ex.status_path("t4"))
    assert state["status"] == "cancelled"
    assert ex.db.get_task("t4")["status"] == "cancelled"


def test_idempotent_rerun_guard(tmp_path):
    cfg, client, ex = make_executor(tmp_path)

    first = ex.run(client, "t5", "hello", [], None, str(tmp_path))
    assert first["status"] == "done"

    # Sabotage the executable: a real re-run would now fail, so a successful
    # "done" proves the finished status.json short-circuited execution.
    cfg.hermes.bin = str(write_fake(tmp_path, "failing-hermes", FAILING_HERMES))
    second = ex.run(client, "t5", "hello", [], None, str(tmp_path))

    assert second == first
    assert second["status"] == "done"


def test_empty_toolsets_forces_safe_toolset(tmp_path):
    # An omitted -t makes hermes fall back to profile defaults (which include
    # terminal). The executor must always pass -t, defaulting to "safe".
    _cfg, client, ex = make_executor(tmp_path)
    ex.run(client, "t6", "hello", [], None, str(tmp_path))

    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert "-t" in argv
    assert argv[argv.index("-t") + 1] == "safe"


def test_explicit_toolsets_passed_through(tmp_path):
    _cfg, client, ex = make_executor(tmp_path)
    ex.run(client, "t7", "hello", ["file", "web"], None, str(tmp_path))

    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert argv[argv.index("-t") + 1] == "file,web"
