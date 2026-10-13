"""数据页维护完成后，现有网页消费者及时读取共享结果。"""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import data, ext_data, regime
from app.services import ext_presets, market_mainline, regime_builder, shared_runtime
from app.services.events import EventBus
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def maintenance(tmp_path, monkeypatch):
    writer = KlineRepository(DataStore(tmp_path))
    reader = KlineRepository(DataStore(tmp_path, read_only=True))
    peer_reader = KlineRepository(DataStore(tmp_path, read_only=True))
    app = FastAPI()
    for router in (data.router, regime.router, ext_data.router):
        app.include_router(router)
    source = SimpleNamespace(_repo=writer, status=lambda: {}, get_index_quotes=lambda: pl.DataFrame())
    app.state.repo = writer
    app.state.quote_service = source
    mirror = shared_runtime.QuoteMirror(reader)
    web = SimpleNamespace(repo=reader, quote_service=mirror)
    peer = SimpleNamespace(repo=peer_reader, quote_service=shared_runtime.QuoteMirror(peer_reader))
    events = EventBus()
    monkeypatch.setattr(shared_runtime, "runtime_bus", events)
    subscription = events.subscribe()
    web.peer = peer
    web.peer_subscription = events.subscribe()
    day = date(2026, 10, 9)

    def refreshed():
        writer.install_shared_snapshot({
            "_enriched_cache": pl.DataFrame({"symbol": ["600000.SH"], "close": [12.5], "date": [day]}),
        }, {"_enriched_cache_date": day.isoformat()})

    monkeypatch.setattr(writer, "refresh_cache", refreshed)
    writer.install_shared_snapshot({
        "_enriched_cache": pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0], "date": [day]}),
    }, {"_enriched_cache_date": day.isoformat()})
    shared_runtime.publish_quotes(source)
    mirror.load_snapshot(web)
    peer.quote_service.load_snapshot(peer)
    try:
        yield TestClient(app), web, subscription, tmp_path
    finally:
        events.unsubscribe(subscription)
        events.unsubscribe(web.peer_subscription)
        writer.db.close()
        reader.db.close()
        peer_reader.db.close()
        regime.invalidate_regime_cache()
        ext_data.invalidate_dimension_intraday_cache()


def _consume_update(web, subscription):
    event = subscription.get_nowait()
    assert web.peer_subscription.get_nowait() == event
    assert event["type"] == "data_updated"
    shared_runtime.refresh_web_data(web, event["data"]["datasets"])
    shared_runtime.refresh_web_data(web.peer, event["data"]["datasets"])
    return event["data"]["datasets"]


def test_rebuilding_cache_publishes_new_snapshot(maintenance):
    client, web, subscription, _ = maintenance
    assert web.repo.get_enriched_latest()[0]["close"].to_list() == [10.0]
    assert client.post("/api/data/refresh-cache").json() == {"ok": True}
    assert _consume_update(web, subscription) == ["enriched"]
    assert web.repo.get_enriched_latest()[0]["close"].to_list() == [12.5]
    assert web.peer.repo.get_enriched_latest()[0]["close"].to_list() == [12.5]


@pytest.mark.parametrize("path,expected", [
    ("/api/regime/recompute", ["mainline", "regime"]),
    ("/api/regime/mainline/recompute", ["mainline"]),
])
def test_environment_maintenance_notifies_existing_readers(maintenance, monkeypatch, path, expected):
    client, web, subscription, root = maintenance
    day = date(2026, 10, 9)
    monkeypatch.setattr(regime_builder, "earliest_enriched_date", lambda repo: day)
    monkeypatch.setattr(regime_builder, "run_regime_batch", lambda repo, **kwargs: pl.DataFrame({"date": [day], "state": ["warming"]}))
    monkeypatch.setattr(regime_builder, "refresh_phase_labels", lambda data_dir: 1)

    def compute(repo, data_dir, start, end, *, kind):
        return pl.DataFrame({"date": [day], "kind": [kind], "rank": [1], "member": ["新主线"], "score": [90.0], "max_boards": [3], "leader_symbol": ["600000.SH"]})

    monkeypatch.setattr(market_mainline, "compute_mainline_range", compute)
    if path.endswith("/regime/recompute"):
        assert client.get("/api/regime/history").json()["rows"] == []
    response = client.post(path)
    assert response.status_code == 200 and response.json()["ok"]
    assert _consume_update(web, subscription) == expected
    assert market_mainline.load_mainline_history(root)["member"].to_list() == ["新主线"]
    if path.endswith("/regime/recompute"):
        rows = client.get("/api/regime/history").json()["rows"]
        assert rows == [{"date": "2026-10-09", "state": "warming"}]


