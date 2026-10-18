from datetime import date, datetime

import numpy as np
import polars as pl
import pytest

from app.backtest.fundamentals import (
    attach_fundamental_factors,
    build_fundamental_matrices,
    load_fundamental_snapshot,
)
from app.backtest.matrix import build_market_data_matrix
from app.services.financial_versions import record_versions


def _row(period="2025-06-30", updated="2025-08-15", **values):
    return pl.DataFrame([{
        "symbol": "600519.SH", "period_end": period, "announce_date": updated,
        "source_update_date": updated, "date_basis": "source_update",
        "disclosure_type": "formal", **values,
    }])


def _record(tmp_path, table, frame, observed):
    return record_versions(tmp_path, table, frame, "fuyao", datetime.fromisoformat(observed))


def _values(tmp_path, dates, names):
    panel = pl.DataFrame({
        "symbol": ["600519.SH"] * len(dates), "date": [date.fromisoformat(d) for d in dates],
        "open": [10.] * len(dates), "high": [11.] * len(dates), "low": [9.] * len(dates),
        "close": [5.] * len(dates), "raw_close": [10.] * len(dates), "volume": [100.] * len(dates),
    })
    snapshot = load_fundamental_snapshot(tmp_path)
    attached = attach_fundamental_factors(panel, snapshot, names)
    market = build_market_data_matrix(panel, field_columns={"raw_close"})
    matrices = build_fundamental_matrices(market, snapshot, names)
    for name in names:
        np.testing.assert_allclose(
            matrices[name][:, 0], [np.nan if v is None else v for v in attached[name]],
            equal_nan=True, rtol=1e-6,
        )
    return attached


def test_revision_is_append_only_and_does_not_rewrite_old_backtest(tmp_path):
    original = _row(roe=10., bps=5.)
    _record(tmp_path, "metrics", original, "2025-09-01T08:00:00+08:00")
    repeated = _record(tmp_path, "metrics", original, "2025-09-10T08:00:00+08:00")
    assert repeated["observed_at"].item().startswith("2025-09-01")
    assert repeated["available_date"].item() == "2025-08-15"
    # 日期未变而数值变化，也不能把后来发现的修订放回过去。
    _record(tmp_path, "metrics", _row(roe=20., bps=8.), "2025-10-10T08:00:00+08:00")
    result = _values(tmp_path, ["2025-08-15", "2025-08-18", "2025-10-10", "2025-10-13"],
                     ["roe_latest", "pb_latest"])
    assert result["roe_latest"].to_list() == [None, 10., 10., 20.]
    assert result["pb_latest"].to_list() == [None, 2., 2., 1.25]
    versions = pl.read_parquet(tmp_path / "financials/versions/metrics/part.parquet")
    assert versions.height == 2


def test_unknown_dates_observed_in_china_and_valuation_bps_not_backdated(tmp_path):
    frame = _row(roe=12., bps=5., bps_basis="valuation_snapshot").with_columns(
        pl.lit("observed").alias("date_basis"),
    )
    # UTC 16:30 已经是中国次日，不得用服务器日期。
    _record(tmp_path, "metrics", frame, "2026-10-12T16:30:00+00:00")
    result = _values(tmp_path, ["2026-10-13", "2026-10-14"], ["roe_latest", "pb_latest"])
    assert result["roe_latest"].to_list() == [None, 12.]
    assert result["pb_latest"].to_list() == [None, None]


def test_old_quarter_revision_cannot_displace_new_quarter(tmp_path):
    _record(tmp_path, "metrics", _row("2025-03-31", "2025-04-25", roe=10.),
            "2025-08-01T08:00:00+08:00")
    _record(tmp_path, "metrics", _row(roe=20.), "2025-08-20T08:00:00+08:00")
    _record(tmp_path, "metrics", _row("2025-03-31", "2025-08-28", roe=11.),
            "2025-08-28T08:00:00+08:00")
    result = _values(tmp_path, ["2025-08-20", "2025-08-21", "2025-08-29"], ["roe_latest"])
    assert result["roe_latest"].to_list() == [10., 20., 20.]


def test_ratios_and_growth_wait_for_all_inputs_and_revised_comparatives(tmp_path):
    baseline = pl.concat([
        _row("2024-06-30", "2024-08-15", revenue=100., net_income_attributable=10.),
        _row(revenue=200., operating_cost=120., net_income=30., net_income_attributable=20.),
    ], how="diagonal_relaxed")
    _record(tmp_path, "income", baseline, "2025-09-01T08:00:00+08:00")
    _record(tmp_path, "balance_sheet", _row(updated="2025-08-25", total_assets=1000.,
                                            total_liabilities=400.), "2025-09-01T08:00:00+08:00")
    _record(tmp_path, "income", _row("2024-06-30", "2025-09-15", revenue=125.,
                                    net_income_attributable=16.), "2025-09-15T08:00:00+08:00")
    result = _values(tmp_path, ["2025-08-18", "2025-08-26", "2025-09-15", "2025-09-16"],
                     ["gross_margin_latest", "net_margin_latest", "revenue_yoy_latest",
                      "net_income_yoy_latest", "debt_ratio_latest", "roe_latest"])
    assert result["gross_margin_latest"].to_list() == [40.] * 4
    assert result["net_margin_latest"].to_list() == [15.] * 4
    assert result["revenue_yoy_latest"].to_list() == [100., 100., 100., 60.]
    assert result["net_income_yoy_latest"].to_list() == [100., 100., 100., 25.]
    assert result["debt_ratio_latest"].to_list() == [None, 40., 40., 40.]
    assert result["roe_latest"].null_count() == 4  # 不伪造加权 ROE


