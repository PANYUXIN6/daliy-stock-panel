"""用定稿的第二段启动和本地正式行情评价 K+1 至 K+10。"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from datetime import date

from app.services.evening_report_rules import compare, finite
from app.services.evening_report_wave import start_key
from app.services.trading_day import calendar_status

VERSION = "second-start-close-v2"
SESSIONS = 10
END_RETURN = .08


def is_live_original(report):
    # 盘后已提交的 D 日任务可在午夜后定稿。
    return report.get("kind") == "original" and report.get("mode", "live") == "live"


def _window(calendar, start):
    index = bisect_left(calendar, start)
    if index == len(calendar) or calendar[index] != start:
        return []
    return calendar[index:index + SESSIONS + 1]


def evaluate_start(event, *, window, prices, factors, suspended, as_of):
    symbol, start = event["security_id"], event["date"]
    target_day = window[-1] if len(window) == SESSIONS + 1 else None
    result = {"security_id": symbol, "start_id": event["start_id"], "rule_version": event["rule_version"],
              "start_date": start, "evidence_date": event.get("evidence_date"), "target_date": target_day,
              "report_version": event.get("report_version"), "as_of": as_of}

    def finish(status, reason, day=None):
        return {**result, "status": status, "evaluation_reason_code": reason, "terminal_date": day}

    high = (event.get("target_high") or {}).get("price")
    low = (event.get("failure_low") or {}).get("price")
    start_low, start_close = event.get("start_low"), event.get("start_close")
    if not all(finite(value, positive=True) for value in (high, low, start_low, start_close)):
        return finish("unavailable", "missing_frozen_anchor")
    if compare(start_close / high, 1., "gt"):
        return finish("success_at_start", "success_at_start", start)
    opening_factor = factors.get(start)
    for day in window[1:]:
        if day > as_of:
            break
        if (symbol, day) in suspended:
            return finish("unavailable", "suspended", day)
        raw, factor = prices.get((symbol, day)), factors.get(day)
        if not all(finite(value, positive=True) for value in (raw, opening_factor, factor)):
            return finish("unavailable", "missing_price_or_factor", day)
        close = raw * factor / opening_factor
        if compare(close / low, 1., "lt"):
            return finish("failed", "close_below_failure_low", day)
        if compare(close / start_low, 1., "lt"):
            return finish("failed", "close_below_start_low_before_new_high", day)
        if compare(close / high, 1., "gt"):
            return finish("success", "close_above_target_high", day)
        if day == target_day:
            if compare(close / start_close - 1, END_RETURN):
                return finish("success", "end_return_target_met", day)
            return finish("expired", "target_not_reached", day)
    return finish("not_due", "not_due")


def run_study(store, as_of: date, *, evaluated_at):
    from app.services.evening_report_inputs import load_evaluation_inputs

    through = as_of.isoformat()
    calendar = sorted(set(calendar_status(store.root.parent).get("days", [])))
    queue = {}
    for entry in reversed(store.dates()):
        if entry["date"] > through:
            continue
        report = store.get(entry["date"])
        if not report or not is_live_original(report):
            continue
        for event in report.get("wave_starts", ()):
            if event.get("segment_index") != 2:
                continue
            key = start_key(event)
            queue.setdefault(key, {**event, "date": event.get("date", entry["date"]), "report_version": report["version"]})
    windows = {event["date"]: _window(calendar, event["date"]) for event in queue.values()}
    needed = {day for window in windows.values() for day in window if day <= through}
    prices, factors, suspended = {}, {}, set()
    if needed:
        market = load_evaluation_inputs(store.root.parent, as_of, symbols={key[0] for key in queue}, days=needed,
                                        include_multipliers=True)
        prices = {(row["symbol"], str(row["date"])): row["raw_close"] for row in market.prices.iter_rows(named=True)}
        factors = market.provenance["evaluation_multipliers"]
        suspended = {(r["symbol"], str(r["date"])) for r in market.provenance.get("suspensions", [])}
    results = [evaluate_start(event, window=windows[event["date"]], prices=prices,
                                  factors=factors.get(event["security_id"], {}), suspended=suspended, as_of=through)
                   for event in queue.values()]
    counts = Counter(row["status"] for row in results)
    mature = [row for row in results if row["status"] in ("success", "failed", "expired")
              and row["target_date"] and row["target_date"] <= through]
    return {"evaluation_rule_version": VERSION, "as_of": through, "evaluated_at": evaluated_at,
            "sessions": SESSIONS, "end_return_threshold": END_RETURN, "results": results,
            "summary": {"total": len(results), "success_at_start": counts["success_at_start"],
                        "not_due": counts["not_due"], "unavailable": counts["unavailable"],
                        "counts": dict(counts), "mature_evaluable": len(mature),
                        "success_rate": sum(row["status"] == "success" for row in mature) / len(mature) if mature else None}}
