"""A 股趋势强度梯队，复用前复权日线指标，不触发采集或写入。

动量分档及状态优先级移植自 hzy1522/tickflow-stock-panel 的强度梯队。
新高、新低统一采用 0.5% 容差，缺失输入不视为零值或突破。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.services.screener import ScreenerService

NEAR_HIGH_RATIO = 0.995
NEAR_LOW_RATIO = 1.005
# 含下界、不含上界；数值均为小数制收益率。
MOMENTUM_TIERS = ((5, 0.25, None), (4, 0.15, 0.25), (3, 0.08, 0.15), (2, 0.03, 0.08))


def build_strength_ladder(frame: pl.DataFrame, as_of: date | None) -> dict:
    result = {
        "as_of": as_of.isoformat() if as_of else None,
        "tiers": [], "total": frame.height, "eligible": 0,
        "counts": {"near_high": 0, "near_low": 0}, "warnings": [],
    }
    if frame.is_empty():
        return result
    if "symbol" not in frame.columns:
        result["warnings"].append("行情缺少股票代码，无法计算趋势梯队")
        return result

    numeric_cols = ("close", "change_pct", "momentum_20d", "vol_ratio_5d", "high_60d", "low_60d")
    expressions = [pl.col("symbol"), pl.col("name") if "name" in frame.columns else pl.lit(None).alias("name")]
    for name in numeric_cols:
        value = pl.col(name).cast(pl.Float64, strict=False) if name in frame.columns else pl.lit(None, dtype=pl.Float64)
        if name == "momentum_20d":
            # 11.5 / 10 - 1 会得到 0.1499999999999999，不能因此误落到15%以下一档。
            value = value.round(12)
        expressions.append(pl.when(value.is_finite()).then(value).otherwise(None).alias(name))
    for name in ("signal_n_day_high", "signal_n_day_low"):
        expressions.append(pl.col(name).fill_null(False) if name in frame.columns else pl.lit(False).alias(name))
    df = frame.select(expressions).filter(pl.col("symbol").is_not_null() & (pl.col("close") > 0))
    near_high = (
        ((pl.col("high_60d") > 0) & (pl.col("close") >= pl.col("high_60d") * NEAR_HIGH_RATIO)).fill_null(False)
        | pl.col("signal_n_day_high")
    )
    near_low = (
        ((pl.col("low_60d") > 0) & (pl.col("close") <= pl.col("low_60d") * NEAR_LOW_RATIO)).fill_null(False)
        | pl.col("signal_n_day_low")
    )
    df = df.with_columns(near_high.alias("near_high"), near_low.alias("near_low"))
    result["counts"] = {"near_high": int(df["near_high"].sum()), "near_low": int(df["near_low"].sum())}
    result["eligible"] = df.filter(pl.col("momentum_20d").is_not_null()).height
    if result["eligible"] < result["total"]:
        result["warnings"].append("部分股票缺少有效的20日动量或价格，未纳入梯队；20日动量需要至少21个交易日行情")
    if "high_60d" not in frame.columns or "low_60d" not in frame.columns:
        result["warnings"].append("60日高低点数据不完整，相关状态与统计仅使用已有有效信号")

    # 先新高，再放量上涨，其余为动量；只有20日动量至少3%的股票入榜。
    df = df.filter(pl.col("momentum_20d") >= 0.03).with_columns(
        pl.when(pl.col("near_high")).then(pl.lit("high"))
        .when((pl.col("vol_ratio_5d") >= 1.5) & (pl.col("change_pct") > 0)).then(pl.lit("volume"))
        .otherwise(pl.lit("momentum")).alias("status"),
    ).sort(["momentum_20d", "change_pct", "symbol"], descending=[True, True, False], nulls_last=True)
    columns = ["symbol", "name", "close", "change_pct", "momentum_20d", "vol_ratio_5d", "status"]
    for level, minimum, maximum in MOMENTUM_TIERS:
        mask = pl.col("momentum_20d") >= minimum
        if maximum is not None:
            mask &= pl.col("momentum_20d") < maximum
        stocks = df.filter(mask).select(columns).to_dicts()
        if stocks:
            result["tiers"].append({"level": level, "minimum": minimum, "maximum": maximum, "count": len(stocks), "stocks": stocks})
    return result


def get_strength_ladder(repo, as_of: date | None = None) -> dict:
    service = ScreenerService(repo)
    target = as_of or service.latest_date()
    # 60根实际交易记录同时覆盖20日动量（21根）、60日极值及5日量比。
    frame = service._load_enriched_for_date(target, required_bars=60) if target else pl.DataFrame()
    return build_strength_ladder(frame, target)
