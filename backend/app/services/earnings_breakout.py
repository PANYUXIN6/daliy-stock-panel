"""本地财务版本 -> 固定财报事件 -> 日K断层与回踩标记；查询不采集、不写盘。"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from app.indicators.earnings_breakout import (
    compute_earnings_breakout,
    compute_prepared_earnings_breakout,
    pullback_price_candidates,
)
from app.indicators.pipeline import filter_halt_days
from app.services import preferences
from app.services.trading_day import calendar_status

VERSION_COLUMNS = {
    "symbol", "source", "period_end", "disclosure_type", "available_date",
    "observed_at", "availability_basis", "net_income",
}

PRICE_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
PULLBACK_COLUMNS = [
    "signal_earnings_9", "signal_earnings_42",
    "earnings_9_event_date", "earnings_42_event_date",
]


@dataclass
class EarningsPriceSnapshot:
    """One command's immutable market inputs; never stored in shared/user caches."""

    data_dir: Path | None = None
    as_of: date | None = None
    symbols: frozenset[str] = frozenset()
    prices: pl.DataFrame = field(default_factory=pl.DataFrame)
    pullback: dict[str, pl.DataFrame] = field(default_factory=dict)

    def capture(self, repo, frame: pl.DataFrame, as_of: date) -> pl.DataFrame:
        self.data_dir = repo.store.data_dir.resolve()
        self.as_of = as_of
        self.symbols = frozenset(frame["symbol"].unique().to_list())
        prices = repo.get_daily_batch(sorted(self.symbols), date(1990, 1, 1), as_of, columns=PRICE_COLUMNS)
        if set(PRICE_COLUMNS) <= set(frame.columns):
            prices = _overlay_prices(prices, frame.select(PRICE_COLUMNS))
        self.prices = filter_halt_days(prices).sort(["symbol", "date"]) if not prices.is_empty() else prices
        self.pullback.clear()
        return self.prices

    def matches(self, repo, end: date) -> bool:
        return self.data_dir == repo.store.data_dir.resolve() and self.as_of == end

    def prepare_pullback_candidates(self, targets: pl.DataFrame) -> pl.DataFrame:
        candidates = pullback_price_candidates(self.prices, targets, prepared=self.pullback)
        if self.pullback:
            # Retain the prepared frames' OHLCV buffers, not a second complete copy.
            self.prices = pl.concat([frame.select(PRICE_COLUMNS) for frame in self.pullback.values()],
                                    rechunk=False)
        return candidates


def _overlay_prices(prices: pl.DataFrame, live: pl.DataFrame) -> pl.DataFrame:
    if prices.is_empty():
        return live
    return pl.concat([prices.join(live.select("symbol", "date"), on=["symbol", "date"], how="anti"), live],
                     how="diagonal_relaxed")


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _profit(row: dict) -> float | None:
    value = row.get("net_income")
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def report_events(versions: pl.DataFrame, calendar: dict, end: date, *,
                  diagnostics: list[dict] | None = None) -> tuple[list[dict], int]:
    """每期只在首次可用交易日尝试一次，不用修订重新激活旧报告或补认旧缺口。

    当前期和比较期均取事件日之前已生效的同源完整版本。首次基线沿用版本库的
    保守可用日期；没有该日期之前的版本证据时不前推。无需复制财报或保存新基线。
    """
    if versions.is_empty() or not set(versions.columns) >= VERSION_COLUMNS:
        return [], 0
    if versions.select(["symbol", "source"]).unique().height > 1:
        raise ValueError("财报事件不能混合标的或数据源")
    rows = []
    for row in versions.iter_rows(named=True):
        period, available = _day(row["period_end"]), _day(row["available_date"])
        if (row["disclosure_type"] == "formal" and period and available
                and period <= available <= end and row["observed_at"]
                and row["availability_basis"] in {"observed", "source_update_conservative"}):
            observed = datetime.fromisoformat(row["observed_at"])
            if observed.tzinfo is None:
                continue
            rows.append({**row, "_period": period, "_available": available,
                         "_observed": observed.astimezone(UTC)})
    # 同一报告期按首次观察确定首版；修订的数值与日期不改变这次尝试的事件日。
    rows.sort(key=lambda r: (r["_observed"], r["_available"]))
    reports: dict[date, list[dict]] = {}
    for row in rows:
        reports.setdefault(row["_period"], []).append(row)
    days = sorted({_day(d) for d in calendar.get("days", [])} - {None})
    cal_start, cal_end = _day(calendar.get("start")), _day(calendar.get("end"))
    events = []
    if not days or not cal_start or not cal_end:
        return events, len(reports)
    for period, history in sorted(reports.items()):
        first = history[0]
        available = first["_available"]
        if not cal_start <= available <= cal_end:
            continue
        index = bisect_right(days, available)
        if index >= len(days) or days[index] > min(end, cal_end):
            continue
        event_day = days[index]
        active = {
            p: max((r for r in records if r["_available"] < event_day),
                   key=lambda r: (r["_available"], r["_observed"]), default=None)
            for p, records in reports.items()
        }
        # 旧季度晚到或晚更新，不能伪装成最新一期首次财报事件。
        if period != max(p for p, row in active.items() if row is not None):
            continue
        current = active[period]
        prior_period = _day(f"{period.year - 1}{period.isoformat()[4:]}")
        prior = active.get(prior_period)
        profit, previous = _profit(current), _profit(prior or {})
        reason, status = "", "confirmed"
        if profit is None:
            reason, status = "缺少当时可用的净利润", "pending"
        elif profit <= 0:
            reason, status = "对应财报净利润不为正", "excluded"
        elif previous is None or previous == 0:
            reason, status = "缺少当时有效的上年同期利润，无法核实同比", "pending"
        elif (profit - previous) / abs(previous) <= 0:
            reason, status = "对应财报净利润同比未增长", "excluded"
        if diagnostics is not None:
            diagnostics.append({"date": event_day.isoformat(), "period_end": period.isoformat(),
                                "verification_status": status, "verification_reason": reason})
        if status != "confirmed":
            continue
        growth = 100 * (profit - previous) / abs(previous)
        events.append({"date": event_day.isoformat(), "period_end": period.isoformat(),
                       "available_date": current["available_date"],
                       "observed_at": current["observed_at"],
                       "comparison_observed_at": prior["observed_at"],
                       "availability_basis": current["availability_basis"],
                       "net_income": profit, "net_income_yoy": growth})
    return events, len(reports)