def test_types_sources_and_legacy_data_are_not_silently_mixed(tmp_path):
    path = tmp_path / "financials/metrics/part.parquet"
    path.parent.mkdir(parents=True)
    _row(roe=99.).write_parquet(path)
    assert load_fundamental_snapshot(tmp_path) is None  # 旧文件无法证明来源与版本时间
    forecast = _row(roe=99.).with_columns(pl.lit("forecast").alias("disclosure_type"))
    _record(tmp_path, "metrics", forecast, "2025-09-01T08:00:00+08:00")
    assert load_fundamental_snapshot(tmp_path) is None
    _record(tmp_path, "income", _row(revenue=100., operating_cost=60.),
            "2025-09-01T08:00:00+08:00")
    # 另一数据源的资产负债表不能参与同源财务计算。
    record_versions(tmp_path, "balance_sheet", _row(total_assets=100., total_liabilities=20.),
                    "other", datetime.fromisoformat("2025-09-01T08:00:00+08:00"))
    assert _values(tmp_path, ["2025-09-02"], ["roe_latest"])["roe_latest"].item() is None


def test_corrupt_version_file_fails_closed(tmp_path):
    path = tmp_path / "financials/versions/income/part.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"broken")
    with pytest.raises((pl.exceptions.PolarsError, OSError)):
        _record(tmp_path, "income", _row(revenue=200.), "2025-09-01T08:00:00+08:00")
    assert path.read_bytes() == b"broken"


def test_invalid_report_period_cannot_publish_partial_versions(tmp_path):
    _record(tmp_path, "income", _row(revenue=100.), "2025-09-01T08:00:00+08:00")
    path = tmp_path / "financials/versions/income/part.parquet"
    original = path.read_bytes()
    mixed = pl.concat([_row(revenue=200.), _row(period="invalid", revenue=300.)])
    with pytest.raises(ValueError, match="报告期无效"):
        _record(tmp_path, "income", mixed, "2025-10-10T08:00:00+08:00")
    assert path.read_bytes() == original


def test_sync_rechecks_old_periods_preserves_versions_and_publishes(tmp_path, monkeypatch):
    from app.services import financial_sync, preferences, shared_runtime
    from app.tickflow.capabilities import CapabilitySet

    baseline = pl.concat([
        _row("2025-03-31", "2025-04-25", roe=10., bps=5.),
        _row(roe=20., bps=6.),
    ])
    clock = ["2025-09-01T08:00:00+08:00"]
    frames, calls, published = [baseline], [], []
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "fuyao")
    def fetch(table, symbols, capset, latest_only):
        calls.append(latest_only)
        return frames[0]
    def capture(table, frame, data_dir):
        return _record(data_dir, table, frame, clock[0])
    monkeypatch.setattr(financial_sync, "_fetch_table", fetch)
    monkeypatch.setattr(financial_sync, "_financial_batch_policy", lambda: None)
    monkeypatch.setattr(financial_sync, "_record_table", capture)
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda root, datasets: published.append(datasets))
    sync = financial_sync._sync_history_table_for_symbols
    assert sync("metrics", ["600519.SH"], tmp_path, CapabilitySet()) == 2
    before = _values(tmp_path, ["2025-08-18"], ["roe_latest"])["roe_latest"].item()
    frames[0] = _row(updated="2025-09-15", roe=None)  # 新完整行不能逐列补上旧 BPS/ROE
    clock[0] = "2025-09-15T08:00:00+08:00"
    assert sync("metrics", ["600519.SH"], tmp_path, CapabilitySet()) == 2
    assert calls == [False, False]  # 已有股票也重查旧报告期
    result = _values(tmp_path, ["2025-08-18", "2025-09-16"], ["roe_latest", "pb_latest"])
    assert before == 20.
    assert result["roe_latest"].to_list() == [20., None]
    assert result["pb_latest"].to_list() == [pytest.approx(10. / 6.), None]
    stored = pl.read_parquet(tmp_path / "financials/metrics/part.parquet")
    assert stored.filter(pl.col("period_end") == "2025-06-30")["bps"].item() is None
    assert pl.read_parquet(tmp_path / "financials/versions/metrics/part.parquet").height == 3
    assert published == [["financials"], ["financials"]]


def test_missing_raw_price_does_not_fall_back_to_adjusted_price(tmp_path):
    _record(tmp_path, "metrics", _row(bps=5.), "2025-09-01T08:00:00+08:00")
    snapshot = load_fundamental_snapshot(tmp_path)
    panel = pl.DataFrame({"symbol": ["600519.SH"], "date": [date(2025, 9, 2)],
                          "open": [5.], "high": [6.], "low": [4.], "close": [5.], "volume": [100.]})
    assert attach_fundamental_factors(panel, snapshot, ["pb_latest"])["pb_latest"].item() is None
    market = build_market_data_matrix(panel)
    assert np.isnan(build_fundamental_matrices(market, snapshot, ["pb_latest"])["pb_latest"]).all()
