# Telemetry Wave 3 — services & hardening spec

Scope: Wave 3 of the lab OTel rollout (fleet plan `2026-09-06_otel-instrumentation.md`,
items 12–15): instrument the hermes-mcp-gateway service, ship the public-repo inert
pattern in a public lab, verify retention, document.

Targets:

| # | Item | Repo / system |
|---|---|---|
| 12 | Service instrumentation (auth/RBAC/HITL/task spans) | `hermes-mcp-gateway` (public MIT) |
| 13 | Public-repo inert demo (shim + extra, AGENTS.md note) | `agent-evolution-gym` (public MIT) |
| 14 | Retention/maintenance check | duckdb-otel sink data dir |
| 15 | `docs/telemetry.md` per repo | both repos above |

## Design decisions (adapted from plan item 12)

The plan said "FastAPI/httpx auto-instrumentation". The gateway's actual stack
(P1–P3) is **bare Starlette** with a custom `GatewayRouter` (own `/mcp` dispatch),
a Bearer-auth middleware, and **no outbound HTTP** — tasks are spawned as `hermes
chat -q` subprocesses, not HTTP calls. Auto-instrumentation packages would add
dependency weight and capture surface we do not control. Instead:

- **Manual ASGI middleware** (`TelemetryMiddleware`) — one span per HTTP request,
  attrs: method, path, status, `client_id` (post-auth). No bodies, no headers.
- **Decision-point spans** (auth / token / policy / approval / task), not
  request-covering auto-instrumentation. No prompt or content ever recorded;
  ids/handles/status/cost-shaped attrs only (privacy rule from waves 1–2).
- **Task spans are root spans in the executor thread** (subprocess model):
  `hermes-mcp-gateway.task` carries task_id/client_id/toolsets/model/status/
  exit_code/session_id/duration. Model cost is NOT available in-process (the
  LLM call happens inside the spawned hermes session) — duration + outcome are
  the service-level signals; cost remains in yt-* / Codex instrumentation.
- Operator CLI (`approvals approve|deny`) emits `hermes-mcp-gateway.approval`
  spans when run under `otel-env.sh` — CLI runs stay env-gated, default inert.
- Service is long-lived → SDK `BatchSpanProcessor` defaults are correct
  (5s schedule); `atexit` flush on graceful shutdown.
- No telemetry dependencies outside the optional `[telemetry]` extra; vendored
  canonical shim `src/hermes_mcp_gateway/telemetry.py` (byte-identical via
  `sync-shim.sh`). Public repo stays inert: no endpoint/token strings anywhere;
  auth reaches the process only via systemd `EnvironmentFile` (deploy-time,
  outside the repo).

## Span inventory

| Span name | Where | Attrs |
|---|---|---|
| `hermes-mcp-gateway.http` | TelemetryMiddleware (every HTTP request) | `http.method`, `http.path`, `http.status_code`, `client_id` |
| `hermes-mcp-gateway.auth` | McpAuthMiddleware (`/mcp` only) | `decision` (authorized\|rejected), `client_id`, `scopes`, `reason` |
| `hermes-mcp-gateway.token` | `POST /token` | `decision` (ok\|invalid_client\|invalid_scope\|…), `http.status_code`, `scopes` |
| `hermes-mcp-gateway.policy` | `tools.run_task` RBAC checks | `decision` (submitted\|pending_approval\|denied), `reason`, `task_id`, `client_id`, `model`, `toolsets`, `workdir` |
| `hermes-mcp-gateway.approval` | operator CLI approve/deny | `decision` (approved\|denied), `task_id`, `client_id`, `reason` (deny) |
| `hermes-mcp-gateway.task` | `TaskExecutor.run` (executor thread) | `task_id`, `client_id`, `toolsets`, `model`, `status`, `exit_code`, `session_id` |

Rule: attrs are primitives only, never `None`, never content-bearing.

## Enabling in production (deploy-time, not in repo)

1. `uv sync --extra telemetry` in the repo (installs otel SDK into the service
   venv; the service runs `.venv/bin/python` directly, so extras persist).
2. Generate a local systemd `EnvironmentFile` (0600) with the lab's telemetry
   env: master gate on, local sink endpoint, Bearer from the lab pass store
   (same values the repo wrapper scripts source at call time).
3. systemd unit: add `EnvironmentFile=-%h/.hermes/mcp-gateway/otel-env`.
4. Restart via `kill -TERM <pid>` (hermes session text-block on systemd
   restart commands); `Restart=always` respawns.
5. Smoke: e2e task run → `otel-query` shows http/auth/policy/task spans.

## Public-safety checklist (both repos)

- [ ] No endpoint/token/pass strings in repo (grep before commit).
- [ ] `TELEMETRY_ENABLED` unset → zero otel imports/network (test).
- [ ] Deps only behind `[project.optional-dependencies] telemetry`.
- [ ] Shim byte-identical to canonical (`cmp`).
- [ ] Tests + ruff green; commits state AI-assisted.

## Verification

- `uv run pytest -v` and `uvx ruff check` green.
- Synthetic-only telemetry tests (fake SDK via `sys.modules` stubs, no network).
- Live: `otel-query` shows real gateway spans after smoke task.
- Retention: sink data dir size + span volume query sane;
  checkpoint defaults unchanged; S3 backup = separate decision (not in wave).
