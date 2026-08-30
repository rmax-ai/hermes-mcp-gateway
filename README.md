# hermes-mcp-gateway

A governed MCP gateway that exposes Hermes Agent one-shot task execution to
remote MCP clients over MCP Streamable HTTP. Clients authenticate with OAuth
2.1 client credentials, receive per-client RBAC (toolset/model/workdir
allowlists, concurrency and duration caps), and terminal-capable clients run
behind task-level human-in-the-loop approval. Every task, approval, and issued
token is written to a SQLite audit store.

## Architecture

```
MCP client ── HTTP (Bearer JWT) ──> gateway (policy / audit)
                                        │
                                        ▼
              hermes -p mcp-worker chat -q <prompt>  (subprocess)
                                        │
                                        ▼
                              per-task status.json + session store
```

The gateway is a single Starlette app: `/token` and `/.well-known/*` handle
OAuth discovery and issuance, `/healthz` reports liveness, and `/mcp` is the
Bearer-authed MCP Streamable HTTP endpoint. Tasks run as governed
`hermes -p mcp-worker chat -q` subprocesses with capped duration, captured
stdout/stderr, and the Hermes session id attributed back to the task row.

## Quickstart

```sh
uv sync

mkdir -p ~/.hermes/mcp-gateway
cp deploy/config.yaml.example ~/.hermes/mcp-gateway/config.yaml
# edit the config: point hermes.bin at hermes and set workdirs to a real path

openssl rand -hex 32   # this is your signing key
export HERMES_MCP_GATEWAY_SIGNING_KEY="<the hex value>"

# hash a client secret and put its sha256 into config for the client
uv run hermes-mcp-gateway clients hash spec-client-secret
# edit config: replace the placeholder secret_hash for your client

uv run hermes-mcp-gateway --check   # validate config, no server started
uv run hermes-mcp-gateway serve     # or just: hermes-mcp-gateway
curl -sf http://127.0.0.1:8778/healthz
```

## Getting a token

The token endpoint speaks client credentials with either
`client_secret_basic` or `client_secret_post`:

```sh
# client_secret_basic: HTTP Basic (client_id:client_secret)
curl -s -u research:spec-client-secret \
  -d grant_type=client_credentials \
  http://127.0.0.1:8778/token

# client_secret_post: credentials in the form body
curl -s \
  -d grant_type=client_credentials \
  -d client_id=research \
  -d client_secret=spec-client-secret \
  http://127.0.0.1:8778/token
```

The response is `{"access_token": "...", "token_type": "Bearer",
"expires_in": 600, "scope": "..."}`. Pass `access_token` as the bearer token on
`/mcp`.

## MCP client configuration

OAuth bearer handling is client-specific, so configure your client to send the
bearer token directly on the `/mcp` URL.

Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "hermes-mcp-gateway": {
      "type": "http",
      "url": "http://127.0.0.1:8778/mcp",
      "headers": {
        "Authorization": "Bearer <access_token>"
      }
    }
  }
}
```

Generic `mcpServers` JSON (many clients accept this shape):

```json
{
  "mcpServers": {
    "hermes-mcp-gateway": {
      "url": "http://127.0.0.1:8778/mcp",
      "headers": {
        "Authorization": "Bearer <access_token>"
      }
    }
  }
}
```

## Security model

- Deny-by-default toolset scopes: a client can only request toolsets granted by
  a `toolset:*` scope, and any disallowed toolset rejects the whole request.
- Empty resolved toolsets are forced to the locked-down `safe` toolset (never
  the profile's own defaults, which would include terminal).
- Workdir jail: tasks run only inside a real directory that resolves under a
  configured workdir root, symlink escapes rejected.
- Model allowlist: configured `models` restrict which models a client may pick.
- Per-client concurrency and duration caps (`max_concurrency`,
  `max_duration_s`) plus a turn budget (`max_turns`).
- Task-level human-in-the-loop: `requires_approval` clients with terminal
  park the task as `pending_approval` until an operator approves or denies.
- Per-task `session_id` attribution: the Hermes session id is parsed from task
  stderr and stored.
- SQLite audit store records a row for every task, approval decision, and
  issued token.
- `--yolo` is never passed to hermes; approval timeouts and non-TTY dangerous
  command approvals stay at hermes defaults and auto-deny inside hermes.

## Operator CLI

The `hermes-mcp-gateway` entry point serves the gateway by default and exposes
operator verbs. Every verb reads `--config` like `serve` does.

| Command | Effect |
| --- | --- |
| `serve` (default) | Run the gateway server |
| `clients list` | Table of clients: id, scopes, approval flag, caps, workdirs, models |
| `clients hash <secret\|->` | Print `sha256:<hex>` of a client secret (`-` reads stdin) |
| `approvals pending` | Rows of `task_id`, `client_id`, `created_at`, prompt |
| `approvals approve <task_id>` | Approve and spawn a pending task (no-op if already decided) |
| `approvals deny <task_id> [reason]` | Deny a pending task with an optional reason |
| `tasks list [--client ID] [--limit N]` | List tasks, newest first |
| `tasks show <task_id>` | Full task row plus approval state and output path |
| `tasks cancel <task_id>` | Cancel a running task (no-op if already finished) |
| `token issue <client_id> [scopes...]` | Dev-only token issuance (requires `HERMES_MCP_GATEWAY_ALLOW_DEV_TOKEN=1`) |

All verbs exit nonzero on an unknown task or client.

## Deployment (systemd user unit)

```sh
mkdir -p ~/.config/systemd/user
cp deploy/hermes-mcp-gateway.service ~/.config/systemd/user/
chmod 600 ~/.hermes/mcp-gateway/env   # file holds HERMES_MCP_GATEWAY_SIGNING_KEY
systemctl --user daemon-reload
systemctl --user enable --now hermes-mcp-gateway
journalctl --user -u hermes-mcp-gateway -f
```

The unit template lives at `deploy/hermes-mcp-gateway.service`; it runs the
gateway from the project venv, reads the signing key from
`~/.hermes/mcp-gateway/env`, and restarts on failure.

## Roadmap

- Command-level HITL via the hermes approval queue (finer than task-level).
- RS256 / JWKS token verification for stateless, header-only clients.
- Notification push (webhook/queue) when a task needs approval or finishes.
- Per-task cost and token evidence sourced from the session store.
