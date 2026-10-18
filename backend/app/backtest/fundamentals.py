"""共享财务时间口径：版本生效日后首个交易日可用，缺失保持 null/NaN。

首次回填按源更新日保守门控，持续观察后按本地观察日与更新日的较晚者门控。
仅使用版本库；浏览表的旧数据没有历史版本证据。派生指标从同源、当时可用的
完整报表计算，比较期修订也只能影响修订生效后的因子。最新报告期优先于更新日。
"""
from __future__ import annotations

import logging
import math
from itertools import groupby
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np
import polars as pl

from app.services.financial_versions import read_versions

logger = logging.getLogger(__name__)

# 财务因子名 -> (metrics 表列名, 是否需要除以收盘价)
# pb_latest 使用不复权价格 / 财报 BPS，估值快照反推值不满足该口径。
FUNDAMENTAL_FACTORS: dict[str, dict[str, Any]] = {
    "pb_latest": {"column": "bps", "price_ratio": True},
    "roe_latest": {"column": "roe", "price_ratio": False},
    "gross_margin_latest": {"column": "gross_margin", "price_ratio": False},
    "net_margin_latest": {"column": "net_margin", "price_ratio": False},
    "revenue_yoy_latest": {"column": "revenue_yoy", "price_ratio": False},
    "net_income_yoy_latest": {"column": "net_income_yoy", "price_ratio": False},
    "debt_ratio_latest": {"column": "debt_to_asset_ratio", "price_ratio": False},
}

FUNDAMENTAL_FACTOR_NAMES = frozenset(FUNDAMENTAL_FACTORS)


def load_fundamental_snapshot(data_dir: Path | None) -> pl.DataFrame | None:
    """重建按版本生效的指标事件，不从旧浏览文件或 mtime 推断可用时间。"""
    if data_dir is None:
        return None
    try:
        events = []
        needed = {spec["column"] for spec in FUNDAMENTAL_FACTORS.values()} | {
            "symbol", "source", "period_end", "available_date", "observed_at",
            "availability_basis", "disclosure_type", "bps_basis", "revenue", "operating_cost",
            "net_income", "net_income_attributable", "total_assets", "total_liabilities",
        }
        for table in ("metrics", "income", "balance_sheet"):
            frame = read_versions(data_dir, table, needed)
            for row in frame.iter_rows(named=True):
                if (row.get("available_date") and row.get("symbol") and row.get("period_end")
                        and row.get("disclosure_type") in {"formal", "unknown"}):
                    events.append({**row, "_table": table})
    except (OSError, pl.exceptions.PolarsError) as exc:
        logger.warning("读取财务版本失败: %s", exc, exc_info=True)
        return None
    rows = []
    events.sort(key=lambda r: (r["symbol"], r["source"], r["available_date"],
                               r["observed_at"], r["period_end"], r["_table"]))
    for (symbol, source), source_events in groupby(events, lambda r: (r["symbol"], r["source"])):
        state = {table: {} for table in ("metrics", "income", "balance_sheet")}
        for available, daily in groupby(source_events, lambda r: r["available_date"]):
            for event in daily:
                state[event["_table"]][event["period_end"]] = event
            period = max(p for periods in state.values() for p in periods)
            metrics = state["metrics"].get(period, {})
            income = state["income"].get(period, {})
            balance = state["balance_sheet"].get(period, {})
            previous = state["income"].get(f"{int(period[:4]) - 1}{period[4:]}", {})
            basis = {r["availability_basis"] for r in (metrics, income, balance, previous) if r}
            values = {spec["column"]: _number(metrics.get(spec["column"]))
                      for spec in FUNDAMENTAL_FACTORS.values()}
            if metrics.get("bps_basis") == "valuation_snapshot":
                values["bps"] = None
            derived = {
                "gross_margin": _margin(income.get("revenue"), income.get("operating_cost")),
                "net_margin": _ratio(income.get("net_income"), income.get("revenue")),
                "revenue_yoy": _growth(income.get("revenue"), previous.get("revenue")),
                "net_income_yoy": _growth(income.get("net_income_attributable"),
                                           previous.get("net_income_attributable")),
                "debt_to_asset_ratio": _ratio(balance.get("total_liabilities"), balance.get("total_assets")),
            }
            for column, value in derived.items():
                dependency = balance if column == "debt_to_asset_ratio" else income
                if dependency:
                    values[column] = value
            rows.append({"symbol": symbol, "source": source, "period_end": period,
                         "announce_date": available, "availability_basis": ",".join(sorted(basis)), **values})
    return _report_events(pl.DataFrame(rows, infer_schema_length=None)) if rows else None


