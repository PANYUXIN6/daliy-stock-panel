"""重点ETF资金看板：只读快照、交易日对齐与有覆盖证据的聚合。"""
from __future__ import annotations

import json
import math
from datetime import date
from functools import lru_cache
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services.etf_flow_universe import CATEGORIES, FOCUS_FUNDS, FOCUS_SYMBOLS

HISTORY_START = date(2024, 1, 1)
FLOW_THRESHOLD = 100 * 1e8
# 分钟K为结束时刻标签，09:30为开盘集合竞价，午后首根13:01。
MINUTE_TIMES = tuple(f"{minute // 60:02d}:{minute % 60:02d}"
                     for minute in (*range(570, 691), *range(781, 901)))


def snapshot_path(data_dir: Path) -> Path:
    return data_dir / "market_reference" / "etf_flow.json"


@lru_cache(maxsize=4)
def _read_cached(path: str, modified: int) -> dict:
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    snapshot["history"] = history_coverage(snapshot)
    return snapshot


def read_snapshot(data_dir: Path) -> dict:
    path = snapshot_path(data_dir)
    try:
        return _read_cached(str(path), path.stat().st_mtime_ns)
    except FileNotFoundError:
        return {}


def enabled_symbols(data_dir: Path) -> tuple[str, ...]:
    # 由管理员首次显式同步激活；页面浏览不会添加后台订阅。
    return FOCUS_SYMBOLS if snapshot_path(data_dir).exists() else ()


def finite(value, *, positive: bool = False) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and (number > 0 if positive else number >= 0) else None


def build_daily(funds: list[dict], sessions: list[str], shares: list[dict], nav: list[dict],
                prices: list[dict]) -> list[dict]:
    """份额必须与上一交易日相连。缺失、上市首日不假定前一日份额为零。"""
    if len(sessions) < 2 or not funds:
        return []
    calendar = pl.DataFrame({"date": sessions[1:], "previous_date": sessions[:-1]})
    universe = pl.DataFrame(funds).select("symbol", "category", "list_date")
    grid = universe.join(calendar, how="cross").filter(pl.col("list_date") <= pl.col("date"))
    schema = {"symbol": pl.String, "date": pl.String}

    def frame(rows, field):
        result = pl.DataFrame(rows, schema={**schema, field: pl.Float64}) if rows else pl.DataFrame(schema={**schema, field: pl.Float64})
        if result.select("symbol", "date").is_duplicated().any():
            raise ValueError("ETF资金输入存在重复的标的日期")
        return result.with_columns(pl.when(pl.col(field).is_finite() & (pl.col(field) > 0))
                                   .then(pl.col(field)).otherwise(None).alias(field))

    share_frame = frame(shares, "shares")
    joined = (grid.join(share_frame, on=["symbol", "date"], how="left")
              .join(share_frame.rename({"date": "previous_date", "shares": "previous_shares"}),
                    on=["symbol", "previous_date"], how="left")
              .join(frame(nav, "unit_nav"), on=["symbol", "date"], how="left")
              .with_columns(((pl.col("shares") - pl.col("previous_shares")) * pl.col("unit_nav")).alias("net_flow_yuan")))
    by_day = {str(key[0]): group.to_dicts() for key, group in joined.partition_by("date", as_dict=True).items()}
    price_map = {(row["symbol"], row["date"]): row for row in prices}
    output = []
    for day in sessions[1:]:
        rows = by_day.get(day, [])

        def aggregate(items):
            flows = [row["net_flow_yuan"] for row in items if row["net_flow_yuan"] is not None]
            return {"net_flow_yuan": sum(flows) if items and len(flows) == len(items) else None,
                    "available_flow_yuan": sum(flows) if flows else None,
                    "covered": len(flows), "expected": len(items)}

        focus = [row for row in rows if row["symbol"] in FOCUS_SYMBOLS]
        amounts = [finite(price_map.get((row["symbol"], day), {}).get("amount")) for row in focus]
        valid = [amount for amount in amounts if amount is not None]
        item = {"date": day, **aggregate(focus),
                "broad": aggregate(rows),
                "amount_yuan": sum(valid) if focus and len(valid) == len(focus) else None,
                "amount_covered": len(valid),
                "index_close": finite(price_map.get(("000001.SH", day), {}).get("close"), positive=True),
                "categories": {category: aggregate([row for row in rows if row["category"] == category]) for category in CATEGORIES}}
        item["signal"] = item["net_flow_yuan"] > FLOW_THRESHOLD if item["net_flow_yuan"] is not None else None
        output.append(item)
    return output


