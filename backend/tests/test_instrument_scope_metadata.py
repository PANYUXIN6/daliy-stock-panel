"""来源证券元数据的范围归一与风险警示名称更新。"""
import polars as pl
import pytest

from app.services.instrument_sync import _flatten_instruments, enrich_names_from_quotes


def instrument(symbol, **kwargs):
    return {"symbol": symbol, "code": symbol.split('.')[0], "exchange": symbol.split('.')[1],
            "type": "stock", "name": "示例股份", **kwargs}


@pytest.mark.parametrize("item,expected", [
    (instrument("600000.SH"), ("main", "a_share", False)),
    (instrument("302001.SZ"), ("chinext", "a_share", False)),
    (instrument("689001.SH"), ("star", "other", False)),
    (instrument("688001.SH"), ("star", "a_share", False)),
    (instrument("920001.BJ"), ("beijing", "a_share", False)),
    (instrument("900901.SH"), (None, "b_share", False)),
    (instrument("200001.SZ"), (None, "b_share", False)),
    (instrument("510300.SH", type="etf"), (None, "other", None)),
    (instrument("000001.SZ", exchange="SH"), (None, None, False)),
    (instrument("000001.SZ", name="*ST示例"), ("main", "a_share", True)),
    (instrument("000001.SZ", name="000001"), ("main", "a_share", None)),
    (instrument("000001.SZ", name=""), ("main", "a_share", None)),
    (instrument("000001.SZ", ext={"is_st": False}), ("main", "a_share", False)),
    (instrument("000001.SZ", ext={"is_st": None, "board": "star"}), (None, "a_share", None)),
    (instrument("000001.SZ", type=None), (None, None, None)),
])
def test_scope_metadata_requires_coherent_source(item, expected):
    row = _flatten_instruments([item], source="test-provider")[0]
    assert (row["board"], row["security_type"], row["is_st"]) == expected
    assert row["metadata_source"] == "test-provider"
    assert row["metadata_observed_at"]
    assert row["metadata_rule_version"] == "instrument-scope-v1"
    assert row["metadata_basis"]


def test_quote_name_fills_unknown_without_overwriting_explicit_metadata(tmp_path):
    path = tmp_path / "instruments" / "instruments.parquet"
    path.parent.mkdir()
    pl.DataFrame({"symbol": ["000001.SZ", "600000.SH", "600001.SH"], "name": ["", "来源股份", ""],
                  "is_st": [None, False, False], "is_st_source": ["unknown", "provider_explicit", "provider_explicit"],
                  "as_of": ["2026-10-30"] * 3}).write_parquet(path)
    enrich_names_from_quotes(tmp_path, [{"symbol": symbol, "ext": {"name": "*ST补充"}}
                                      for symbol in ["000001.SZ", "600000.SH", "600001.SH"]])
    result = pl.read_parquet(path)
    assert result["name"].to_list() == ["*ST补充", "来源股份", "*ST补充"]
    assert result["is_st"].to_list() == [True, False, False]
    assert result["as_of"].to_list() == ["2026-10-30"] * 3
    assert result["is_st_source"].to_list() == ["quote_name", "provider_explicit", "provider_explicit"]
    assert result["name_observed_at"].null_count() == 1
