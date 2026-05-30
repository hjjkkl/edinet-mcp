"""Thin synchronous wrapper around ``edgartools`` for SEC EDGAR access.

``edgartools`` is the actual HTTP client, cache, and parser for SEC EDGAR — it
ships its own on-disk cache (``EDGAR_LOCAL_DATA_DIR``) and rate limiting
(``pyrate-limiter`` + ``httpxthrottlecache``). So unlike :mod:`edinet_mcp`, we
do **not** reimplement a cache or rate limiter here; this module just:

* sets the SEC identity (fail-fast if missing — SEC requires it),
* resolves tickers/CIKs/accession numbers to edgartools objects,
* normalises edgartools' rich/pandas return values into JSON-able structures,
* caps text payloads so a multi-MB 10-K never blows up the LLM context.

All methods are **synchronous and blocking** (edgartools is sync). The MCP
server calls them via :func:`asyncio.to_thread` to keep the event loop free.
Hard failures raise normal exceptions; the server layer converts those into
``{"error": true, ...}`` payloads (the "errors are data" rule).
"""

from __future__ import annotations

import math
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from edgar_mcp._config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterable


class EdgarError(Exception):
    """Raised for EDGAR access problems (bad identifier, missing filing, ...)."""


# 13F status labels produced by ThirteenF.compare_holdings().data['Status'].
_COMPARE_BUCKETS = {
    "NEW": "new",
    "CLOSED": "closed",
    "INCREASED": "increased",
    "DECREASED": "decreased",
}


def _to_jsonable(value: Any) -> Any:
    """Recursively coerce edgartools / pandas / numpy values to JSON-safe types.

    Deliberately avoids importing pandas/numpy (keeps them as untyped upstream
    detail). Handles NaN/NaT, numpy scalars, dates/Timestamps, and Decimals.
    """
    if value is None:
        return None
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    # numpy scalar / pandas NA-like → unwrap via .item()
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _to_jsonable(item())
        except (ValueError, TypeError):
            pass
    # date / datetime / pandas Timestamp
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except (ValueError, TypeError):
            pass
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_to_jsonable(v) for v in value]
    # Decimal and anything else numeric-ish
    try:
        from decimal import Decimal

        if isinstance(value, Decimal):
            f = float(value)
            return None if math.isnan(f) else f
    except (ValueError, TypeError):
        pass
    text = str(value)
    # pandas prints NaN/NaT as these strings
    return None if text in ("nan", "NaT", "<NA>", "None") else text


def _records(obj: Any) -> list[dict[str, Any]]:
    """Best-effort conversion of an edgartools result into a list of row dicts.

    Handles pandas DataFrames, objects exposing ``.to_dataframe()`` /
    ``.data`` (a DataFrame), iterables of dicts, and ``None``.
    """
    if obj is None:
        return []

    # Object wrapping a DataFrame in `.data` (e.g. HoldingsComparison) — it is
    # also iterable yielding row dicts.
    df = getattr(obj, "data", None)
    if df is None and hasattr(obj, "to_dataframe"):
        try:
            df = obj.to_dataframe()
        except (ValueError, TypeError, AttributeError):
            df = None
    if df is None and hasattr(obj, "to_dict") and hasattr(obj, "columns"):
        df = obj  # obj itself is a DataFrame

    if df is not None and hasattr(df, "to_dict") and hasattr(df, "columns"):
        try:
            # Replace NA with None before serialising.
            clean = df.where(df.notna(), None)
            rows = clean.to_dict(orient="records")
            return [{str(k): _to_jsonable(v) for k, v in row.items()} for row in rows]
        except (ValueError, TypeError, AttributeError):
            pass

    # Fall back to iteration (HoldingsComparison/-History yield dicts).
    try:
        out: list[dict[str, Any]] = []
        for row in obj:
            if isinstance(row, dict):
                out.append({str(k): _to_jsonable(v) for k, v in row.items()})
            else:
                out.append({"value": _to_jsonable(row)})
        return out
    except TypeError:
        return []


