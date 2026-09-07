# Agent instructions

- Never commit secrets, endpoints, or telemetry tokens. `src/hermes_mcp_gateway/telemetry.py`
  is a vendored canonical shim — do not edit it by hand; it is synced from a
  lab-internal canonical source. Telemetry deps live only behind the optional
  `[telemetry]` extra; the repo must stay inert by default.
- Span attribute hygiene: primitives only, no `None`, never prompts/output/
  headers/tokens (see `docs/telemetry.md`).
- Tests: `uv run pytest -v` green before every commit; `uv run ruff check src tests` clean.
- TDD: write failing test → run it → implement → run it → commit.
