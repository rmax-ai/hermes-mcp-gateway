"""Console entry point.

Delegates to :func:`hermes_mcp_gateway.app_factory.main`, which owns argument
parsing so future operator verbs (P3) can be added without touching the
packaging entry point.
"""

from __future__ import annotations

import sys

from .app_factory import main

if __name__ == "__main__":
    sys.exit(main())
