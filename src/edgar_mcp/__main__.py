"""Allow running edgar-mcp as ``python -m edgar_mcp``."""

from edgar_mcp.cli import cli

if __name__ == "__main__":
    cli()
