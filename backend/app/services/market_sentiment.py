"""共用收盘情绪：版本化的描述规则及昨日集合价格表现。

收益仅使用同一 enriched 代际的复权 close；不是成交模拟。日期来自本地交易
日历，缺行保持缺数，不能把另一交易日拼成昨日或把缺失表现写成零。
"""

from __future__ import annotations

import math
from datetime import date
from itertools import pairwise

import polars as pl

VERSION = "market-sentiment-v1"
INPUT_VERSION = "market-sentiment-inputs-v1"
MIN_SAMPLE = 10
COHORTS = {
    "previous_limit_up": ("signal_limit_up", "previous_limit_up_premium"),
    "previous_broken": ("signal_broken_limit_up", "previous_broken_performance"),
}


def _number(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def _and(*values):
    return False if False in values else None if None in values else True


def _or(*values):
    return True if True in values else None if None in values else False


def _test(value, predicate):
    return predicate(value) if _number(value) is not None else None


def _sample(row: dict, prefix: str, value_key: str) -> dict:
    count = row.get(f"{prefix}_valid")
    complete = row.get(f"{prefix}_complete") is True
    value = _number(row.get(value_key))
    usable = (row.get("sentiment_inputs_version") == INPUT_VERSION and complete
              and count is not None and count >= MIN_SAMPLE and value is not None)
    reason = (
        "旧版未记录昨日集合表现" if row.get("sentiment_inputs_version") != INPUT_VERSION
        else "昨日集合或端点数据不完整" if not complete
        else f"有效样本不足{MIN_SAMPLE}只" if count is None or count < MIN_SAMPLE
        else None
    )
    return {
        "value": value,
        "unit": "ratio",
        "source_date": row.get("previous_trade_date"),
        "as_of": str(row["date"]),
        "pool": row.get(f"{prefix}_pool"),
        "valid": count,
        "suspended": row.get(f"{prefix}_suspended"),
        "missing": row.get(f"{prefix}_missing"),
        "limited": row.get(f"{prefix}_limited"),
        "complete": complete,
        "usable": usable,
        "missing_reason": reason,
        "price_basis": "同一数据版本前复权收盘价",
        "data_version": row.get("sentiment_data_version"),
        "rule_version": INPUT_VERSION,
        "formula": "mean(C(D)/C(D-1)-1)，昨日固定集合，剔除已确认停牌端点",
    }


def classify_sentiment(row: dict, prior_rows: list[dict]) -> dict:
    """只读取当日及此前数据；独立组合采用三态逻辑，已成立分支优先。"""
    as_of = str(row["date"])
    complete = row.get("is_complete_day") is True
    n = _number(row.get("limit_up")) if complete else None
    b = _number(row.get("broken_rate")) if complete else None
    premium = _sample(row, "previous_limit_up", "previous_limit_up_premium")
    broken = _sample(row, "previous_broken", "previous_broken_performance")
    p = premium["value"] if complete and premium["usable"] else None
    pool = _number(row.get("promo_pool"))
    j = (_number(row.get("promo_rate"))
         if complete and pool is not None and pool >= MIN_SAMPLE
         and row.get("sentiment_inputs_version") == INPUT_VERSION
         and row.get("previous_trade_date") is not None else None)
    prior = sorted((r for r in prior_rows if str(r["date"]) < as_of), key=lambda r: str(r["date"]))[-5:]
    chain = [*prior, row]
    consecutive = len(prior) == 5 and all(
        str(current.get("previous_trade_date")) == str(previous["date"])
        for previous, current in pairwise(chain)
    )
    comparable = consecutive and all(
        r.get("is_complete_day") is True
        and r.get("method_version") == row.get("method_version")
        and r.get("exclude_st") == row.get("exclude_st")
        for r in prior
    )
    vote_specs = (("limit_up", n, 5, 1), ("broken_rate", b, .03, -1),
                  ("previous_limit_up_premium", p, .005, 1))
    votes = []
    for key, value, threshold, polarity in vote_specs:
        values = [
            _number(r.get(key)) if key != "previous_limit_up_premium"
            or _sample(r, "previous_limit_up", key)["usable"] else None
            for r in prior
        ]
        mean = sum(values) / 5 if comparable and all(v is not None for v in values) else None
        delta = value - mean if value is not None and mean is not None else None
        vote = None if delta is None else (
            1 if polarity * delta >= threshold - 1e-12
            else -1 if polarity * delta <= -threshold + 1e-12 else 0
        )
        votes.append({"key": key, "value": value, "prior_mean": mean, "change": delta,
                      "vote": vote, "prior_dates": [str(r["date"]) for r in prior]})
    ballots = [v["vote"] for v in votes]
    direction = (
        "unknown" if None in ballots else "improving" if ballots.count(1) >= 2
        else "deteriorating" if ballots.count(-1) >= 2 else "mixed"
    )
    heat = "unknown" if n is None else "high" if n >= 60 else "medium" if n >= 40 else "low"
    quality = "unknown" if b is None else "good" if b < .25 else "divergent" if b < .35 else "fragile"
    hot = _test(n, lambda v: v >= 60)
    high_b = _test(b, lambda v: v >= .35)
    low_b = _test(b, lambda v: v < .25)
    p_change = votes[2]["change"]
    branches = [
        ("hot_fragile", _and(hot, high_b)),
        ("hot", _and(hot, low_b, _test(p, lambda v: v > 0))),
        ("warm", _and(hot, _test(b, lambda v: .25 <= v < .35))),
        ("cold", _and(_test(n, lambda v: v < 40), high_b,
                      _or(_test(p, lambda v: v < 0), _test(j, lambda v: v < .15)))),
        ("repair", _and(_test(n, lambda v: v < 60), low_b,
                        _test(p_change, lambda v: v >= .005 - 1e-12),
                        None if direction == "unknown" else direction == "improving")),
    ]
    state = next((label for label, matched in branches if matched is True), None)
    if state is None:
        state = "unknown" if any(matched is None for _, matched in branches) else "mixed"
    return {
        "version": VERSION, "as_of": as_of, "phase": row.get("phase") or "unknown",
        "heat_level": heat, "quality_level": quality,
        "sentiment_direction": direction, "sentiment_state": state,
        "direction_votes": votes,
        "evidence": {
            "limit_up": n, "broken_rate": b,
            "seal_rate": 1 - b if b is not None else None,
            "limit_down": _number(row.get("limit_down")),
            "ge2_count": row.get("ge2_count"), "promo_rate": j, "promo_pool": pool,
            "limit_up_amount": _number(row.get("limit_up_amount")),
            "previous_limit_up_premium": premium, "previous_broken_performance": broken,
        },
        "unknown_reasons": (["收盘口径尚未确认"] if not complete else [])
        + (["涨停家数或炸板率不可计算"] if n is None or b is None else [])
        + (["方向需要此前连续5个完整同口径交易日及有效集合样本"] if direction == "unknown" else [])
        + ([premium["missing_reason"]] if premium["missing_reason"] else []),
        "universe": {"exclude_st": row.get("exclude_st"), "method_version": row.get("method_version")},
    }


def cohort_performance(frame: pl.DataFrame, calendar: list[date]) -> pl.DataFrame:
    """向量化左连接昨日集合到确切目标日，保留缺行和停牌分母。

    frame 已应用 ST 范围，但仍含停牌行；close 必须来自同一 enriched 代际。
    日历缺失时返回未知，绝不以自然日或最近一根有效行情替代。
    """
    if frame.is_empty() or "date" not in frame.columns:
        return pl.DataFrame()
    dates = frame.select(pl.col("date").cast(pl.Date)).unique().sort("date")
    columns = {"date": pl.Date, "previous_trade_date": pl.String,
               "sentiment_inputs_version": pl.String}
    for prefix, (_, value_key) in COHORTS.items():
        columns[value_key] = pl.Float64
        columns.update({f"{prefix}_{suffix}": pl.Int64
                        for suffix in ("pool", "valid", "suspended", "missing", "limited")})
        columns[f"{prefix}_complete"] = pl.Boolean
    output = dates.with_columns([pl.lit(None, dtype=dtype).alias(key)
                                 for key, dtype in columns.items() if key != "date"])
    output = output.with_columns(pl.lit(INPUT_VERSION).alias("sentiment_inputs_version"))
    days = sorted(set(calendar))
    if not days or not {"symbol", "close"}.issubset(frame.columns):
        return output
    pairs = pl.DataFrame({"date": days[1:], "previous_trade_date": days[:-1]},
                         schema={"date": pl.Date, "previous_trade_date": pl.Date})
    defaults = {"volume": pl.Float64, "change_pct": pl.Float64, "quote_ts": pl.Int64,
                "raw_high": pl.Float64, "raw_low": pl.Float64,
                "signal_limit_up": pl.Boolean, "signal_limit_down": pl.Boolean,
                "signal_broken_limit_up": pl.Boolean}
    df = frame.with_columns([pl.lit(None, dtype=dtype).alias(key)
                            for key, dtype in defaults.items() if key not in frame.columns])
    df = df.with_columns(pl.col("date").cast(pl.Date))
    if df.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise ValueError("市场集合表现输入存在重复证券交易日")
    suspended = (pl.col("volume").eq(0) & pl.col("change_pct").eq(0)).fill_null(False)
    formal = pl.col("quote_ts").is_null()
    valid = pl.col("close").is_finite() & pl.col("close").gt(0) & formal
    limited = (pl.col("signal_limit_up") | pl.col("signal_limit_down")
               | (pl.col("raw_high").eq(pl.col("raw_low")) & pl.col("raw_high").gt(0))).fill_null(False)
    df = df.with_columns(suspended.alias("_suspended"), valid.fill_null(False).alias("_valid"),
                         formal.alias("_formal"), limited.alias("_limited"))
    result = dates.join(pairs, on="date", how="left")
    for prefix, (signal, value_key) in COHORTS.items():
        known = pl.col(signal).is_not_null()
        if prefix == "previous_broken":
            known &= pl.col("signal_limit_up").is_not_null()
        known |= pl.col("_suspended")
        source = df.group_by("date").agg(
            (known & pl.col("_formal")).all().alias(f"{prefix}_source_complete"))
        result = result.join(source.rename({"date": "previous_trade_date"}),
                             on="previous_trade_date", how="left")
        pool = df.filter(pl.col(signal).eq(True) & ~pl.col("_suspended"))
        if prefix == "previous_broken":
            pool = pool.filter(pl.col("signal_limit_up").eq(False))
        pool = pool.select("symbol", pl.col("date").alias("previous_trade_date"),
                           pl.col("close").alias("_previous_close"),
                           pl.col("_valid").alias("_previous_valid"),
                           pl.col("_limited").alias("_previous_limited"))
        pool = pool.join(pairs, on="previous_trade_date", how="inner")
        pool = pool.join(df.select("symbol", "date", "close", "_suspended", "_valid",
                                   "_formal", "_limited"), on=["symbol", "date"], how="left")
        halted = (pl.col("_suspended") & pl.col("_formal")).fill_null(False)
        evaluable = (pl.col("_previous_valid") & pl.col("_valid") & ~halted).fill_null(False)
        samples = pool.group_by("date").agg(
            pl.len().cast(pl.Int64).alias(f"{prefix}_pool"),
            evaluable.sum().cast(pl.Int64).alias(f"{prefix}_valid"),
            halted.sum().cast(pl.Int64).alias(f"{prefix}_suspended"),
            (~halted & ~evaluable).sum().cast(pl.Int64).alias(f"{prefix}_missing"),
            (evaluable & (pl.col("_limited") | pl.col("_previous_limited")))
            .sum().cast(pl.Int64).alias(f"{prefix}_limited"),
            (pl.col("close") / pl.col("_previous_close") - 1).filter(evaluable).mean().alias(value_key),
        )
        result = result.join(samples, on="date", how="left")
        known_source = pl.col(f"{prefix}_source_complete").is_not_null()
        result = result.with_columns([
            pl.when(known_source).then(pl.col(f"{prefix}_{key}").fill_null(0))
            .otherwise(None).alias(f"{prefix}_{key}")
            for key in ("pool", "valid", "suspended", "missing", "limited")
        ])
        result = result.with_columns(
            (pl.col(f"{prefix}_source_complete") & pl.col(f"{prefix}_missing").eq(0))
            .alias(f"{prefix}_complete"))
    return result.with_columns(
        pl.col("previous_trade_date").cast(pl.String),
        pl.lit(INPUT_VERSION).alias("sentiment_inputs_version"),
    ).select(list(columns))