@pytest.mark.parametrize("preset,field,source_field,labels,expected", [
    ("ext_gn_ths", "所属概念", "concepts", ["机器人", "人工智能"], "机器人;人工智能"),
    ("ext_hy_ths", "所属同花顺行业", "industries", ["制造", "设备"], "制造-设备"),
])
def test_preset_fetch_keeps_conversion_and_notifies_consumers(maintenance, monkeypatch, preset, field, source_field, labels, expected):
    client, web, subscription, _ = maintenance

    async def fetch(url):
        return [{"symbol": "600000.SH", "name": "示例", source_field: labels}]

    monkeypatch.setattr(ext_presets, "_fetch_json", fetch)
    response = client.post(f"/api/ext-data/presets/{preset}/fetch")
    assert response.json() == {"status": "ok", "rows": 1}
    assert _consume_update(web, subscription) == ["enriched", "ext_data"]
    rows = client.get(f"/api/ext-data/{preset}/rows").json()["rows"]
    assert rows[0][field] == expected and rows[0]["symbol"] == "600000.SH"
    assert web.repo.get_enriched_latest()[0]["close"].to_list() == [12.5]
    assert web.peer.repo.get_enriched_latest()[0]["close"].to_list() == [12.5]


def test_existing_dimension_minute_query_updates_without_waiting_for_ttl(maintenance, monkeypatch):
    client, web, _, root = maintenance

    async def fetch(url):
        return [{"symbol": "600000.SH", "name": "示例", "concepts": ["机器人"]}]

    monkeypatch.setattr(ext_presets, "_fetch_json", fetch)
    assert client.post("/api/ext-data/presets/ext_gn_ths/fetch").status_code == 200
    query = "/api/ext-data/ext_gn_ths/dimension-intraday"
    params = {"field": "所属概念", "value": "机器人", "date": "2026-10-09"}
    assert client.get(query, params=params).json()["status"] == "no_data"
    output = root / "kline_minute" / "date=2026-10-09" / "part.parquet"
    output.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "datetime": [datetime(2026, 10, 9, 10)], "close": [12.5]}).write_parquet(output)
    # 新分钟落盘后，缓存中的 no_data 必须随采集通知失效。
    shared_runtime.refresh_web_data(web, ["minute"])
    shared_runtime.refresh_web_data(web.peer, ["minute"])
    result = client.get(query, params=params).json()
    assert result["status"] == "ok"
    assert result["member_count"] == 1 and result["points"][0]["time"] == "10:00"

    async def updated_members(url):
        return [{"symbol": "600000.SH", "name": "示例", "concepts": ["新概念"]}]

    monkeypatch.setattr(ext_presets, "_fetch_json", updated_members)
    assert client.post("/api/ext-data/presets/ext_gn_ths/fetch").status_code == 200
    changed = client.get(query, params=params).json()
    assert changed["status"] == "empty" and changed["reason"] == "no_members"


def test_existing_json_ingest_publishes_through_shared_maintenance(maintenance):
    client, web, subscription, _ = maintenance
    created = client.post("/api/ext-data", json={
        "id": "scores", "label": "评分", "mode": "snapshot",
        "fields": [{"name": "symbol", "dtype": "string"}, {"name": "score", "dtype": "float"}],
    })
    assert created.status_code == 200
    _consume_update(web, subscription)
    assert client.get("/api/ext-data/scores/rows").json()["rows"] == []
    response = client.post("/api/ext-data/scores/ingest", json={"rows": [{"symbol": "600000.SH", "score": 90.0}]})
    assert response.status_code == 200 and response.json()["rows"] == 1
    assert _consume_update(web, subscription) == ["enriched", "ext_data"]
    rows = client.get("/api/ext-data/scores/rows").json()["rows"]
    assert rows[0]["score"] == 90.0


def test_failed_preset_fetch_publishes_no_success(maintenance, monkeypatch):
    client, _, subscription, _ = maintenance

    async def fail(url):
        raise RuntimeError("测试供应商不可用")

    monkeypatch.setattr(ext_presets, "_fetch_json", fail)
    response = client.post("/api/ext-data/presets/ext_gn_ths/fetch")
    assert response.status_code == 400
    assert subscription.empty()
