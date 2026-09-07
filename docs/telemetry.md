# Telemetry (OpenTelemetry)

The gateway can emit OTLP traces for HTTP requests and governance decision
points. **Disabled by default** — nothing is exported unless the process is
started with telemetry environment variables set. No telemetry dependency is
installed unless the optional `telemetry` extra is requested.

See `docs/telemetry-wave3-spec.md` for the full wave-3 design decisions.

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `TELEMETRY_ENABLED` | master gate (`1`/`true`/`yes` enables) | unset → off |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP exporter URL | SDK default |
| `OTEL_EXPORTER_OTLP_HEADERS` | e.g. `Authorization=Bearer <token>` | — |
| `OTEL_SERVICE_NAME` | overrides service name | `hermes-mcp-gateway` (set in code) |

Local lab setup sources an environment-only enable script (never committed);
the systemd unit reads a deploy-time `EnvironmentFile`. The repo contains **no
endpoint or token strings**.

Install the exporter deps in the service venv once: `uv sync --extra telemetry`.

## Spans

| Span name | When | Attributes |
|---|---|---|
| `hermes-mcp-gateway.http` | every HTTP request | `http.method`, `http.path`, `http.status_code`, `client_id` |
| `hermes-mcp-gateway.auth` | `/mcp` bearer validation | `decision` (`authorized`\|`rejected`), `client_id`, `scopes`, `reason` |
| `hermes-mcp-gateway.token` | `POST /token` | `decision` (`ok`\|error code), `http.status_code`, `scopes` |
| `hermes-mcp-gateway.policy` | `task_run` RBAC checks | `decision` (`submitted`\|`pending_approval`\|`denied`), `reason`, `task_id`, `client_id`, `model`, `toolsets`, `workdir`, `max_duration_s` |
| `hermes-mcp-gateway.approval` | operator CLI approve/deny | `decision` (`approved`\|`denied`), `task_id`, `client_id`, `reason` |
| `hermes-mcp-gateway.task` | task lifecycle (executor thread, root span) | `task_id`, `client_id`, `toolsets`, `model`, `status`, `exit_code`, `session_id` |

Privacy rules: attributes are primitives only, never `None`, and never carry
prompts, output, headers, or tokens. Task execution happens in a spawned
`hermes chat -q` subprocess, so model cost is not visible to the gateway
process — duration and outcome are the service-level signals.

## Example queries (DuckLake sink)

```sql
-- request volume by path and status
SELECT attr_http_path, attr_http_status_code, count(*) n
FROM lake.main.otlp_traces
WHERE service_name = 'hermes-mcp-gateway' AND name = 'hermes-mcp-gateway.http'
GROUP BY 1, 2 ORDER BY 3 DESC;

-- governance decisions over the last day
SELECT attr_decision, count(*) n
FROM lake.main.otlp_traces
WHERE service_name = 'hermes-mcp-gateway'
  AND name IN ('hermes-mcp-gateway.auth', 'hermes-mcp-gateway.token',
               'hermes-mcp-gateway.policy', 'hermes-mcp-gateway.approval')
GROUP BY 1 ORDER BY 2 DESC;

-- task outcomes
SELECT attr_status, count(*), avg(duration_ms)
FROM lake.main.otlp_traces
WHERE service_name = 'hermes-mcp-gateway' AND name = 'hermes-mcp-gateway.task'
GROUP BY 1;
```

(Column/attr naming follows the sink schema; adjust for your storage.)