def _number(value) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _ratio(numerator, denominator) -> float | None:
    numerator, denominator = _number(numerator), _number(denominator)
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return 100 * numerator / denominator


def _margin(revenue, cost) -> float | None:
    revenue, cost = _number(revenue), _number(cost)
    return _ratio(revenue - cost, revenue) if revenue is not None and cost is not None else None


def _growth(current, previous) -> float | None:
    current, previous = _number(current), _number(previous)
    if current is None or previous is None or previous == 0:
        return None
    return 100 * (current - previous) / abs(previous)


def _report_events(snapshot: pl.DataFrame) -> pl.DataFrame:
    """先按生效时间过滤版本，再选择最新报告期；旧季度晚更新不能倒退报告期。"""
    columns = {spec["column"] for spec in FUNDAMENTAL_FACTORS.values()}
    frame = snapshot.with_columns([
        pl.lit(None, dtype=pl.Float64).alias(c) for c in columns - set(snapshot.columns)
    ])
    frame = frame.with_columns(
        pl.col("announce_date").cast(pl.String).str.slice(0, 10).str.to_date(strict=False).alias("_announce"),
        pl.col("period_end").cast(pl.String).str.slice(0, 10).str.to_date(strict=False).alias("_period"),
    ).filter(pl.col("_announce").is_not_null() & pl.col("_period").is_not_null())
    frame = frame.sort(["symbol", "_announce", "period_end"], maintain_order=True).with_columns(
        pl.col("_period").cum_max().over("symbol").alias("_latest_period")
    ).filter(pl.col("_period") == pl.col("_latest_period"))
    return frame.unique(subset=["symbol", "_announce"], keep="last", maintain_order=True).drop("_latest_period", "_period")


def attach_fundamental_factors(
    panel: pl.DataFrame,
    snapshot: pl.DataFrame | None,
    names: Any,
) -> pl.DataFrame:
    """把财务因子列按公告日门控地并入日频面板。

    - snapshot 为 None (本地无财务数据): 产出全 null 列, 保持面板形状,
      由上层决定是否报"无财务数据"错误;
    - 面板必须已按 (symbol, date) 排序 (存储与挖掘路径均满足)。
    """
    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    missing_columns = [name for name in requested if name not in panel.columns]
    if not missing_columns:
        return panel

    if snapshot is None:
        return panel.with_columns([
            pl.lit(None, dtype=pl.Float64).alias(name)
            for name in missing_columns
        ])

    snapshot = _report_events(snapshot)

    columns = sorted(
        {FUNDAMENTAL_FACTORS[name]["column"] for name in missing_columns}
    )
    # asof 键取生效日 (公告日次日) 而非公告日: 直接用公告日回看会在换报告期的
    # 公告当日取到「尚未生效」的新一期并被门控置 null, 打断上一期的前向填充,
    # 与矩阵路径 (searchsorted side="right") 不一致。
    right = (
        snapshot.select(["symbol", "_announce", *columns])
        .with_columns(pl.col("_announce").dt.offset_by("1d").alias("_effective"))
        .sort(["symbol", "_effective"])
    )
    joined = panel.join_asof(
        right,
        left_on="date",
        right_on="_effective",
        by="symbol",
        strategy="backward",
        check_sortedness=False,  # 双侧均已按 (symbol, key) 排序, 免除逐组检查开销
    )
    announced = pl.col("_announce").is_not_null() & (pl.col("date") > pl.col("_announce"))
    expressions = []
    for name in missing_columns:
        spec = FUNDAMENTAL_FACTORS[name]
        source = pl.col(spec["column"])
        if spec["price_ratio"]:
            value = (
                pl.when(source > 0)
                .then((pl.col("raw_close") if "raw_close" in panel.columns else pl.lit(None, dtype=pl.Float64)) / source)
                .otherwise(None)
            )
        else:
            value = source
        expressions.append(
            pl.when(announced).then(value).otherwise(None).alias(name)
        )
    return joined.with_columns(expressions).drop("_effective")


