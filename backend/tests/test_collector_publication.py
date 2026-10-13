import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import auction_benchmark, dragon_tiger, review_publication
from app.services.analysis_archive import archive_analysis


@pytest.mark.parametrize("service,reader", [(dragon_tiger, "get_dragon_tiger"), (auction_benchmark, "get_auction_benchmark")])
def test_board_cold_and_current_cache_never_load_provider(tmp_path, monkeypatch, service, reader):
    forbidden = Mock(side_effect=AssertionError("查询不能加载数据源"))
    monkeypatch.setattr(service, "_provider", forbidden)
    day = date(2026, 10, 9)
    read = getattr(service, reader)
    assert read(tmp_path, day)["state"] == "no_data"
    service._store_cache(service._cache_path(tmp_path, day), {"state": "source_unavailable"})
    for _ in range(2):
        assert read(tmp_path, day)["state"] == "source_unavailable"
    forbidden.assert_not_called()


def test_publication_is_shared_saved_snapshot(tmp_path, monkeypatch):
    from app.services import market_overview_builder, shared_runtime
    day = date(2026, 10, 9)
    assert review_publication.get_publication(tmp_path, day) == {
        "as_of": "2026-10-09", "published": False, "published_at": None, "overview": None,
    }
    monkeypatch.setattr(market_overview_builder, "build_market_overview", lambda *args: {"as_of": "2026-10-09", "breadth": {"up": 321}})
    events = []
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda root, datasets: events.append(review_publication.get_publication(root, day)))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    published = review_publication.publish_review(repo, day)
    assert published["overview"]["breadth"]["up"] == 321
    assert review_publication.get_publication(tmp_path, day) == published
    assert events == [published]


@pytest.mark.asyncio
async def test_analysis_saved_before_archived_done(tmp_path, monkeypatch):
    from app.services import shared_runtime
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: None)
    records = []
    async def stream():
        for event in [{"type": "meta", "as_of": "2026-10-09"}, {"type": "delta", "content": "复盘正文"}, {"type": "done"}]:
            yield json.dumps(event)
    def save(report):
        records.append(report)
        return {**report, "id": "report-1"}
    result = [json.loads(item) async for item in archive_analysis(stream(), save, tmp_path, "review", {"focus": "关注点"})]
    assert records == [{"as_of": "2026-10-09", "content": "复盘正文", "focus": "关注点"}]
    assert result[-1] == {"type": "done", "archived": True, "report_id": "report-1"}


@pytest.mark.parametrize("now,last_day,expected", [
    ("2026-10-09T12:00:00+08:00", date(2026, 10, 9), None),
    ("2026-10-10T16:00:00+08:00", date(2026, 10, 9), date(2026, 10, 9)),
    ("2026-10-09T15:35:00+08:00", date(2026, 10, 9), date(2026, 10, 9)),
])
def test_publication_uses_completed_session_not_wall_clock_day(monkeypatch, now, last_day, expected):
    from datetime import datetime
    monkeypatch.setattr(review_publication, "cn_now", lambda: datetime.fromisoformat(now))
    publish = Mock(return_value={"published": True})
    monkeypatch.setattr(review_publication, "publish_review", publish)
    repo = SimpleNamespace(get_enriched_latest=lambda: (None, last_day))
    review_publication.publish_review_if_ready(repo)
    if expected is None:
        publish.assert_not_called()
    else:
        assert publish.call_args.args[1] == expected
