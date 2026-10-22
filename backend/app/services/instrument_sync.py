"""标的维表同步服务。

盘前 9:10 调用 tf.exchanges.get_instruments("SH"/"SZ"/"BJ", type="stock")
获取全量标的元数据，flatten ext 字段，写入 instruments.parquet。

Starter+ 盘后可用 quotes.get(universes) 顺便补充 name。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services.fs_utils import atomic_write_parquet
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ", "BJ"]


def _flatten_instruments(items: list[dict]) -> list[dict]:
    """把 SDK 返回的 Instrument 列表 flatten 成扁平行。"""
    rows = []
    for item in items:
        row = {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "code": item.get("code"),
            "exchange": item.get("exchange"),
            "region": item.get("region"),
            "type": item.get("type"),
        }
        ext = item.get("ext") or {}
        # None 表示来源未声明当前成员状态，不能推断为退市或停牌。
        row["is_current"] = ext.get("is_current")
        listing_date = ext.get("listing_date")
        row["listing_date"] = (None if str(listing_date).startswith("1970-01-01")
                               else listing_date)
        row["total_shares"] = ext.get("total_shares")
        row["float_shares"] = ext.get("float_shares")
        row["tick_size"] = ext.get("tick_size")
        row["limit_up"] = ext.get("limit_up")
        row["limit_down"] = ext.get("limit_down")
        rows.append(row)
    return rows


def _mark_current_stocks(tf, rows: list[dict]) -> None:
    """交易所维表含历史标的；用本轮完整 A 股成员名单标记当前范围。"""
    try:
        detail = tf.universes.get("CN_Equity_A")
    except Exception as exc:
        raise RuntimeError("当前 A 股成员名单同步失败") from exc
    symbols = detail.get("symbols") if isinstance(detail, dict) else None
    if not isinstance(symbols, list) or not symbols or any(
        not isinstance(symbol, str) or not symbol for symbol in symbols
    ):
        raise RuntimeError("当前 A 股成员名单为空或无效")
    current = set(symbols)
    count = detail.get("symbol_count", len(symbols))
    if len(current) != len(symbols) or count != len(current):
        raise RuntimeError("当前 A 股成员名单数量不一致")
    missing = current - {row["symbol"] for row in rows}
    if missing:
        raise RuntimeError(f"股票维表缺少 {len(missing)} 只当前 A 股成员")
    for row in rows:
        row["is_current"] = row["symbol"] in current


def _fetch_instruments_via_provider(*, require_complete: bool = False) -> list[dict] | None:
    """若当前日K数据源不是 tickflow 且该 provider 提供 get_instruments, 用它拉标的维表。

    返回 flatten 行列表; 未命中(仍应走 tickflow)时返回 None。
    标的维表跟随日K数据源(二者天然耦合, 无独立偏好项)。
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        return None
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider(provider_name):
        return None
    provider = custom_sources.get_provider(provider_name)
    if not hasattr(provider, "get_instruments"):
        return None
    try:
        items = provider.get_instruments("stock") or []
    except Exception as e:
        if require_complete:
            raise RuntimeError(f"数据源 {provider_name} 股票维表同步失败") from e
        logger.warning("provider %s get_instruments 失败: %s", provider_name, e)
        return None
    rows = _flatten_instruments(items)
    logger.info("instruments via %s: %d stocks", provider_name, len(rows))
    return rows


def sync_instruments(data_dir: Path, *, require_complete: bool = False) -> int:
    """全量同步标的维表 → data/instruments/instruments.parquet。

    返回写入的行数。
    """
    all_rows = (_fetch_instruments_via_provider(require_complete=True)
                if require_complete else _fetch_instruments_via_provider())
    if all_rows is None:
        # 未命中非 tickflow provider → 走 tickflow 直连
        tf = get_client()
        all_rows = []
        for ex in _EXCHANGES:
            try:
                items = tf.exchanges.get_instruments(ex, instrument_type="stock")
                if items:
                    all_rows.extend(_flatten_instruments(items))
                    logger.info("instruments %s: %d stocks", ex, len(items))
                elif require_complete:
                    raise RuntimeError(f"{ex} 股票维表为空")
            except Exception as e:
                if require_complete:
                    raise RuntimeError(f"{ex} 股票维表同步失败") from e
                logger.warning("get_instruments(%s) failed: %s", ex, e, exc_info=True)
        if require_complete:
            # 不用行情快照或旧池缓存推断成员，停牌股票也必须保留在验收范围。
            # 校验失败不覆盖维表；下一次盘后重试重新获取名单。
            _mark_current_stocks(tf, all_rows)

    if not all_rows:
        return 0

    df = pl.DataFrame(all_rows)
    if "is_current" in df.columns:
        df = df.with_columns(pl.col("is_current").cast(pl.Boolean, strict=True))
    df = df.with_columns(pl.lit(cn_today()).alias("as_of"))

    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df, out)

    logger.info("instruments synced: %d rows → %s", df.height, out)
    return df.height


def instruments_synced_on(data_dir: Path, day: date) -> bool:
    """非空维表的 as_of 才是同步成功证据，名称补充的文件时间不能代替。"""
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.is_file():
        return False
    try:
        frame = pl.read_parquet(path, columns=["as_of"])
        return frame.height > 0 and frame["as_of"].eq(day).fill_null(False).all()
    except (OSError, pl.exceptions.PolarsError) as exc:
        logger.warning("instruments 同步日期不可读取，不作为完成证据: %s", exc)
        return False


def enrich_names_from_quotes(
    data_dir: Path,
    quotes_data: list[dict],
) -> int:
    """从 quotes 响应中提取 name，更新 instruments 维表（兜底补充）。

    盘后 quotes.get(universes) 返回的数据中包含 ext.name，
    用来补充 instruments 中可能缺失的 name。
    """
    if not quotes_data:
        return 0

    # 构建 symbol → name 映射
    name_map: dict[str, str] = {}
    for q in quotes_data:
        symbol = q.get("symbol", "")
        ext = q.get("ext") or {}
        name = ext.get("name") or q.get("name", "")
        if symbol and name:
            name_map[symbol] = name

    if not name_map:
        return 0

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0

    df = pl.read_parquet(inst_path)

    # 只更新空 name 的行
    updates = pl.DataFrame({
        "symbol": list(name_map.keys()),
        "_new_name": list(name_map.values()),
    })
    df = df.join(updates, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("name").is_null() | (pl.col("name") == ""))
        .then(pl.col("_new_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
    ).drop("_new_name")

    atomic_write_parquet(df, inst_path)
    logger.info("instruments name enriched from quotes: %d names", len(name_map))
    return len(name_map)