def build_fundamental_matrices(
    market: Any,
    snapshot: pl.DataFrame | None,
    names: Any,
) -> dict[str, np.ndarray]:
    """为 MarketDataMatrix 构建财务因子 TxN float32 字段。

    与 attach_fundamental_factors 同一口径: 公告日次一交易日起前向填充,
    无数据为 NaN。PB 使用 raw_close / 财报 BPS。
    """
    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    if not requested:
        return {}

    shape = market.shape
    result: dict[str, np.ndarray] = {}
    if snapshot is None:
        for name in requested:
            result[name] = np.full(shape, np.nan, dtype=np.float32)
        return result

    snapshot = _report_events(snapshot)

    asset_index = {symbol: index for index, symbol in enumerate(market.symbols)}
    labels = market.timestamp_labels
    label_dates = np.array([label[:10] for label in labels], dtype="datetime64[D]")

    raw_columns = {
        FUNDAMENTAL_FACTORS[name]["column"]: np.full(shape, np.nan, dtype=np.float32)
        for name in requested
    }
    announce_text = snapshot["announce_date"].str.slice(0, 10)
    for row_index, symbol in enumerate(snapshot["symbol"].to_list()):
        column_index = asset_index.get(symbol)
        if column_index is None:
            continue
        announce = announce_text[row_index]
        if announce is None:
            continue
        # 公告日之后 (严格大于) 的首个时间行索引
        start = int(np.searchsorted(label_dates, np.datetime64(announce, "D"), side="right"))
        if start >= shape[0]:
            continue
        for column, target in raw_columns.items():
            value = snapshot[column][row_index]
            numeric = float("nan") if value is None else float(value)
            # 新一期该指标为空时必须覆盖旧值为 NaN: 跳过写入会让同一行混用两期
            # 报告 (bps 取新期、roe 停在上一期), 与 polars 侧 join_asof 只认
            # 最新一期整行的口径不一致。
            target[start:, column_index] = numeric if np.isfinite(numeric) else np.nan

    for name in requested:
        spec = FUNDAMENTAL_FACTORS[name]
        source = raw_columns[spec["column"]]
        if spec["price_ratio"]:
            with np.errstate(divide="ignore", invalid="ignore"):
                price = market.fields.get("raw_close")
                matrix = (price / source).astype(np.float32) if price is not None else np.full(shape, np.nan, dtype=np.float32)
            matrix[~(source > 0)] = np.nan
            matrix[np.isinf(matrix)] = np.nan
        else:
            matrix = source
        result[name] = matrix
    return result


def attach_matrix_fundamental_fields(market: Any, data_dir: Path | None, names: Any) -> Any:
    """把财务因子作为 matrix fields 附加到 (frozen) MarketDataMatrix 副本。"""
    import dataclasses

    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    if not requested:
        return market
    snapshot = load_fundamental_snapshot(data_dir)
    extra = build_fundamental_matrices(market, snapshot, requested)
    if not extra:
        return market
    merged = {**dict(market.fields), **extra}
    for array in extra.values():
        array.flags.writeable = False
    return dataclasses.replace(market, fields=MappingProxyType(merged))