def minute_profile(frame: pl.DataFrame, index: pl.DataFrame, day: str,
                   previous_closes: dict[str, float] | None = None) -> dict:
    """同分钟20只齐全才给总额；累计值遇到缺口后保持缺失。"""
    previous_closes = previous_closes or {}
    expected = len(FOCUS_SYMBOLS)
    bars = {}
    if not frame.is_empty():
        for row in frame.filter(pl.col("symbol").is_in(FOCUS_SYMBOLS)
                                & (pl.col("datetime").dt.date().cast(pl.String) == day)).to_dicts():
            minute = row["datetime"].strftime("%H:%M")
            if minute in MINUTE_TIMES:
                bars[(row["symbol"], minute)] = row
    latest = max((minute for _, minute in bars), default=None)
    index_map = {} if index.is_empty() else {
        row["datetime"].strftime("%H:%M"): finite(row.get("close"), positive=True)
        for row in index.filter(pl.col("datetime").dt.date().cast(pl.String) == day).to_dicts()
    }
    points, cumulative, intact = [], 0.0, True
    for minute in MINUTE_TIMES:
        values = [finite(bars.get((symbol, minute), {}).get("amount")) for symbol in FOCUS_SYMBOLS]
        valid = [value for value in values if value is not None]
        amount = sum(valid) if len(valid) == expected else None
        intact = intact and amount is not None
        if intact:
            cumulative += amount
        points.append({"time": minute, "amount_yuan": amount,
                       "cumulative_yuan": cumulative if intact else None,
                       "covered": len(valid), "index_close": index_map.get(minute)})
    ranking = []
    elapsed = [minute for minute in MINUTE_TIMES if latest is not None and minute <= latest]
    for symbol, name, category in FOCUS_FUNDS:
        values = [finite(bars.get((symbol, minute), {}).get("amount")) for minute in elapsed]
        complete = bool(values) and all(value is not None for value in values)
        close = finite(bars.get((symbol, latest), {}).get("close"), positive=True)
        previous = finite(previous_closes.get(symbol), positive=True)
        ranking.append({"symbol": symbol, "name": name, "category": category,
                        "amount_yuan": sum(values) if complete else None,
                        "change_pct": close / previous - 1 if close is not None and previous is not None else None,
                        "minutes": sum(value is not None for value in values), "expected_minutes": len(elapsed)})
    total = next((p["cumulative_yuan"] for p in points if p["time"] == latest), None)
    for row in ranking:
        row["weight"] = row["amount_yuan"] / total if total is not None and total > 0 and row["amount_yuan"] is not None else None
    ranking.sort(key=lambda row: (row["amount_yuan"] is not None, row["amount_yuan"] or 0), reverse=True)
    return {"date": day, "as_of": latest, "points": points, "ranking": ranking,
            "amount_yuan": total, "covered": sum(row["amount_yuan"] is not None for row in ranking),
            "expected": expected, "complete": all(p["amount_yuan"] is not None for p in points)}


def average_profiles(profiles: list[dict]) -> list[dict]:
    points = []
    for index, minute in enumerate(MINUTE_TIMES):
        sample = [profile["points"][index] for profile in profiles]
        amounts = [p["amount_yuan"] for p in sample if p["amount_yuan"] is not None]
        cumulative = [p["cumulative_yuan"] for p in sample if p["cumulative_yuan"] is not None]
        points.append({"time": minute, "amount_yuan": sum(amounts) / len(amounts) if amounts else None,
                       "samples": len(amounts), "cumulative_yuan": sum(cumulative) / len(cumulative) if cumulative else None,
                       "cumulative_samples": len(cumulative)})
    return points


