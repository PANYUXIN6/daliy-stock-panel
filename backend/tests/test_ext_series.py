"""扩展迷你图的真实 Parquet → HTTP 序列契约。"""
from __future__ import annotations

from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ext_data import router
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig


@pytest.fixture
def series_client(tmp_path):
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    store = ExtConfigStore(tmp_path)
    store.upsert(ExtConfig(
        id="flow", label="资金", mode="timeseries",
        fields=[ExtField("symbol"), ExtField("ts"), ExtField("value", "float")],
        pull=PullConfig(time_field="ts"),
    ))
    with TestClient(app) as client:
        yield client


def _write(tmp_path, day, rows):
    directory = tmp_path / "ext_data" / "flow" / "timeseries" / f"date={day}"
    directory.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(directory / "part.parquet")


def test_intraday_batches_symbols_normalizes_time_and_excludes_invalid_points(series_client, tmp_path):
    _write(tmp_path, "2026-10-16", {
        "symbol": ["000001.SZ"] * 7 + ["510300.SH"],
        "ts": ["2026-10-16T09:25:00", "09:19", "09:21:00", "09:22:00", "bad", "09:23:00", "09:24:00", "09:19:00"],
        "value": [10.5, 10.0, None, float("nan"), 99.0, float("inf"), float("-inf"), 3.123],
    })
    response = series_client.get("/api/ext-data/flow/series", params={
        "field": "value", "symbols": "510300.SH,000001.SZ", "date": "2026-10-16",
    })
    assert response.status_code == 200
    assert response.json() == {
        "config_id": "flow", "field": "value", "granularity": "intraday", "date": "2026-10-16",
        "items": {"000001.SZ": [["09:19:00", 10.0], ["09:25:00", 10.5]], "510300.SH": [["09:19:00", 3.123]]},
    }
    assert series_client.get("/api/ext-data/flow/series", params={
        "field": "value", "date": "2026-10-15",
    }).json()["items"] == {}
    assert series_client.get("/api/ext-data/flow/series", params={
        "field": "value", "symbols": "510300.SH",
    }).json()["items"] == {"510300.SH": [["09:19:00", 3.123]]}


def test_daily_window_uses_existing_partitions_and_last_finite_value(series_client, tmp_path):
    for day, value in [("2026-10-12", 1.0), ("2026-10-14", 2.0), ("2026-10-16", 3.0)]:
        _write(tmp_path, day, {
            "symbol": ["000001.SZ"] * 3, "ts": ["15:00:00", "09:30:00", "15:01:00"],
            "value": [value, 99.0, None],
        })
    response = series_client.get("/api/ext-data/flow/series", params={
        "field": "value", "granularity": "daily", "days": 2, "date": "2026-10-15",
    })
    assert response.status_code == 200
    assert response.json()["items"] == {"000001.SZ": [["2026-10-12", 1.0], ["2026-10-14", 2.0]]}
    assert response.json()["days"] == 2


def test_daily_auto_and_schema_follow_business_date_without_lookahead(series_client, tmp_path):
    store = ExtConfigStore(tmp_path)
    config = store.get("flow")
    config.pull = PullConfig(time_field="日期", field_map={"date": "日期"})
    store.upsert(config)
    _write(tmp_path, "2026-10-16", {
        "symbol": ["000001.SZ"] * 3,
        "日期": ["2026-10-14", "2026-10-15", "2026-10-19"], "value": [1.0, 2.0, 99.0],
    })
    response = series_client.get("/api/ext-data/flow/series", params={"field": "value"})
    assert response.status_code == 200
    assert response.json()["granularity"] == "daily"
    assert response.json()["items"] == {"000001.SZ": [["2026-10-14", 1.0], ["2026-10-15", 2.0]]}
    schema = series_client.get("/api/ext-data/schema-all").json()["items"][0]
    assert schema["granularity"] == "daily"
    assert schema["time_field"] == "日期"
    config.pull.granularity = "intraday"
    store.upsert(config)
    assert series_client.get("/api/ext-data/schema-all").json()["items"][0]["granularity"] == "intraday"


def test_invalid_date_does_not_read_outside_partition_and_corruption_is_not_empty_success(series_client, tmp_path):
    response = series_client.get("/api/ext-data/flow/series", params={"field": "value", "date": "../../data"})
    assert response.status_code == 400
    _write(tmp_path, "2026-10-16", {"symbol": ["000001.SZ"], "ts": ["09:19:00"], "value": [1.0]})
    path = tmp_path / "ext_data/flow/timeseries/date=2026-10-16/part.parquet"
    path.write_bytes(b"broken parquet")
    response = series_client.get("/api/ext-data/flow/series", params={"field": "value"})
    assert response.status_code == 500
    assert response.json()["detail"] == "扩展序列读取失败，请检查数据文件"


def test_empty_daily_table_and_snapshot_contract(series_client, tmp_path):
    store = ExtConfigStore(tmp_path)
    config = store.get("flow")
    config.pull = None
    store.upsert(config)
    response = series_client.get("/api/ext-data/flow/series", params={"field": "value", "days": 120})
    assert response.status_code == 200
    assert response.json()["granularity"] == "daily"
    assert response.json()["items"] == {}
    columns = series_client.get("/api/ext-data/schema-all").json()["items"][0]["columns"]
    assert {column["name"]: column["type"] for column in columns}["value"] == "float"
    config.mode = "snapshot"
    store.upsert(config)
    assert series_client.get("/api/ext-data/flow/series", params={"field": "value"}).status_code == 400


def test_old_pull_config_defaults_to_auto_and_persists_explicit_granularity():
    assert PullConfig.from_dict({"time_field": "ts"}).granularity == "auto"
    config = PullConfig.from_dict({"time_field": "ts", "granularity": "daily"})
    assert PullConfig.from_dict(config.to_dict()).granularity == "daily"


def test_schema_returns_actual_numeric_type_and_does_not_invent_partition_column(series_client, tmp_path):
    _write(tmp_path, "2026-10-16", {
        "symbol": ["000001.SZ"], "ts": ["09:19:00"], "value": [1.0], "extra": [3],
    })
    single = series_client.get("/api/ext-data/schema/flow").json()["columns"]
    all_columns = series_client.get("/api/ext-data/schema-all").json()["items"][0]["columns"]
    expected = {"symbol": "VARCHAR", "ts": "VARCHAR", "value": "DOUBLE", "extra": "BIGINT"}
    assert {column["name"]: column["type"] for column in single} == expected
    assert {column["name"]: column["type"] for column in all_columns} == expected
