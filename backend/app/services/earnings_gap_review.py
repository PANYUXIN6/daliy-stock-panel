"""缺口优先的只读候选与财报核验；外部采集由显式后台任务调用。"""
from __future__ import annotations

import math
from datetime import date

import polars as pl

from app.indicators.earnings_breakout import upward_gap_expr
from app.indicators.pipeline import filter_halt_days
from app.services import preferences
from app.services.earnings_breakout import PRICE_COLUMNS, VERSION_COLUMNS, report_events
from app.services.trading_day import calendar_status

STRATEGY_ID = "earnings_breakout_gap"
_CANDIDATE_COLUMNS = [*PRICE_COLUMNS, "name", "change_pct", "turnover_rate", "status",
                      "total_shares", "float_shares"]


def find_gap_candidates(repo, frame: pl.DataFrame, as_of: date,
                        pool: list[str] | None = None) -> pl.DataFrame:
    """先只读日线找缺口，不在本步骤读取财务或请求供应商。"""
    if frame.is_empty():
        return frame
    current = frame.filter(pl.col("date") == as_of)
    if pool:
        current = current.filter(pl.col("symbol").is_in(pool))
    if current.is_empty():
        return current
    prices = repo.get_daily_batch(current["symbol"].unique().to_list(), date(1990, 1, 1),
                                  as_of, columns=PRICE_COLUMNS)
    live = current.select([c for c in PRICE_COLUMNS if c in current.columns])
    if not prices.is_empty():
        prices = prices.join(live.select("symbol", "date"), on=["symbol", "date"], how="anti")
    prices = filter_halt_days(pl.concat([prices, live], how="diagonal_relaxed"))
    if prices.is_empty():
        return current.head(0)
    gaps = prices.sort(["symbol", "date"]).with_columns(
        upward_gap_expr().alias("_upward_gap"),
        pl.col("high").shift(1).over("symbol").alias("gap_lower"),
        pl.col("low").alias("gap_upper"),
    ).filter((pl.col("date") == as_of) & pl.col("_upward_gap"))
    return current.select([c for c in _CANDIDATE_COLUMNS if c in current.columns]).join(
        gaps.select("symbol", "date", "gap_lower", "gap_upper"), on=["symbol", "date"], how="inner",
    ).unique(subset=["symbol", "date"]).sort("symbol")


def review_candidates(repo, candidates: pl.DataFrame, as_of: date, *,
                      status: str = "not_checked", checked_at: str | None = None,
                      errors: dict[str, str] | None = None, job_id: str | None = None) -> dict:
    """候选与已确认结果分开；无法证明缺口前已可用的版本永不倒填确认。"""
    errors = errors or {}
    versions = pl.DataFrame()
    calendar = {}
    if not candidates.is_empty():
        versions = repo.get_financial_versions("income", columns=VERSION_COLUMNS)
        if not versions.is_empty() and set(versions.columns) >= VERSION_COLUMNS:
            versions = versions.filter(
                (pl.col("source") == preferences.get_financial_provider())
                & pl.col("symbol").is_in(candidates["symbol"].to_list())
            )
        else:
            versions = pl.DataFrame()
        calendar = calendar_status(repo.store.data_dir)
    histories = versions.partition_by("symbol", as_dict=True) if not versions.is_empty() else {}
    rows, classified = [], []
    counts = {"confirmed": 0, "pending": 0, "excluded": 0}
    for original in candidates.iter_rows(named=True):
        row = {key: None if isinstance(value, float) and not math.isfinite(value) else value
               for key, value in original.items()}
        symbol = row["symbol"]
        history = histories.get((symbol,), pl.DataFrame())
        state = "pending"
        reason = "未取得缺口当时可用、且与缺口日对应的首次财报依据"
        if not calendar.get("days"):
            reason = "交易日历覆盖不足，无法核实休市公布后的首个交易日"
        elif history.is_empty():
            reason = "尚无当时可用的同源正式利润表版本"
        else:
            diagnostics: list[dict] = []
            events, _ = report_events(history, calendar, as_of, diagnostics=diagnostics)
            event = next((e for e in events if e["date"] == as_of.isoformat()), None)
            evidence = next((e for e in diagnostics if e["date"] == as_of.isoformat()), None)
            if event:
                state = "confirmed"
                reason = "利润为正且同比增长；版本可用日后的首个交易日形成完整向上缺口"
                row.update(earnings_period_end=event["period_end"],
                           earnings_net_income_yoy=event["net_income_yoy"])
            elif evidence:
                state, reason = evidence["verification_status"], evidence["verification_reason"]
        if symbol in errors:
            # 已经形成的历史确认不因本次网络失败被抹掉，失败仍独立留痕。
            row["financial_refresh_error"] = errors[symbol]
            if state == "pending":
                reason = f"候选利润表更新未完成：{errors[symbol]}；仍缺少缺口当时依据"
        row.update(verification_status=state, verification_reason=reason)
        classified.append(row)
        counts[state] += 1
        if state == "confirmed":
            rows.append(row)
    return {
        "as_of": as_of.isoformat(), "strategy": STRATEGY_ID, "rows": rows,
        "total": len(rows), "elapsed_ms": 0.0,
        "verification": {
            "status": status, "job_id": job_id, "candidate_count": len(classified),
            "confirmed_count": counts["confirmed"], "pending_count": counts["pending"],
            "excluded_count": counts["excluded"], "checked_at": checked_at,
            "candidates": classified,
        },
    }
