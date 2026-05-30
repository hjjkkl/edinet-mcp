"""edgar-mcp: SEC EDGAR MCP server, wrapping ``edgartools``.

A read-only Model Context Protocol server exposing SEC EDGAR primary-source
data: fundamentals (10-K/10-Q/8-K + XBRL), research (13F / Form 4 / SC 13D/G),
and near-real-time filing monitoring.

Requires the ``edgar`` extra::

    pip install "edinet-mcp[edgar]"

Quick start::

    from edgar_mcp import EdgarClient

    client = EdgarClient()          # reads EDGAR_IDENTITY from the environment
    print(client.lookup_cik("AAPL"))
"""

from edgar_mcp._config import Settings, get_settings
from edgar_mcp.client import EdgarClient, EdgarError

__all__ = [
    "EdgarClient",
    "EdgarError",
    "Settings",
    "get_settings",
]

__version__ = "0.6.6"
