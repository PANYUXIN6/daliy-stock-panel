"""T4 日频表。E1 来自同向价格异常披露次数，E2 来自当期股票严重异常波动公告。"""
from __future__ import annotations

import logging
from collections import defaultdict

import httpx

from app.data_providers.abnormal_disclosures import fetch_disclosures

logger = logging.getLogger(__name__)


def sync_events(day, *, calendar):
    """九个已结束交易日的计数仍落在下一交易日十日窗口；上次严重异常后重新计数。"""
    day_iso = day.isoformat()
    sessions = sorted({str(d) for d in calendar if str(d) <= day_iso})[-9:]
    result = {"trade_date": day_iso, "status": "unknown", "events": {}}
    if len(sessions) < 9 or sessions[-1] != day_iso:
        return result
    try:
        announcements, moves = fetch_disclosures(sessions[0], day_iso)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        logger.warning("晚报 %s 严重异动事件资料暂不可用：%s", day_iso, exc)
        return result
    latest = {}
    for item in announcements:
        if item["symbol"] not in latest or item["date"] > latest[item["symbol"]]["date"]:
            latest[item["symbol"]] = item
    events = {}
    for symbol, item in latest.items():
        if item["date"] > sessions[-2]:
            events[symbol] = {"e2": {"source": "cninfo", "disclosed_on": item["date"],
                                     "announcement_id": item["id"], "title": item["title"]}}
    grouped = defaultdict(set)
    for symbol, on_date, direction in moves:
        if on_date in sessions and on_date > latest.get(symbol, {}).get("date", ""):
            grouped[symbol, direction].add(on_date)
    for (symbol, direction), dates in sorted(grouped.items()):
        threshold = 3 if symbol.startswith("30") else 4
        if len(dates) == threshold - 1 and "e2" not in events.get(symbol, {}):
            events.setdefault(symbol, {})["e1"] = {
                "source": "exchange_billboard", "direction": direction, "disclosure_days": sorted(dates),
                "count": len(dates), "threshold": threshold,
            }
    return {**result, "status": "complete", "events": events}


def event_paths(table, symbol, day):
    if not table or table.get("status") != "complete" or table.get("trade_date") != day:
        return {"e1": None, "e2": None}
    active = table["events"].get(symbol, {})
    return {path: path in active for path in ("e1", "e2")}
