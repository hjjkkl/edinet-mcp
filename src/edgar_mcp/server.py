"""MCP server exposing SEC EDGAR tools to LLMs via FastMCP.

Wraps :class:`edgar_mcp.client.EdgarClient` (which wraps ``edgartools``) and
exposes 13 read-only tools covering fundamentals (10-K/10-Q/8-K + XBRL),
research (13F / Form 4 / SC 13D/G), and near-real-time filing monitoring.

Conventions (mirrors :mod:`edinet_mcp.server`):

* Lazily-initialised shared client behind an :class:`asyncio.Lock`.
* edgartools is synchronous, so every tool offloads to a worker thread via
  :func:`asyncio.to_thread` to keep the event loop responsive.
* **Errors are data**: every tool returns ``{"error": true, "reason": ...}``
  rather than raising, so MCP calls never crash the client.
* Every record carries ``source_url`` + ``accession`` + ``filing_date``.

Usage with Claude Desktop (add to ``claude_desktop_config.json``)::

    {
      "mcpServers": {
        "edgar": {
          "command": "uvx",
          "args": ["--from", "edinet-mcp[edgar]", "edgar-mcp", "serve"],
          "env": {"EDGAR_IDENTITY": "Jason <jason@ksinq.com>"}
        }
      }
    }
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastmcp import FastMCP
from pydantic import Field

from edgar_mcp.client import EdgarClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Lazily initialized client with lock for concurrent-safe access.
_client: EdgarClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> EdgarClient:
    """Return the shared EdgarClient, creating it on first call.

    Uses double-checked locking so concurrent tool calls don't race on
    construction (which also calls ``set_identity`` / fails fast on a missing
    ``EDGAR_IDENTITY``).
    """
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            _client = EdgarClient()
    return _client


async def _safe(method_name: str, *args: Any, **kwargs: Any) -> Any:
    """Run a (sync) client method in a worker thread, errors-as-data.

    Returns the method's result, or ``{"error": true, "reason": ...}`` on any
    failure — so a bad ticker or missing filing never crashes the MCP call.
    """
    try:
        client = await _get_client()
        method = getattr(client, method_name)
        return await asyncio.to_thread(method, *args, **kwargs)
    except Exception as exc:
        return {
            "error": True,
            "reason": str(exc),
            "hint": (
                "Check the ticker/CIK/accession, that the company files this "
                "form type, and that EDGAR_IDENTITY is set."
            ),
        }


async def _safe_dict(method_name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """:func:`_safe` for tools whose client method returns a dict (or an error
    dict). Narrows the ``Any`` so the tool signatures stay honest."""
    return cast("dict[str, Any]", await _safe(method_name, *args, **kwargs))


@asynccontextmanager
async def _lifespan(server: FastMCP[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """No-op lifespan (edgartools manages its own HTTP client/cache)."""
    yield {}


mcp: FastMCP[Any] = FastMCP(
    name="EDGAR",
    lifespan=_lifespan,
    instructions=(
        "EDGAR MCP server provides read-only access to SEC EDGAR — the U.S. "
        "primary-source filing system. Use it for fundamentals (10-K/10-Q/8-K "
        "and XBRL financials), research (13F institutional holdings, Form 4 "
        "insider trades, SC 13D/G ownership), and near-real-time filing "
        "monitoring.\n\n"
        "Identifiers: tools accept a ticker (e.g. 'AAPL'), a CIK (e.g. 320193), "
        "or — for filing-specific tools — an accession number (e.g. "
        "'0000320193-24-000123').\n\n"
        "Key tools:\n"
        "- lookup_cik / search_filings: resolve companies and list filings\n"
        "- get_filing_text: paged markdown/text of a single filing\n"
        "- extract_8k_items / get_financials / get_xbrl_concept: fundamentals\n"
        "- get_13f_holdings / compare_13f_holdings / get_insider_transactions: "
        "smart-money signals\n"
        "- get_current_filings / watch_filings: poll for new filings\n\n"
        "Text tools are capped (default 20k chars) and paged via offset — never "
        "expect a whole 10-K in one call. Every record includes source_url, "
        "accession, and filing_date. Errors are returned as "
        '{"error": true, "reason": ...} rather than raised.'
    ),
)


# ----------------------------------------------------------------------
# A group — fundamentals / filing access
# ----------------------------------------------------------------------


@mcp.tool()
async def lookup_cik(
    query: Annotated[
        str,
        Field(description="Ticker symbol or company name, e.g. 'AAPL' or 'Apple'."),
    ],
) -> dict[str, Any]:
    """Resolve a ticker or company name to its SEC CIK.

    Example: lookup_cik("AAPL") -> {"ticker": "AAPL", "cik": 320193, ...}
    """
    return await _safe_dict("lookup_cik", query)


@mcp.tool()
async def search_filings(
    identifier: Annotated[str, Field(description="Ticker or CIK, e.g. 'AAPL' or '320193'.")],
    forms: Annotated[
        list[str] | None,
        Field(description="Form types to include, e.g. ['10-K','10-Q','8-K']."),
    ] = None,
    limit: Annotated[int, Field(description="Max filings to return.", ge=1, le=100)] = 20,
    date_range: Annotated[
        str | None,
        Field(description="Optional 'YYYY-MM-DD:YYYY-MM-DD' filing-date range."),
    ] = None,
) -> dict[str, Any]:
    """List recent filings for a company, returning accession numbers.

    Use the returned `accession` with get_filing_text / extract_8k_items /
    other filing-specific tools.
    """
    result = await _safe(
        "search_filings", identifier, forms=forms, limit=limit, date_range=date_range
    )
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"identifier": identifier, "count": len(result), "filings": result}


@mcp.tool()
async def get_filing_text(
    accession: Annotated[str, Field(description="Accession number, e.g. '0000320193-24-000123'.")],
    format: Annotated[str, Field(description="'markdown' (default) or 'text'.")] = "markdown",
    max_chars: Annotated[
        int | None,
        Field(description="Max characters to return (default EDGAR_MAX_TEXT_CHARS)."),
    ] = None,
    offset: Annotated[int, Field(description="Character offset for paging.", ge=0)] = 0,
) -> dict[str, Any]:
    """Return a single filing's content as paged markdown/text.

    A 10-K can be megabytes; output is capped and paged. When `truncated` is
    true, call again with `offset = next_offset` to continue.
    """
    return await _safe_dict(
        "get_filing_text", accession, fmt=format, max_chars=max_chars, offset=offset
    )


@mcp.tool()
async def extract_8k_items(
    accession: Annotated[str, Field(description="Accession number of an 8-K filing.")],
) -> dict[str, Any]:
    """List the Items reported in an 8-K (e.g. 1.01, 2.02, 5.02) with snippets.

    Event-driven view: which material events the 8-K reported.
    """
    return await _safe_dict("extract_8k_items", accession)


@mcp.tool()
async def get_financials(
    identifier: Annotated[str, Field(description="Ticker or CIK.")],
    statement: Annotated[
        str, Field(description="'income', 'balance', 'cashflow', or 'all'.")
    ] = "income",
    source_form: Annotated[
        str, Field(description="'10-K' (annual, default) or '10-Q' (quarterly).")
    ] = "10-K",
) -> dict[str, Any]:
    """Return structured financial statements from the latest 10-K/10-Q.

    Line items with values and periods, parsed from XBRL.
    """
    return await _safe_dict(
        "get_financials", identifier, statement=statement, source_form=source_form
    )


@mcp.tool()
async def get_xbrl_concept(
    identifier: Annotated[str, Field(description="Ticker or CIK.")],
    concept: Annotated[
        str,
        Field(
            description=(
                "XBRL concept. Use a 'us-gaap:Tag' for an exact tag (e.g. "
                "'us-gaap:Revenues'), or a plain name (e.g. 'Revenues') for "
                "synonym matching."
            )
        ),
    ],
    periods: Annotated[int, Field(description="Number of periods to return.", ge=1, le=40)] = 8,
) -> dict[str, Any]:
    """Return a time series for a single XBRL concept across periods.

    Example: get_xbrl_concept("AAPL", "us-gaap:Revenues", 4)
    """
    return await _safe_dict("get_xbrl_concept", identifier, concept, periods=periods)


# ----------------------------------------------------------------------
# B group — research / smart money
# ----------------------------------------------------------------------


@mcp.tool()
async def get_13f_holdings(
    fund: Annotated[str, Field(description="Fund CIK or name, e.g. 1423053.")],
    top: Annotated[int, Field(description="Top N holdings by value.", ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Return an institution's latest 13F-HR holdings (top N by value)."""
    return await _safe_dict("get_13f_holdings", str(fund), top=top)


