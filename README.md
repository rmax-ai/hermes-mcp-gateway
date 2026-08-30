# hermes-mcp-gateway

A governed MCP gateway that exposes Hermes Agent one-shot task execution to
remote MCP clients. This phase provides the foundations: validated YAML
config, client secrets + short-lived JWTs, an RBAC policy engine, a SQLite
audit store, a governed subprocess task executor, and a task-level
human-in-the-loop approval queue.

> The operator CLI and the MCP transport arrive in later phases; the entry
> point `hermes-mcp-gateway` is currently a stub that exits non-zero.

## Layout

```
src/hermes_mcp_gateway/
  config.py     YAML config -> typed dataclasses (validation on load)
  auth.py       client secret verification + HS256 JWT issue/validate
  policy.py     toolset/model/workdir allowlists, approval gating
  db.py         SQLite (WAL) audit store: tasks, approvals, token issues
  executor.py   thread-per-task Hermes subprocess runner (capture/timeout/cancel)
  approvals.py  HITL queue helpers over the db functions
  cli.py        placeholder entry point
```

## Install

```sh
uv sync
uv run pytest -q
```

## Configuration

Config is loaded from `HERMES_MCP_GATEWAY_CONFIG` or
`~/.hermes/mcp-gateway/config.yaml`. The template embeds no secrets: the JWT
signing key is read from the environment variable named by
`auth.signing_secret_env`.

```yaml
server:
  bind: 127.0.0.1
  port: 8778

hermes:
  bin: hermes
  profile: mcp-worker
  task_dir: /home/example/.hermes/mcp-gateway/tasks
  db_path: /home/example/.hermes/mcp-gateway/gateway.db
  timeout_s: 1800

auth:
  issuer: hermes-mcp-gateway
  signing_secret_env: HERMES_MCP_GATEWAY_SIGNING_KEY
  token_ttl_s: 600

clients:
  - client_id: example-client
    secret_hash: sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    scopes:
      - task:run
      - toolset:file
      - toolset:search
    requires_approval: false
    max_concurrency: 1
    max_duration_s: 300
    max_turns: 60
    workdirs:
      - /home/example/src
    models:
      - deepseek-v4-flash
```

Set the signing key (at least 32 characters) before starting:

```sh
export HERMES_MCP_GATEWAY_SIGNING_KEY="<a long random value, e.g. 40+ chars>"
```

## Execution contract

`TaskExecutor.run()` spawns:

```sh
hermes -p <profile> chat -q "<prompt>" [-t <toolsets>] [-m <model>] \
  --source mcp:<client_id> --max-turns <max_turns> -Q
```

with `cwd=<workdir>`, stdout/stderr captured to
`<task_dir>/<task_id>/stdout.txt` and `stderr.txt`, and a `status.json` written
atomically on every transition. The Hermes session id is parsed out of stderr.
`--yolo` is never passed; approval timeouts and non-TTY dangerous-command
denials stay at Hermes defaults.

Tasks survive gateway restarts: `status.json` and the DB are the source of
truth, and tasks left `running` for over an hour are reconciled to `failed`.
