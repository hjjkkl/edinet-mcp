"""Command-line interface for edgar-mcp.

Provides:
- ``edgar-mcp test``: verify EDGAR_IDENTITY and connectivity
- ``edgar-mcp serve``: start the MCP server (stdio / sse / http)

Mirrors :mod:`edinet_mcp.cli` (click group, loguru-intercepts-stdlib).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING

import click
from loguru import logger

if TYPE_CHECKING:
    from types import FrameType


class _InterceptHandler(logging.Handler):
    """Route stdlib logging through loguru for unified formatting."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame: FrameType | None = logging.currentframe()
        depth = 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """SEC EDGAR data tools and MCP server."""
    level = "DEBUG" if verbose else "INFO"
    logger.remove()
    logger.add(sys.stderr, level=level, format="{time:HH:mm:ss} | {level:<7} | {message}")
    logging.basicConfig(handlers=[_InterceptHandler()], level=level, force=True)


@cli.command("test")
def test_connection() -> None:
    """Test EDGAR_IDENTITY and connectivity to SEC EDGAR.

    Verifies EDGAR_IDENTITY is set (fail-fast) and makes a lightweight call.

    Examples:

        edgar-mcp test
    """
    import os

    from edgar_mcp import __version__

    click.echo(f"edgar-mcp v{__version__}\n")

    identity = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not identity:
        click.echo("[FAIL] EDGAR_IDENTITY is not set", err=True)
        click.echo(
            "  SEC requires a declared identity. Set it with:\n"
            '    export EDGAR_IDENTITY="Your Name <you@example.com>"',
            err=True,
        )
        sys.exit(1)
    click.echo(f"[OK]   EDGAR_IDENTITY is set ({identity})")

    click.echo("\nTesting EDGAR connectivity (lookup_cik AAPL)...")

    from edgar_mcp.client import EdgarClient

    async def _test() -> dict[str, object]:
        client = EdgarClient()
        return await asyncio.to_thread(client.lookup_cik, "AAPL")

    try:
        result = asyncio.run(_test())
    except Exception as exc:
        click.echo(f"[FAIL] EDGAR error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"[OK]   {json.dumps(result, ensure_ascii=False)}")
    click.echo("\nAll checks passed.")


@cli.command()
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse", "http"]),
    default="stdio",
    help="MCP transport. 'http' = Streamable HTTP (for reverse-proxy mounting).",
)
@click.option("--host", default="127.0.0.1", help="Bind host for http/sse transport.")
@click.option("--port", default=8000, type=int, help="Bind port for http/sse transport.")
def serve(transport: str, host: str, port: int) -> None:
    """Start the EDGAR MCP server.

    For Claude Desktop (stdio), add to your config:

        {"mcpServers": {"edgar": {"command": "uvx",
          "args": ["--from", "edinet-mcp[edgar]", "edgar-mcp", "serve"],
          "env": {"EDGAR_IDENTITY": "Your Name <you@example.com>"}}}}

    For mcp.ksinq.com (Streamable HTTP behind a reverse proxy):

        edgar-mcp serve --transport http --host 127.0.0.1 --port 8000
    """
    import os

    if not os.environ.get("EDGAR_IDENTITY", "").strip():
        click.echo(
            "[FAIL] EDGAR_IDENTITY is not set. SEC requires a declared identity.\n"
            '  export EDGAR_IDENTITY="Your Name <you@example.com>"',
            err=True,
        )
        sys.exit(1)

    from edgar_mcp.server import mcp

    fastmcp_transport = "streamable-http" if transport == "http" else transport
    logger.info(f"Starting EDGAR MCP server ({fastmcp_transport} transport)")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=fastmcp_transport, host=host, port=port)  # type: ignore[arg-type]
