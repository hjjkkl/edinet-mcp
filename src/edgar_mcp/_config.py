"""Application settings for the EDGAR MCP server, loaded from the environment.

Mirrors :mod:`edinet_mcp._config` conventions (pydantic-settings, ``.env``
support, no env prefix) but for SEC EDGAR access via ``edgartools``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration for edgar-mcp.

    Values are read from environment variables or a ``.env`` file.

    Attributes:
        edgar_identity: SEC User-Agent identity string, e.g.
            ``"Jason <jason@ksinq.com>"``. **Required** — SEC rejects/throttles
            requests without a declared identity. Validated at startup.
        edgar_cache_dir: Local cache directory passed to edgartools
            (``EDGAR_LOCAL_DATA_DIR``). Defaults to ``~/.edgar``.
        edgar_max_text_chars: Default per-response text length cap (rule 3 —
            a single 10-K can be several MB; never return it whole).
    """

    edgar_identity: str = ""
    edgar_cache_dir: Path = Path.home() / ".edgar"
    edgar_max_text_chars: int = 20000

    model_config = {"env_prefix": "", "env_file": ".env", "extra": "ignore"}

    @field_validator("edgar_identity")
    @classmethod
    def _validate_identity(cls, v: str) -> str:
        # We do NOT fail here on empty — get_settings() is also used by tests
        # and tooling. Fail-fast happens in EdgarClient.__init__ / serve so the
        # message is surfaced at startup. Here we just normalise whitespace.
        return v.strip()

    @field_validator("edgar_max_text_chars")
    @classmethod
    def _validate_max_chars(cls, v: int) -> int:
        if v < 100:
            msg = "edgar_max_text_chars must be at least 100"
            raise ValueError(msg)
        return v


def get_settings(**overrides: Any) -> Settings:
    """Create a Settings instance, allowing programmatic overrides."""
    return Settings(**overrides)
