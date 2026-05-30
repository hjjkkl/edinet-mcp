"""Unit tests for edgar_mcp.client pure helpers + identity fail-fast.

No network. The JSON-normalisation helpers are the riskiest pure logic, so
they get direct coverage; live edgartools behaviour is covered by
scripts/smoke_test.py.
"""

from __future__ import annotations

import math
from typing import ClassVar

import pytest

pytest.importorskip("edgar", reason="edgartools (the 'edgar' extra) is not installed")

from edgar_mcp.client import EdgarClient, EdgarError, _records, _to_jsonable


class TestToJsonable:
    def test_primitives(self):
        assert _to_jsonable(1) == 1
        assert _to_jsonable("a") == "a"
        assert _to_jsonable(True) is True
        assert _to_jsonable(None) is None

    def test_nan_and_inf_become_none(self):
        assert _to_jsonable(float("nan")) is None
        assert _to_jsonable(float("inf")) is None

    def test_nested(self):
        out = _to_jsonable({"a": [1, float("nan")], "b": (2, 3)})
        assert out == {"a": [1, None], "b": [2, 3]}

    def test_na_like_objects_stringify_to_none(self):
        # A genuine string is kept as-is; only NA-like *objects* whose str() is
        # "NaT"/"<NA>" are nulled (pandas NaT, pd.NA).
        assert _to_jsonable("NaT") == "NaT"

        class FakeNaT:
            def __str__(self):
                return "NaT"

        assert _to_jsonable(FakeNaT()) is None

    def test_object_with_item(self):
        class Np:
            def item(self):
                return 5

        assert _to_jsonable(Np()) == 5

    def test_object_with_isoformat(self):
        import datetime

        assert _to_jsonable(datetime.date(2024, 1, 2)) == "2024-01-02"


class TestRecords:
    def test_none(self):
        assert _records(None) == []

    def test_list_of_dicts_via_iter(self):
        class Comparison:
            def __iter__(self):
                return iter([{"Status": "NEW", "Value": float("nan")}])

        assert _records(Comparison()) == [{"Status": "NEW", "Value": None}]

    def test_fake_dataframe(self):
        class FakeDF:
            columns: ClassVar[list[str]] = ["a", "b"]

            def notna(self):
                return self

            def where(self, _cond, _other):
                return self

            def to_dict(self, orient):
                assert orient == "records"
                return [{"a": 1, "b": math.nan}]

        assert _records(FakeDF()) == [{"a": 1, "b": None}]


class TestMapHolding:
    def test_equity_row_putcall_none(self):
        row = {
            "Cusip": "67066G104",
            "Ticker": "NVDA",
            "Issuer": "NVIDIA CORP",
            "Class": "COM",
            "Type": "Shares",
            "PutCall": "",  # equity → normalised to None
            "SharesPrnAmount": 100,
            "Value": 1000.0,
        }
        out = EdgarClient._map_holding(row, total=2000.0)
        assert out["ticker"] == "NVDA"
        assert out["put_call"] is None
        assert out["class"] == "COM"
        assert out["type"] == "Shares"
        assert out["value_usd"] == 1000.0
        assert out["pct_of_portfolio"] == 50.0

    def test_put_row_preserved(self):
        row = {"Cusip": "67066G104", "Ticker": "NVDA", "PutCall": "Put", "Value": 500.0}
        out = EdgarClient._map_holding(row, total=2000.0)
        # Same ticker as the equity row but distinct option exposure.
        assert out["put_call"] == "Put"
        assert out["pct_of_portfolio"] == 25.0

    def test_zero_total_no_div_error(self):
        out = EdgarClient._map_holding({"Value": 10.0}, total=0.0)
        assert out["pct_of_portfolio"] is None


class TestIdentityFailFast:
    def test_empty_identity_raises(self, monkeypatch):
        monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
        with pytest.raises(EdgarError, match="EDGAR_IDENTITY"):
            EdgarClient(identity="")
