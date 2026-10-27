"""本地前复权日线 -> 周期 K 线及箱体；只读，不采集、不写盘。"""
from __future__ import annotations

import calendar as month_calendar
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Literal

import polars as pl

from app.indicators.box_oscillation import PERIOD_CONFIG, compute_box_oscillation
from app.indicators.pipeline import compute_indicators, filter_halt_days
from app.market_time import CN_TZ, cn_now
from app.services.trading_day import calendar_status

BoxPeriod = Literal["daily", "weekly", "monthly"]
_PRICE_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "quote_ts"]
_CHART_INDICATORS = {
    "ma5", "ma10", "ma20", "ma60", "bull_bear_line", "macd_dif", "macd_dea",
    "macd_hist", "rsi_6", "rsi_14", "rsi_24", "kdj_k", "kdj_d", "kdj_j",
    "boll_upper", "boll_lower",
}


def _period_end(day: date, period: BoxPeriod) -> date:
    if period == "weekly":
        return day + timedelta(days=6 - day.weekday())
    if period == "monthly":
        return day.replace(day=month_calendar.monthrange(day.year, day.month)[1])
    return day


def _period_closed(last: date, end: date, period: BoxPeriod, calendar: dict) -> bool:
    """截止日之后尚有未知日期时，不把当前周/月提前封口。"""
    natural_end = _period_end(last, period)
    if natural_end <= end:
        return True
    if not calendar or calendar.get("start", "9999") > last.isoformat() or calendar.get("end", "") < natural_end.isoformat():
        return False
    return not any(last.isoformat() < day <= natural_end.isoformat() for day in calendar["days"])


def _prepare_daily(bars: pl.DataFrame, symbol: str, end: date, now: datetime) -> tuple[pl.DataFrame, list[str]]:
    diagnostics: list[str] = []
    if bars.is_empty():
        return bars, diagnostics
    required = {"date", "open", "high", "low", "close"}
    if not required <= set(bars.columns):
        raise ValueError("本地日线缺少 OHLC 字段")
    if bars.schema["date"] == pl.String:
        bars = bars.with_columns(pl.col("date").str.slice(0, 10).str.to_date(strict=False))
    else:
        bars = bars.with_columns(pl.col("date").cast(pl.Date, strict=False))
    bars = bars.filter(pl.col("date").is_not_null() & (pl.col("date") <= end))
    bars = bars.sort("date").unique("date", keep="last", maintain_order=True)
    if bars.is_empty():
        return bars, diagnostics
    for column in ("volume", "amount", "quote_ts"):
        if column not in bars.columns:
            bars = bars.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    bars = bars.with_columns(
        pl.lit(symbol).alias("symbol"),
        # 停牌过滤的三值布尔逻辑会丢掉 null 行。局部以 NaN 保留未知值，
        # 让无效价格进入下方的隔断判断；未知量额也不能直接当成零成交停牌。
        *(pl.col(column).cast(pl.Float64, strict=False).fill_null(float("nan"))
          for column in _PRICE_COLUMNS[2:-1]),
        pl.col("quote_ts").cast(pl.Float64, strict=False),
    )
    bars = filter_halt_days(bars)
    if bars.is_empty():
        return bars, diagnostics
    valid_price = pl.all_horizontal(
        pl.col(column).is_finite() & (pl.col(column) > 0)
        for column in ("open", "high", "low", "close")
    ) & (pl.col("high") >= pl.max_horizontal("open", "close")) & (pl.col("low") <= pl.min_horizontal("open", "close"))
    # null quote_ts 是正式日线；非空值必须确为该日收盘后的快照。
    day_ms = pl.col("date").cast(pl.Datetime).dt.replace_time_zone("Asia/Shanghai").dt.epoch("ms")
    quote_complete = pl.col("quote_ts").is_null() | (
        pl.col("quote_ts").is_finite()
        & (pl.col("quote_ts") >= day_ms + 15 * 3_600_000)
        & (pl.col("quote_ts") < day_ms + 86_400_000)
    )
    day_closed = (pl.col("date") < now.date()) | (
        (pl.col("date") == now.date()) & pl.lit(now.time() >= dt_time(15, 30))
    )
    bars = bars.with_columns(valid_price.fill_null(False).alias("_valid"),
                             (quote_complete & day_closed).fill_null(False).alias("is_complete"))
    bars = bars.with_columns(
        pl.col("_valid").all().over(pl.col("date").dt.truncate("1w")).alias("_week_valid"),
        pl.col("_valid").all().over(pl.col("date").dt.truncate("1mo")).alias("_month_valid"),
    )
    bad_prices = bars.filter(~pl.col("_valid")).height
    stale_snapshots = bars.filter(pl.col("_valid") & ~quote_complete.fill_null(False)).height
    if bad_prices:
        diagnostics.append(f"已排除 {bad_prices} 根价格异常 K 线，箱体不会跨异常记录确认。")
    if stale_snapshots:
        diagnostics.append(f"有 {stale_snapshots} 根日线仍为未收盘快照，不参与箱体确认。")
    bars = bars.with_columns((~pl.col("_valid") | ~pl.col("is_complete")).cast(pl.Int64).cum_sum().alias("_segment"))
    bars = bars.filter(pl.col("_valid")).drop("_valid")
    return bars, diagnostics


