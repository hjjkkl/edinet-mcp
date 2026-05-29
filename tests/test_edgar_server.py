"""Tests for the EDGAR MCP server tools (fully mocked — no network)."""

from __future__ import annotations

import pytest

pytest.importorskip("edgar", reason="edgartools (the 'edgar' extra) is not installed")

from unittest.mock import MagicMock, patch

from edgar_mcp.server import (
    compare_13f_holdings,
    extract_8k_items,
    get_13f_holdings,
    get_current_filings,
    get_filing_text,
    get_financials,
    get_insider_transactions,
    get_ownership_filings,
    get_xbrl_concept,
    lookup_cik,
    search_filings,
    watch_filings,
)

# FastMCP wraps tools; access the original coroutine via .fn.
_lookup_cik = lookup_cik.fn
_search_filings = search_filings.fn
_get_filing_text = get_filing_text.fn
_extract_8k_items = extract_8k_items.fn
_get_financials = get_financials.fn
_get_xbrl_concept = get_xbrl_concept.fn
_get_13f_holdings = get_13f_holdings.fn
_compare_13f_holdings = compare_13f_holdings.fn
_get_insider_transactions = get_insider_transactions.fn
_get_ownership_filings = get_ownership_filings.fn
_get_current_filings = get_current_filings.fn
_watch_filings = watch_filings.fn


@pytest.fixture(autouse=True)
def _reset_client():
    """Reset the global client singleton between tests."""
    import edgar_mcp.server as server_mod

    server_mod._client = None
    yield
    server_mod._client = None


@pytest.fixture()
def mock_client():
    """A MagicMock EdgarClient. Server offloads sync methods via to_thread,
    which works fine with plain (non-async) MagicMock methods."""
    client = MagicMock()
    client.lookup_cik.return_value = {
        "ticker": "AAPL",
        "cik": 320193,
        "company_name": "Apple Inc.",
    }
    client.search_filings.return_value = [
        {
            "form": "10-K",
            "filing_date": "2024-11-01",
            "accession": "0000320193-24-000123",
            "source_url": "https://www.sec.gov/Archives/edgar/data/320193/x.htm",
        }
    ]
    client.get_filing_text.return_value = {
        "form": "10-K",
        "content": "x" * 2000,
        "truncated": True,
        "next_offset": 2000,
        "accession": "0000320193-24-000123",
    }
    client.extract_8k_items.return_value = {
        "filing_date": "2024-11-01",
        "items": [{"item_no": "2.02", "title": "Results", "text_snippet": "..."}],
    }
    client.get_financials.return_value = {"statements": {"income_statement": [{"x": 1}]}}
    client.get_xbrl_concept.return_value = {
        "concept": "us-gaap:Revenues",
        "series": [{"value": 1}],
    }
    client.get_13f_holdings.return_value = {"holdings": [{"cusip": "1", "value_usd": 1.0}]}
    client.compare_13f_holdings.return_value = {
        "new": [],
        "closed": [],
        "increased": [],
        "decreased": [],
    }
    client.get_insider_transactions.return_value = [
        {"insider_name": "X", "transaction_type": "buy", "price": 1.0, "shares": 10}
    ]
    client.get_ownership_filings.return_value = []
    client.get_current_filings.return_value = [{"form": "8-K", "accession": "a"}]
    client.watch_filings.return_value = {"new_filings": [], "watermark": None}
    return client


def _patch(mock_client):
    async def _fake_get_client():
        return mock_client

    return patch("edgar_mcp.server._get_client", _fake_get_client)


class TestLookupCik:
    async def test_returns_cik(self, mock_client):
        with _patch(mock_client):
            result = await _lookup_cik("AAPL")
        assert result["cik"] == 320193


class TestSearchFilings:
    async def test_wraps_with_count(self, mock_client):
        with _patch(mock_client):
            result = await _search_filings("AAPL", ["10-K"], 3)
        assert result["count"] == 1
        assert result["filings"][0]["accession"] == "0000320193-24-000123"


class TestGetFilingText:
    async def test_paging_fields(self, mock_client):
        with _patch(mock_client):
            result = await _get_filing_text("0000320193-24-000123", max_chars=2000)
        assert result["truncated"] is True
        assert len(result["content"]) <= 2000
        assert result["next_offset"] == 2000


class TestExtract8kItems:
    async def test_items(self, mock_client):
        with _patch(mock_client):
            result = await _extract_8k_items("acc")
        assert result["items"][0]["item_no"] == "2.02"


class TestGetFinancials:
    async def test_statements(self, mock_client):
        with _patch(mock_client):
            result = await _get_financials("AAPL", "income")
        assert "income_statement" in result["statements"]


class TestGetXbrlConcept:
    async def test_series(self, mock_client):
        with _patch(mock_client):
            result = await _get_xbrl_concept("AAPL", "us-gaap:Revenues", 4)
        assert result["series"][0]["value"] == 1


class TestThirteenF:
    async def test_holdings(self, mock_client):
        with _patch(mock_client):
            result = await _get_13f_holdings("1423053")
        assert result["holdings"][0]["cusip"] == "1"

    async def test_compare_buckets(self, mock_client):
        with _patch(mock_client):
            result = await _compare_13f_holdings("1423053")
        assert set(["new", "closed", "increased", "decreased"]) <= set(result)


class TestInsiderAndOwnership:
    async def test_insider_wraps(self, mock_client):
        with _patch(mock_client):
            result = await _get_insider_transactions("NVDA", 10)
        assert result["count"] == 1
        assert result["transactions"][0]["transaction_type"] == "buy"

    async def test_ownership_empty_ok(self, mock_client):
        with _patch(mock_client):
            result = await _get_ownership_filings("AAPL")
        assert result["count"] == 0


class TestMonitoring:
    async def test_current(self, mock_client):
        with _patch(mock_client):
            result = await _get_current_filings(limit=5)
        assert result["count"] == 1

    async def test_watch(self, mock_client):
        with _patch(mock_client):
            result = await _watch_filings(["AAPL", "NVDA"], since="2024-01-01")
        assert "new_filings" in result
        assert "watermark" in result


class TestErrorsAreData:
    async def test_exception_becomes_error_dict(self, mock_client):
        mock_client.lookup_cik.side_effect = RuntimeError("boom")
        with _patch(mock_client):
            result = await _lookup_cik("AAPL")
        assert result["error"] is True
        assert "boom" in result["reason"]