@mcp.tool()
async def compare_13f_holdings(
    fund: Annotated[str, Field(description="Fund CIK or name.")],
) -> dict[str, Any]:
    """Quarter-over-quarter 13F changes: new / closed / increased / decreased.

    Core smart-money rotation signal.
    """
    return await _safe_dict("compare_13f_holdings", str(fund))


@mcp.tool()
async def get_13f_holding_history(
    fund: Annotated[str, Field(description="Fund CIK or name.")],
    periods: Annotated[int, Field(description="Number of quarters of history.", ge=2, le=20)] = 4,
) -> dict[str, Any]:
    """Multi-quarter 13F holdings history (per-security share trend)."""
    return await _safe_dict("get_13f_holding_history", str(fund), periods=periods)


@mcp.tool()
async def get_insider_transactions(
    identifier: Annotated[str, Field(description="Ticker or CIK.")],
    limit: Annotated[int, Field(description="Max Form 4 filings to scan.", ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Recent insider (Form 4) transactions: buys/sells with shares and price.

    Useful for spotting cluster buys (multiple insiders buying together).
    """
    result = await _safe("get_insider_transactions", identifier, limit=limit)
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"identifier": identifier, "count": len(result), "transactions": result}


@mcp.tool()
async def get_ownership_filings(
    identifier: Annotated[str, Field(description="Ticker or CIK.")],
    forms: Annotated[
        list[str] | None,
        Field(description="Ownership forms, default ['SC 13D','SC 13G']."),
    ] = None,
    limit: Annotated[int, Field(description="Max filings.", ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """List 5%+ ownership filings (SC 13D = activist, SC 13G = passive)."""
    result = await _safe("get_ownership_filings", identifier, forms=forms, limit=limit)
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"identifier": identifier, "count": len(result), "filings": result}


# ----------------------------------------------------------------------
# C group — real-time monitoring (poll + watermark)
# ----------------------------------------------------------------------


@mcp.tool()
async def get_current_filings(
    forms: Annotated[
        str | None, Field(description="Optional single form filter, e.g. '8-K'.")
    ] = None,
    limit: Annotated[int, Field(description="Max filings.", ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """Return the market-wide stream of filings filed most recently today."""
    result = await _safe("get_current_filings", forms=forms, limit=limit)
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"count": len(result), "filings": result}


@mcp.tool()
async def watch_filings(
    watchlist: Annotated[
        list[str], Field(description="Tickers or CIKs to watch, e.g. ['AAPL','NVDA'].")
    ],
    forms: Annotated[
        list[str] | None,
        Field(description="Optional form filter, e.g. ['8-K','4']."),
    ] = None,
    since: Annotated[
        str | None,
        Field(
            description=(
                "Last watermark: an ISO date/timestamp OR an accession number. "
                "Only filings newer than this are returned."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Return new filings for a watchlist since the last watermark.

    Store the returned `watermark` and pass it back as `since` next poll. SEC's
    index updates several times a day, so polling is effectively near-real-time
    — but don't poll on a tight loop (be a good SEC citizen).
    """
    return await _safe_dict("watch_filings", watchlist, forms=forms, since=since)