def _aggregate(bars: pl.DataFrame, period: BoxPeriod, end: date, now: datetime, calendar: dict) -> pl.DataFrame:
    if bars.is_empty():
        return bars
    if period == "daily":
        return bars
    window = "1w" if period == "weekly" else "1mo"
    grouped = bars.group_by_dynamic("date", every=window, label="left", closed="left").agg(
        pl.col("symbol").first(), pl.col("date").last().alias("_last_date"),
        pl.col("open").first(), pl.col("high").max(), pl.col("low").min(), pl.col("close").last(),
        pl.col("volume").sum(), pl.col("amount").sum(),
        pl.col("is_complete").all(), pl.col("_segment").last(),
        (pl.col("_segment").n_unique() == 1).alias("_unbroken"),
        pl.col("_week_valid" if period == "weekly" else "_month_valid").all().alias("_period_valid"),
    ).drop("date").rename({"_last_date": "date"}).sort("date")
    # 只判断每个周期一次；计算指标和聚合本身仍采用 Polars 批处理。
    # 月末当天若尚未收盘且仓库还没有今日行，不能仅凭“今天是月末”
    # 把昨天结尾的聚合月 K 提前确认。日历能证明今天休市时才可提前封口。
    closed_through = now.date() if now.time() >= dt_time(15, 30) else now.date() - timedelta(days=1)
    closed = [_period_closed(day, min(end, closed_through), period, calendar) for day in grouped["date"]]
    grouped = grouped.with_columns(
        (pl.col("is_complete") & pl.col("_unbroken") & pl.col("_period_valid") & pl.Series("_closed", closed)).alias("is_complete"),
    ).drop("_unbroken", "_period_valid")
    return grouped


def get_box_oscillation(repo, symbol: str, start: date, end: date, period: BoxPeriod = "daily", *, now: datetime | None = None) -> dict:
    """全量已存历史确定结构，显示范围只裁剪结果，避免改变起点及首次确认日。"""
    now = now or cn_now()
    now = now.astimezone(CN_TZ) if now.tzinfo else now.replace(tzinfo=CN_TZ)
    end = min(end, now.date())
    asset = repo.resolve_asset_type(symbol)
    config = PERIOD_CONFIG[period]
    report = {
        "symbol": symbol, "asset_type": asset, "period": period, "status": "no_prices",
        "history_start": None, "as_of": None, "rows": [], "boxes": [], "diagnostics": [],
        "config": {"pivot_window": config.pivot_left, "min_bars": config.min_bars,
                   "medium_bars": config.medium_bars, "long_bars": config.long_bars,
                   "touch_tolerance_ratio": config.touch_tolerance_ratio,
                   "breakout_margin_ratio": config.breakout_margin_ratio,
                   "breakout_closes": config.breakout_closes},
    }
    raw = repo.get_daily_asset(asset, symbol, date(1990, 1, 1), end, columns=_PRICE_COLUMNS)
    bars, diagnostics = _prepare_daily(raw, symbol, end, now)
    report["diagnostics"] = diagnostics
    if bars.is_empty():
        return report
    calendar = calendar_status(repo.store.data_dir)
    report["history_start"] = bars["date"][0].isoformat()
    if not calendar or calendar.get("start", "9999") > bars["date"][0].isoformat():
        diagnostics.append("本地交易日历未覆盖全部历史，按已存有效交易记录计数，不补齐缺失 K 线。")
    else:
        saved = set(bars["date"].to_list())
        missing = sum(1 for day in calendar["days"] if bars["date"][0].isoformat() <= day <= end.isoformat() and date.fromisoformat(day) not in saved)
        if missing:
            diagnostics.append(f"日历范围内有 {missing} 个交易日无有效记录（可能停牌或缺采），不补点；请核对本地数据完整性。")
    periods = _aggregate(bars, period, end, now, calendar)
    if periods.filter(~pl.col("is_complete")).height:
        diagnostics.append("未完成的日／周／月 K 线仅供展示，不参与触点、箱体或突破确认。")
    complete = periods.filter(pl.col("is_complete"))
    if not complete.is_empty():
        report["as_of"] = complete["date"][-1].isoformat()
    report["status"] = "ready" if complete.height >= config.min_bars else "insufficient_data"
    boxes: list[dict] = []
    # 缺失/未完成的历史快照隔断判断，不能把缺口两边拼成新的往返。
    for chunk in complete.partition_by("_segment", maintain_order=True):
        found = compute_box_oscillation(chunk, period)
        if chunk["date"][-1] != complete["date"][-1]:
            for box in found:
                if box["status"] in {"candidate", "confirmed"}:
                    box["status"] = "invalidated"
                    box["missing_conditions"].append("后续记录不完整，结构已停止延续")
        boxes.extend(box for box in found if box["status"] != "invalidated" or box["confirmed_date"])
    report["boxes"] = [box for box in boxes if box["end_date"] >= start.isoformat()]
    chart = compute_indicators(periods.with_columns(pl.col("volume").fill_nan(None).fill_null(0)), needed=_CHART_INDICATORS)
    chart = chart.filter(pl.col("date") >= start).select("date", "open", "high", "low", "close", "volume", "amount", "is_complete", *_CHART_INDICATORS)
    float_columns = [column for column, dtype in chart.schema.items() if dtype.is_float()]
    chart = chart.with_columns(pl.when(pl.col(column).is_finite()).then(pl.col(column)).otherwise(None).alias(column) for column in float_columns)
    report["rows"] = chart.to_dicts()
    return report
