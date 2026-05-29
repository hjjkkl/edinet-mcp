#!/usr/bin/env python3
"""Acceptance smoke test for the EDGAR MCP server (Step 6).

Exercises every tool against LIVE SEC EDGAR via the in-process tool functions
(no MCP transport needed — calls the ``.fn`` behind each FastMCP tool).

Requires network access to SEC EDGAR and ``EDGAR_IDENTITY`` set:

    export EDGAR_IDENTITY="Your Name <you@example.com>"
    python scripts/smoke_test.py

Each check prints PASS/FAIL and a short note. Exit code is non-zero if any
required check fails. Designed to be pasted into the delivery notes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

# Import the underlying async functions behind the FastMCP tools.
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

_results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, note: str = "") -> None:
    _results.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {note}" if note else ""))


def _is_error(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("error") is True


async def run() -> int:
    citadel_cik = "1423053"

    # 1. identity fail-fast — construct a client with identity unset.
    saved = os.environ.pop("EDGAR_IDENTITY", None)
    try:
        from edgar_mcp.client import EdgarClient, EdgarError

        try:
            EdgarClient(identity="")
            _record("fail_fast_without_identity", False, "expected EdgarError")
        except EdgarError:
            _record("fail_fast_without_identity", True, "raised as expected")
    finally:
        if saved:
            os.environ["EDGAR_IDENTITY"] = saved

    # 2. lookup_cik AAPL -> 320193
    res = await lookup_cik.fn("AAPL")
    _record("lookup_cik(AAPL)==320193", str(res.get("cik")) == "320193", str(res))

    # 3. search_filings AAPL 10-K
    res = await search_filings.fn("AAPL", ["10-K"], 3)
    filings = res.get("filings", []) if not _is_error(res) else []
    accession_10k = filings[0]["accession"] if filings else None
    _record(
        "search_filings(AAPL,10-K)>=1",
        bool(filings) and bool(filings[0].get("accession")) and bool(filings[0].get("source_url")),
        f"accession={accession_10k}",
    )

    # 4. get_filing_text paging
    if accession_10k:
        res = await get_filing_text.fn(accession_10k, max_chars=2000)
        ok = (
            not _is_error(res)
            and res.get("truncated") is True
            and len(res.get("content", "")) <= 2000
            and res.get("next_offset")
        )
        _record(
            "get_filing_text(max_chars=2000)", bool(ok), f"next_offset={res.get('next_offset')}"
        )
    else:
        _record("get_filing_text(max_chars=2000)", False, "no 10-K accession")

    # 5. extract_8k_items — latest NVDA 8-K
    res = await search_filings.fn("NVDA", ["8-K"], 1)
    nvda_8k = res.get("filings", [{}])[0].get("accession") if not _is_error(res) else None
    if nvda_8k:
        res = await extract_8k_items.fn(nvda_8k)
        items = res.get("items", []) if not _is_error(res) else []
        _record(
            "extract_8k_items(NVDA)",
            bool(items) and all("item_no" in i for i in items),
            f"{len(items)} items",
        )
    else:
        _record("extract_8k_items(NVDA)", False, "no NVDA 8-K accession")

    # 6. get_financials AAPL income
    res = await get_financials.fn("AAPL", "income")
    stmts = res.get("statements", {}) if not _is_error(res) else {}
    _record("get_financials(AAPL,income)", bool(stmts.get("income_statement")), str(list(stmts)))

    # 7. get_xbrl_concept AAPL Revenues
    res = await get_xbrl_concept.fn("AAPL", "us-gaap:Revenues", 4)
    series = res.get("series", []) if not _is_error(res) else []
    _record("get_xbrl_concept(AAPL,Revenues,4)", len(series) >= 1, f"{len(series)} periods")

    # 8. get_13f_holdings Citadel
    res = await get_13f_holdings.fn(citadel_cik)
    holdings = res.get("holdings", []) if not _is_error(res) else []
    _record("get_13f_holdings(Citadel)", bool(holdings), f"{len(holdings)} holdings")

    # 9. compare_13f_holdings Citadel
    res = await compare_13f_holdings.fn(citadel_cik)
    ok = not _is_error(res) and all(k in res for k in ("new", "closed", "increased", "decreased"))
    _record("compare_13f_holdings(Citadel)", bool(ok), "four buckets present" if ok else str(res))

    # 10. get_insider_transactions NVDA
    res = await get_insider_transactions.fn("NVDA", 10)
    txns = res.get("transactions", []) if not _is_error(res) else []
    _record("get_insider_transactions(NVDA)", bool(txns), f"{len(txns)} rows")

    # 11. get_ownership_filings AAPL (may be empty, must not error)
    res = await get_ownership_filings.fn("AAPL")
    _record("get_ownership_filings(AAPL)", not _is_error(res), f"count={res.get('count')}")

    # 12. get_current_filings
    res = await get_current_filings.fn(limit=5)
    _record(
        "get_current_filings(5)",
        not _is_error(res) and res.get("count", 0) >= 1,
        f"count={res.get('count')}",
    )

    # 13. watch_filings AAPL/NVDA since 10 days ago
    import datetime

    since = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    res = await watch_filings.fn(["AAPL", "NVDA"], since=since)
    ok = not _is_error(res) and "new_filings" in res and "watermark" in res
    _record("watch_filings(since=-10d)", bool(ok), f"watermark={res.get('watermark')}")

    # 14. error-as-data on bad ticker
    res = await lookup_cik.fn("NOTAREALTICKERXYZ")
    _record("error_as_data(bad ticker)", _is_error(res), "returned error dict")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


def main() -> int:
    if not os.environ.get("EDGAR_IDENTITY", "").strip():
        print('EDGAR_IDENTITY not set. export EDGAR_IDENTITY="Your Name <you@example.com>"')
        return 1
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