def filter_pullback_signals(repo, frame: pl.DataFrame, *, price_snapshot: EarningsPriceSnapshot | None = None) -> pl.DataFrame:
    """只返回输入中的9或42回踩行，保持现有回踩策略口径。"""
    selected = _filter_earnings_signals(repo, frame, gap=False, price_snapshot=price_snapshot)
    if selected.is_empty():
        return selected
    return selected.with_columns(
        pl.when(pl.col("signal_earnings_9") & pl.col("signal_earnings_42"))
        .then(pl.lit("缺口、中点"))
        .when(pl.col("signal_earnings_9")).then(pl.lit("缺口"))
        .otherwise(pl.lit("中点")).alias("earnings_pullback")
    )


def filter_gap_signals(repo, frame: pl.DataFrame, *, price_snapshot: EarningsPriceSnapshot | None = None) -> pl.DataFrame:
    """只返回业绩断层形成日，对应主图“断”，不附加回踩条件。"""
    return _filter_earnings_signals(repo, frame, gap=True, price_snapshot=price_snapshot)


def _filter_earnings_signals(repo, frame: pl.DataFrame, *, gap: bool,
                           price_snapshot: EarningsPriceSnapshot | None = None) -> pl.DataFrame:
    """跳空与回踩共用财报事件及价格计算，返回输入日期内的命中行。

    财务与价格各批量读取一次；按股计算只在内存中进行。输入行情覆盖同日存储行，
    使实时筛选使用调用方当前日K。价格从完整前序起算，不受筛选历史短窗影响。
    """
    if frame.is_empty():
        return frame
    output_columns = ["earnings_gap"] if gap else PULLBACK_COLUMNS
    end = frame["date"].max()
    source = preferences.get_financial_provider()
    versions = repo.get_financial_versions("income", columns=VERSION_COLUMNS)
    if versions.is_empty() or not set(versions.columns) >= VERSION_COLUMNS:
        return frame.head(0)
    versions = versions.filter(
        (pl.col("source") == source) & pl.col("symbol").is_in(frame["symbol"].unique().to_list())
    )
    if versions.is_empty():
        return frame.head(0)
    calendar = calendar_status(repo.store.data_dir)
    if not calendar.get("days"):
        raise ValueError("缺少交易日历，无法核实业绩断层的财报生效交易日")
    events_by_symbol = {}
    for (symbol,), history in versions.partition_by("symbol", as_dict=True).items():
        events, _ = report_events(history, calendar, end)
        if events:
            events_by_symbol[symbol] = {_day(event["date"]) for event in events}
    if not events_by_symbol:
        return frame.head(0)
    reused = price_snapshot if price_snapshot is not None and price_snapshot.matches(repo, end) else None
    if reused is None:
        prices = repo.get_daily_batch(list(events_by_symbol), date(1990, 1, 1), end, columns=PRICE_COLUMNS)
    else:
        prices = reused.prices.filter(pl.col("symbol").is_in(list(events_by_symbol))) if not reused.prices.is_empty() else reused.prices
        missing = set(events_by_symbol) - reused.symbols
        if missing:
            extra = repo.get_daily_batch(sorted(missing), date(1990, 1, 1), end, columns=PRICE_COLUMNS)
            prices = pl.concat([prices, extra], how="diagonal_relaxed")
    # 只使用调用方输入的前复权OHLCV，既补实时尾部，也不把结果回写行情缓存。
    live = frame.filter(pl.col("symbol").is_in(list(events_by_symbol))).select(
        [column for column in PRICE_COLUMNS if column in frame.columns]
    )
    prices = _overlay_prices(prices, live)
    hits = []
    for (symbol,), bars in prices.partition_by("symbol", as_dict=True).items():
        prepared = reused.pullback.get(symbol) if reused is not None else None
        # The final history window can override more than the current candle.
        # Reuse only identical OHLCV; changed callers retain the normal calculation.
        valid_bars = filter_halt_days(bars).sort("date")
        if (prepared is not None and set(PRICE_COLUMNS) <= set(valid_bars.columns)
                and prepared.select(PRICE_COLUMNS).equals(valid_bars.select(PRICE_COLUMNS))):
            calculated = compute_prepared_earnings_breakout(prepared, events_by_symbol[symbol])
        else:
            calculated = compute_earnings_breakout(bars, events_by_symbol[symbol])
        if calculated.is_empty():
            continue
        condition = (pl.col("earnings_gap") if gap else
                     pl.col("signal_earnings_9") | pl.col("signal_earnings_42"))
        selected = calculated.filter(condition)
        if not selected.is_empty():
            hits.append(selected.select("symbol", "date", *output_columns))
    if not hits:
        return frame.head(0)
    signals = pl.concat(hits).unique(subset=["symbol", "date"])
    return frame.drop([column for column in output_columns if column in frame.columns]).join(
        signals, on=["symbol", "date"], how="inner",
    )


