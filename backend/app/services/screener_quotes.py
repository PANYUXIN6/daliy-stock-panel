"""在响应副本上叠加当日行情；策略信号和持久化快照保持计算时点口径。"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any

import polars as pl

from app.market_time import CN_TZ, cn_today

logger = logging.getLogger(__name__)

_QUOTE_FIELDS = (
    "open", "high", "low", "close", "prev_close", "volume", "amount",
    "change_pct", "change_amount", "turnover_rate",
    "raw_close", "raw_high", "raw_low", "quote_ts",
)


def _day(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return None


def _same_day(row: dict, today: str, as_of: Any) -> bool:
    dates = [value for value in (as_of, row.get("date")) if value is not None]
    return bool(dates) and all(_day(value) == today for value in dates)


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        stamp = float(value)
        return stamp if math.isfinite(stamp) and stamp > 0 else None
    except (ValueError, TypeError):
        return None


def _quotes(repo, symbols: set[str], today: str) -> dict[str, dict]:
    loader = getattr(repo, "get_enriched_latest", None)
    if not callable(loader) or not symbols:
        return {}
    try:
        frame, cache_date = loader(refresh=False)
        if frame is None or frame.is_empty() or _day(cache_date) != today or "symbol" not in frame.columns:
            return {}
        columns = [field for field in ("symbol", "date", *_QUOTE_FIELDS) if field in frame.columns]
        rows = frame.filter(pl.col("symbol").is_in(sorted(symbols))).select(columns).to_dicts()
    except Exception:  # noqa: BLE001 — 行情增强不可阻断已保存结果的读取
        logger.debug("screener live quotes unavailable", exc_info=True)
        return {}
    quotes = {}
    for row in rows:
        if not _same_day(row, today, cache_date):
            continue
        close = row.get("close")
        if not isinstance(close, (int, float)) or not math.isfinite(close) or close <= 0:
            continue
        raw_stamp = row.get("quote_ts")
        stamp = _timestamp(raw_stamp)
        if raw_stamp is not None:
            try:
                if stamp is None or datetime.fromtimestamp(stamp / 1000, CN_TZ).date().isoformat() != today:
                    continue
            except (TypeError, ValueError, OverflowError, OSError):
                continue
        quotes[str(row["symbol"])] = {
            # 行情展示作为一个快照替换，缺失字段清空，避免新价格混用旧涨幅或时间。
            field: (None if isinstance(value := row.get(field), float) and not math.isfinite(value) else value)
            for field in _QUOTE_FIELDS
        }
    return quotes


def with_live_quotes(cached: dict, repo, *, strategy_id: str | None = None) -> dict:
    """同日行情只覆盖展示列；单策略请求只投影该策略及其曾命中行。"""
    today = cn_today().isoformat()
    results = cached.get("results") or {}
    ever = cached.get("today_ever_rows") or {}
    ids = set(results) | set(ever) if strategy_id is None else {strategy_id}
    symbols: set[str] = set()
    for sid in ids:
        result = results.get(sid)
        if isinstance(result, dict):
            as_of = result.get("as_of") or cached.get("as_of")
            symbols.update(
                str(row["symbol"]) for row in result.get("rows") or []
                if isinstance(row, dict) and row.get("symbol") and _same_day(row, today, as_of)
            )
        ever_rows = ever.get(sid)
        for symbol, row in (ever_rows.items() if isinstance(ever_rows, dict) else ()):
            if isinstance(row, dict) and _same_day(row, today, cached.get("as_of")):
                symbols.add(str(symbol))
    quotes = _quotes(repo, symbols, today)
    if not quotes:
        return cached

    def overlay(row, as_of, symbol=None):
        if not isinstance(row, dict) or not _same_day(row, today, as_of):
            return row
        quote = quotes.get(str(symbol or row.get("symbol") or ""))
        if not quote:
            return row
        old_stamp, new_stamp = _timestamp(row.get("quote_ts")), _timestamp(quote.get("quote_ts"))
        if old_stamp is not None and new_stamp is not None and new_stamp < old_stamp:
            return row
        return {**row, **quote}

    new_results, new_ever = dict(results), dict(ever)
    for sid in ids:
        result = results.get(sid)
        if isinstance(result, dict):
            as_of = result.get("as_of") or cached.get("as_of")
            new_results[sid] = {**result, "rows": [overlay(row, as_of) for row in result.get("rows") or []]}
        if isinstance(ever.get(sid), dict):
            new_ever[sid] = {
                symbol: overlay(row, cached.get("as_of"), symbol)
                for symbol, row in ever[sid].items()
            }
    return {**cached, "results": new_results, **({"today_ever_rows": new_ever} if ever else {})}
