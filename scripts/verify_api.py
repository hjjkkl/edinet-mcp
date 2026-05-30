#!/usr/bin/env python3
"""Verify the ``edgartools`` API surface this server relies on (Step 1).

Runs OFFLINE — only ``hasattr`` / ``inspect`` checks, no network. Prints a
table of expected callables/attributes and whether they exist in the installed
edgartools version. Use this after upgrading edgartools to catch API drift
before tools start raising ``AttributeError`` at runtime.

Usage:
    python scripts/verify_api.py
"""

from __future__ import annotations

import sys


def _check(label: str, ok: bool) -> bool:
    print(f"  [{'OK ' if ok else 'MISS'}] {label}")
    return ok


def main() -> int:
    try:
        import edgar
    except ImportError:
        print("edgartools not installed. Install with: pip install 'edinet-mcp[edgar]'")
        return 1

    print(f"edgartools version: {getattr(edgar, '__version__', '?')}\n")
    all_ok = True

    print("Top-level functions:")
    for name in (
        "set_identity",
        "Company",
        "get_filings",
        "get_by_accession_number",
        "get_current_filings",
    ):
        all_ok &= _check(name, hasattr(edgar, name))

    print("\nCompany:")
    company = edgar.Company
    for attr in ("cik", "name", "tickers", "get_filings", "get_facts"):
        all_ok &= _check(f"Company.{attr}", hasattr(company, attr))

    print("\nFilings:")
    from edgar._filings import Filing, Filings

    for attr in ("filter", "latest", "head", "next"):
        all_ok &= _check(f"Filings.{attr}", hasattr(Filings, attr))
    print("\nFiling (methods; cik/form/etc. are instance attrs set in __init__):")
    for attr in (
        "obj",
        "text",
        "markdown",
        "search",
        "exhibits",
        "accession_number",
        "period_of_report",
        "homepage_url",
    ):
        all_ok &= _check(f"Filing.{attr}", hasattr(Filing, attr))

    print("\n8-K / 10-K (company_reports):")
    from edgar.company_reports import EightK, TenK

    all_ok &= _check("EightK.items", hasattr(EightK, "items"))
    for attr in ("financials", "income_statement", "balance_sheet", "cash_flow_statement"):
        all_ok &= _check(f"TenK.{attr}", hasattr(TenK, attr))

    print("\n13F (thirteenf):")
    from edgar.thirteenf import ThirteenF

    for attr in (
        "holdings",
        "compare_holdings",
        "holding_history",
        "total_value",
        "report_period",
    ):
        all_ok &= _check(f"ThirteenF.{attr}", hasattr(ThirteenF, attr))

    print("\nForm 4 (ownership):")
    from edgar.ownership.ownershipforms import Form4

    for attr in ("to_dataframe", "insider_name", "market_trades"):
        all_ok &= _check(f"Form4.{attr}", hasattr(Form4, attr))

    print("\nXBRL facts (EntityFacts):")
    from edgar.entity import EntityFacts

    for attr in ("time_series", "get_concept", "query", "to_dataframe"):
        all_ok &= _check(f"EntityFacts.{attr}", hasattr(EntityFacts, attr))

    print("\n" + ("All expected API present." if all_ok else "Some API MISSING — review above."))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