def get_earnings_breakout(repo, symbol: str, start: date, end: date, live_row=None) -> dict:
    source = preferences.get_financial_provider()
    versions = repo.get_financial_versions("income", symbol, VERSION_COLUMNS)
    if not versions.is_empty() and "source" in versions.columns:
        versions = versions.filter(pl.col("source") == source)
    result = {"symbol": symbol, "source": source, "status": "ready", "reports": 0,
              "eligible_reports": 0, "events": [], "markers": []}
    if versions.is_empty():
        result["status"] = "no_versions"
        return result
    calendar = calendar_status(repo.store.data_dir)
    if not calendar.get("days"):
        result["status"] = "no_calendar"
        return result
    candidates, count = report_events(versions, calendar, end)
    result.update(reports=count, eligible_reports=len(candidates))
    if not candidates:
        result["status"] = "data_insufficient"
        return result
    # 前序 EMA/COUNT 不在可见窗口重置；存储列下推跳过其他指标与全市场计算。
    bars = repo.get_daily_asset("stock", symbol, date(1990, 1, 1), end,
                                columns=["symbol", "date", "open", "high", "low", "close", "volume", "amount"])
    if live_row is not None:
        live = pl.DataFrame([live_row]).with_columns(pl.col("date").str.to_date())
        bars = pl.concat([bars.filter(pl.col("date") != live["date"][0]), live],
                         how="diagonal_relaxed") if not bars.is_empty() else live
    frame = compute_earnings_breakout(bars, {_day(e["date"]) for e in candidates})
    if frame.is_empty():
        result["status"] = "no_prices"
        return result
    gap_days = set(frame.filter(pl.col("earnings_gap"))["date"].to_list())
    result["events"] = [e for e in candidates if _day(e["date"]) in gap_days and start <= _day(e["date"]) <= end]
    frame = frame.filter(pl.col("date") >= start)
    for icon in (9, 42):
        for row in frame.filter(pl.col(f"signal_earnings_{icon}")).iter_rows(named=True):
            result["markers"].append({"date": row["date"].isoformat(), "icon": icon,
                                      "event_date": row[f"earnings_{icon}_event_date"].isoformat()})
    result["markers"].sort(key=lambda m: (m["date"], m["icon"]))
    return result
