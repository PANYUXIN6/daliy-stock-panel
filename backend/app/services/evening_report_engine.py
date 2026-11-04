"""每日晚报计算边界：只消费已验收输入，不抓取、不读写仓库。

行情价格/ATR 为同版本前复权，amount 为人民币元；formal 由输入层验证每条
历史记录及其指标前置状态。分类由本轮完整观察提供，不使用合并扩展表推断退出。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise

import polars as pl

from app.services.evening_report_rules import (
    PURPOSES,
    RULE_VERSION,
    SELECTION_POLICY_VERSION,
    advance_identity,
    advance_risk,
    advance_round,
    all_of,
    any_of,
    compare,
    confirmation,
    finite,
    judgement,
    negate,
    normalized,
    primary_purpose,
    reference_interval,
    resilience,
    weighted,
)
from app.services.market_mainline import _SCORE_WEIGHTS

SCOPE_VERSION = "main-chinext-nonst-v1/evening-classification-v1"

DISCLAIMER = "本报告用于识别与持续跟踪值得观察的股票，不构成交易指令。观察规则为初值，识别与跟踪效果仍需积累记录。范围为主板、创业板非ST股票，未核验隔夜新闻公告。"


@dataclass
class EveningInputs:
    trade_date: str
    calendar: list[str]
    prices: pl.DataFrame
    metadata: pl.DataFrame
    memberships: pl.DataFrame
    benchmark: pl.DataFrame
    next_trade_date: str | None = None
    cutoff: str = ""
    market: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


def _frame(frame: pl.DataFrame, defaults: dict) -> pl.DataFrame:
    return frame.with_columns(
        [pl.lit(value).alias(key) for key, value in defaults.items() if key not in frame.columns]
    )


def _percentiles(rows: list[dict], key: str, *, minimum: int = 1) -> dict[str, float | None]:
    if len(rows) < minimum or any(not finite(row.get(key)) for row in rows):
        return {row["id"]: None for row in rows}
    frame = pl.DataFrame({"id": [r["id"] for r in rows], "value": [r[key] for r in rows]})
    ranked = frame.with_columns(
        (
            pl.lit(50.0)
            if len(rows) == 1
            else 100 * (pl.col("value").rank("average") - 1) / (len(rows) - 1)
        ).alias("p")
    )
    return dict(zip(ranked["id"].to_list(), ranked["p"].to_list(), strict=True))


def _compound(values: list[float | None]) -> float | None:
    if not values or not all(finite(x) for x in values):
        return None
    result = 1.0
    for value in values:
        result *= 1 + value
    return result - 1


def _relative(stock: float | None, benchmark: float | None) -> float | None:
    return (
        (1 + stock) / (1 + benchmark) - 1
        if finite(stock) and finite(benchmark) and benchmark > -1
        else None
    )


def _ge_price(a, b):
    return a >= b if finite(a, positive=True) and finite(b, positive=True) else None


def _prepare(
    inputs: EveningInputs, dates: list[str], tracked_symbols=()
) -> tuple[dict, dict, dict]:
    numeric = [
        "close",
        "high",
        "amount",
        "ma5",
        "ma10",
        "ma20",
        "atr_14",
        "high_60d",
        "raw_open",
        "raw_high",
        "raw_low",
        "raw_close",
        "consecutive_limit_ups",
    ]
    defaults = {k: None for k in [*numeric, "signal_limit_up", "signal_limit_down"]}
    defaults["formal"] = None
    prices = (
        _frame(inputs.prices, defaults)
        .with_columns(pl.col("date").cast(pl.String))
        .filter(pl.col("date") <= inputs.trade_date)
        .sort(["symbol", "date"])
    )
    if prices.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise ValueError("晚报输入存在重复股票交易日")
    # Null/NaN handling is explicit before window aggregation; no forward-fill or skipped dates.
    prices = prices.with_columns(
        [
            pl.when(pl.col(k).cast(pl.Float64).is_finite())
            .then(pl.col(k).cast(pl.Float64))
            .otherwise(None)
            .alias(k)
            for k in numeric
        ]
        + [pl.col(k).cast(pl.Boolean) for k in ("formal", "signal_limit_up", "signal_limit_down")]
    )
    # Aggregate the historical input in Polars; only 21 near-end rows per symbol cross
    # into Python for lifecycle and bounded pressure-event processing.
    prices = prices.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_r"),
        (pl.col("close").log() - pl.col("close").shift(1).over("symbol").log())
        .clip(lower_bound=0)
        .alias("_positive_log"),
    )
    metadata = _frame(
        inputs.metadata,
        {
            k: None
            for k in (
                "name",
                "in_scope",
                "trading_normal",
                "regime",
                "suspension_window",
                "qualification_pause",
            )
        },
    )
    if metadata["symbol"].is_duplicated().any():
        raise ValueError("晚报标准元数据存在重复股票")
    rows = {r["symbol"]: r for r in metadata.to_dicts()}
    for symbol in set(prices["symbol"].to_list()) | set(tracked_symbols):
        rows.setdefault(symbol, {"symbol": symbol})
    recent = pl.col("date").is_in(dates)
    last20 = pl.col("date").is_in(dates[-20:])
    last5 = pl.col("date").is_in(dates[-5:])
    valid_close = pl.col("close").is_not_null() & pl.col("close").gt(0)
    amount = pl.col("amount")
    boards = pl.col("consecutive_limit_ups")
    aggs = prices.group_by("symbol").agg(
        pl.col("formal").eq(True).fill_null(False).all().alias("formal_all"),
        (pl.col("formal").eq(True) & valid_close).sum().alias("warmup_count"),
        (
            (pl.col("date").filter(recent).len() == 21)
            & (pl.col("formal").eq(True) & valid_close).filter(recent).fill_null(False).all()
        ).alias("near"),
        pl.struct(pl.all().exclude("symbol")).filter(recent).alias("window"),
        pl.when((amount.filter(last20).count() == 20) & amount.filter(last20).ge(0).all())
        .then(amount.filter(last20).median())
        .otherwise(None)
        .alias("amount_median"),
        pl.when(amount.filter(last5).count() == 5)
        .then(amount.filter(last5).mean())
        .otherwise(None)
        .alias("amount5"),
        pl.when(pl.col("signal_limit_up").filter(last20).count() == 20)
        .then(pl.col("signal_limit_up").filter(last20).sum())
        .otherwise(None)
        .alias("up_count"),
        pl.when(boards.filter(last20).count() == 20)
        .then(boards.filter(last20).max())
        .otherwise(None)
        .alias("max_boards"),
        pl.when(boards.filter(last5).count() == 5)
        .then(boards.filter(last5).max())
        .otherwise(None)
        .alias("recent_board"),
        pl.col("_positive_log").filter(last20).sum().alias("positive20"),
        pl.col("_positive_log").filter(last5).sum().alias("positive5"),
    )
    aggregate = {r["symbol"]: r for r in aggs.to_dicts()}
    profiles = {}
    amount_counts = defaultdict(Counter)
    for symbol, meta in sorted(rows.items()):
        agg = aggregate.get(symbol, {})
        by_date = {r["date"]: r for r in agg.get("window", [])}
        window = [by_date.get(d, {}) for d in dates]
        warmup = agg.get("warmup_count", 0)
        formal = True if agg.get("formal_all") is True else None
        near = agg.get("near") is True
        continuous = False if meta.get("suspension_window") is True else True if near else None
        pre = judgement(
            {
                "研究范围": meta.get("in_scope"),
                "正常交易": meta.get("trading_normal"),
                "60根有效预热": warmup >= 60 if formal is True else None,
                "历史正式性": formal,
                "21交易日连续价格": continuous,
            }
        )
        median = agg.get("amount_median")
        amount_ok = compare(median, 100_000_000)
        eligible = all_of(pre["value"], amount_ok)
        regime = str(meta.get("regime") or "unknown")
        if pre["value"] is True:
            amount_counts[regime]["n_pre"] += 1
            amount_counts[regime][
                "n_pass" if amount_ok is True else "n_fail" if amount_ok is False else "n_unknown"
            ] += 1
        today = window[-1]
        feature = {k: today.get(k) for k in numeric}
        closes = [r.get("close") for r in window]
        if near:
            returns = [r.get("_r") for r in window[1:]]
            feature.update(
                R20=closes[-1] / closes[0] - 1,
                R5=closes[-1] / closes[-6] - 1,
                a=agg["positive5"] / agg["positive20"] if agg["positive20"] else 0,
            )
        else:
            returns = [None] * 20
            feature.update(R20=None, R5=None, a=None)
        feature.update(amount5=agg.get("amount5"), recent_board=agg.get("recent_board"))
        feature["I"] = weighted(
            [normalized(agg.get("up_count"), 0, 6), normalized(agg.get("max_boards"), 0, 5)],
            [0.6, 0.4],
        )
        feature["A"] = normalized(feature["a"], 0.25, 0.65)
        profiles[symbol] = {
            "id": symbol,
            "meta": meta,
            "pre": pre,
            "eligible": eligible,
            "amount_ok": amount_ok,
            "amount_median": median,
            "features": feature,
            "window": window,
            "returns": returns,
            "warmup_count": warmup,
            "formal": formal,
        }
    market_groups = defaultdict(list)
    for p in profiles.values():
        if p["eligible"] is True:
            market_groups[p["meta"].get("regime")].append({"id": p["id"], **p["features"]})
    for regime, group in market_groups.items():
        for key, target in (("R20", "S"), ("R5", "J")):
            percentiles = _percentiles(group, key, minimum=20) if regime is not None else {}
            for p in group:
                profiles[p["id"]]["features"][target] = percentiles.get(p["id"])
                profiles[p["id"]]["features"]["market_percentile_n"] = len(group)
    return profiles, amount_counts, {}


def _market_policy(market: dict) -> dict:
    state, phase = market.get("state"), market.get("phase")
    raw_weak = all_of(
        compare(market.get("promo_rate"), 0.13, "le"), compare(market.get("seal_rate"), 0.57, "le")
    )
    defensive = {
        "市场偏弱：观察其对个股相对强弱和核心地位的影响": state == "lean_weak" if state else None,
        "市场弱势：观察其对个股相对强弱和核心地位的影响": state == "weak" if state else None,
        "短线退潮：观察个股强弱能否延续": phase == "ebb" if phase else None,
        "市场冰点：观察个股强弱及后续修复表现": phase == "ice" if phase else None,
        "原始晋级率≤13%且封板率≤57%：市场防守信号，观察对个股表现的影响": raw_weak,
    }
    return {
        **judgement({
            "市场状态可用": None if state is None else state != "unavailable",
            "阶段允许精选": None
            if phase in {None, "unknown"}
            else phase != "climax",
            "市场防守证据可判定": None if raw_weak is None else True,
        }),
        "observation_hints": [reason for reason, value in defensive.items() if value is True],
    }


def _price_risk(p: dict) -> tuple[dict, dict]:
    f, meta, window = p["features"], p["meta"], p["window"]
    c, atr, ma = f.get("close"), f.get("atr_14"), f.get("ma20")
    valid = p["formal"] is True and p["warmup_count"] >= 60
    structure = (
        ma - c >= 2 * atr if valid and all(finite(x, positive=True) for x in (c, atr, ma)) else None
    )
    abnormals = []
    for i in (-1, -2):
        cur, prev = window[i], window[i - 1]
        r = (
            cur.get("close") / prev["close"] - 1
            if all(finite(x, positive=True) for x in (cur.get("close"), prev.get("close")))
            else None
        )
        v = (
            prev.get("atr_14") / prev["close"]
            if valid
            and finite(prev.get("atr_14"), positive=True)
            and finite(prev.get("close"), positive=True)
            else None
        )
        abnormals.append(
            all_of(compare(r, -0.05, "le"), r <= -2 * v if finite(r) and finite(v) else None)
        )
    parts = {
        "收盘跌停": window[-1].get("signal_limit_down"),
        "跌破均线2ATR": structure,
        "连续异常下跌": all_of(*abnormals),
        "交易资格暂停": meta.get("qualification_pause"),
    }
    risk = judgement(parts, mode="or")
    overheating = any_of(
        (c - f["ma5"]) / atr >= 2
        if valid and all(finite(x, positive=True) for x in (c, f.get("ma5"), atr))
        else None,
        (c - ma) / atr >= 4
        if valid and all(finite(x, positive=True) for x in (c, ma, atr))
        else None,
    )
    raw = [f.get(k) for k in ("raw_open", "raw_high", "raw_low", "raw_close")]
    one_price = all_of(
        window[-1].get("signal_limit_up"),
        max(raw) - min(raw) < 0.005 if all(finite(x, positive=True) for x in raw) else None,
    )
    one_price_count, boundary_known = 0, False
    for row in reversed(window):
        raw_row = [row.get(k) for k in ("raw_open", "raw_high", "raw_low", "raw_close")]
        same_price = (
            max(raw_row) - min(raw_row) < 0.005
            if all(finite(x, positive=True) for x in raw_row)
            else None
        )
        value = all_of(row.get("signal_limit_up"), same_price)
        if value is not True:
            boundary_known = value is False
            break
        one_price_count += 1
    position = judgement(
        {
            "收盘不低于MA20": _ge_price(c, ma),
            "不过热": negate(overheating),
            "非一字涨停": negate(one_price),
        }
    )
    position.update(
        observation_eligibility=judgement(
            {
                "收盘不低于MA20": _ge_price(c, ma),
                "非一字涨停": negate(one_price),
                "位置输入完整": True
                if valid and all(finite(x, positive=True) for x in (c, f.get("ma5"), ma, atr))
                else None,
            }
        ),
        overheated=overheating,
        one_price_limit_up=one_price,
        one_price_consecutive_days=one_price_count if boundary_known else None,
        one_price_consecutive_at_least=one_price_count,
        near_ma5=all_of(
            abs(c - f["ma5"]) / atr <= 0.5
            if valid and all(finite(x, positive=True) for x in (c, f.get("ma5"), atr))
            else None,
            _ge_price(c, f.get("ma10")),
        ),
        near_ma10=all_of(
            abs(c - f["ma10"]) / atr <= 0.5
            if valid and all(finite(x, positive=True) for x in (c, f.get("ma10"), atr))
            else None,
            _ge_price(c, ma),
        ),
    )
    return risk, position


def _build_sectors(
    inputs: EveningInputs,
    profiles: dict,
    dates: list[str],
    history: dict,
    previous: dict,
    market_returns: list,
    cancel_check: Callable,
) -> tuple[list, dict]:
    members = inputs.memberships.unique(subset=["source", "sector_id", "symbol"]).sort(
        ["source", "sector_id", "symbol"]
    )
    sectors, scores = [], {}
    for (source, sector_id), group in members.partition_by(
        ["source", "sector_id"], as_dict=True
    ).items():
        cancel_check()
        classified = group["symbol"].to_list()
        scope = [
            s for s in classified if s in profiles and profiles[s]["meta"].get("in_scope") is True
        ]
        eligible = [s for s in scope if profiles[s]["eligible"] is True]
        sid = f"{source}:{sector_id}"
        daily = (
            [sum(profiles[s]["returns"][i] for s in eligible) / len(eligible) for i in range(20)]
            if len(eligible) >= 6
            else [None] * 20
        )
        r20, r5 = _compound(daily), _compound(daily[-5:])
        x20, x5 = (
            _relative(r20, _compound(market_returns)),
            _relative(r5, _compound(market_returns[-5:])),
        )
        coverage = {
            "scope_members": len(scope),
            "qualified_members": len(eligible),
            "suspended": sum(profiles[s]["meta"].get("trading_normal") is False for s in scope),
            "resumption_window": sum(
                profiles[s]["meta"].get("suspension_window") is True
                and profiles[s]["meta"].get("trading_normal") is True
                for s in scope
            ),
            "qualification_unknown": sum(profiles[s]["eligible"] is None for s in scope),
            "qualification_failed": sum(profiles[s]["eligible"] is False for s in scope),
            "scope_unknown": sum(
                s not in profiles or profiles[s]["meta"].get("in_scope") is None for s in classified
            ),
            "computable": finite(x20) and finite(x5),
            "structurally_ineligible": len(scope) < 6
            and all(
                s in profiles and profiles[s]["meta"].get("in_scope") is not None
                for s in classified
            ),
        }
        sector = {
            "id": sid,
            "source": source,
            "sector_id": sector_id,
            "sector_name": group["sector_name"][0],
            "members": eligible,
            "classification_members": classified,
            "member_count": len(eligible),
            "regime_counts": dict(
                Counter(str(profiles[s]["meta"].get("regime") or "unknown") for s in eligible)
            ),
            "features": {"R20": r20, "X5": x5, "X20": x20},
            "coverage": coverage,
            "calculation_reasons": []
            if coverage["computable"]
            else ["合格成员不足6只" if len(eligible) < 6 else "同组或市场基准数据缺失"],
        }
        before = previous.get("sectors", {}).get(sid)
        sector["membership_change"] = {
            "added": sorted(set(classified) - set(before["classification_members"]))
            if before
            else [],
            "removed": sorted(set(before["classification_members"]) - set(classified))
            if before
            else [],
            "status": "observed_change" if before else "first_observed",
            "note": "比较口径发生变化，排名变化原因尚未分离"
            if before and set(before["members"]) != set(eligible)
            else "首次观测、此前归属未知"
            if not before
            else "成员口径未变化",
        }
        up_members = [
            s
            for s in scope
            if profiles[s]["meta"].get("trading_normal") is not False
            and profiles[s]["window"][-1].get("signal_limit_up") is True
        ]
        up_known = coverage["scope_unknown"] == 0 and all(
            profiles[s]["meta"].get("trading_normal") is False
            or (
                profiles[s]["window"][-1].get("formal") is True
                and profiles[s]["window"][-1].get("signal_limit_up") is not None
            )
            for s in scope
        )
        consec = [profiles[s]["features"].get("consecutive_limit_ups") for s in up_members]
        ecology = {
            "limit_up_count": len(up_members),
            "max_boards": max(consec) if consec and all(finite(x) for x in consec) else 0,
            "ge2_count": sum(x >= 2 for x in consec if finite(x)),
            "rungs_filled": len({x for x in consec if finite(x) and x >= 2}),
        }
        sector["ecology"] = {
            **ecology,
            "computable": up_known and all(finite(x) for x in consec),
            "rank": None,
        }
        sectors.append(sector)
        if len(eligible) < 6:
            continue
        paths = []
        for s in eligible:
            p, f = profiles[s], dict(profiles[s]["features"])
            peer = [
                (daily[i] * len(eligible) - p["returns"][i]) / (len(eligible) - 1)
                for i in range(20)
            ]
            f.update(
                X20=_relative(f["R20"], _compound(peer)),
                X5=_relative(f["R5"], _compound(peer[-5:])),
                rank_n=len(eligible),
            )
            paths.append({"id": s, **f, "_peer": peer})
        for key, target in (("X20", "_p20"), ("X5", "_p5"), ("amount5", "V")):
            percentiles = _percentiles(paths, key, minimum=6)
            for p in paths:
                p[target] = percentiles[p["id"]]
        q60 = pl.Series([p["R20"] for p in paths]).quantile(0.6, interpolation="linear")
        for p in paths:
            high = p.get("high_60d")
            proximity = p["close"] / high if finite(high, positive=True) else None
            p["L"] = weighted(
                [p["_p20"], p["_p5"], normalized(proximity, 0.85, 1)], [0.5, 0.3, 0.2]
            )
            p["H_b"] = weighted([p.get("S"), p.get("I"), p["L"]], [0.25, 0.30, 0.25])
            p["H_t"] = weighted([p.get("S"), p["L"], p["V"]], [0.35, 0.30, 0.10])
            p["H_e"] = weighted([p.get("J"), p.get("A"), p["L"]], [0.35, 0.25, 0.20])
            p["R20_q60"] = q60
        for h, rank in (("H_b", "rank_b"), ("H_t", "rank_t"), ("H_e", "rank_e")):
            ordered = (
                sorted(paths, key=lambda p: (-p[h], -p["L"], -p["X20"], p["id"]))
                if all(finite(p[h]) and finite(p["L"]) and finite(p["X20"]) for p in paths)
                else []
            )
            ranks = {p["id"]: i + 1 for i, p in enumerate(ordered)}
            for p in paths:
                p[rank] = ranks.get(p["id"])
        sector["ranking_evidence"] = {}
        for kind, required in (
            ("board", ("S", "I", "L")),
            ("trend", ("S", "L", "V")),
            ("acceleration", ("J", "A", "L")),
        ):
            missing, insufficient = [], []
            for member in paths:
                for component in required:
                    if finite(member.get(component)):
                        continue
                    if (
                        component in {"S", "J"}
                        and profiles[member["id"]]["meta"].get("regime") is not None
                        and member.get("market_percentile_n", 0) < 20
                    ):
                        insufficient.append(
                            {
                                "symbol": member["id"],
                                "component": component,
                                "regime": profiles[member["id"]]["meta"].get("regime"),
                                "n": member.get("market_percentile_n", 0),
                            }
                        )
                    else:
                        missing.append({"symbol": member["id"], "component": component})
            status = (
                "mixed"
                if missing and insufficient
                else "data_missing"
                if missing
                else "sample_insufficient"
                if insufficient
                else "complete"
            )
            sector["ranking_evidence"][kind] = {
                "status": status,
                "missing": missing,
                "insufficient": insufficient,
            }
            kind_label = {"board": "连板", "trend": "趋势", "acceleration": "加速"}[kind]
            if insufficient:
                regimes = sorted({f"{item['regime']}制度{item['n']}只" for item in insufficient})
                sector["calculation_reasons"].append(
                    f"{kind_label}完整排名样本不足：市场同制度组须至少20只，当前"
                    + "、".join(regimes)
                )
            if missing:
                components = sorted({item["component"] for item in missing})
                sector["calculation_reasons"].append(
                    f"{kind_label}完整排名输入缺失：必要分项"
                    + "、".join(components)
                    + "未完整，保留全组不删成员"
                )
        scores[sid] = {p["id"]: p for p in paths}
        sector["scores"] = {
            p["id"]: {
                k: p.get(k)
                for k in (
                    "X20",
                    "X5",
                    "L",
                    "V",
                    "H_b",
                    "H_t",
                    "H_e",
                    "rank_b",
                    "rank_t",
                    "rank_e",
                    "rank_n",
                    "R20_q60",
                )
            }
            for p in paths
        }
        sector["score_common_fields_source"] = "calculations.features"
    for source in ("concept", "industry"):
        same = [s for s in sectors if s["source"] == source]
        comparable = [{"id": s["id"], **s["features"]} for s in same if s["coverage"]["computable"]]
        percentiles = _percentiles(comparable, "X20", minimum=20)
        ecological = [
            dict(id=s["id"], **s["ecology"])
            for s in same
            if s["ecology"]["computable"] and s["ecology"]["limit_up_count"] >= 3
        ]
        ecology_complete = all(s["ecology"]["computable"] for s in same)
        ecology_scores = {s["id"]: 0.0 for s in ecological}
        for key, weight in _SCORE_WEIGHTS.items():
            values = _percentiles(ecological, key)
            for sid, value in values.items():
                # Match existing ecology algorithm's singleton score (0), not general percentile 50.
                ecology_scores[sid] += (0 if len(ecological) == 1 else value) * weight
        ordered = sorted(ecology_scores, key=lambda sid: (-ecology_scores[sid], sid))
        ranks = {sid: i + 1 for i, sid in enumerate(ordered)} if ecology_complete else {}
        for sector in same:
            sid, f = sector["id"], sector["features"]
            f.update(percentile=percentiles.get(sid), percentile_n=len(comparable))
            sector["ecology"].update(
                rank=ranks.get(sid),
                score=ecology_scores.get(sid),
                complete_ranking=ecology_complete,
            )
            today_top = ranks.get(sid, 999999) <= 5 if ecology_complete else None
            top_days = [
                history.get(d, {}).get("state", {}).get("top5", {}).get(sid) for d in dates[-5:-1]
            ] + [today_top]
            count = sum(x is True for x in top_days)
            emotion = any_of(today_top, True if count >= 2 else None if None in top_days else False)
            trend = all_of(
                compare(f["R20"], 0, "gt"),
                compare(f["X5"], 0, "gt"),
                compare(f["X20"], 0, "gt"),
                compare(f["percentile"], 90),
            )
            entry = judgement(
                {
                    "情绪或趋势入口": any_of(emotion, trend),
                    "5日超额为正": compare(f["X5"], 0, "gt"),
                    "20日超额为正": compare(f["X20"], 0, "gt"),
                }
            )
            old_round = previous.get("rounds", {}).get(sid)
            sector.update(
                entry=entry,
                emotion_entry=emotion,
                trend_entry=trend,
                today_top5=today_top,
                top5_count=count,
                round=advance_round(
                    old_round,
                    date=inputs.trade_date,
                    entry=entry["value"],
                    active=all_of(compare(f["X5"], 0, "gt"), compare(f["X20"], 0, "gt")),
                    medium_failed=compare(f["X20"], 0, "le"),
                ),
            )
    return sectors, scores


def _sector_order(sector: dict) -> tuple:
    return (
        -sector["top5_count"],
        sector["ecology"].get("rank") or 999999,
        -sector["features"]["X20"] if finite(sector["features"]["X20"]) else float("inf"),
        sector["id"],
    )


def _entry(p: dict, f: dict, kind: str, *, maintain=False) -> dict:
    threshold, l_min, rank = (65, 60, 3) if maintain else (75, 70, 2)
    suspended = (
        p["meta"].get("trading_normal") is False or p["meta"].get("suspension_window") is True
    )
    base = None if suspended else p["eligible"]
    # Amount failure only has meaning when the upstream qualification is known complete.
    if maintain and p["pre"]["value"] is True and p["amount_ok"] is False:
        base = False
    parts = {
        "基础资格": base,
        "地位分门槛": compare(f.get("H_b" if kind == "board" else "H_t"), threshold),
        "领涨分门槛": compare(f.get("L"), l_min),
        "完整组排名": compare(f.get("rank_b" if kind == "board" else "rank_t"), rank, "le"),
        "20日收益为正": compare(f.get("R20"), 0, "gt"),
        "20日超额为正": compare(f.get("X20"), 0, "gt"),
        "收盘不低于MA20": _ge_price(f.get("close"), f.get("ma20")),
    }
    if kind == "board" and not maintain:
        parts.update(
            {
                "辨识度门槛": compare(f.get("I"), 50),
                "近5日真实连板": compare(f.get("recent_board"), 2),
            }
        )
    return judgement(parts)


def build_report(
    inputs: EveningInputs, history: list[dict] | None = None, cancel_check: Callable | None = None
) -> dict:
    """Build a JSON-compatible report and forward state; history contains original prior releases only."""
    cancel = cancel_check or (lambda: None)
    cancel()
    calendar = sorted({str(d) for d in inputs.calendar if str(d) <= inputs.trade_date})
    if not calendar or calendar[-1] != inputs.trade_date:
        raise ValueError("量化日期必须在交易日历内")
    if len(calendar) < 21:
        raise ValueError("市场交易日历不足21日")
    dates = calendar[-21:]
    scope_version = inputs.provenance.get("scope_version", SCOPE_VERSION)
    archive = {
        r["trade_date"]: r
        for r in (history or [])
        if r.get("rule_version") == RULE_VERSION
        and r.get("scope_version", SCOPE_VERSION) == scope_version
        and r.get("trade_date", "") < inputs.trade_date
    }
    last = archive[max(archive)] if archive else {}
    previous = last.get("state", {})
    adjacent = last.get("trade_date") == dates[-2]
    if not adjacent:
        # Keep intervals and pauses; gaps break counts, never erase previously confirmed facts.
        previous = {
            **previous,
            "rounds": {
                k: {**v, "entry_count": 0, "exit_count": 0}
                for k, v in previous.get("rounds", {}).items()
            },
            "identities": {
                k: {**v, "failed_count": 0, "recovery_count": 0}
                for k, v in previous.get("identities", {}).items()
            },
            "risks": {k: {**v, "clear_count": 0} for k, v in previous.get("risks", {}).items()},
        }
    tracked_symbols = {key.split("|")[0] for key in previous.get("associations", {})}
    profiles, amount_counts, _ = _prepare(inputs, dates, tracked_symbols)
    benchmark = _frame(inputs.benchmark, {"formal": None}).with_columns(
        pl.col("date").cast(pl.String)
    )
    bench = {r["date"]: r for r in benchmark.to_dicts()}
    market_returns = []
    for d0, d1 in pairwise(dates):
        a, b = bench.get(d0, {}), bench.get(d1, {})
        market_returns.append(
            b["close"] / a["close"] - 1
            if a.get("formal") is True
            and b.get("formal") is True
            and finite(a.get("close"), positive=True)
            and finite(b.get("close"), positive=True)
            else None
        )
    sectors, scores = _build_sectors(
        inputs,
        profiles,
        dates,
        archive,
        previous if adjacent else {**previous, "sectors": {}},
        market_returns,
        cancel,
    )
    sector_by_id = {s["id"]: s for s in sectors}
    state = {
        "rounds": {s["id"]: s["round"] for s in sectors},
        "sectors": sector_by_id,
        "top5": {s["id"]: s["today_top5"] for s in sectors},
        "associations": {},
        "identities": {},
        "paths": {},
        "risks": {},
        "starts": {},
        "candidate_stages": {},
    }
    events, records = [], []
    policy = _market_policy(inputs.market)
    for sector in sectors:
        old = previous.get("rounds", {}).get(sector["id"], {}).get("status")
        if old != sector["round"]["status"]:
            events.append(
                {
                    "date": inputs.trade_date,
                    "sector_id": sector["id"],
                    "type": "round_" + sector["round"]["status"],
                    "reason": "板块轮次条件变化",
                }
            )
    for symbol, p in profiles.items():
        cancel()
        if p["meta"].get("in_scope") is False:
            for key, identity in previous.get("identities", {}).items():
                if (
                    key.startswith(symbol + "|")
                    and identity.get("intervals")
                    and identity["intervals"][-1].get("ended_at") is None
                ):
                    ended = advance_identity(
                        identity,
                        date=inputs.trade_date,
                        entry=False,
                        confirmed=False,
                        maintenance=False,
                        immediate_exit=True,
                    )
                    state["identities"][key] = ended
                    events.append(
                        {
                            "date": inputs.trade_date,
                            "symbol": symbol,
                            "type": "scope_exit",
                            "reason": "已知退出研究范围，历史身份结束",
                        }
                    )
            continue
        paths = []
        for source in ("concept", "industry"):
            assoc_key = f"{symbol}|{source}"
            old_assoc = previous.get("associations", {}).get(assoc_key)
            choices = [
                s
                for s in sectors
                if s["source"] == source and symbol in s["classification_members"]
            ]
            old_sector = sector_by_id.get(old_assoc.get("sector") if old_assoc else None)
            retained = (
                old_sector
                and symbol in old_sector["classification_members"]
                and old_sector["round"]["status"] != "ended"
            )
            active_choices = [s for s in choices if s["round"]["status"] == "active"]
            # An observation association is not a formal lock. The first formal choice
            # must be among active rounds; a formal association survives pauses.
            keep = retained and (
                old_assoc.get("formal") or not active_choices or old_sector in active_choices
            )
            sector = (
                old_sector
                if keep
                else min(active_choices or choices, key=_sector_order)
                if choices
                else None
            )
            if old_assoc and not old_assoc.get("formal") and active_choices:
                sector = min(active_choices, key=_sector_order)
                keep = retained and sector is old_sector
            retained = bool(keep)
            if old_assoc and (not retained):
                for key, identity in previous.get("identities", {}).items():
                    if (
                        key.startswith(old_assoc["path_id"] + "|")
                        and identity.get("intervals")
                        and identity["intervals"][-1].get("ended_at") is None
                    ):
                        state["identities"][key] = advance_identity(
                            identity,
                            date=inputs.trade_date,
                            entry=False,
                            confirmed=False,
                            maintenance=False,
                            immediate_exit=True,
                        )
                        events.append(
                            {
                                "date": inputs.trade_date,
                                "symbol": symbol,
                                "type": "identity_invalidated",
                                "reason": "轮次结束或明确退出成员关系",
                            }
                        )
            if sector is None:
                continue
            sid, round_state = sector["id"], sector["round"]
            chain = (
                round_state.get("chain_id")
                or round_state.get("round_id")
                or (old_assoc.get("chain") if retained and old_assoc else inputs.trade_date)
            )
            path_id = f"{symbol}|{source}|{sid}|{chain}"
            assoc = {
                "sector": sid,
                "chain": chain,
                "path_id": path_id,
                "formal": round_state["status"] == "active"
                or bool(retained and old_assoc.get("formal")),
            }
            state["associations"][assoc_key] = assoc
            raw = scores.get(sid, {}).get(symbol)
            f = {
                k: v
                for k, v in (raw or p["features"]).items()
                if not k.startswith("_") and k != "id"
            }
            old_path = previous.get("paths", {}).get(path_id, {})
            identities, judges = [], {}
            for kind in ("board", "trend"):
                key = f"{path_id}|{kind}"
                entry = _entry(p, f, kind)
                maintenance = _entry(p, f, kind, maintain=True)
                recent = [
                    archive.get(d, {})
                    .get("state", {})
                    .get("paths", {})
                    .get(path_id, {})
                    .get("judgements", {})
                    .get(kind + "_entry", {})
                    .get("value")
                    for d in dates[-3:-1]
                ] + [entry["value"]]
                confirmed = confirmation(
                    entry["value"], recent, round_state["status"] == "active" and assoc["formal"]
                )
                old_identity = previous.get("identities", {}).get(key)
                identity = advance_identity(
                    old_identity,
                    date=inputs.trade_date,
                    entry=entry["value"],
                    confirmed=confirmed,
                    maintenance=maintenance["value"],
                    immediate_exit=round_state["status"] == "ended",
                )
                identity.update(
                    type=kind,
                    id=key,
                    sector_id=sid,
                    round_id=round_state.get("round_id"),
                    rule_version=RULE_VERSION,
                )
                since = identity.get("unverified_since")
                identity["unverified_duration_market_days"] = (
                    len(calendar) - 1 - calendar.index(since) if since in calendar else None
                )
                for interruption in identity.get("verification_events", []):
                    start_day, end_day = interruption["started_at"], interruption["resolved_at"]
                    interruption["duration_market_days"] = (
                        calendar.index(end_day) - calendar.index(start_day)
                        if start_day in calendar and end_day in calendar
                        else None
                    )
                state["identities"][key] = identity
                identities.append(identity)
                judges.update(
                    {
                        kind + "_entry": entry,
                        kind + "_maintenance": maintenance,
                        kind + "_confirmation": judgement(
                            {
                                "当日进入": entry["value"],
                                "近3日完整且至少2日成立": None
                                if None in recent
                                else sum(v is True for v in recent) >= 2,
                                "活跃正式轮次": round_state["status"] == "active"
                                and assoc["formal"],
                            }
                        ),
                    }
                )
                if (
                    identity["status"] != (old_identity or {}).get("status")
                    and identity["status"] != "none"
                    and (
                        identity["status"] != "unverified"
                        or identity.get("intervals")
                        or (old_identity or {}).get("entry") is True
                    )
                ):
                    events.append(
                        {
                            "date": inputs.trade_date,
                            "symbol": symbol,
                            "sector_id": sid,
                            "type": "identity_" + identity["status"],
                            "identity_type": kind,
                            "reason": "回归确认"
                            if identity.get("returned") and identity["status"] == "confirmed"
                            else "核心身份判定变化",
                        }
                    )
            atr_evidence = (
                [r.get("atr_14") for r in p["window"]]
                if p["formal"] is True and p["warmup_count"] >= 60
                else [None] * 21
            )
            rs = resilience(
                [r.get("close") for r in p["window"]],
                atr_evidence,
                raw["_peer"] if raw else [None] * 20,
                market_returns,
                dates,
            )
            previous_high = p["window"][-2].get("high")
            f["daily_strengthening"] = all_of(
                compare(f.get("close"), previous_high, "gt")
                if finite(previous_high, positive=True)
                else None,
                compare(p["returns"][-1], raw["_peer"][-1], "gt") if raw else None,
            )
            f["limit_up_today"] = p["window"][-1].get("signal_limit_up")
            f["broken_board"] = all_of(
                compare(p["window"][-2].get("consecutive_limit_ups"), 1),
                negate(f["limit_up_today"]),
            )
            startup = all_of(p["eligible"], compare(f.get("R5"), 0.12), compare(f.get("a"), 0.45))
            start = previous.get("starts", {}).get(path_id)
            if start is None and startup is True:
                # Prior non-trigger evidence is accumulated on the same observed chain.
                # A missing day or unknown never becomes a known non-trigger later.
                verified = adjacent and old_path.get("startup_history_complete") is True
                prior_features = old_path.get("features", {}) if adjacent else {}
                start = {
                    "k": inputs.trade_date,
                    "verified": verified,
                    "left_boundary_unknown": not verified,
                    "prior_R20": prior_features.get("R20"),
                    "prior_q60": prior_features.get("R20_q60"),
                }
            if start:
                state["starts"][path_id] = start
            early = any_of(
                all_of(
                    compare(f.get("H_t"), 65),
                    compare(f.get("L"), 60),
                    compare(f.get("X5"), 0, "gt"),
                    f["H_t"] > old_path.get("features", {}).get("H_t")
                    if adjacent
                    and finite(f.get("H_t"))
                    and finite(old_path.get("features", {}).get("H_t"))
                    else None,
                    f["L"] > old_path.get("features", {}).get("L")
                    if adjacent
                    and finite(f.get("L"))
                    and finite(old_path.get("features", {}).get("L"))
                    else None,
                ),
                all_of(startup, compare(f.get("X5"), 0, "gt")),
            )
            path = {
                "id": path_id,
                "source": source,
                "sector_id": sid,
                "sector_name": sector["sector_name"],
                "round_id": round_state.get("round_id"),
                "round_status": round_state["status"],
                "features": f,
                "identities": identities,
                "judgements": judges,
                "resilience": rs,
                "startup": startup,
                "startup_history_complete": startup is False
                and (
                    (old_path.get("startup_history_complete") is True and adjacent)
                    or (not old_path and chain == inputs.trade_date)
                ),
                "start": start,
                "early": all_of(p["eligible"], bool(raw), early),
                "limitations": [],
                "calculation_reasons": sector["calculation_reasons"],
            }
            state["paths"][path_id] = path
            paths.append(path)
        risk_j, position = _price_risk(p)
        price_clear = all_of(
            *(negate(v) for k, v in risk_j["inputs"].items() if k != "交易资格暂停")
        )
        x5 = (
            any_of(*(compare(path["features"].get("X5"), 0, "gt") for path in paths))
            if paths
            else None
        )
        risk = advance_risk(
            previous.get("risks", {}).get(symbol),
            risk=risk_j["value"],
            price_clear=price_clear,
            recovery=all_of(
                _ge_price(p["features"].get("close"), p["features"].get("ma20")),
                x5,
                negate(p["meta"].get("qualification_pause")),
            ),
            date=inputs.trade_date,
        )
        risk["judgement"] = risk_j
        state["risks"][symbol] = risk
        if risk["paused"] != previous.get("risks", {}).get(symbol, {}).get("paused", False):
            events.append(
                {
                    "date": inputs.trade_date,
                    "symbol": symbol,
                    "type": "risk_pause" if risk["paused"] else "risk_recovered",
                    "reason": "当前风险暂停状态变化",
                }
            )
        records.append(
            {
                "symbol": symbol,
                "name": p["meta"].get("name") or symbol,
                "regime": p["meta"].get("regime"),
                "paths": paths,
                "risk": risk,
                "position": position,
                "limitations": [],
                "selected": False,
                "qualification": {
                    **p["pre"],
                    "eligible": p["eligible"],
                    "amount_ok": p["amount_ok"],
                    "amount_median": p["amount_median"],
                    "warmup_count": p["warmup_count"],
                },
                "wait_conditions": [
                    "观察同题材相对强弱及核心地位能否延续",
                    "留意价格与20日均线的关系、位置热度及压力后的表现",
                ],
            }
        )
    _finalize_paths(records, state, previous, archive, calendar, dates, policy, inputs)
    _annotate_events(records, state, previous, events, inputs.trade_date)
    # A later catch-up only needs intervals overlapping its seven-day continuity
    # window. Old unconfirmed probes are not forward state.
    for key, identity in previous.get("identities", {}).items():
        intervals = identity.get("intervals", [])
        relevant = any(
            interval.get("ended_at") is None or interval["ended_at"] >= dates[-7]
            for interval in intervals
        )
        if key not in state["identities"] and relevant:
            state["identities"][key] = identity
    current = []
    previous_symbols = {r["symbol"] for r in last.get("records", []) if r.get("purpose")}
    for record in records:
        _classify_record(record, previous_symbols, state, inputs)
        if record["purpose"] is not None:
            current.append(record)
        elif record["symbol"] in previous_symbols:
            events.append(
                {
                    "date": inputs.trade_date,
                    "symbol": record["symbol"],
                    "type": "observation_ended",
                    "reason": "当日观察资格结束",
                }
            )
    selected = _select(current, sector_by_id)
    for record in current:
        record["selected"] = record["symbol"] in selected
    amount_coverage = []
    for regime, count in sorted(amount_counts.items()):
        passed, failed, unknown = count["n_pass"], count["n_fail"], count["n_unknown"]
        total, decidable = passed + failed + unknown, passed + failed
        amount_coverage.append(
            {
                "regime": regime,
                "n_pre": total,
                "n_pass": passed,
                "n_fail": failed,
                "n_unknown": unknown,
                "n_decidable": decidable,
                "coverage_rate": passed / total if total else None,
                "decidable_pass_rate": passed / decidable if decidable else None,
            }
        )
    sector_coverage = {}
    for source in ("concept", "industry"):
        same = [s for s in sectors if s["source"] == source]
        expected = [s for s in same if not s["coverage"]["structurally_ineligible"]]
        computable = sum(s["coverage"]["computable"] for s in expected)
        sector_coverage[source] = {
            "expected": len(expected),
            "computable": computable,
            "structurally_ineligible": len(same) - len(expected),
            "missing": len(expected) - computable,
            "coverage_rate": computable / len(expected) if expected else None,
            "entry_count": sum(s["entry"]["value"] is True for s in expected),
            "round_counts": dict(Counter(s["round"]["status"] for s in same)),
            "emotion_decidable": sum(s["emotion_entry"] is not None for s in expected),
            "trend_decidable": sum(s["trend_entry"] is not None for s in expected),
            "emotion_entered": sum(s["emotion_entry"] is True for s in expected),
            "trend_entered": sum(s["trend_entry"] is True for s in expected),
        }
    stats = {
        "scope_count": sum(p["meta"].get("in_scope") is True for p in profiles.values()),
        "eligible_count": sum(p["eligible"] is True for p in profiles.values()),
        "scored_count": len(
            {
                s
                for group in scores.values()
                for s, f in group.items()
                if finite(f.get("H_t")) or finite(f.get("H_b"))
            }
        ),
        "unknown_count": sum(p["eligible"] is None for p in profiles.values()),
        "failed_count": sum(p["eligible"] is False for p in profiles.values()),
        "report_count": len(current),
        "selected_count": len(selected),
        "purpose_counts": {p: sum(r["purpose"] == p for r in current) for p in PURPOSES},
        "amount_coverage": amount_coverage,
        "sector_coverage": sector_coverage,
        "zero_selection_reasons": sorted(
            {
                reason
                for r in current
                for path in r["paths"]
                for reason in path["selection"]["failed_reasons"]
                + path["selection"]["unknown_reasons"]
            }
        )
        if not selected
        else [],
        "suspended_count": sum(p["meta"].get("trading_normal") is False for p in profiles.values()),
        "resumption_window_count": sum(
            p["meta"].get("suspension_window") is True and p["meta"].get("trading_normal") is True
            for p in profiles.values()
        ),
    }
    if not selected and not stats["zero_selection_reasons"]:
        stats["zero_selection_reasons"] = ["没有完整核心或候选资格"]
    stats.update(
        _audit_statistics(
            records,
            current,
            sectors,
            profiles,
            previous,
            archive,
            calendar,
            inputs.trade_date,
            last.get("statistics", {}),
        )
    )
    # Archive each numerical evidence set once. Forward state stores only the fields
    # consumed on a later day; public records retain full independent explanations.
    current_paths = {association["path_id"] for association in state["associations"].values()}
    compact_identities = {}
    for key, identity in state["identities"].items():
        intervals = identity.get("intervals", [])
        if not intervals and (
            key.rsplit("|", 1)[0] not in current_paths
            or identity["status"] == "none"
            or (
                identity["status"] == "unverified"
                and not state["candidate_stages"].get(key.rsplit("|", 1)[0], {}).get("active")
            )
        ):
            continue
        earlier = [
            interval
            for interval in intervals
            if interval.get("ended_at") is not None and interval["ended_at"] < dates[-7]
        ]
        recent = [
            interval
            for interval in intervals
            if interval.get("ended_at") is None or interval["ended_at"] >= dates[-7]
        ]
        compact_identities[key] = {
            **identity,
            "intervals": [*earlier[-1:], *recent],
            "verification_events": identity.get("verification_events", [])[-1:],
        }
    state["identities"] = compact_identities
    state["risks"] = {symbol: risk for symbol, risk in state["risks"].items() if risk["paused"]}
    state["candidate_stages"] = {
        key: stage for key, stage in state["candidate_stages"].items() if stage.get("active")
    }
    state["sectors"] = {
        sid: {k: sector[k] for k in ("members", "classification_members", "coverage")}
        for sid, sector in sector_by_id.items()
    }
    state["paths"] = {
        key: {
            "features": {
                name: path["features"].get(name) for name in ("H_t", "L", "R20", "R20_q60")
            },
            "judgements": {
                name: {"value": value["value"]}
                for name, value in path["judgements"].items()
                if name.endswith("_entry")
            },
            "startup": path["startup"],
            "startup_history_complete": path["startup_history_complete"],
            "catchup": {"value": path["catchup"]["value"]},
        }
        for key, path in state["paths"].items()
    }
    cancel()
    return {
        "trade_date": inputs.trade_date,
        "next_trade_date": inputs.next_trade_date,
        "cutoff": inputs.cutoff,
        "rule_version": RULE_VERSION,
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "scope_version": scope_version,
        "disclaimer": DISCLAIMER,
        "summary": f"今日入选观察{len(selected)}只，完整跟踪{len(current)}只。"
        if selected
        else "今日暂无入选观察股票；完整跟踪记录及限制继续保留。",
        "market": {**inputs.market, "policy": policy},
        "records": current,
        "sectors": sectors,
        "events": events,
        "statistics": stats,
        "selected": selected,
        "state": state,
        "provenance": inputs.provenance,
        "limitations": [
            "观察规则为初值，识别与跟踪效果仍需积累记录；当前成员同组比较用于观察相对强弱",
            "停牌中断频率及持续时间尚未实测；持续积累逐日证据",
        ]
        + (
            [inputs.provenance["sector_filter"]["limitation"]]
            if inputs.provenance.get("sector_filter", {}).get("limitation")
            else []
        ),
        "calculations": [
            {
                "symbol": r["symbol"],
                "qualification": r["qualification"],
                "features": profiles[r["symbol"]]["features"],
                "risk": r["risk"],
                "position": r["position"],
            }
            for r in records
        ],
    }


def _finalize_paths(records, state, previous, archive, calendar, dates, policy, inputs):
    all_identities = {**previous.get("identities", {}), **state["identities"]}
    reference_groups = defaultdict(list)
    for key, identity in all_identities.items():
        if identity.get("intervals") and identity.get("rule_version") == RULE_VERSION:
            reference_groups[(identity.get("sector_id"), identity.get("round_id"))].append(
                (key, identity)
            )
    for record in records:
        for path in record["paths"]:
            f, start, references = path["features"], path.get("start"), []
            k = start.get("k") if start and start.get("verified") else None
            k_index = calendar.index(k) if k in calendar else None
            continuity_dates = (
                calendar[k_index - 1 :]
                if k_index is not None and k_index > 0 and len(calendar) - 1 - k_index <= 5
                else []
            )
            for key, identity in reference_groups[(path["sector_id"], path["round_id"])]:
                if key.startswith(record["symbol"] + "|"):
                    continue
                for interval in identity.get("intervals", []):
                    validity = {}
                    for day in continuity_dates:
                        observed = (
                            state
                            if day == inputs.trade_date
                            else archive.get(day, {}).get("state", {})
                        )
                        item = observed.get("identities", {}).get(key, {})
                        same_interval = next(
                            (
                                x
                                for x in item.get("intervals", [])
                                if x["confirmed_at"] == interval["confirmed_at"]
                            ),
                            None,
                        )
                        if (
                            same_interval
                            and same_interval.get("ended_at")
                            and same_interval["ended_at"] <= day
                        ):
                            validity[day] = False
                        elif (
                            same_interval
                            and item.get("observed_at") == day
                            and item.get("intervals", [])[-1]["confirmed_at"]
                            == interval["confirmed_at"]
                        ):
                            validity[day] = item.get("valid")
                        else:
                            validity[day] = None
                    reference = reference_interval(
                        interval, k=k, dates=continuity_dates, validity=validity
                    )
                    references.append(
                        {
                            **reference,
                            "symbol": key.split("|")[0],
                            "type": identity["type"],
                            "sector_id": path["sector_id"],
                            "round_id": path["round_id"],
                            "rule_version": RULE_VERSION,
                            "daily_validity": validity,
                        }
                    )
            coverage = bool(continuity_dates) and all(
                d == inputs.trade_date or d in archive for d in continuity_dates
            )
            ref_value = any_of(*(r["value"] for r in references), *([] if coverage else [None]))
            pre_position = (
                start["prior_R20"] <= start["prior_q60"]
                if start and finite(start.get("prior_R20")) and finite(start.get("prior_q60"))
                else None
            )
            no_core = not any(
                i.get("intervals") and i["intervals"][-1].get("ended_at") is None
                for p in record["paths"]
                for i in p["identities"]
            )
            window_ok = (
                len(calendar) - 1 - calendar.index(start["k"]) <= 5
                if start and start.get("k") in calendar
                else None
            )
            catchup = judgement(
                {
                    "活跃正式轮次": path["round_status"] == "active",
                    "可验证首次启动": True if k else None,
                    "至少一条完整先行核心参照": ref_value,
                    "启动前不高于60%分位": pre_position,
                    "5日收益至少12%": compare(f.get("R5"), 0.12),
                    "加速比至少45%": compare(f.get("a"), 0.45),
                    "加速质量至少70": compare(f.get("H_e"), 70),
                    "领涨分至少60": compare(f.get("L"), 60),
                    "5日超额为正": compare(f.get("X5"), 0, "gt"),
                    "收盘不低于MA20": _ge_price(f.get("close"), f.get("ma20")),
                    "尚无确认核心": no_core,
                    "启动不超过5交易日": window_ok,
                }
            )
            prior = [
                archive.get(d, {})
                .get("state", {})
                .get("paths", {})
                .get(path["id"], {})
                .get("catchup", {})
                .get("value")
                for d in dates[-3:-1]
            ] + [catchup["value"]]
            catchup.update(
                references=references,
                k=start.get("k") if start else None,
                verified_start=bool(k),
                sustained=confirmation(catchup["value"], prior, True),
            )
            path["catchup"] = catchup
            live = any(i["valid"] is True for i in path["identities"])
            candidate = any(
                i["entry"] is True
                and i["confirmation"] is not None
                and not (i.get("intervals") and i["intervals"][-1].get("ended_at") is None)
                for i in path["identities"]
            )
            rs = path["resilience"]
            resilience_pass = all_of(
                rs["quality"] != "poor",
                not rs["local_weak"],
                None if rs["ongoing"] is None else True,
                None if rs["status"] == "data_missing" else True,
            )
            board_ok = path["round_status"] == "active"
            common = {
                "观察位置条件满足": record["position"]["observation_eligibility"]["value"],
                "当前风险核验通过": negate(record["risk"]["risk_now"]),
                "无未解除暂停": not record["risk"]["paused"],
                "市场政策通过": policy["value"],
                "板块活跃": board_ok,
                "韧性无观察限制": resilience_pass,
            }
            by_type = {}
            for identity in path["identities"]:
                has_interval = bool(
                    identity.get("intervals") and identity["intervals"][-1].get("ended_at") is None
                )
                if has_interval:
                    identity_ok = all_of(
                        identity["valid"],
                        identity["maintenance"],
                        None
                        if identity["status"] == "unverified"
                        else identity["status"] == "confirmed",
                    )
                else:
                    identity_ok = all_of(
                        identity["entry"], None if identity["confirmation"] is None else True
                    )
                by_type[identity["type"]] = {
                    "role": identity["type"] if has_interval else "candidate",
                    **judgement({"完整核心或候选路径": identity_ok, **common}),
                }
            if not live and not candidate:
                catchup_gate = all_of(
                    catchup["sustained"],
                    inputs.market.get("phase") == "rally" if inputs.market.get("phase") else None,
                )
                by_type["catchup"] = {
                    "role": "catchup",
                    **judgement({"完整核心或候选路径": catchup_gate, **common}),
                }
            identity_ok = any_of(
                *(value["inputs"]["完整核心或候选路径"] for value in by_type.values())
            )
            selection = judgement({"完整核心或候选路径": identity_ok, **common})
            available = [kind for kind, value in by_type.items() if value["value"] is True]
            role_order = {"board": 0, "trend": 1, "candidate": 2, "catchup": 3}
            best_type = (
                min(available, key=lambda kind: (role_order[by_type[kind]["role"]], kind))
                if available
                else None
            )
            role = by_type[best_type]["role"] if best_type else None
            path.update(
                selection=selection,
                selection_role=role,
                selection_type=best_type,
                selection_types=available,
                selection_by_type=by_type,
            )
            limits = []
            if not board_ok:
                limits.append("板块暂停或待核验，停止新增正式确认")
            for identity in path["identities"]:
                if identity["status"] == "divergent":
                    limits.append(
                        f"{'连板' if identity['type'] == 'board' else '趋势'}核心处于分歧或恢复观察"
                    )
                elif identity["status"] == "unverified" and identity.get("intervals"):
                    limits.append(
                        f"{'连板' if identity['type'] == 'board' else '趋势'}历史确认身份当前待核验"
                    )
            if rs["status"] in {"untested", "limited"}:
                limits.append("近20日无足够压力样本，抗跌与修复能力尚未验证")
            if rs["status"] == "data_missing":
                limits.extend(rs["unknown_reasons"])
            if rs["quality"] == "poor" or rs["local_weak"]:
                limits.append("韧性整体或局部较差，限制精选")
            if rs["ongoing"]:
                limits.append("压力进行中，成熟历史不能掩盖当前冲击")
            if catchup["value"] is None and path["startup"] is True:
                limits.append("加速事实成立，当前补涨证据不足")
            path["limitations"] = limits
            candidate_now = (
                any(i["entry"] is True for i in path["identities"]) or catchup["value"] is True
            )
            old_stage = previous.get("candidate_stages", {}).get(path["id"])
            if candidate_now and not live:
                stage = {
                    "candidate_first_entered_at": old_stage["candidate_first_entered_at"]
                    if old_stage and old_stage.get("active")
                    else inputs.trade_date,
                    "active": True,
                    "new_today": not old_stage or not old_stage.get("active"),
                }
                state["candidate_stages"][path["id"]] = stage
                path.update(stage)
            elif (
                old_stage
                and old_stage.get("active")
                and not live
                and (
                    any(i["entry"] is None for i in path["identities"])
                    or (path.get("start") and catchup["value"] is None)
                )
            ):
                stage = {**old_stage, "active": True, "new_today": False, "unverified": True}
                state["candidate_stages"][path["id"]] = stage
                path.update(stage)
            else:
                state["candidate_stages"][path["id"]] = {**(old_stage or {}), "active": False}


def _classify_record(record, previous_symbols, state, inputs):
    identities = [i for p in record["paths"] for i in p["identities"]]
    live = any(i["valid"] is True for i in identities)
    maintenance_unknown = any(
        i["valid"] is None and i.get("intervals") and i["intervals"][-1].get("ended_at") is None
        for i in identities
    )
    invalid = any(
        i["status"] == "invalidated"
        and i.get("intervals")
        and i["intervals"][-1].get("ended_at") == inputs.trade_date
        for i in identities
    )
    invalid = invalid or any(
        k.startswith(record["symbol"] + "|")
        and i.get("intervals")
        and i["intervals"][-1].get("ended_at") == inputs.trade_date
        for k, i in state["identities"].items()
    )
    candidate = any(
        i["entry"] is True
        and i["confirmation"] is not None
        and not (i.get("intervals") and i["intervals"][-1].get("ended_at") is None)
        for i in identities
    ) or any(p["catchup"]["value"] is True for p in record["paths"])
    candidate_unknown = any(i["entry"] is True and i["confirmation"] is None for i in identities)
    observed = (
        record["symbol"] in previous_symbols
        or live
        or invalid
        or candidate
        or candidate_unknown
        or any(p["early"] is True for p in record["paths"])
    )
    if record["symbol"] in previous_symbols:
        candidate_unknown = (
            candidate_unknown
            or any(i["entry"] is None for i in identities)
            or any(p["catchup"]["value"] is None and p["startup"] is True for p in record["paths"])
        )
    record["purpose"] = (
        primary_purpose(
            paused=record["risk"]["paused"] and observed,
            invalidated=invalid,
            live=live,
            maintenance_unknown=maintenance_unknown,
            candidate=candidate,
            candidate_unknown=candidate_unknown,
            position=record["position"]["observation_eligibility"]["value"],
            early=any(p["early"] is True for p in record["paths"]),
        )
        if observed or maintenance_unknown
        else None
    )
    limits = []
    if record["risk"]["risk_now"] is None:
        limits.append(
            "当前风险证据不足：" + "、".join(record["risk"]["judgement"]["unknown_reasons"])
        )
    if record["risk"]["paused"]:
        limits.append("风险暂停尚未满足全部解除条件")
    policy = _market_policy(inputs.market)
    limits.extend(policy["observation_hints"])
    if policy["value"] is None:
        limits.append("市场政策证据不足：" + "、".join(policy["unknown_reasons"]))
    elif policy["value"] is False:
        limits.append("市场观察限制：" + "、".join(policy["failed_reasons"]))
    if inputs.market.get("phase") == "unclassified":
        reason = inputs.market.get("phase_reason")
        limits.append(
            "阶段候选待确认"
            if reason == "pending_confirmation"
            else "阶段未分类：当前未命中阶段规则"
            if reason == "no_rule_match"
            else "阶段未归类，具体原因未记录"
        )
    if inputs.market.get("state") == "unavailable":
        limits.append("市场状态不可用")
    if inputs.market.get("phase") == "unknown":
        limits.append("阶段未知：核心输入不足")
    if record["position"]["overheated"] is True:
        limits.append("位置过热：仅作后续跟踪，留意价格回落")
    eligibility = record["position"]["observation_eligibility"]
    if eligibility["value"] is False:
        limits.append("位置条件未满足：" + "、".join(eligibility["failed_reasons"]))
    elif eligibility["value"] is None:
        limits.append("位置证据不足：" + "、".join(eligibility["unknown_reasons"]))
    record["limitations"] = limits
    if record["purpose"] not in {"已确认核心跟踪", "新增观察候选"}:
        for path in record["paths"]:
            path["selection"] = judgement(
                {**path["selection"]["inputs"], "全股主要用途允许精选": False}
            )
            for kind, typed in path["selection_by_type"].items():
                path["selection_by_type"][kind] = {
                    "role": typed["role"],
                    **judgement({**typed["inputs"], "全股主要用途允许精选": False}),
                }
            path["selection_types"] = []


def _select(records: list[dict], sectors: dict) -> list[str]:
    selected, catchup_count = [], 0
    for source in ("concept", "industry"):
        buckets = defaultdict(list)
        for record in records:
            for path in record["paths"]:
                if path["source"] == source and path["selection"]["value"] is True:
                    buckets[path["sector_id"]].append((record, path))
        ordered = sorted(buckets, key=lambda sid: _sector_order(sectors[sid]))
        industry_count = 0
        used_roles = defaultdict(set)
        for turn in range(2):
            for sid in ordered:
                choices = [
                    (r, p)
                    for r, p in buckets[sid]
                    if r["symbol"] not in selected
                    and (p["selection_role"] != "catchup" or catchup_count < 1)
                ]

                def order(item, used=used_roles[sid], second=bool(turn)):
                    record, path = item
                    available = path["selection_types"]

                    def type_order(kind):
                        role = path["selection_by_type"][kind]["role"]
                        role_order = {"board": 0, "trend": 1, "candidate": 2, "catchup": 3}[role]
                        rank_key = (
                            "rank_b"
                            if kind == "board"
                            else "rank_e"
                            if kind == "catchup"
                            else "rank_t"
                        )
                        return (
                            kind in used if second else False,
                            role_order,
                            path["features"].get(rank_key) or 999999,
                            kind,
                        )

                    kind = min(available, key=type_order)
                    return (*type_order(kind), record["symbol"])

                if not choices:
                    continue
                record, path = min(choices, key=order)
                selected.append(record["symbol"])
                path["selected_path"] = True
                chosen_type = order((record, path))[-2]
                path["selected_type"] = chosen_type
                path["selection_type"] = chosen_type
                path["selection_role"] = path["selection_by_type"][chosen_type]["role"]
                used_roles[sid].add(chosen_type)
                catchup_count += path["selection_role"] == "catchup"
                if source == "industry":
                    industry_count += 1
                if len(selected) == 5:
                    return selected
                if source == "industry" and industry_count >= 2:
                    return selected
    return selected


def _annotate_events(records, state, previous, events, date):
    """Events are independent from summary quotas and keep identity interval boundaries."""
    for record in records:
        for path in record["paths"]:
            if path.get("new_today"):
                events.append(
                    {
                        "date": date,
                        "symbol": record["symbol"],
                        "sector_id": path["sector_id"],
                        "type": "candidate_entered",
                        "reason": "进入本轮候选观察阶段",
                    }
                )
            for identity in path["identities"]:
                intervals = identity.get("intervals", [])
                if not intervals or intervals[-1]["confirmed_at"] != date:
                    continue
                peers = [
                    old
                    for key, old in previous.get("identities", {}).items()
                    if not key.startswith(record["symbol"] + "|")
                    and old.get("sector_id") == path["sector_id"]
                    and old.get("round_id") == path["round_id"]
                    and old.get("type") == identity["type"]
                ]
                ended = [
                    old
                    for old in peers
                    if old.get("intervals")
                    and (state["identities"].get(old.get("id"), old).get("intervals") or [{}])[
                        -1
                    ].get("ended_at")
                    is not None
                ]
                if ended:
                    events.append(
                        {
                            "date": date,
                            "symbol": record["symbol"],
                            "sector_id": path["sector_id"],
                            "type": "core_replacement",
                            "reason": "同轮同类型旧核心已明确失效，新核心确认",
                            "previous_identities": [old.get("id") for old in ended],
                        }
                    )
                elif any(old.get("valid") is True for old in peers):
                    events.append(
                        {
                            "date": date,
                            "symbol": record["symbol"],
                            "sector_id": path["sector_id"],
                            "type": "core_added",
                            "reason": "新核心加入，未证明替代原核心",
                        }
                    )


def _audit_statistics(
    records, current, sectors, profiles, previous, archive, calendar, date, previous_statistics
):
    """Observed production denominators; never infer missing historical suspension evidence."""
    eligible_objects, only_risk_unknown, mixed_risk_unknown = set(), set(), set()
    blocking = defaultdict(set)
    path_audit = []
    for record in records:
        for path in record["paths"]:
            identities = path["identities"]
            relevant = (
                any(i["valid"] is True or i["entry"] is True for i in identities)
                or path["catchup"]["value"] is True
            )
            if not relevant:
                continue
            eligible_objects.add(record["symbol"])
            judgement_ = path["selection"]
            parts = judgement_["inputs"]
            unknown_risk = parts.get("当前风险核验通过") is None
            other_pass = all(v is True for k, v in parts.items() if k != "当前风险核验通过")
            if unknown_risk:
                (only_risk_unknown if other_pass else mixed_risk_unknown).add(record["symbol"])
            for reason in judgement_["failed_reasons"] + judgement_["unknown_reasons"]:
                blocking[reason].add(record["symbol"])
            path_audit.append(
                {
                    "symbol": record["symbol"],
                    "path_id": path["id"],
                    "value": judgement_["value"],
                    "only_risk_unknown": unknown_risk and other_pass,
                    "failed_reasons": judgement_["failed_reasons"],
                    "unknown_reasons": judgement_["unknown_reasons"],
                }
            )
    # A passing independent path makes a stock eligible even if another path is blocked.
    passing = {
        r["symbol"] for r in records if any(p["selection"]["value"] is True for p in r["paths"])
    }
    blocked = eligible_objects - passing
    selected = {r["symbol"] for r in current if r.get("selected")}
    prior_frequency = previous_statistics.get("publication_frequency", {})
    # Cumulative counters make a bounded seven-report read equivalent to a full replay.
    zero_days = prior_frequency.get(
        "zero_selection_days",
        sum(r.get("statistics", {}).get("selected_count") == 0 for r in archive.values()),
    ) + (not selected)
    report_days = prior_frequency.get("report_days", len(archive)) + 1
    only_risk_days = prior_frequency.get(
        "zero_with_risk_only_unknown_days",
        sum(
            r.get("statistics", {}).get("zero_selection_only_risk_unknown", False)
            for r in archive.values()
        ),
    )
    zero_risk = not selected and bool(only_risk_unknown & blocked)
    suspensions = []
    for source in ("concept", "industry"):
        same = [
            s
            for s in sectors
            if s["source"] == source and not s["coverage"]["structurally_ineligible"]
        ]
        affected, still_computable, insufficient, previous_six, lost_after_suspension = (
            0,
            0,
            0,
            0,
            0,
        )
        for sector in same:
            before = previous.get("sectors", {}).get(sector["id"])
            lost = (
                [
                    symbol
                    for symbol in before["members"]
                    if symbol not in sector["members"]
                    and symbol in profiles
                    and (
                        profiles[symbol]["meta"].get("trading_normal") is False
                        or profiles[symbol]["meta"].get("suspension_window") is True
                    )
                ]
                if before
                else []
            )
            old_count = len(before["members"]) if before else None
            sector["coverage"].update(
                previous_member_count=old_count,
                suspension_related_lost_members=lost,
                previous_size_group="unknown"
                if old_count is None
                else "6"
                if old_count == 6
                else "7-10"
                if 7 <= old_count <= 10
                else "11-20"
                if 11 <= old_count <= 20
                else "20+"
                if old_count > 20
                else "other",
            )
            exposure = (
                sector["coverage"]["suspended"] > 0 or sector["coverage"]["resumption_window"] > 0
            )
            affected += exposure
            still_computable += exposure and sector["coverage"]["computable"]
            insufficient += exposure and sector["member_count"] < 6
            previous_six += old_count is not None and old_count >= 6
            lost_after_suspension += (
                old_count is not None
                and old_count >= 6
                and sector["member_count"] < 6
                and bool(lost)
            )
        suspensions.append(
            {
                "source": source,
                "expected": len(same),
                "exposed": affected,
                "still_computable": still_computable,
                "insufficient": insufficient,
                "previous_at_least_six": previous_six,
                "suspension_related_shortfall": lost_after_suspension,
                "related_shortfall_rate": lost_after_suspension / previous_six
                if previous_six
                else None,
            }
        )
    catchup_audit = []
    for record in records:
        for path in record["paths"]:
            start = path.get("start")
            if not start:
                continue
            cj = path["catchup"]
            other = all_of(*(v for k, v in cj["inputs"].items() if k != "至少一条完整先行核心参照"))
            catchup_audit.append(
                {
                    "symbol": record["symbol"],
                    "path_id": path["id"],
                    "k": start["k"],
                    "verified_start": start["verified"],
                    "other_conditions": other,
                    "reference_condition": cj["inputs"]["至少一条完整先行核心参照"],
                    "value": cj["value"],
                    "previous_value": previous.get("paths", {})
                    .get(path["id"], {})
                    .get("catchup", {})
                    .get("value"),
                    "failed_reasons": cj["failed_reasons"],
                    "unknown_reasons": cj["unknown_reasons"],
                }
            )
    identity_audit = []
    identity_lookup = {i["id"]: i for r in records for p in r["paths"] for i in p["identities"]}
    for key, old in previous.get("identities", {}).items():
        if not old.get("intervals") or old["intervals"][-1].get("ended_at") is not None:
            continue
        symbol = key.split("|")[0]
        current_identity = identity_lookup.get(key)
        meta = profiles.get(symbol, {}).get("meta", {})
        reasons = []
        if meta.get("trading_normal") is False:
            reasons.append("本人当日停牌")
        if meta.get("suspension_window") is True and meta.get("trading_normal") is True:
            reasons.append("本人复牌窗口未恢复")
        sector = next((s for s in sectors if s["id"] == old.get("sector_id")), None)
        if sector and sector["member_count"] < 6:
            reasons.append("同组人数不足")
        unknown = bool(current_identity and current_identity.get("valid") is None)
        if unknown and not reasons:
            reasons.append("其他必要证据缺口")
        identity_audit.append(
            {
                "identity_id": key,
                "symbol": symbol,
                "unverified": unknown,
                "reasons": reasons,
                "status": current_identity.get("status")
                if current_identity
                else "exited_or_unobserved",
            }
        )
    unverified = sum(i["unverified"] for i in identity_audit)
    return {
        "selection_path_audit": path_audit,
        "selection_eligible_before_quota": len(passing),
        "selection_quota_excluded": len(passing - selected),
        "selection_blocked_stock_count": len(blocked),
        "selection_blocking_counts": {
            reason: len(symbols & blocked) for reason, symbols in blocking.items()
        },
        "risk_unknown_only_stock_count": len(only_risk_unknown & blocked),
        "risk_unknown_mixed_stock_count": len(mixed_risk_unknown & blocked),
        "zero_selection_only_risk_unknown": zero_risk,
        "publication_frequency": {
            "report_days": report_days,
            "zero_selection_days": zero_days,
            "zero_selection_rate": zero_days / report_days,
            "zero_with_risk_only_unknown_days": only_risk_days + zero_risk,
        },
        "suspension_sector_audit": suspensions,
        "identity_continuity_audit": identity_audit,
        "catchup_opportunity_audit": catchup_audit,
        "catchup_reference_coverage": {
            "eligible_opportunity_days": sum(a["other_conditions"] is True for a in catchup_audit),
            "true": sum(
                a["other_conditions"] is True and a["reference_condition"] is True
                for a in catchup_audit
            ),
            "false": sum(
                a["other_conditions"] is True and a["reference_condition"] is False
                for a in catchup_audit
            ),
            "unknown": sum(
                a["other_conditions"] is True and a["reference_condition"] is None
                for a in catchup_audit
            ),
            "left_boundary_unknown": sum(not a["verified_start"] for a in catchup_audit),
        },
        "previous_open_identity_count": len(identity_audit),
        "unverified_identity_count": unverified,
        "unverified_identity_rate": unverified / len(identity_audit) if identity_audit else None,
        "unverified_stock_count": len({i["symbol"] for i in identity_audit if i["unverified"]}),
    }
