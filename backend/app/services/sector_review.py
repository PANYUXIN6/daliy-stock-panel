"""Read-only orchestration, fixed membership, version checks and bounded single-flight cache."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from app.market_time import CN_TZ, cn_now, cn_today
from app.services import sector_review_rules as rules
from app.services.ext_data import ExtConfigStore
from app.services.index_const import CORE_INDEX_NAMES
from app.services.market_overview_builder import _dimension_values, _symbol_keys
from app.services.snapshot_cache import SnapshotCache
from app.services.trading_day import calendar_status
from app.tickflow.sector_review_reader import ReviewDataUnavailableError

_CACHE = SnapshotCache(ttl=300, maxsize=8)
_AVAILABILITY = SnapshotCache(ttl=30, maxsize=4)


class ReviewParameterError(ValueError):
    pass


class ReviewSnapshotConflictError(ValueError):
    pass


def _id(*parts) -> str:
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()[:24]


def _calendar(reader) -> tuple[dict, list[date]]:
    try:
        calendar = calendar_status(reader.data_dir)
        days = sorted({date.fromisoformat(d) for d in calendar.get("days", [])})
        if not days or not calendar.get("start") or not calendar.get("end"):
            raise ValueError
        return calendar, days
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise ReviewDataUnavailableError("本地交易日历不足，请先维护日历") from exc


def _dimensions(reader) -> list[tuple[dict, object, str]]:
    dimensions = []
    for config in ExtConfigStore(reader.data_dir).load_all_readonly():
        if config.market_level:
            continue
        for field in config.fields:
            text = f"{field.name} {field.label}".lower()
            kind = (
                "concept"
                if any(v in text for v in ("概念", "concept", "theme"))
                else "industry"
                if any(v in text for v in ("行业", "industry", "sector"))
                else None
            )
            if kind:
                dimensions.append(
                    (
                        {
                            "dimension_id": _id(config.id, field.name),
                            "config_id": config.id,
                            "field": field.name,
                            "label": f"{config.label} · {field.label}",
                            "kind": kind,
                            "levels": [1, 2, 3] if kind == "industry" else [],
                            "mode": config.mode,
                        },
                        config,
                        field.name,
                    )
                )
    return dimensions


def _benchmark(reader, symbol: str, days: list[date], event_date: date | None) -> dict[date, dict]:
    frame = reader.raw("kline_index_daily", days[0], days[-1]).filter(pl.col("symbol") == symbol)
    if frame.select(pl.col("date").is_duplicated().any()).item():
        raise ReviewDataUnavailableError("基准指数存在重复日期，请先核对日线")
    valid_price = (
        pl.col("quote_ts").is_null()
        & pl.all_horizontal(
            pl.col(c).is_finite() & (pl.col(c) > 0) for c in ("open", "high", "low", "close")
        )
        & (pl.col("high") >= pl.max_horizontal("open", "close", "low"))
        & (pl.col("low") <= pl.min_horizontal("open", "close"))
    )
    frame = (
        pl.DataFrame({"date": days})
        .join(frame, on="date", how="left")
        .with_columns(
            pl.when(valid_price).then(pl.col("close")).otherwise(None).alias("price"),
            pl.when(valid_price & pl.col("amount").is_finite() & (pl.col("amount") >= 0))
            .then(pl.col("amount"))
            .otherwise(None)
            .alias("amount"),
        )
        .with_columns(
            (pl.col("price") / pl.col("price").shift(1) - 1).alias("day_return"),
            pl.col("amount").shift(1).rolling_mean(20, min_samples=20).alias("amount_base"),
            pl.col("price").shift(1).rolling_max(20, min_samples=20).alias("prior20_close_high"),
        )
    )
    if event_date:
        amounts = frame.filter(pl.col("date") < event_date).tail(20)["amount"]
        base = amounts.mean() if len(amounts) == 20 and amounts.null_count() == 0 else None
        frame = frame.with_columns(pl.lit(base, dtype=pl.Float64).alias("event_amount_base"))
    base = pl.col("event_amount_base") if event_date else pl.col("amount_base")
    frame = frame.with_columns(
        pl.when(base > 0).then(pl.col("amount") / base).otherwise(None).alias("amount_ratio")
    )
    return {r["date"]: r for r in frame.to_dicts()}


def availability(repo) -> dict:
    reader = repo.sector_review_reader()
    try:
        versions = reader.versions()
        key = (str(reader.data_dir.resolve()), cn_today(), _id(versions), rules.RULES_VERSION)
        return _AVAILABILITY.get_or_compute(key, lambda: _availability(reader, versions))
    except (ReviewDataUnavailableError, OSError, pl.exceptions.PolarsError) as exc:
        return {
            "status": "unavailable",
            "default_date": None,
            "latest_local_date": None,
            "calendar": None,
            "dimensions": [d[0] for d in _dimensions(reader)],
            "benchmarks": [{"symbol": s, "name": n} for s, n in CORE_INDEX_NAMES.items()],
            "etf": {"status": "unconfigured", "reason": "尚未配置宽基ETF观察篮子"},
            "warnings": [
                str(exc)
                if isinstance(exc, ReviewDataUnavailableError)
                else "本地行情暂不可读，请稍后刷新"
            ],
        }


def _availability(reader, versions) -> dict:
    calendar, days = _calendar(reader)
    files = reader.files("kline_daily", end=cn_today())
    local_dates = sorted({date.fromisoformat(f.parent.name[5:]) for f in files})
    recent = [d for d in days if d <= cn_today() and d in local_dates]
    default = None
    audit = []
    # Readiness follows the postmarket current universe. Historical research
    # below deliberately retains noncurrent instruments with valid past bars.
    instruments = reader.instruments().filter(pl.col("is_current").fill_null(True))
    # Search backwards in bounded batches; an older complete day must not be
    # silently lost merely because recent partitions contain snapshots.
    if recent and not instruments.is_empty():
        descending = list(reversed(recent))
        for offset in range(0, len(descending), 60):
            batch = descending[offset : offset + 60]
            prices = reader.prices(batch[-1], batch[0], instruments)
            indices = reader.raw("kline_index_daily", batch[-1], batch[0])
            for day in batch:
                expected = instruments.filter(
                    pl.col("listing_date").str.to_date(strict=False).is_null()
                    | (pl.col("listing_date").str.to_date(strict=False) <= day)
                )
                rows = prices.filter(
                    (pl.col("date") == day) & pl.col("symbol").is_in(expected["symbol"].implode())
                )
                good = rows.filter(
                    pl.col("formal_complete") | (pl.col("reason") == "halted")
                ).height
                index_rows = indices.filter(
                    (pl.col("date") == day)
                    & pl.col("symbol").is_in(list(CORE_INDEX_NAMES))
                    & pl.col("quote_ts").is_null()
                    & pl.all_horizontal(
                        pl.col(c).is_finite() & (pl.col(c) > 0)
                        for c in ("open", "high", "low", "close")
                    )
                    & pl.col("amount").is_finite()
                    & (pl.col("amount") >= 0)
                    & (pl.col("high") >= pl.max_horizontal("open", "close", "low"))
                    & (pl.col("low") <= pl.min_horizontal("open", "close"))
                )
                if len(audit) < 5:
                    audit.append(
                        {
                            "date": day.isoformat(),
                            "expected": expected.height,
                            "formal_or_halted": good,
                            "formal_indices": index_rows["symbol"].n_unique(),
                        }
                    )
                if (
                    good == expected.height
                    and good > 0
                    and index_rows["symbol"].n_unique() == len(CORE_INDEX_NAMES)
                ):
                    default = day.isoformat()
                    break
            if default:
                break
    dimensions = []
    for info, config, field in _dimensions(reader):
        if info["kind"] == "industry":
            source = reader.classification(config, field, min(days[-1], cn_today()))
            depths = (
                [
                    len(value.split("-"))
                    for raw in source[field].to_list()
                    for value in _dimension_values(raw)
                ]
                if field in source.columns
                else []
            )
            info["levels"] = list(range(1, min(3, max(depths, default=1)) + 1))
        dimensions.append(info)
    if reader.versions() != versions:
        raise ReviewDataUnavailableError("本地数据正在更新，请重新读取")
    return {
        "status": "ready" if default else "partial",
        "default_date": default,
        "latest_local_date": local_dates[-1].isoformat() if local_dates else None,
        "calendar": {"start": calendar["start"], "end": calendar["end"], "days": calendar["days"]},
        "dimensions": dimensions,
        "benchmarks": [{"symbol": s, "name": n} for s, n in CORE_INDEX_NAMES.items()],
        "etf": {"status": "unconfigured", "reason": "尚未配置宽基ETF观察篮子"},
        "audit": audit,
        "input_versions": versions,
        "warnings": []
        if default
        else ["本地尚未发现股票及四指数均完整的正式日线；请明确选择日期核对缺失。"],
    }


def _check_day(day: date, calendar: dict, days: list[date]):
    if day > cn_today():
        raise ReviewParameterError("请求日期不能晚于北京时间今天")
    if not calendar["start"] <= day.isoformat() <= calendar["end"]:
        raise ReviewDataUnavailableError("请求日期超出本地交易日历覆盖")
    if day not in days:
        before = next((d.isoformat() for d in reversed(days) if d < day), "无")
        after = next((d.isoformat() for d in days if d > day), "无")
        raise ReviewParameterError(f"所选日期不是交易日，相邻交易日：{before}、{after}")


@dataclass
class ReviewSnapshot:
    payload: dict
    grid: pl.DataFrame
    mapping: pl.DataFrame
    eligibility: pl.DataFrame
    period: list[date]


def get_snapshot(
    repo,
    *,
    mode: str,
    kind: str = "industry",
    dimension_id: str | None = None,
    level: int = 1,
    benchmark: str = "000001.SH",
    as_of: date | None = None,
    window: int = 20,
    event_date: date | None = None,
    view_as_of: date | None = None,
) -> ReviewSnapshot:
    if (
        benchmark not in CORE_INDEX_NAMES
        or window not in rules.WINDOWS
        or kind not in {"industry", "concept"}
        or level not in (1, 2, 3)
        or mode not in {"event", "countertrend"}
    ):
        raise ReviewParameterError("不支持的基准、分类、层级或窗口")
    reader = repo.sector_review_reader()
    metadata_versions = reader.versions()
    calendar, days = _calendar(reader)
    target = event_date if mode == "event" else as_of
    if target is None:
        latest = availability(repo)["default_date"]
        if latest is None:
            raise ReviewDataUnavailableError("尚无完整正式行情日期，请选择日期核对或维护正式日线")
        target = date.fromisoformat(latest)
    _check_day(target, calendar, days)
    end = (view_as_of or target) if mode == "event" else target
    _check_day(end, calendar, days)
    tidx, eidx = days.index(target), days.index(end)
    if mode == "event" and not 0 <= eidx - tidx <= 3:
        raise ReviewParameterError("观察日必须在事件T至T+3交易日内")
    start_idx = tidx if mode == "event" else tidx - window + 1
    if start_idx < 1:
        raise ReviewDataUnavailableError("日历不足以覆盖所选窗口及成员形成日")
    formation = days[start_idx - 1]
    needed = days[max(0, start_idx - 82) : eidx + 1]
    selected = next(
        (
            d
            for d in _dimensions(reader)
            if d[0]["kind"] == kind
            and (dimension_id is None or d[0]["dimension_id"] == dimension_id)
        ),
        None,
    )
    if selected is None:
        raise ReviewParameterError("分类来源不存在或不属于所选类型，请重新选择")
    context = {
        "mode": mode,
        "kind": kind,
        "dimension_id": selected[0]["dimension_id"],
        "dimension_label": selected[0]["label"],
        "level": level if kind == "industry" else None,
        "benchmark": benchmark,
        "benchmark_name": CORE_INDEX_NAMES[benchmark],
        "as_of": end.isoformat(),
        "event_date": target.isoformat() if mode == "event" else None,
        "view_as_of": end.isoformat() if mode == "event" else None,
        "window": window if mode == "countertrend" else None,
        "formation_date": formation.isoformat(),
        "window_start": days[start_idx].isoformat(),
        "min_members": rules.MIN_MEMBERS,
        "core_k": 5,
        "breakout_window": 20,
    }
    for _ in range(2):
        versions = reader.versions(needed[0], end)
        if any(versions[key] != metadata_versions[key] for key in ("calendar", "classification")):
            raise ReviewDataUnavailableError("日历或分类在解析期间更新，请重新读取")
        key = (
            str(reader.data_dir.resolve()),
            _id(context),
            _id(versions),
            rules.RULES_VERSION,
            cn_today(),
        )
        try:
            return _CACHE.get_or_compute(
                key,
                lambda versions=versions: _build(
                    reader, selected, context, versions, days, needed, start_idx, eidx
                ),
            )
        except ReviewSnapshotConflictError:
            continue
    raise ReviewDataUnavailableError("读取期间数据持续更新，请稍后刷新")


def _mapping(
    reader, selected, instruments: pl.DataFrame, level: int, cutoff: date
) -> tuple[pl.DataFrame, list[dict]]:
    info, config, field = selected
    frame = reader.classification(config, field, cutoff)
    symbols = {s: s for s in instruments["symbol"]}
    symbols.update({s.split(".")[0]: s for s in instruments["symbol"]})
    groups = {}
    for row in frame.to_dicts():
        symbol = next((symbols[key] for key in _symbol_keys(row, config) if key in symbols), None)
        if symbol is None:
            # Keep a visible excluded member instead of silently dropping unknown assets.
            symbol = next(iter(_symbol_keys(row, config)), "")
        if not symbol:
            continue
        for value in _dimension_values(row.get(field)):
            parts = value.split("-") if info["kind"] == "industry" else [value]
            if info["kind"] == "industry" and level > len(parts):
                continue
            path = "-".join(parts[:level]) if info["kind"] == "industry" else value
            sid = _id(info["dimension_id"], level if info["kind"] == "industry" else None, path)
            group = groups.setdefault(
                sid,
                {
                    "sector_id": sid,
                    "name": parts[level - 1] if info["kind"] == "industry" else value,
                    "path": path,
                    "symbols": set(),
                },
            )
            group["symbols"].add(symbol)
    records = [
        {"sector_id": sid, "symbol": s}
        for sid, group in groups.items()
        for s in sorted(group["symbols"])
    ]
    return pl.DataFrame(records, schema={"sector_id": pl.String, "symbol": pl.String}), list(
        groups.values()
    )


def _build(
    reader, selected, context, versions, calendar_days, needed, start_idx, eidx
) -> ReviewSnapshot:
    formation = date.fromisoformat(context["formation_date"])
    event = date.fromisoformat(context["event_date"]) if context["mode"] == "event" else None
    end = date.fromisoformat(context["as_of"])
    period = calendar_days[start_idx : eidx + 1]
    instruments = reader.instruments()
    if instruments.is_empty():
        raise ReviewDataUnavailableError("本地证券维表为空，无法确认可比较A股样本")
    prices = reader.prices(needed[0], end, instruments)
    grid, eligible = rules.prepare_members(prices, instruments, needed, formation, event)
    mapping, groups = _mapping(reader, selected, instruments, context["level"] or 1, formation)
    market_mapping = (
        eligible.filter(pl.col("formation_eligible"))
        .select("symbol")
        .with_columns(pl.lit("_market").alias("sector_id"))
    )
    all_mapping = pl.concat([mapping, market_mapping.select("sector_id", "symbol")])
    aggregated = rules.aggregate(grid, all_mapping, event, formation, needed)
    daily = {}
    for row in aggregated.to_dicts():
        daily.setdefault(row.pop("sector_id"), {})[row["date"]] = row
    benchmark = _benchmark(reader, context["benchmark"], needed, event)
    member_rows = mapping.join(eligible, on="symbol", how="left").with_columns(
        pl.col("formation_eligible").fill_null(False),
        pl.col("exclusion_reason").fill_null(
            pl.when(pl.col("formation_eligible").is_null())
            .then(pl.lit("unknown_asset"))
            .otherwise(None)
        ),
    )
    eligibility_map = {r["symbol"]: r for r in eligible.to_dicts()}
    names = dict(instruments.select("symbol", "name").iter_rows())
    period_grid = grid.filter(pl.col("date").is_in(period))
    window_returns = period_grid.group_by("symbol").agg(
        pl.when(pl.col("day_return").count() == len(period))
        .then((pl.col("day_return") + 1).product() - 1)
        .otherwise(None)
        .alias("window_return")
    )
    # The summary and lazy drilldown share this one continuous-window result.
    window_return_map = dict(window_returns.select("symbol", "window_return").iter_rows())
    missing = (
        period_grid.filter(
            pl.col("formation_eligible")
            & (pl.col("day_return").is_null() | pl.col("amount").is_null())
        )
        .with_columns(pl.col("reason").fill_null("previous_close_missing").alias("reason"))
        .join(mapping, on="symbol")
        .group_by("sector_id", "reason")
        .len()
    )
    missing_counts = {}
    for record in missing.to_dicts():
        missing_counts.setdefault(record["sector_id"], {})[record["reason"]] = record["len"]
    # Countertrend drilldown needs D and the W-day return, not 60 rows per member.
    focus_grid = (period_grid if event else period_grid.filter(pl.col("date") == end)).join(
        window_returns, on="symbol"
    )
    grid_lookup = {(r["symbol"], r["date"]): r for r in focus_grid.to_dicts()}
    rows = []
    for group in groups:
        sid, members = group["sector_id"], group["symbols"]
        formed = [s for s in members if eligibility_map.get(s, {}).get("formation_eligible")]
        core = sorted(formed, key=lambda s: (-eligibility_map[s]["formation_amount_base"], s))[:5]
        sd = daily.get(sid, {})
        summary = rules.period_summary(sd, benchmark, period)
        observations = [rules.observation(d, sd.get(d, {}), benchmark.get(d, {})) for d in period]
        for index, observation in enumerate(observations):
            previous = observations[index - 1] if index else {}
            for metric in ("relative_return", "breadth_up", "amount_ratio"):
                now_value, previous_value = observation.get(metric), previous.get(metric)
                observation[f"{metric}_delta"] = (
                    now_value - previous_value
                    if now_value is not None and previous_value is not None
                    else None
                )
            core_quotes = [grid_lookup.get((s, period[index]), {}) for s in core]
            observation["core_up_count"] = (
                sum(q.get("day_return", 0) > 0 for q in core_quotes)
                if core and all(q.get("day_return") is not None for q in core_quotes)
                else None
            )
            observation["core_count"] = len(core)
            observation["core_amount_share"] = (
                sum(q["amount"] for q in core_quotes) / observation["amount"]
                if core
                and all(q.get("amount") is not None for q in core_quotes)
                and observation.get("amount")
                else None
            )
        member_returns = [window_return_map.get(s) for s in formed]
        valid_returns = [v for v in member_returns if v is not None]
        reasons = Counter(
            eligibility_map.get(s, {}).get("exclusion_reason") or "unknown_asset"
            for s in members
            if s not in formed
        )
        row = {
            "sector_id": sid,
            "name": group["name"],
            "path": group["path"],
            **summary,
            "original_members": len(members),
            "formed_members": len(formed),
            "exclusions": dict(reasons),
            "eligible_for_comparison": len(formed) >= rules.MIN_MEMBERS,
            "member_up_ratio": sum(v > 0 for v in valid_returns) / len(formed)
            if formed and len(valid_returns) == len(formed)
            else None,
            "member_median_return": float(pl.Series(valid_returns).median())
            if formed and len(valid_returns) == len(formed)
            else None,
            "member_return_count": len(valid_returns),
            "latest": observations[-1],
            "observations": observations if event else [observations[-1]],
            "core_members": [
                {
                    "symbol": s,
                    "name": names.get(s),
                    "formation_mean_amount": eligibility_map[s]["formation_amount_base"],
                    "observations": [
                        _member_observation(grid_lookup.get((s, d), {}), d) for d in period
                    ]
                    if event
                    else [_member_observation(grid_lookup.get((s, end), {}), end)],
                }
                for s in core
            ],
        }
        row["data_complete"] = summary["relative_return"] is not None and all(
            o["status"] == "ready" for o in observations
        )
        row["missing_reasons"] = missing_counts.get(sid, {})
        if event:
            row.update(rules.response_path(observations))
            row["background"] = {
                str(w): rules.period_summary(
                    sd, benchmark, calendar_days[max(0, start_idx - w) : start_idx]
                )
                if start_idx >= w
                else _empty_period(w)
                for w in (5, 20, 60)
            }
            bg = row["background"]["20"]
            row["data_complete"] = row["data_complete"] and all(
                b["relative_return"] is not None for b in row["background"].values()
            )
            row["background_label"] = (
                "事前逆势上涨"
                if bg["classification"] == "countertrend_up"
                else "事前相对抗跌"
                if bg["classification"] == "resilient"
                else "此前未跑赢，事件后走强"
                if bg["relative_return"] is not None
                and bg["relative_return"] <= 0
                and row["earliest_observed_strength_date"]
                else None
            )
            # Four positions, including explicit future / outside-S / calendar-unknown slots.
            for offset in range(len(period), 4):
                d = (
                    calendar_days[start_idx + offset]
                    if start_idx + offset < len(calendar_days)
                    else None
                )
                row["observations"].append(
                    {
                        "date": d.isoformat() if d else None,
                        "offset": offset,
                        "status": "calendar_missing"
                        if d is None
                        else "future"
                        if d > cn_today()
                        else "not_yet_observed",
                    }
                )
        rows.append(row)
    rows.sort(
        key=lambda r: (r["relative_return"] is None, -(r["relative_return"] or 0), r["sector_id"])
    )
    evaluated = sum(r["eligible_for_comparison"] and r["relative_return"] is not None for r in rows)
    ready = sum(r["eligible_for_comparison"] and r["data_complete"] for r in rows)
    coverage = {
        "total_sectors": len(rows),
        "evaluated_sectors": evaluated,
        "complete_sectors": ready,
        "original_market_members": instruments.height,
        "formed_market_members": market_mapping.height,
        "exchanges": sorted(instruments["exchange"].unique().to_list()),
        "market_basis": "可比较A股样本",
        "market_exclusions": dict(
            Counter(eligible.filter(~pl.col("formation_eligible"))["exclusion_reason"].to_list())
        ),
        "excluded_by_policy": sum(not r["eligible_for_comparison"] for r in rows),
    }
    status = "unavailable" if not evaluated else "ready" if ready == len(rows) else "partial"
    warnings = [
        "当前分类名册回看历史行情；不包含完整历史退市名册。",
        "板块为固定名单、每日等权重置的理论观察序列，不含交易成本。",
    ]
    if versions["stock"] == "legacy_file_signature":
        warnings.append("历史数据缺少正式发布版本，当前版本核对保障较弱，数据更新后请重新读取。")
    if evaluated < len(rows):
        warnings.append(
            "部分板块缺少固定成员的完整正式行情，未知指标未参与判断；少于5名形成成员不进入默认比较。"
        )
    if not {"SH", "SZ", "BJ"}.issubset(coverage["exchanges"]):
        warnings.append("证券维表未覆盖沪深北全部交易所，市场对照仅代表已列明的可比较A股样本。")
    payload = {
        "status": status,
        "context": context,
        "rules_version": rules.RULES_VERSION,
        "snapshot_id": _id(context, versions, rules.RULES_VERSION),
        "calculation_basis": "recomputed",
        "membership_basis": "current_snapshot_backcast",
        "membership_observed_at": _observed_at(reader, selected[1]),
        "membership_fingerprint": versions["classification"],
        "input_versions": versions,
        "computed_at": cn_now().isoformat(),
        "warnings": warnings,
        "coverage": coverage,
        "rows": rows,
        "benchmark_evidence": _index_evidence(benchmark, event or end, calendar_days),
        "etf": {
            "status": "unconfigured",
            "reason": "尚未配置宽基ETF观察篮子；不影响股票板块复盘。",
        },
    }
    if event:
        payload["response_overview"] = {
            "same_day_candidates": sum(
                r["eligible_for_comparison"]
                and r["observations"][0]["daily_resonance_candidate"] is True
                for r in rows
            ),
            "first_strength_counts": {
                d.isoformat(): sum(
                    r["eligible_for_comparison"]
                    and r["confirmed_first_strength_date"] == d.isoformat()
                    for r in rows
                )
                for d in period
            },
            "window_finished": eidx - start_idx == 3,
            "overlap_note": "相邻事件观察窗口可能重叠，不能将后续表现归因于单一事件。",
        }
    if reader.versions(needed[0], end) != versions:
        raise ReviewSnapshotConflictError("数据已更新，请刷新")
    # Keep only the member evidence needed for lazy, paginated drilldown.
    return ReviewSnapshot(payload, focus_grid, member_rows, eligible, period)


def _empty_period(window):
    return {
        "sector_return": None,
        "benchmark_return": None,
        "relative_return": None,
        "classification": "pending",
        "down_days": rules.down_statistics([None] * window, [None] * window),
        "return_days": 0,
        "required_days": window,
    }


def _observed_at(reader, config):
    base = reader.data_dir / "ext_data" / config.id
    paths = [base / "config.json", *base.rglob("*.parquet")]
    times = [p.stat().st_mtime for p in paths if p.exists()]
    return datetime.fromtimestamp(max(times), CN_TZ).isoformat() if times else None


def _index_evidence(benchmark, target, days):
    idx = days.index(target)
    row = benchmark.get(target, {})
    prev = benchmark.get(days[idx - 1], {}).get("price") if idx else None
    close, low, high = row.get("price"), row.get("low"), row.get("high")
    # Raw intraday extremes cannot be evidence if that row was not formal.
    low = low if close is not None else None
    high = high if close is not None else None
    pre = {
        str(w): rules.compound(
            [benchmark.get(d, {}).get("day_return") for d in days[idx - w : idx]]
        )
        if idx >= w
        else None
        for w in (5, 20)
    }
    amount = rules.above(row.get("amount_ratio"), 1)
    return {
        "date": target.isoformat(),
        "day_return": row.get("day_return"),
        "amount": row.get("amount"),
        "amount_ratio": row.get("amount_ratio"),
        "pre_returns": pre,
        "low_vs_prev_close": low / prev - 1 if low is not None and prev else None,
        "close_position": (close - low) / (high - low)
        if close is not None and high is not None and low is not None and high > low
        else None,
        "prior20_close_high": row.get("prior20_close_high"),
        "labels": {
            "reclaim_with_expansion": rules.tri_and(
                None if low is None or prev is None else low < prev,
                rules.above(row.get("day_return")),
                amount,
            ),
            "rebound_with_expansion": rules.tri_and(
                None if pre["5"] is None else pre["5"] < 0,
                rules.above(row.get("day_return")),
                amount,
            ),
            "breakout20_with_expansion": rules.tri_and(
                None
                if close is None or row.get("prior20_close_high") is None
                else close > row["prior20_close_high"],
                amount,
            ),
        },
    }


def _member_observation(row, day):
    return {
        "date": day.isoformat(),
        "price": row.get("price"),
        "day_return": row.get("day_return"),
        "amount": row.get("amount"),
        "amount_ratio": row.get("amount_ratio"),
        "reason": row.get("reason")
        or ("previous_close_missing" if row.get("day_return") is None else None),
    }


def members(
    repo, *, sector_id: str, snapshot_id: str, page: int = 1, page_size: int = 50, **context
) -> dict:
    snapshot = get_snapshot(repo, **context)
    payload = snapshot.payload
    if payload["snapshot_id"] != snapshot_id:
        raise ReviewSnapshotConflictError("数据已更新，刷新后查看")
    sector = next((r for r in payload["rows"] if r["sector_id"] == sector_id), None)
    if sector is None:
        raise ReviewParameterError("板块不属于当前复盘上下文")
    selected = snapshot.mapping.filter(pl.col("sector_id") == sector_id).sort("symbol")
    selected = selected.slice((page - 1) * page_size, page_size)
    symbols = selected["symbol"].to_list()
    grid = snapshot.grid.filter(pl.col("symbol").is_in(symbols))
    lookup = {(r["symbol"], r["date"]): r for r in grid.to_dicts()}
    core = {r["symbol"] for r in sector["core_members"]}
    rows = []
    for member in selected.to_dicts():
        symbol = member["symbol"]
        observations = (
            [_member_observation(lookup.get((symbol, d), {}), d) for d in snapshot.period]
            if context["mode"] == "event"
            else [
                _member_observation(
                    lookup.get((symbol, snapshot.period[-1]), {}), snapshot.period[-1]
                )
            ]
        )
        # A complete window return requires every consecutive daily price, not only endpoints.
        window_return = next(
            (
                lookup[(symbol, d)].get("window_return")
                for d in snapshot.period
                if (symbol, d) in lookup
            ),
            None,
        )
        rows.append(
            {
                "symbol": symbol,
                "name": next(
                    (
                        lookup[(symbol, d)].get("name")
                        for d in snapshot.period
                        if (symbol, d) in lookup
                    ),
                    None,
                ),
                "formation_eligible": member["formation_eligible"],
                "exclusion_reason": member["exclusion_reason"],
                "is_core": symbol in core,
                "window_return": window_return,
                "observations": observations,
            }
        )
    return {
        "snapshot_id": snapshot_id,
        "sector_id": sector_id,
        "page": page,
        "page_size": page_size,
        "total": sector["original_members"],
        "rows": rows,
    }
