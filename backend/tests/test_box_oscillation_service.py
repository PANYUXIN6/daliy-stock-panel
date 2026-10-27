"""周期、资产、完成状态和接口契约；算法结构样例另见 test_box_oscillation。"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline
from app.market_time import CN_TZ
from app.services import box_oscillation as service


def prices(days, *, closes=None):
    closes = closes or [10. + index for index in range(len(days))]
    return pl.DataFrame([
        {"date": day, "symbol": "600000.SH", "open": close - .25,
         "high": close + 1., "low": close - 1., "close": close,
         "volume": 100., "amount": close * 100., "quote_ts": None}
        for day, close in zip(days, closes, strict=True)
    ])


def repository(frame, tmp_path, asset="stock"):
    calls = []

    def read(asset_type, symbol, start, end, *, columns):
        calls.append((asset_type, symbol, start, end, columns))
        # 故意返回含未来数据的帧，验证服务不依赖仓库代替自己执行截止日。
        return frame

    return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           resolve_asset_type=lambda symbol: asset, get_daily_asset=read), calls


def test_week_and_month_aggregate_ohlcv_before_recomputing_indicators(tmp_path):
    frame = prices([date(2025, 12, 29), date(2025, 12, 31), date(2026, 1, 2),
                    date(2026, 1, 5), date(2026, 1, 30), date(2026, 2, 2)],
                   closes=[10., 12., 11., 15., 18., 20.])
    repo, calls = repository(frame, tmp_path, "index")
    now = datetime(2026, 3, 1, tzinfo=CN_TZ)
    weekly = service.get_box_oscillation(repo, "000001.SH", date(2025, 12, 1), date(2026, 2, 28), "weekly", now=now)
    first = weekly["rows"][0]
    assert {key: first[key] for key in ("date", "open", "high", "low", "close", "volume")} == {
        "date": date(2026, 1, 2), "open": 9.75, "high": 13., "low": 9., "close": 11., "volume": 300.,
    }
    assert first["is_complete"] is True
    assert first["ma5"] is None  # 不能复制该组末日的日线 MA。
    monthly = service.get_box_oscillation(repo, "000001.SH", date(2025, 12, 1), date(2026, 2, 28), "monthly", now=now)
    assert [row["date"] for row in monthly["rows"]] == [date(2025, 12, 31), date(2026, 1, 30), date(2026, 2, 2)]
    january = monthly["rows"][1]
    assert (january["open"], january["high"], january["low"], january["close"], january["volume"]) == (10.75, 19., 10., 18., 300.)
    assert all(call[0] == "index" and call[2] == date(1990, 1, 1) for call in calls)


def test_period_indicators_use_period_closes_with_history_before_display(tmp_path):
    # 五个月各一个真实有效记录；MA5=五个月收盘均值而不是最近五日。
    frame = prices([date(2025, month, 20) for month in range(1, 6)], closes=[10., 20., 30., 40., 50.])
    repo, _ = repository(frame, tmp_path)
    report = service.get_box_oscillation(repo, "600000.SH", date(2025, 5, 1), date(2025, 6, 1), "monthly",
                                         now=datetime(2026, 1, 1, tzinfo=CN_TZ))
    assert len(report["rows"]) == 1
    assert report["rows"][0]["ma5"] == 30.
    assert report["history_start"] == "2025-01-20"
    assert report["status"] == "insufficient_data"


def test_cutoff_and_display_start_do_not_change_detector_history(tmp_path, monkeypatch):
    frame = prices([date(2026, 1, 1) + timedelta(days=index) for index in range(25)])
    repo, _ = repository(frame, tmp_path)
    observed = []
    monkeypatch.setattr(service, "compute_box_oscillation", lambda data, period: observed.append(data["date"].to_list()) or [])
    for start in (date(2026, 1, 1), date(2026, 1, 18)):
        report = service.get_box_oscillation(repo, "600000.SH", start, date(2026, 1, 20),
                                             now=datetime(2026, 2, 1, tzinfo=CN_TZ))
        assert report["as_of"] == "2026-01-20"
        assert all(start <= row["date"] <= date(2026, 1, 20) for row in report["rows"])
    assert observed[0] == observed[1] == frame.head(20)["date"].to_list()


def test_current_day_requires_final_window_and_close_timestamp(tmp_path):
    day = date(2026, 10, 26)
    frame = prices([day - timedelta(days=3), day])
    close_ts = int(datetime(2026, 10, 26, 15, 0, tzinfo=CN_TZ).timestamp() * 1000)
    frame = frame.with_columns(pl.Series("quote_ts", [None, close_ts]))
    repo, _ = repository(frame, tmp_path)
    early = service.get_box_oscillation(repo, "600000.SH", date(2026, 10, 1), day,
                                        now=datetime(2026, 10, 26, 15, 29, tzinfo=CN_TZ))
    assert early["rows"][-1]["is_complete"] is False
    assert early["as_of"] == "2026-10-23"
    final = service.get_box_oscillation(repo, "600000.SH", date(2026, 10, 1), day,
                                        now=datetime(2026, 10, 26, 15, 30, tzinfo=CN_TZ))
    assert final["as_of"] == "2026-10-26"
    frame = frame.with_columns(pl.Series("quote_ts", [None, close_ts - 60_000]))
    stale_repo, _ = repository(frame, tmp_path)
    stale = service.get_box_oscillation(stale_repo, "600000.SH", date(2026, 10, 1), day,
                                        now=datetime(2026, 10, 26, 20, tzinfo=CN_TZ))
    assert stale["as_of"] == "2026-10-23"
    assert any("快照" in message for message in stale["diagnostics"])


def test_calendar_must_prove_early_week_month_completion(tmp_path, monkeypatch):
    frame = prices([date(2026, 10, 19), date(2026, 10, 23)])
    repo, _ = repository(frame, tmp_path)
    now = datetime(2026, 10, 23, 16, tzinfo=CN_TZ)
    monkeypatch.setattr(service, "calendar_status", lambda path: {})
    unknown = service.get_box_oscillation(repo, "600000.SH", date(2026, 10, 1), now.date(), "weekly", now=now)
    assert unknown["as_of"] is None
    assert unknown["rows"][0]["is_complete"] is False
    calendar = {"start": "2026-10-01", "end": "2026-10-31", "days": ["2026-10-19", "2026-10-23", "2026-10-26"]}
    monkeypatch.setattr(service, "calendar_status", lambda path: calendar)
    proven_week = service.get_box_oscillation(repo, "600000.SH", date(2026, 10, 1), now.date(), "weekly", now=now)
    assert proven_week["as_of"] == "2026-10-23"
    unclosed_month = service.get_box_oscillation(repo, "600000.SH", date(2026, 10, 1), now.date(), "monthly", now=now)
    assert unclosed_month["as_of"] is None  # 10/26 仍是本月已知的未来交易日。
    calendar["days"].remove("2026-10-26")
    proven_month = service.get_box_oscillation(repo, "600000.SH", date(2026, 10, 1), now.date(), "monthly", now=now)
    assert proven_month["as_of"] == "2026-10-23"


def test_month_end_without_today_row_cannot_close_before_market_close(tmp_path):
    frame = prices([date(2025, 2, 26), date(2025, 2, 27)])
    repo, _ = repository(frame, tmp_path)
    early = service.get_box_oscillation(repo, "600000.SH", date(2025, 2, 1), date(2025, 2, 28), "monthly",
                                        now=datetime(2025, 2, 28, 10, tzinfo=CN_TZ))
    assert early["rows"][0]["is_complete"] is False
    assert early["as_of"] is None
    closed = service.get_box_oscillation(repo, "600000.SH", date(2025, 2, 1), date(2025, 2, 28), "monthly",
                                         now=datetime(2025, 2, 28, 16, tzinfo=CN_TZ))
    assert closed["rows"][0]["is_complete"] is True


def test_halt_does_not_count_and_bad_price_or_stale_snapshot_splits_evidence(tmp_path, monkeypatch):
    frame = prices([date(2026, 1, 1) + timedelta(days=index) for index in range(7)])
    # 1/2 停牌剔除；1/4 错价隔断；1/6 历史盘中快照隔断。
    frame = frame.with_columns(
        pl.when(pl.col("date") == date(2026, 1, 2)).then(0.).otherwise(pl.col("open")).alias("open"),
        pl.when(pl.col("date") == date(2026, 1, 2)).then(0.).otherwise(pl.col("high")).alias("high"),
        pl.when(pl.col("date") == date(2026, 1, 4)).then(float("nan")).otherwise(pl.col("low")).alias("low"),
        pl.when(pl.col("date") == date(2026, 1, 6)).then(
            int(datetime(2026, 1, 6, 10, tzinfo=CN_TZ).timestamp() * 1000)).otherwise(None).alias("quote_ts"),
    )
    repo, _ = repository(frame, tmp_path)
    chunks = []
    monkeypatch.setattr(service, "compute_box_oscillation", lambda data, period: chunks.append(data["date"].to_list()) or [])
    report = service.get_box_oscillation(repo, "600000.SH", date(2026, 1, 1), date(2026, 1, 10),
                                         now=datetime(2026, 2, 1, tzinfo=CN_TZ))
    assert chunks == [[date(2026, 1, 1), date(2026, 1, 3)], [date(2026, 1, 5)], [date(2026, 1, 7)]]
    assert len(report["rows"]) == 5
    assert report["as_of"] == "2026-01-07"


def test_null_price_cannot_bridge_confirmed_box_and_unknown_volume_is_not_halt(tmp_path):
    closes = [105., 107., 110., 108., 105., 102., 100., 102., 105., 108.,
              110., 108., 105., 102., 100., 102., 105., 108., 110., 108.]
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(closes))]
    frame = prices(days, closes=closes)
    # 未知量额不表示零成交，完整 OHLC 仍应可以形成箱体。
    frame = frame.with_columns(pl.lit(None).alias("volume"), pl.lit(None).alias("amount"))
    repo, _ = repository(frame, tmp_path)
    complete = service.get_box_oscillation(repo, "600000.SH", days[0], days[-1],
                                           now=datetime(2026, 2, 1, tzinfo=CN_TZ))
    assert len(complete["rows"]) == len(closes)
    assert any(box["status"] == "confirmed" for box in complete["boxes"])
    broken = frame.with_columns(
        pl.when(pl.col("date") == days[9]).then(None).otherwise(pl.col(column)).alias(column)
        for column in ("open", "high", "low", "close")
    )
    repo, _ = repository(broken, tmp_path)
    report = service.get_box_oscillation(repo, "600000.SH", days[0], days[-1],
                                         now=datetime(2026, 2, 1, tzinfo=CN_TZ))
    assert len(report["rows"]) == len(closes) - 1
    assert not any(box["status"] == "confirmed" for box in report["boxes"])
    assert any("异常" in message for message in report["diagnostics"])


@pytest.mark.parametrize("period", ["weekly", "monthly"])
def test_bad_first_day_cannot_make_aggregated_period_complete(tmp_path, period):
    frame = prices([date(2026, 1, 5), date(2026, 1, 6)]).with_columns(
        pl.when(pl.col("date") == date(2026, 1, 5)).then(None).otherwise(pl.col(column)).alias(column)
        for column in ("open", "high", "low", "close")
    )
    repo, _ = repository(frame, tmp_path)
    report = service.get_box_oscillation(repo, "600000.SH", date(2026, 1, 1), date(2026, 1, 31), period,
                                         now=datetime(2026, 2, 1, tzinfo=CN_TZ))
    assert report["rows"][0]["is_complete"] is False
    assert report["as_of"] is None


@pytest.mark.parametrize("asset,symbol", [("stock", "600000.SH"), ("index", "000001.SH")])
def test_api_asset_routing_no_data_validation_and_read_failure(tmp_path, monkeypatch, asset, symbol):
    repo, calls = repository(pl.DataFrame(), tmp_path, asset)
    app = FastAPI()
    app.include_router(kline.router)
    app.state.repo = repo
    client = TestClient(app)
    params = {"symbol": symbol, "start_date": "2026-01-01", "end_date": "2026-10-01", "period": "weekly"}
    response = client.get("/api/kline/boxes", params=params)
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["asset_type"], body["rows"], body["boxes"]) == ("no_prices", asset, [], [])
    assert calls[0][0] == asset
    assert client.get("/api/kline/boxes", params={**params, "period": "minute"}).status_code == 422
    assert client.get("/api/kline/boxes", params={**params, "symbol": "../x"}).status_code == 422
    assert client.get("/api/kline/boxes", params={**params, "end_date": "bad"}).status_code == 422
    assert client.get("/api/kline/boxes", params={**params, "end_date": "2025-01-01"}).status_code == 400

    def broken(*args, **kwargs):
        raise OSError("private local path")

    monkeypatch.setattr(repo, "get_daily_asset", broken)
    failure = client.get("/api/kline/boxes", params=params)
    assert failure.status_code == 503
    assert "private local path" not in failure.text