class EdgarClient:
    """Synchronous facade over ``edgartools``.

    Args:
        identity: SEC identity string (``"Name <email>"``). If ``None``, read
            from settings / ``EDGAR_IDENTITY``. **Required** — missing identity
            raises immediately (SEC throttles/blocks anonymous requests).
        cache_dir: edgartools local data dir. If ``None``, from settings.
        max_text_chars: default text cap. If ``None``, from settings.
    """

    def __init__(
        self,
        identity: str | None = None,
        cache_dir: str | Path | None = None,
        max_text_chars: int | None = None,
    ) -> None:
        settings = get_settings()

        resolved_identity = (identity if identity is not None else settings.edgar_identity).strip()
        if not resolved_identity:
            msg = (
                "SEC requires a declared identity. Set EDGAR_IDENTITY "
                '(e.g. EDGAR_IDENTITY="Jason <jason@ksinq.com>"). '
                "SEC throttles or blocks requests without one."
            )
            raise EdgarError(msg)

        cache_path = Path(cache_dir) if cache_dir is not None else settings.edgar_cache_dir
        # edgartools reads EDGAR_LOCAL_DATA_DIR for its on-disk cache. Set it
        # before importing edgar so the library picks it up.
        os.environ.setdefault("EDGAR_LOCAL_DATA_DIR", str(cache_path))
        Path(cache_path).mkdir(parents=True, exist_ok=True)

        self._max_text_chars = max_text_chars or settings.edgar_max_text_chars

        import edgar  # imported lazily so the package imports without the extra

        edgar.set_identity(resolved_identity)
        self._edgar = edgar
        logger.info(f"EDGAR identity set; cache dir {cache_path}")

        # Process-local cache: accession -> Filing, to avoid re-fetching the
        # same filing within a session (rule: don't re-pull the same filing).
        self._filing_cache: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    def _company(self, identifier: str | int) -> Any:
        """Resolve a ticker / CIK / name to an edgartools ``Company``."""
        ident: str | int = identifier
        if isinstance(identifier, str):
            stripped = identifier.strip()
            ident = int(stripped) if stripped.isdigit() else stripped
        try:
            company = self._edgar.Company(ident)
        except Exception as exc:  # edgartools raises various types
            raise EdgarError(f"Could not resolve company {identifier!r}: {exc}") from exc
        if company is None or getattr(company, "cik", None) is None:
            raise EdgarError(f"No company found for {identifier!r}")
        return company

    def _filing_by_accession(self, accession: str) -> Any:
        """Fetch a Filing by accession number, with a process-local cache."""
        key = accession.strip()
        with self._lock:
            cached = self._filing_cache.get(key)
        if cached is not None:
            return cached
        try:
            filing = self._edgar.get_by_accession_number(key)
        except Exception as exc:
            raise EdgarError(f"Could not fetch accession {accession!r}: {exc}") from exc
        if filing is None:
            raise EdgarError(f"No filing found for accession {accession!r}")
        with self._lock:
            self._filing_cache[key] = filing
        return filing

    @staticmethod
    def _source_url(filing: Any) -> str | None:
        for attr in ("homepage_url", "filing_url", "url"):
            val = getattr(filing, attr, None)
            if val:
                return str(val)
        return None

    def _filing_record(self, filing: Any) -> dict[str, Any]:
        """Standard per-filing record: source + accession + timestamp."""
        return {
            "form": _to_jsonable(getattr(filing, "form", None)),
            "company": _to_jsonable(getattr(filing, "company", None)),
            "cik": _to_jsonable(getattr(filing, "cik", None)),
            "filing_date": _to_jsonable(getattr(filing, "filing_date", None)),
            "period_of_report": _to_jsonable(getattr(filing, "period_of_report", None)),
            "accession": _to_jsonable(
                getattr(filing, "accession_no", None) or getattr(filing, "accession_number", None)
            ),
            "source_url": self._source_url(filing),
        }

    # ------------------------------------------------------------------
    # A group — fundamentals / filing access
    # ------------------------------------------------------------------

    def lookup_cik(self, query: str) -> dict[str, Any]:
        company = self._company(query)
        tickers = getattr(company, "tickers", None) or []
        ticker = None
        try:
            ticker = tickers[0] if len(tickers) else None
        except TypeError:
            ticker = None
        return {
            "ticker": _to_jsonable(ticker),
            "cik": _to_jsonable(getattr(company, "cik", None)),
            "company_name": _to_jsonable(
                getattr(company, "name", None) or getattr(company, "display_name", None)
            ),
        }

    def search_filings(
        self,
        identifier: str,
        forms: list[str] | None = None,
        limit: int = 20,
        date_range: str | None = None,
    ) -> list[dict[str, Any]]:
        company = self._company(identifier)
        filings = company.get_filings(form=forms) if forms else company.get_filings()
        if date_range:
            # edgartools expects "YYYY-MM-DD:YYYY-MM-DD" (same as our spec).
            filings = filings.filter(filing_date=date_range)
        records: list[dict[str, Any]] = []
        for filing in self._iter_filings(filings, limit):
            records.append(self._filing_record(filing))
        return records

    @staticmethod
    def _iter_filings(filings: Any, limit: int) -> Iterable[Any]:
        """Yield up to *limit* filings from a Filings collection."""
        if filings is None:
            return
        # Filings supports head(n) which returns a Filings; iterate it.
        try:
            subset = filings.head(limit)
        except (AttributeError, TypeError, ValueError):
            subset = filings
        count = 0
        try:
            for filing in subset:
                yield filing
                count += 1
                if count >= limit:
                    return
        except TypeError:
            return

    def get_filing_text(
        self,
        accession: str,
        fmt: str = "markdown",
        max_chars: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        filing = self._filing_by_accession(accession)
        cap = max_chars if max_chars is not None else self._max_text_chars
        content = filing.text() if fmt == "text" else filing.markdown(include_page_breaks=True)
        content = content or ""
        offset = max(0, offset)
        chunk = content[offset : offset + cap]
        end = offset + len(chunk)
        truncated = end < len(content)
        record = self._filing_record(filing)
        record.update(
            {
                "content": chunk,
                "format": "text" if fmt == "text" else "markdown",
                "offset": offset,
                "truncated": truncated,
                "next_offset": end if truncated else None,
                "total_chars": len(content),
            }
        )
        return record

    def extract_8k_items(self, accession: str) -> dict[str, Any]:
        filing = self._filing_by_accession(accession)
        obj = filing.obj()
        raw_items = getattr(obj, "items", None)
        items: list[dict[str, Any]] = []
        snippet_cap = 800
        for entry in raw_items or []:
            # 8-K items may be plain strings ("Item 2.02") or richer objects.
            item_no: Any
            title: Any
            text: Any
            if isinstance(entry, str):
                item_no = entry
                title = None
                text = None
                getter = getattr(obj, "__getitem__", None)
                if callable(getter):
                    try:
                        text = obj[entry]
                    except (KeyError, TypeError, IndexError):
                        text = None
            else:
                item_no = getattr(entry, "item_no", None) or getattr(entry, "number", None)
                title = getattr(entry, "title", None) or getattr(entry, "name", None)
                text = getattr(entry, "text", None) or getattr(entry, "content", None)
            text_str = str(text) if text is not None else None
            items.append(
                {
                    "item_no": _to_jsonable(item_no),
                    "title": _to_jsonable(title),
                    "text_snippet": text_str[:snippet_cap] if text_str else None,
                }
            )
        record = self._filing_record(filing)
        record["items"] = items
        return record

    def get_financials(
        self,
        identifier: str,
        statement: str = "income",
        source_form: str = "10-K",
    ) -> dict[str, Any]:
        company = self._company(identifier)
        filings = company.get_filings(form=source_form)
        latest = filings.latest() if filings is not None else None
        if latest is None:
            raise EdgarError(f"No {source_form} filing found for {identifier!r}")
        obj = latest.obj()

        wanted = {
            "income": ["income_statement"],
            "balance": ["balance_sheet"],
            "cashflow": ["cash_flow_statement", "cash_flow"],
            "all": ["income_statement", "balance_sheet", "cash_flow_statement"],
        }.get(statement)
        if wanted is None:
            raise EdgarError(f"Invalid statement {statement!r}; use income|balance|cashflow|all")

        result = self._filing_record(latest)
        statements: dict[str, Any] = {}
        for name in (
            ["income_statement", "balance_sheet", "cash_flow_statement"]
            if statement == "all"
            else wanted
        ):
            data = self._resolve_statement(obj, name)
            if data is not None:
                statements[name] = _records(data)
        result["statements"] = statements
        if not statements:
            result["note"] = "No structured financial statements available in this filing."
        return result

    @staticmethod
    def _resolve_statement(obj: Any, name: str) -> Any:
        """Pull a statement off a TenK/TenQ obj or its .financials, if present."""
        candidates = [name]
        if name == "cash_flow_statement":
            candidates += ["cash_flow", "cashflow_statement"]
        for cand in candidates:
            attr = getattr(obj, cand, None)
            if attr is not None:
                return attr
        financials = getattr(obj, "financials", None)
        if financials is not None:
            for cand in candidates:
                meth = getattr(financials, cand, None)
                if meth is None:
                    continue
                try:
                    return meth() if callable(meth) else meth
                except (ValueError, TypeError, AttributeError):
                    continue
        return None

    def get_xbrl_concept(
        self,
        identifier: str,
        concept: str,
        periods: int = 8,
    ) -> dict[str, Any]:
        company = self._company(identifier)
        facts = company.get_facts()
        if facts is None:
            raise EdgarError(f"No XBRL facts available for {identifier!r}")
        df = facts.time_series(concept, periods=periods)
        rows = _records(df)
        series: list[dict[str, Any]] = []
        for row in rows:
            series.append(
                {
                    "period_start": row.get("period_start"),
                    "period": row.get("period_end"),
                    "fiscal_period": row.get("fiscal_period"),
                    "fiscal_year": row.get("fiscal_year"),
                    "value": row.get("numeric_value"),
                    "duration_days": row.get("duration_days"),
                }
            )
        return {
            "identifier": identifier,
            "cik": _to_jsonable(getattr(company, "cik", None)),
            "concept": concept,
            "series": series,
        }

    # ------------------------------------------------------------------
    # B group — smart money
    # ------------------------------------------------------------------

    def _latest_13f(self, fund: str) -> Any:
        company = self._company(fund)
        filings = company.get_filings(form="13F-HR")
        latest = filings.latest() if filings is not None else None
        if latest is None:
            raise EdgarError(f"No 13F-HR filing found for {fund!r}")
        return company, latest.obj()

    def get_13f_holdings(self, fund: str, top: int = 50) -> dict[str, Any]:
        company, obj = self._latest_13f(fund)
        rows = _records(getattr(obj, "holdings", None))
        total_value = _to_jsonable(getattr(obj, "total_value", None))
        try:
            total = float(total_value) if total_value else 0.0
        except (ValueError, TypeError):
            total = 0.0
        holdings = [self._map_holding(row, total) for row in rows[:top]]
        return {
            "fund": _to_jsonable(getattr(company, "name", None)) or fund,
            "cik": _to_jsonable(getattr(company, "cik", None)),
            "period": _to_jsonable(getattr(obj, "report_period", None)),
            "total_value_usd": total_value,
            # 13F splits a security into separate rows per option type, so the
            # same ticker can appear multiple times (long shares + Put + Call).
            # `put_call` distinguishes them ("Put"/"Call"/None=equity) — never
            # sum a Put against a long position (opposite exposure).
            "holdings": holdings,
        }

    @staticmethod
    def _map_holding(row: dict[str, Any], total: float) -> dict[str, Any]:
        """Map one edgartools holdings row to our schema.

        Surfaces ``put_call`` / ``class`` / ``type`` so callers can tell a long
        equity row apart from an options (Put/Call) row on the same security —
        13F lists them as distinct rows (grouped by CUSIP + PutCall upstream).
        """
        value = row.get("Value") or row.get("value")
        try:
            value_f = float(value) if value is not None else None
        except (ValueError, TypeError):
            value_f = None
        pct = round(value_f / total * 100, 4) if value_f and total else None
        # PutCall is "" for equity, "Put"/"Call" for options — normalise "" to None.
        put_call = row.get("PutCall") or row.get("put_call") or None
        return {
            "cusip": row.get("Cusip") or row.get("cusip"),
            "ticker": row.get("Ticker") or row.get("ticker"),
            "name": row.get("Issuer") or row.get("issuer") or row.get("name"),
            "class": row.get("Class") or row.get("class"),
            "type": row.get("Type") or row.get("type"),
            "put_call": put_call,
            "shares": row.get("SharesPrnAmount") or row.get("shares"),
            "value_usd": value_f,
            "pct_of_portfolio": pct,
        }

    def compare_13f_holdings(self, fund: str) -> dict[str, Any]:
        company, obj = self._latest_13f(fund)
        comparison = obj.compare_holdings()
        buckets: dict[str, list[dict[str, Any]]] = {
            "new": [],
            "closed": [],
            "increased": [],
            "decreased": [],
        }
        if comparison is not None:
            for row in _records(comparison):
                status = str(row.get("Status") or row.get("status") or "").upper()
                target = _COMPARE_BUCKETS.get(status)
                if target is None:
                    continue
                buckets[target].append(
                    {
                        "cusip": row.get("Cusip") or row.get("cusip"),
                        "ticker": row.get("Ticker") or row.get("ticker"),
                        "name": row.get("Issuer") or row.get("issuer"),
                        "shares": row.get("Shares") or row.get("shares"),
                        "prev_shares": row.get("PrevShares"),
                        "share_change": row.get("ShareChange"),
                        "value_usd": row.get("Value") or row.get("value"),
                        "value_change": row.get("ValueChange"),
                    }
                )
        return {
            "fund": _to_jsonable(getattr(company, "name", None)) or fund,
            "cik": _to_jsonable(getattr(company, "cik", None)),
            "period": _to_jsonable(getattr(obj, "report_period", None)),
            "new": buckets["new"],
            "closed": buckets["closed"],
            "increased": buckets["increased"],
            "decreased": buckets["decreased"],
        }

    def get_13f_holding_history(self, fund: str, periods: int = 4) -> dict[str, Any]:
        company, obj = self._latest_13f(fund)
        history = obj.holding_history(periods=periods)
        return {
            "fund": _to_jsonable(getattr(company, "name", None)) or fund,
            "cik": _to_jsonable(getattr(company, "cik", None)),
            "periods": _to_jsonable(getattr(history, "periods", None)) if history else None,
            "rows": _records(history),
        }

    def get_insider_transactions(self, identifier: str, limit: int = 20) -> list[dict[str, Any]]:
        company = self._company(identifier)
        filings = company.get_filings(form="4")
        out: list[dict[str, Any]] = []
        for filing in self._iter_filings(filings, limit):
            record = self._filing_record(filing)
            try:
                form4 = filing.obj()
                insider = getattr(form4, "insider_name", None)
                for row in _records(form4.to_dataframe()):
                    txn_type = (
                        row.get("AcquiredDisposed")
                        or row.get("acquired_disposed")
                        or row.get("Code")
                        or row.get("TransactionType")
                    )
                    out.append(
                        {
                            "insider_name": _to_jsonable(insider) or row.get("Insider"),
                            "role": (
                                row.get("Position") or row.get("Relationship") or row.get("Role")
                            ),
                            "transaction_type": _to_jsonable(txn_type),
                            "shares": row.get("Shares") or row.get("shares"),
                            "price": row.get("Price") or row.get("price"),
                            "value": row.get("Value") or row.get("value"),
                            "date": row.get("Date") or row.get("date") or record["filing_date"],
                            "accession": record["accession"],
                            "source_url": record["source_url"],
                        }
                    )
            except Exception as exc:  # one bad Form 4 shouldn't sink the batch
                logger.warning(f"Form 4 parse failed for {record['accession']}: {exc}")
                out.append({**record, "parse_error": str(exc)})
        return out

    def get_ownership_filings(
        self,
        identifier: str,
        forms: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        forms = forms or ["SC 13D", "SC 13G"]
        company = self._company(identifier)
        filings = company.get_filings(form=forms)
        out: list[dict[str, Any]] = []
        for filing in self._iter_filings(filings, limit):
            record = self._filing_record(filing)
            record["filer"] = record.get("company")
            record["pct_owned"] = None  # not reliably machine-parseable from SC 13D/G
            out.append(record)
        return out

    # ------------------------------------------------------------------
    # C group — monitoring (poll + watermark)
    # ------------------------------------------------------------------

    def get_current_filings(
        self,
        forms: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        current = self._edgar.get_current_filings(form=forms or "")
        out: list[dict[str, Any]] = []
        for filing in self._iter_filings(current, limit):
            out.append(self._filing_record(filing))
        return out

    def watch_filings(
        self,
        watchlist: list[str],
        forms: list[str] | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Return watchlist filings newer than *since* + a fresh watermark.

        *since* is an ISO date/timestamp **or** an accession number. The caller
        stores the returned ``watermark`` and passes it back next poll.
        """
        want_forms = {f.upper() for f in forms} if forms else None
        # Heuristic: accession numbers look like 0000320193-24-000123 (two
        # dashes, 18 digits). Anything else is treated as an ISO date/timestamp.
        is_accession = bool(since and since.count("-") == 2 and len(since.replace("-", "")) >= 16)
        since_accession = since if is_accession else None
        since_date = None if is_accession else since

        new_filings: list[dict[str, Any]] = []
        watermark: str | None = None
        seen_accessions: set[str] = set()

        for ident in watchlist:
            try:
                company = self._company(ident)
            except EdgarError as exc:
                logger.warning(f"watch_filings: skip {ident!r}: {exc}")
                continue
            filings = (
                company.get_filings(form=list(want_forms) if want_forms else None)
                if want_forms
                else company.get_filings()
            )
            for filing in self._iter_filings(filings, 50):
                record = self._filing_record(filing)
                accession = record.get("accession")
                if not accession or accession in seen_accessions:
                    continue
                if since_accession and accession == since_accession:
                    continue
                if (
                    since_date
                    and record.get("filing_date")
                    and str(record["filing_date"]) < since_date[:10]
                ):
                    continue
                seen_accessions.add(accession)
                record["watchlist_match"] = ident
                new_filings.append(record)

        # Newest filing_date / accession becomes the new watermark.
        if new_filings:
            new_filings.sort(
                key=lambda r: (str(r.get("filing_date") or ""), str(r.get("accession") or "")),
                reverse=True,
            )
            watermark = new_filings[0].get("accession")

        return {
            "new_filings": new_filings,
            "watermark": watermark,
            "since": since,
            "count": len(new_filings),
        }
