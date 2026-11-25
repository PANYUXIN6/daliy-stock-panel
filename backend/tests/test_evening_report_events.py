from datetime import date, datetime

import httpx
import pytest

from app.data_providers import abnormal_disclosures as source
from app.market_time import CN_TZ
from app.services import evening_report_events as t4

DAYS = ["2026-09-" + str(d).zfill(2) for d in (7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 21)]
DAY = date.fromisoformat(DAYS[-1])


def notice(symbol, day):
    return {"symbol": symbol, "date": day, "id": "1225577490", "title": "股票交易严重异常波动公告"}


def test_actual_disclosures_count_once_by_direction_with_market_window_and_reset(monkeypatch):
    sh, gem, reset, stale, disclosed = ("600001.SH", "300001.SZ", "000001.SZ", "000002.SZ", "600005.SH")
    moves = [(sh, d, "涨") for d in DAYS[-5:-2]] + [(sh, DAYS[-3], "涨"), (sh, DAYS[-2], "跌")]
    moves += [(gem, d, "跌") for d in DAYS[-2:]]
    moves += [(stale, d, "涨") for d in (DAYS[1], *DAYS[-2:])]
    moves += [(reset, d, "涨") for d in DAYS[-5:-1]]
    moves += [(disclosed, d, "涨") for d in DAYS[-4:-1]]
    announcements = [notice(reset, DAYS[-4]), notice(disclosed, "2026-09-19")]
    monkeypatch.setattr(t4, "fetch_disclosures", lambda *_: (announcements, moves))
    result = t4.sync_events(DAY, calendar=DAYS)
    assert result["status"] == "complete"
    events = result["events"]
    assert events[sh]["e1"]["disclosure_days"] == DAYS[-5:-2]
    assert events[gem]["e1"]["threshold"] == 3 and events[gem]["e1"]["direction"] == "跌"
    assert t4.event_paths(result, stale, DAYS[-1]) == {"e1": False, "e2": False}
    assert reset not in events
    assert t4.event_paths(result, disclosed, DAYS[-1]) == {"e1": False, "e2": True}
    assert t4.event_paths(result, disclosed, DAYS[-2]) == {"e1": None, "e2": None}


def test_unavailable_and_completed_empty_are_distinct(monkeypatch):
    def failed(*_):
        raise httpx.ReadTimeout("服务超时")
    monkeypatch.setattr(t4, "fetch_disclosures", failed)
    assert t4.sync_events(DAY, calendar=DAYS)["status"] == "unknown"
    monkeypatch.setattr(t4, "fetch_disclosures", lambda *_: ([], []))
    assert t4.sync_events(DAY, calendar=DAYS) == {"trade_date": DAYS[-1], "events": {}, "status": "complete"}


def test_normalized_public_sources_do_not_promote_ordinary_risks_or_incomplete_pages():
    iso = "2026-09-23"
    stamp = int(datetime.fromisoformat(iso).replace(tzinfo=CN_TZ).timestamp() * 1000)
    titles = ["关于股票交易严重异常波动暨风险提示的公告", "关于股票交易异常波动、严重异常波动的复函",
              "公司股票可能触及严重异常波动的风险提示公告", "可转换公司债券交易严重异常波动的公告",
              "关于公司股价严重异常波动的公告", "关于公司股价严重异常波动的澄清公告",
              "关于公司股价可能出现严重异常波动的公告", "关于公司股价严重异常波动及可转债交易的公告"]
    notices = [{"announcementTitle": title, "secCode": "000993", "announcementTime": stamp,
                "announcementId": str(i)} for i, title in enumerate(titles)]
    reasons = ["日换手率达到20%的前5只证券", "连续三个交易日内，涨幅偏离值累计达到20%的证券",
               "连续三个交易日内，涨幅偏离值累计达到20%的证券", "非S证券连续三个交易日内收盘价格跌幅偏离值累计达到20%的证券"]
    quotes = [{"SECUCODE": "000993.SZ", "TRADE_DATE": iso, "EXPLANATION": reason} for reason in reasons]

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"totalAnnouncement": len(notices), "announcements": notices, "hasMore": False})
        return httpx.Response(200, json={"code": 0, "success": True,
                                         "result": {"data": quotes, "pages": 1, "count": 4}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert source._disclosures(client, iso, iso) == [
            {"symbol": "000993.SZ", "date": iso, "id": str(i), "title": titles[i]} for i in (0, 4)]
        assert source._ordinary_moves(client, iso, iso) == {("000993.SZ", iso, "涨"), ("000993.SZ", iso, "跌")}
    incomplete = httpx.MockTransport(lambda _: httpx.Response(
        200, json={"totalAnnouncement": len(notices) + 1, "announcements": notices, "hasMore": False}))
    with httpx.Client(transport=incomplete) as client, pytest.raises(ValueError, match="分页缺失"):
        source._disclosures(client, iso, iso)
