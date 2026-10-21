"""榜单预取只请求网络，主线程提交时才落盘和发布。"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.plugins.fuyao.client import FuyaoError
from app.services import auction_benchmark, dragon_tiger, shared_runtime


class BoardProvider:
    def __init__(self, fail_at: str | None = None):
        self.fail_at = fail_at
        self.calls: list[tuple[str, str]] = []

    def short_term_benchmark(self, day: str) -> dict:
        self.calls.append(("auction", day))
        if self.fail_at == "auction":
            raise FuyaoError("尚未发布")
        return {"date": "2026-08-27", "item": [{"thscode": "600519.SH"}]}

    def dragon_tiger(self, board: str, day: str) -> dict:
        self.calls.append((board, day))
        if self.fail_at == board:
            raise FuyaoError("尚未发布")
        return {"trade_date": "2026-08-27", "count": 1, "stock_items": [{"thscode": "600519.SH"}]}


@pytest.fixture(params=[
    (auction_benchmark, "auction_benchmark"),
    (dragon_tiger, "dragon_tiger"),
])
def board(request, monkeypatch):
    module, name = request.param
    monkeypatch.setattr(module, "cn_today", lambda: date(2026, 8, 28))
    return module, name, getattr(module, f"prepare_{name}"), getattr(module, f"commit_{name}")


def test_prepare_leaves_existing_cache_and_notifications_untouched(board, tmp_path, monkeypatch):
    module, name, prepare, commit = board
    provider = BoardProvider()
    monkeypatch.setattr(module, "_provider", lambda: provider)
    cache = tmp_path / name / "date=2026-08-28.json"
    cache.parent.mkdir()
    old = json.dumps({"state": "ok", "requested_date": "2026-08-28", "marker": "previous"})
    cache.write_text(old)
    events = []
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: events.append(args))
    enrich_calls = []
    if module is auction_benchmark:
        monkeypatch.setattr(module, "_enrich", lambda *args: enrich_calls.append(args) or [])

    prepared = prepare(tmp_path)

    assert provider.calls
    assert cache.read_text() == old
    assert events == []
    assert enrich_calls == []
    assert prepared["requested_date"] == "2026-08-28"
    assert prepared["trade_date"] == "2026-08-27"  # 保留真实上游日期，不伪装成今天。

    # 提交不能重新解析“今天”，否则跨午夜会将预取结果写到错误日期。
    monkeypatch.setattr(module, "cn_today", lambda: date(2026, 8, 29))
    committed = commit(prepared, tmp_path)
    assert json.loads(cache.read_text()) == prepared
    assert not (cache.parent / "date=2026-08-29.json").exists()
    assert committed["trade_date"] == "2026-08-27"
    assert events == [(tmp_path, [name])]
    if module is auction_benchmark:
        assert len(enrich_calls) == 1


def test_source_unavailable_is_only_persisted_at_commit(board, tmp_path, monkeypatch):
    module, name, prepare, commit = board
    monkeypatch.setattr(module, "_provider", lambda: None)
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: None)

    prepared = prepare(tmp_path, date(2026, 8, 27))
    assert prepared == {"state": "source_unavailable", "requested_date": "2026-08-27"}
    assert not (tmp_path / name).exists()

    assert commit(prepared, tmp_path) == prepared
    assert json.loads((tmp_path / name / "date=2026-08-27.json").read_text()) == prepared


def test_failed_prefetch_preserves_previously_published_result(board, tmp_path, monkeypatch):
    module, name, prepare, _commit = board
    provider = BoardProvider(fail_at="auction" if module is auction_benchmark else "org")
    monkeypatch.setattr(module, "_provider", lambda: provider)
    cache = tmp_path / name / "date=2026-08-28.json"
    cache.parent.mkdir()
    cache.write_text('{"state":"ok","marker":"previous"}')
    events = []
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: events.append(args))

    with pytest.raises(FuyaoError, match="尚未发布"):
        prepare(tmp_path)

    assert json.loads(cache.read_text()) == {"state": "ok", "marker": "previous"}
    assert events == []
    assert list(cache.parent.iterdir()) == [cache]
    if module is dragon_tiger:
        assert provider.calls == [("all", "2026-08-28"), ("org", "2026-08-28")]
