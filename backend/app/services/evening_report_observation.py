"""观察名单：题材、互斥角色与配额。低吸只产生标签，不参与名额分配。"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from math import fsum
from statistics import mean, median

import polars as pl

from app.price_limits import parse_listing_date
from app.services.evening_report_classification import exclusion
from app.services.evening_report_dip import dip_labels
from app.services.evening_report_rules import (
    SELECTION_POLICY_VERSION,
    all_of,
    any_of,
    compare,
    finite,
    judgement,
    ratio,
)
from app.services.evening_report_wave import PARAMETERS
from app.services.evening_report_wave import VERSION as WAVE_VERSION

VERSION = SELECTION_POLICY_VERSION
ROLE_NAMES = {"true_leader": "真龙", "fallback_leader": "兜底龙", "emotion": "情绪股", "other": "其他观察"}
WAVE_VALUE_FIELDS = {
    "current_wave_index", "completed_wave_count", "first_impulse_return", "first_wave_pullback",
    "retrace_pct", "first_pullback_active", "pullback_volume_ratio", "impulse_volume_mean", "pullback_volume_mean",
}


def classify_role(values: dict, last_true_date: str | None = None) -> dict:
    true = all_of(compare(values.get("return_20d"), .50), compare(values.get("identity_score"), 45),
                  compare(values.get("high_proximity"), .85))
    fallback = any_of(compare(values.get("return_20d"), .30), compare(values.get("identity_score"), 30))
    emotion = all_of(compare(values.get("return_5d"), .12), compare(values.get("acceleration_5_20"), .45),
                     compare(values.get("return_20d"), .55, "lt"))
    role = "true_leader" if true is True else "fallback_leader" if fallback is True else "emotion" if emotion is True else "other"
    pending = bool(last_true_date) and true is not True
    return {"role": ("historical_leader" if pending and role in ("emotion", "other") else role), "daily_role": role,
            "role_by_history_tracking": "true_leader" if last_true_date else None,
            "role_status": "review_pending" if pending else "qualified",
            "emotion_attribute": emotion,
            "higher_role_pending": (true is None and role != "true_leader") or (fallback is None and role in ("emotion", "other")),
            "conditions": {"true_leader": true, "fallback_leader": fallback, "emotion": emotion},
            "last_true_date": last_true_date,
            "status_reason": "历史真龙跟踪待复核，结构失效规则尚待核验" if pending else None}


def _known_number(value, positive=False):
    return float(value) if finite(value, positive=positive) else None


def _previous_trading_day(calendar, index, halted):
    index -= 1
    while index >= 0 and calendar[index] in halted:
        index -= 1
    return calendar[index] if index >= 0 else None


def _daily_change(rows, calendar, index, halted):
    day = calendar[index]
    before = rows.get(_previous_trading_day(calendar, index, halted), {})
    today = rows.get(day, {})
    if day in halted or before.get("formal") is not True or today.get("formal") is not True:
        return None
    if finite(today.get("close"), positive=True) and finite(before.get("close"), positive=True):
        return today["close"] / before["close"] - 1
    return None


def stock_values(rows: dict[str, dict], calendar: list[str], halted: set[str]) -> dict:
    """Market-date windows; no skipping suspended/missing endpoints or incomplete windows."""
    def point(day, key, positive=False):
        row = rows.get(day, {})
        formal = row.get("formal")
        return _known_number(row.get(key), positive) if formal is True and day not in halted else None

    def window(key, count, *, offset=0, positive=False):
        days = calendar[len(calendar) - count - offset:len(calendar) - offset] if len(calendar) >= count + offset else []
        result = [point(day, key, positive) for day in days]
        return result if len(result) == count and None not in result else None

    close = point(calendar[-1], "close", True)
    values = {"raw_close": point(calendar[-1], "raw_close", True), "amount_day": point(calendar[-1], "amount"),
              "consecutive_limit_ups": point(calendar[-1], "consecutive_limit_ups")}
    values["change_pct"] = _daily_change(rows, calendar, len(calendar) - 1, halted)
    # 比例保留 12 位小数，使同收益在百分位、回退排名和最终排序中共用同一值。
    for count in (3, 5, 10, 20):
        start = point(calendar[-count - 1], "close", True) if len(calendar) > count else None
        values[f"return_{count}d"] = round(close / start - 1, 12) if close is not None and start else None
    flags = [False if day in halted else rows.get(day, {}).get("signal_limit_up")
             if rows.get(day, {}).get("formal") is True else None for day in calendar[-20:]]
    values["limit_up_count_20d"] = sum(flags) if len(flags) == 20 and None not in flags else None
    count, consecutive = values["limit_up_count_20d"], values["consecutive_limit_ups"]
    values["identity_score"] = min(100, count * 18 + consecutive * 14) if count is not None and consecutive is not None else None
    r20, r5 = values["return_20d"], values["return_5d"]
    values["space_score"] = min(100, r20 / .8 * 100) if r20 is not None else None
    values["acceleration_5_20"] = r5 / r20 if r20 is not None and r20 > 0 and r5 is not None else None
    highs = [point(d, "high", True) for d in calendar[-60:] if d not in halted] if len(calendar) >= 60 else []
    values["high_proximity"] = round(close / max(highs), 12) if highs and close and None not in highs else None
    amounts = window("amount", 20)
    values["amount_median_20d"] = median(amounts) if amounts else None
    for key in ("ma5", "ma10", "ma20", "bull_bear_line"):
        line = point(calendar[-1], key, True)
        values["distance_" + ("bull_bear" if key == "bull_bear_line" else key)] = close / line - 1 if close and line else None
        values[key] = line
    recent, prior = window("volume", 2), window("volume", 10, offset=2)
    values["volume_ratio_2_10"] = mean(recent) / mean(prior) if recent is not None and prior and mean(prior) > 0 else None
    amplitudes = []
    if len(calendar) >= 13:
        for before, day in zip(calendar[-13:-1], calendar[-12:], strict=True):
            h, low, prev = point(day, "high", True), point(day, "low", True), point(before, "close", True)
            amplitudes.append((h - low) / prev if h and low and prev else None)
    values["daily_amplitude"] = amplitudes[-1] if amplitudes else None
    values["amplitude_ratio_2_10"] = (mean(amplitudes[-2:]) / mean(amplitudes[:-2])
                                      if len(amplitudes) == 12 and None not in amplitudes and mean(amplitudes[:-2]) > 0 else None)
    lows = window("low", 3, positive=True)
    values["rising_lows_3d"] = lows[2] > lows[1] > lows[0] if lows else None
    high = point(calendar[-1], "high", True)
    values["intraday_drawdown"] = 1 - close / high if close and high else None
    today = calendar[-1]
    values["atr14_pct"] = ratio(point(today, "atr_14"), close)
    opening = point(today, "open", True)
    values["close_vs_open"] = close / opening - 1 if close and opening else None
    low = point(today, "low", True)
    previous = point(calendar[-2], "low", True) if len(calendar) > 1 else None
    values["low_vs_previous"] = low / previous - 1 if low and previous else None
    return values


def variable_definitions(keys, inputs) -> dict:
    formulas = {
        "change_pct": "当日收盘 / 上一实际交易日收盘 − 1（仅跨已确认停牌日）",
        "identity_score": "min(100, 20日涨停次数×18 + 当前连板数×14)",
        "space_score": "min(100, 20日涨幅 / 0.8 × 100)",
        "high_proximity": "收盘 / 最近60个市场交易日最高价",
        "acceleration_5_20": "5日涨幅 / 20日涨幅（分母>0）",
        "volume_ratio_2_10": "最近2日均量 / 此前不重叠10日均量",
        "amplitude_ratio_2_10": "最近2日平均振幅 / 此前不重叠10日平均振幅",
        "daily_amplitude": "(最高−最低) / 上交易日收盘",
        "intraday_drawdown": "1−收盘/最高",
        "amount_median_20d": "最近20个市场交易日成交额中位数",
        "bull_bear_line": "EMA10((H+L+C)/3), alpha=2/11, adjust=False",
        "rising_lows_3d": "L(D)>L(D−1)>L(D−2)",
        "relative_lead_score": "0.5×同组X20百分位 + 0.3×同组X5百分位 + 0.2×60日高点接近度标准化(0.85,1)",
        "low_vs_previous": "L(D)/L(D−1)−1",
        "first_wave_pullback": "1−C(D)/H1",
        "retrace_pct": "(H1−回调低点)/(H1−L0)",
        "pullback_volume_ratio": "回调区间均量/上涨段均量",
        "first_impulse_return": "H1/L0−1",
        "close_vs_open": "C(D)/O(D)−1", "atr14_pct": "ATR14/C(D)",
    }
    result = {}
    prices = {"raw_close", "ma5", "ma10", "ma20", "bull_bear_line"}
    counts = {"limit_up_count_20d", "consecutive_limit_ups", "current_wave_index", "completed_wave_count"}
    flags = {"rising_lows_3d", "first_pullback_active", "severe_event", "near_severe_event"}
    for key in sorted(set(keys) - {"price_gaps", "abnormal_move"}):
        unit = ("元" if key.startswith("amount") or key in prices else "分" if key.endswith("score")
                else "次" if key in counts else "布尔" if key in flags
                else "标准化成交量" if key.endswith("_volume_mean") else "小数")
        formula = formulas.get(key, "当日正式事实")
        if key.startswith("return_"):
            formula = f"C(D)/C(D−{key[7:-1]})−1"
        elif key.startswith("distance_"):
            formula = "C(D)/对应均线−1"
        result[key] = {"unit": unit, "formula": formula, "as_of": inputs.trade_date,
                       "rule_version": VERSION, "window": key}
        if key in WAVE_VALUE_FIELDS:
            result[key]["rule_version"] = WAVE_VERSION
        if key in {"raw_close", "limit_up_count_20d", "consecutive_limit_ups"}:
            result[key]["price_basis"] = "原始价格"
        elif not (key.startswith("amount") or "volume" in key or key in flags):
            result[key]["price_basis"] = "报告日基准前复权"
    return result


def _descending(value):
    return (value is None, -value if value is not None else 0)


def _competition_tier(obs: dict) -> int:
    if obs["role"] == "emotion":
        return 3
    return {"true_leader": 0, "fallback_leader": 1}.get(obs["daily_role"], 2)


def _sort_candidate(record: dict) -> tuple:
    obs = record["observation"]
    values = obs["values"]
    tier = _competition_tier(obs)
    keys = ("return_5d", "identity_score", "high_proximity") if tier == 3 else (
        "identity_score", "return_20d", "high_proximity")
    lead = (_descending(obs["theme_lead_evidence"]["sorting_value"]),) if tier < 2 else ()
    return (tier, *lead, *(_descending(values.get(key)) for key in keys),
            obs.get("board") != "chinext", record["symbol"])


def _choose_lead_metric(group: list[dict], group_id: list) -> dict:
    observations = [rec["observation"] for rec in group]
    entries = [obs.setdefault("theme_lead_evidence", {}) for obs in observations]
    sources = [entry["source"] for entry in entries if entry.get("source") is not None]
    reasons = []
    if any(entry.get("score") is None or entry.get("source") is None for entry in entries):
        reasons.append("lead_fallback_missing")
    if sources and any(source != sources[0] for source in sources[1:]):
        reasons.append("lead_fallback_inconsistent")
    returns = sorted((obs["values"]["return_20d"] for obs in observations
                      if obs["values"].get("return_20d") is not None), reverse=True)
    ranks = {}
    for index, value in enumerate(returns, 1):
        ranks.setdefault(value, index)
    summary = {"id": group_id, "metric": "return_20d_rank" if reasons else "relative_lead_score",
               "reason_codes": reasons, "total": len(group), "return_20d_known": len(returns),
               "return_20d_missing": len(group) - len(returns)}
    for obs, entry in zip(observations, entries, strict=True):
        rank = ranks.get(obs["values"].get("return_20d"))
        value = (-rank if rank is not None else None) if reasons else entry["score"]
        entry.update(competition_group=group_id, metric=summary["metric"], sorting_value=value,
                     return_20d_rank=rank, coverage=summary,
                     reason_codes=[*reasons, *(["lead_evidence_unknown"] if value is None else [])])
    return summary


def allocate(records: list[dict], themes: list[dict]) -> list[str]:
    """Choose one leading metric per whole competition group before allocation."""
    selected = []
    global_counts = Counter()
    by_theme = defaultdict(list)
    for record in records:
        obs = record["observation"]
        theme_id = obs.get("primary_theme", {}).get("id")
        if theme_id and obs["eligibility"]["value"] is True and obs["role"] != "other":
            by_theme[theme_id].append(record)

    for theme in themes:
        tiers = defaultdict(list)
        for record in by_theme[theme["id"]]:
            obs = record["observation"]
            tier = _competition_tier(obs)
            obs["quota_eligibility"] = tier != 3 or obs.get("board") == "chinext"
            if obs["quota_eligibility"]:
                tiers[tier].append(record)
        for tier, tier_rows in tiers.items():
            group_id = [theme.get("as_of"), theme["id"], tier_rows[0]["observation"]["role"]]
            if tier < 2:
                _choose_lead_metric(tier_rows, group_id)
            else:
                metric = "return_5d" if tier == 3 else "identity_score"
                for row in tier_rows:
                    obs = row["observation"]
                    obs.setdefault("theme_lead_evidence", {}).update(
                        metric=metric, sorting_value=obs["values"].get(metric), competition_group=group_id)
        groups = {
            "leader": [row for tier in (0, 1, 2) for row in sorted(tiers[tier], key=_sort_candidate)],
            "emotion": sorted(tiers[3], key=_sort_candidate),
        }
        for quota, group in groups.items():
            for rank, record in enumerate(group, 1):
                record["observation"]["rank_within_theme_role"] = rank
                record["observation"]["competed_quota_type"] = quota
                record["observation"]["rank_global_role"] = global_counts[quota] + rank
            global_counts[quota] += len(group)
        for record in groups["leader"][:2] + groups["emotion"][:1]:
            selected.append(record["symbol"])
            record["selected"] = True
            record["observation"]["rank"] = len(selected)
        theme["selected_count"] = min(len(groups["leader"]), 2) + min(len(groups["emotion"]), 1)
    return selected


def _member_days(rows, calendar, halted):
    facts = {}
    for index in range(len(calendar) - 5, len(calendar)):
        day = calendar[index]
        if day in halted:
            facts[day] = "halted"
            continue
        row = rows.get(day, {})
        sealed = row.get("signal_limit_up") if row.get("formal") is True else None
        if sealed is True:
            facts[day] = (1, 0, row.get("consecutive_limit_ups"))
        elif sealed is False:
            change = _daily_change(rows, calendar, index, halted)
            if change is not None:
                facts[day] = (0, int(compare(change, .07)), 0)
    return facts


def _rank_themes(inputs, calendar, facts):
    themes = []
    members = inputs.memberships.filter(pl.col("source") == "concept").unique(
        subset=["sector_id", "symbol"], keep="last", maintain_order=True)
    for (sector,), group in members.partition_by("sector_id", as_dict=True).items():
        symbols = sorted(group["symbol"].to_list())
        label = group["sector_name"][0]
        blocked = exclusion(sector) or exclusion(label)
        reasons = ([blocked[0]] if blocked else []) + (["broad_concept"] if len(symbols) > 120 else [])
        theme = {"id": "concept:" + sector, "name": label, "members": symbols, "member_count": len(symbols),
                 "as_of": inputs.trade_date, "members_as_of": inputs.cutoff or inputs.trade_date,
                 "exclusion_reason_codes": reasons,
                 "daily": [], "strength": None, "missing_count": 0, "selected_theme": False}
        themes.append(theme)
        if reasons:
            continue
        total_valid = 0
        for day in calendar[-5:]:
            member_days = [facts.get(symbol, {}).get(day) for symbol in symbols]
            valid = [item for item in member_days if isinstance(item, tuple)]
            heights = [height for _, _, height in valid if finite(height)]
            total_valid += len(valid)
            missing = member_days.count(None)
            theme["missing_count"] += missing
            theme["daily"].append({"date": day, "limit_up": sum(up for up, _, _ in valid) if valid or not missing else None,
                                   "strong": sum(strong for _, strong, _ in valid) if valid or not missing else None,
                                   "max_consecutive": max(heights, default=None), "missing_count": missing})
        if total_valid:
            theme["strength"] = sum(2 ** (-k / 2.5) * (d["limit_up"] + .5 * d["strong"])
                                    for k, d in enumerate(reversed(theme["daily"])) if d["limit_up"] is not None)
    ranked = [t for t in themes if t["strength"] is not None and t["strength"] > 0]
    ranked.sort(key=lambda t: (-t["strength"], _descending(t["daily"][-1]["limit_up"]),
                               _descending(t["daily"][-1]["max_consecutive"]), t["id"]))
    for rank, theme in enumerate(ranked, 1):
        theme["rank"] = rank
        theme["selected_theme"] = rank <= 5
    return themes, ranked[:5]


def _lead_scores(theme, records_by_symbol):
    peers = {}
    for symbol in theme["members"]:
        row = records_by_symbol.get(symbol)
        if row is None:
            continue
        obs = row["observation"]
        if obs["eligibility"]["value"] is True and all(obs["values"][key] is not None for key in ("return_5d", "return_20d")):
            peers[symbol] = obs["values"]
    theme["lead_source_members"] = sorted(peers)
    n = len(peers)
    if n < 6:
        return {}
    components = {symbol: {} for symbol in peers}
    for window in (5, 20):
        key = f"return_{window}d"
        total = fsum(v[key] for v in peers.values())
        relative = {}
        for symbol, values in peers.items():
            benchmark = (total - values[key]) / (n - 1)
            relative[symbol] = (1 + values[key]) / (1 + benchmark) - 1
        ordered = sorted(relative.values())
        for symbol, value in relative.items():
            left, right = bisect_left(ordered, value), bisect_right(ordered, value)
            components[symbol][f"X{window}"] = value
            components[symbol][f"_p{window}"] = 100 * (left + right - 1) / (2 * (n - 1))
    scores = {}
    source = {"sector_id": theme["id"], "sample_count": n, "windows": [20, 5, 60],
              "benchmark": "equal_weight_excluding_self", "as_of": theme["as_of"], "rule_version": VERSION}
    for symbol, part in components.items():
        proximity = peers[symbol]["high_proximity"]
        p = None if proximity is None else min(100., max(0., (proximity - .85) * 100 / .15))
        part.update(lead_proximity=proximity, lead_proximity_score=p)
        score = None if p is None else .5 * part["_p20"] + .3 * part["_p5"] + .2 * p
        scores[symbol] = {"score": score, "components": part, "source": source}
    return scores


def _label_candidate(record, wave, market, table, day, cutoff):
    from app.services.evening_report_events import event_paths

    events = event_paths(table, record["symbol"], day)
    obs = record["observation"]
    values = obs["values"]
    if wave:
        checks = wave["required_checks"]
        distances = [values.get(key) for key in ("distance_ma10", "distance_ma20", "distance_bull_bear")]
        near_line = any_of(*(compare(abs(v), PARAMETERS["support_distance"], "le") if v is not None else None for v in distances))
        near_and_steady = all_of(near_line, compare(values["low_vs_previous"], 0), compare(values["close_vs_open"], 0))
        checks["support"] = any_of(near_and_steady, values["rising_lows_3d"])
        wave["support_evidence"] = {"near_line_and_steady": near_and_steady, "rising_lows": values["rising_lows_3d"]}
        for key in WAVE_VALUE_FIELDS:
            if key.endswith("_volume_mean"):
                values[key] = (wave.get(key.removesuffix("_mean")) or {}).get("mean")
            else:
                values[key] = wave.get(key)
    values["severe_event"] = events.get("e2")
    values["near_severe_event"] = events.get("e1")
    obs["wave"] = wave
    obs["dip"] = dip_labels(values, obs, market, record["regime"], wave=wave, events=events, day=day, cutoff=cutoff)
    for path, applicable in events.items():
        if applicable:
            obs["dip"][path]["event"] = table["events"][record["symbol"]][path]
    obs["variables"] = {key: {"missing_reason": "必要事实或交易日窗口不完整"} for key, value in values.items() if value is None}
    if wave:
        wave["wave_quality"] = {"required_checks": wave.pop("required_checks")}
    quota = "龙头" if obs["competed_quota_type"] == "leader" else "情绪"
    role = ROLE_NAMES.get(obs["role"], "历史龙头待复核")
    rank = obs["rank_within_theme_role"]
    metric = obs["theme_lead_evidence"].get("metric")
    basis = {"relative_lead_score": "综合领先分", "return_20d_rank": "20日收益排名"}.get(metric)
    if basis is None:
        basis = "5日收益" if obs["role"] == "emotion" else "辨识度"
    result = "获配" if record["selected"] else "名额已满"
    obs["reason"] = f"{obs['primary_theme']['name']} · {role}，按{basis}等排序，{quota}名额分配前第{rank}名，{result}"
    if obs["higher_role_pending"]:
        obs["reason"] += "。更高角色证据待核验"


def apply_observation(inputs, calendar, prior_roles, cancel):
    from app.services.evening_report_audit import annotate_audit
    from app.services.evening_report_wave import compute_wave

    day = inputs.trade_date
    frame = inputs.prices.with_columns(pl.col("date").cast(pl.String)).filter(pl.col("date").is_in(calendar))
    frames = frame.partition_by("symbol", as_dict=True)
    metadata = {r["symbol"]: r for r in inputs.metadata.iter_rows(named=True)}
    halts = defaultdict(set)
    for item in inputs.provenance.get("suspensions", []):
        if item.get("confirmed") is True:
            halts[item["symbol"]].add(str(item["date"]))
    role_state = {s: d for s, d in prior_roles.items() if d in calendar[-5:] and metadata.get(s, {}).get("in_scope") is True}
    facts, waves, records, starts = {}, {}, {}, []
    for symbol in sorted(set(metadata) | {s for s, in frames} | set(halts)):
        cancel()
        bars = frames.get((symbol,))
        rows = {r["date"]: r for r in bars.iter_rows(named=True)} if bars is not None else {}
        facts[symbol] = _member_days(rows, calendar, halts[symbol])
        meta = metadata.get(symbol)
        if meta is None or meta.get("in_scope") is False:
            continue
        values = stock_values(rows, calendar, halts[symbol])
        today = rows.get(day, {})
        normal = False if day in halts[symbol] else meta.get("trading_normal")
        raw_formal = today.get("formal")
        raw_known = None if raw_formal is None or today.get("raw_close") is None else values["raw_close"] is not None
        pause = meta.get("qualification_pause")
        eligibility = judgement({"研究范围": meta.get("in_scope"), "正常交易": normal,
                                 "资格允许观察": not pause if pause is not None else None, "当日正式价格": raw_known})
        role = classify_role(values, role_state.get(symbol))
        if eligibility["value"] is True and role["daily_role"] == "true_leader":
            role["last_true_date"] = role_state[symbol] = day
        records[symbol] = {"symbol": symbol, "name": meta.get("name") or symbol, "regime": meta.get("regime"), "selected": False,
                           "observation": {**role, "values": values, "eligibility": eligibility, "board": meta.get("board"),
                                           "current_price_known": raw_known, "primary_theme": {}, "related_themes": [],
                                           "theme_lead_evidence": {}, "rank": None, "rule_version": VERSION}}
        if meta.get("in_scope") is True:
            listing = parse_listing_date(meta.get("listing_date"))
            wave, start = compute_wave(symbol, rows, calendar, halts[symbol], listing_date=listing.isoformat() if listing else None)
            if eligibility["value"] is True and role["role"] != "other":
                waves[symbol] = wave
            if start:
                starts.append(start)
    themes, ranked = _rank_themes(inputs, calendar, facts)
    for theme in ranked:
        cancel()
        scores = _lead_scores(theme, records)
        reference = {k: theme[k] for k in ("id", "name", "strength")}
        for symbol in theme["members"]:
            if symbol not in records:
                continue
            obs = records[symbol]["observation"]
            if obs["primary_theme"]:
                obs["related_themes"].append(reference)
                continue
            obs["primary_theme"] = reference
            obs["theme_lead_evidence"] = scores.get(symbol, {"score": None, "source": None})
            obs["values"]["relative_lead_score"] = obs["theme_lead_evidence"]["score"]
    rows = list(records.values())
    selected = allocate(rows, ranked)
    for row in rows:
        if row["observation"].get("competed_quota_type"):
            symbol = row["symbol"]
            _label_candidate(row, waves.get(symbol), inputs.market, inputs.provenance.get("severe_events"), day, inputs.cutoff)
    _position_details(inputs, rows, calendar)
    audit = annotate_audit(inputs, rows, themes)
    return {"records": rows, "selected": selected, "themes": ranked, "audit": audit,
            "role_state": role_state, "wave_starts": starts}


def _position_details(inputs, records, calendar):
    from app.indicators.pipeline import _BENCHMARK_PREFERENCE
    from app.indicators.price_gaps import compute_price_gaps
    from app.services.abnormal_moves import rule_for

    selected = {r["symbol"] for r in records if r["selected"] or r["observation"].get("competed_quota_type")}
    price_frame = inputs.prices.with_columns(pl.col("date").cast(pl.String)).filter(
        pl.col("symbol").is_in(selected) & pl.col("date").is_in(calendar))
    groups = {key[0]: group for key, group in price_frame.partition_by("symbol", as_dict=True).items()}
    benchmarks = inputs.abnormal_benchmarks
    indices = {key[0]: {str(r["date"]): r for r in group.to_dicts()}
               for key, group in benchmarks.partition_by("symbol", as_dict=True).items()} if not benchmarks.is_empty() else {}
    halted = defaultdict(set)
    for row in inputs.provenance.get("suspensions", []):
        if row.get("confirmed") is True:
            halted[row["symbol"]].add(str(row["date"]))
    for record in records:
        if record["symbol"] not in selected:
            continue
        obs, symbol = record["observation"], record["symbol"]
        bars = groups.get(symbol)
        values = obs["values"]
        risks = []
        if finite(values.get("distance_bull_bear")) and values["distance_bull_bear"] < -.03:
            risks.append("收盘低于多空线3%")
        obs["risks"] = risks
        present = set(bars["date"].to_list()) if bars is not None else set()
        first_day = min(present, default=None)
        expected = {d for d in calendar if first_day is not None and d >= first_day}
        continuous = bool(present) and expected <= present | halted[symbol]
        gaps_available = continuous and {"high", "low", "volume", "amount"} <= set(bars.columns) and bars.select(
            (pl.col("formal").eq(True) & pl.all_horizontal(pl.col(c).is_finite() & pl.col(c).gt(0)
                                                         for c in ("high", "low"))).fill_null(False).all()).item()
        if gaps_available:
            _, gaps = compute_price_gaps(bars.with_columns(pl.col("date").str.to_date()))
            obs["price_gaps"] = {"status": "known", "as_of": inputs.trade_date, "window_start": min(present), "intervals": gaps}
        else:
            obs["price_gaps"] = {"status": "unknown", "as_of": inputs.trade_date, "intervals": [], "reason": "正式高低价序列或交易日窗口不完整"}
        meta_board = obs.get("board")
        key = "GEM" if meta_board == "chinext" else "SH" if symbol.endswith(".SH") else "SZ" if symbol.endswith(".SZ") else None
        benchmark = next((s for s in _BENCHMARK_PREFERENCE.get(key, []) if s in indices), None)
        windows = {}
        stock = {str(r["date"]): r for r in bars.to_dicts()} if bars is not None else {}
        for n, thresholds in rule_for(symbol, record["name"]).thresholds.items():
            if len(calendar) <= n or benchmark is None:
                continue
            first, last = calendar[-n - 1], calendar[-1]
            endpoints = [stock.get(d, {}) for d in (first, last)] + [indices[benchmark].get(d, {}) for d in (first, last)]
            if not all(r.get("formal") is True and finite(r.get("close"), positive=True) for r in endpoints):
                continue
            a, b, x, y = [r["close"] for r in endpoints]
            deviation = b / a - y / x
            threshold = thresholds[0 if deviation >= 0 else 1]
            windows[str(n)] = {"window": n, "value": deviation, "threshold": threshold, "closeness": abs(deviation) / threshold}
        obs["abnormal_move"] = {"status": "approximate" if windows else "unknown", "benchmark": benchmark,
                                "is_approximate": True, "rule_version": VERSION,
                                "windows": windows, "as_of": inputs.trade_date,
                                "limitation": "近似偏离接近度，不等同官方严重异动事件；事件计数路径未覆盖"}