def history_coverage(snapshot: dict) -> dict:
    daily = [r for r in snapshot.get("daily", []) if r["date"] >= HISTORY_START.isoformat()]
    profiles = sorted((p for p in snapshot.get("profiles", {}).values() if p.get("as_of")), key=lambda p: p["date"])
    complete = [p for p in profiles if p.get("complete")]
    flow = [r for r in daily if r["net_flow_yuan"] is not None]
    minute_start = profiles[0]["date"] if profiles else None
    minute_end = profiles[-1]["date"] if profiles else None
    expected = [d for d in snapshot.get("sessions", []) if minute_start and minute_start <= d <= minute_end]
    full_dates = {p["date"] for p in complete}
    symbols = []
    for symbol in FOCUS_SYMBOLS:
        observed = [p for p in profiles if any(r["symbol"] == symbol and r["minutes"] > 0 for r in p["ranking"])]
        symbols.append({"symbol": symbol, "start": observed[0]["date"] if observed else None,
                        "end": observed[-1]["date"] if observed else None, "days": len(observed)})
    return {
        "daily": {"target_start": HISTORY_START.isoformat(), "start": daily[0]["date"] if daily else None,
                  "end": daily[-1]["date"] if daily else None, "expected_days": len(daily),
                  "amount_days": sum(r["amount_yuan"] is not None for r in daily), "flow_days": len(flow),
                  "flow_start": flow[0]["date"] if flow else None, "flow_end": flow[-1]["date"] if flow else None,
                  "broad_flow_days": sum(r.get("broad", {}).get("net_flow_yuan") is not None for r in daily),
                  "missing_flow_dates": [r["date"] for r in daily if r["net_flow_yuan"] is None]},
        "minute": {"start": minute_start, "end": minute_end, "observed_days": len(profiles),
                   "complete_days": len(complete), "expected_days": len(expected),
                   "complete_start": complete[0]["date"] if complete else None,
                   "missing_dates": [d for d in expected if d not in full_dates], "symbols": symbols},
    }


def read_board(repo, selected: date | None = None, window: int = 60) -> dict:
    from app.services import trading_day

    snapshot = read_snapshot(repo.store.data_dir)
    today = cn_today().isoformat()
    calendar = trading_day.calendar_status(repo.store.data_dir)
    sessions = sorted(set(snapshot.get("sessions", [])) | {s for s in calendar.get("days", []) if s <= today})
    day = (selected.isoformat() if selected else sessions[-1] if sessions else today)
    prior = [s for s in sessions if s < day]
    comparison_days = prior[-20:]
    profiles = snapshot.get("profiles", {})
    previous_day = prior[-1] if prior else None
    previous_closes = {r["symbol"]: r["close"] for r in snapshot.get("prices", []) if r["date"] == previous_day}
    from app.services.minute_adjust import minute_basis_is_raw

    # 旧分钟价格为拉取时前复权，历史价格不能和原始日线直接相除。
    comparable = day == today or minute_basis_is_raw(repo.store.data_dir)
    current = minute_profile(repo.get_minute_batch(list(FOCUS_SYMBOLS), date.fromisoformat(day), asset_type="etf", adjust=False),
                             repo.get_minute("000001.SH", date.fromisoformat(day), asset_type="index"), day, previous_closes)
    if not comparable:
        for row in current["ranking"]:
            row["change_pct"] = None
    daily = [row for row in snapshot.get("daily", []) if HISTORY_START.isoformat() <= row["date"] <= day]
    if window:
        daily = daily[-window:]
    history = [profiles[d] for d in comparison_days if profiles.get(d, {}).get("as_of")]
    signal_days = [row["date"] for row in daily if row["signal"] is True]
    normal_days = [row["date"] for row in daily if row["signal"] is False]
    status_path = snapshot_path(repo.store.data_dir).with_name("etf_flow_status.json")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        status = {}
    dates = [d for d in sessions if d >= max(HISTORY_START.isoformat(), (snapshot.get("sessions") or [day])[0])]
    return {"state": "ready" if snapshot else "no_data", "selected_date": day,
            "calendar_through": calendar.get("end"),
            "calendar_current": bool(calendar.get("end") and calendar["end"] >= today),
            "dates": dates, "updated_at": snapshot.get("updated_at"), "status": status,
            "sources": snapshot.get("sources", {}), "catalog_as_of": snapshot.get("catalog_as_of"),
            "history": snapshot.get("history") or history_coverage({}),
            "funds": snapshot.get("funds", []), "categories": list(CATEGORIES), "daily": daily,
            "coverage": {"start": daily[0]["date"] if daily else None, "end": daily[-1]["date"] if daily else None,
                         "flow_days": sum(row["net_flow_yuan"] is not None for row in daily),
                         "daily_days": len(daily), "minute_days": sum(p.get("as_of") is not None for p in history),
                         "minute_start": history[0]["date"] if history else None,
                         "minute_end": history[-1]["date"] if history else None,
                         "comparison_days": len(comparison_days)},
            "intraday": current, "previous": profiles.get(previous_day),
            "average": average_profiles(history),
            "signal_average": average_profiles([profiles[d] for d in signal_days if d in profiles]),
            "normal_average": average_profiles([profiles[d] for d in normal_days if d in profiles]),
            "cases": [profiles[d] for d in signal_days[-5:] if d in profiles],
            "threshold_yuan": FLOW_THRESHOLD}
