"""Operator CLI entry point (not implemented in this phase)."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-mcp-gateway",
        description="Operator CLI for the Hermes MCP Gateway.",
    )
    parser.parse_args(argv)
    print("operator CLI arrives in a later phase", file=sys.stderr)
    return 1
