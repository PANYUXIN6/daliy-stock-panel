"""观察名单：题材、互斥角色与配额。低吸只产生标签，不参与名额分配。"""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean, median

import polars as pl

from app.services.evening_report_classification import exclusion
from app.services.evening_report_dip import dip_labels
from app.services.evening_report_rules import (
    SELECTION_POLICY_VERSION,
    all_of,
    any_of,
    finite,
    judgement,
)
from app.services.evening_report_wave import PARAMETERS, AnalysisFeatures, compare, ratio

VERSION = SELECTION_POLICY_VERSION
ROLE_NAMES = {"true_leader": "真龙", "fallback_leader": "兜底龙", "emotion": "情绪股", "other": "其他观察"}
WAVE_VALUE_FIELDS = {
    "current_wave_index", "completed_wave_count", "first_impulse_return", "first_wave_pullback", "retrace_pct",
    "provisional_retrace_pct", "first_pullback_active", "pivot_density", "pullback_volume_ratio",
    "impulse_volume_mean", "pullback_volume_mean", "provisional_pullback_volume_mean",
    "start_amount", "start_volume_vs_pullback_ratio", "start_volume_vs_impulse_ratio",
}


def classify_role(values: dict, previous: dict | None = None) -> dict:
    true = all_of(compare(values.get("return_20d"), .50), compare(values.get("identity_score"), 45),
                  compare(values.get("high_proximity"), .85))
    fallback = any_of(compare(values.get("return_20d"), .30), compare(values.get("identity_score"), 30))
    emotion = all_of(compare(values.get("return_5d"), .12), compare(values.get("acceleration_5_20"), .45),
                     compare(values.get("return_20d"), .55, "lt"))
    role = "true_leader" if true is True else "fallback_leader" if fallback is True else "emotion" if emotion is True else "other"
    prior = previous or {}
    pending = prior.get("role") == "true_leader" and true is not True
    return {"role": ("historical_leader" if pending and role in ("emotion", "other") else role), "daily_role": role,
            "role_by_history_tracking": prior.get("role"),
            "role_status": "review_pending" if pending else "qualified",
            "emotion_attribute": emotion,
            "higher_role_pending": (true is None and role != "true_leader") or (fallback is None and role in ("emotion", "other")),
            "conditions": {"true_leader": true, "fallback_leader": fallback, "emotion": emotion},
            "last_true_date": prior.get("last_true_date"),
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
        formal = row.get("wave_formal", row.get("formal")) if key == "raw_close" else row.get("formal")
        return _known_number(row.get(key), positive) if formal is True and day not in halted else None

    def window(key, count, *, offset=0, positive=False):
        days = calendar[len(calendar) - count - offset:len(calendar) - offset] if len(calendar) >= count + offset else []
        result = [point(day, key, positive) for day in days]
        return result if len(result) == count and None not in result else None

    close = point(calendar[-1], "close", True)
    values = {"raw_close": point(calendar[-1], "raw_close", True), "amount_day": point(calendar[-1], "amount"),
              "consecutive_limit_ups": point(calendar[-1], "consecutive_limit_ups")}
    values["change_pct"] = _daily_change(rows, calendar, len(calendar) - 1, halted)
    for count in (3, 5, 10, 20):
        start = point(calendar[-count - 1], "close", True) if len(calendar) > count else None
        values[f"return_{count}d"] = close / start - 1 if close is not None and start else None
    flags = [False if day in halted else rows.get(day, {}).get("signal_limit_up")
             if rows.get(day, {}).get("formal") is True else None for day in calendar[-20:]]
    values["limit_up_count_20d"] = sum(flags) if len(flags) == 20 and None not in flags else None
    count, consecutive = values["limit_up_count_20d"], values["consecutive_limit_ups"]
    values["identity_score"] = min(100, count * 18 + consecutive * 14) if count is not None and consecutive is not None else None
    r20, r5 = values["return_20d"], values["return_5d"]
    values["space_score"] = min(100, r20 / .8 * 100) if r20 is not None else None
    values["acceleration_5_20"] = r5 / r20 if r20 is not None and r20 > 0 and r5 is not None else None
    highs = window("high", 60, positive=True)
    values["high_proximity"] = close / max(highs) if highs and close else None
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
    values["severe_event"] = values["near_severe_event"] = None
    today = calendar[-1]
    values["atr14_pct"] = ratio(point(today, "atr_14"), close)
    opening = point(today, "open", True)
    values["close_vs_open"] = close / opening - 1 if close and opening else None
    previous_close = point(calendar[-2], "close", True) if len(calendar) > 1 else None
    previous_ratio = ratio(close, previous_close)
    values["close_vs_previous_non_decreasing"] = compare(previous_ratio, 1)
    for offset, key in ((1, "low_vs_previous"), (2, "low_vs_two_days_before")):
        low = point(today, "low", True)
        previous = point(calendar[-offset - 1], "low", True) if len(calendar) > offset else None
        values[key] = low / previous - 1 if low and previous else None
    previous_pair = window("low", 2, offset=1, positive=True)
    values["previous_low_vs_two_days_before"] = previous_pair[1] / previous_pair[0] - 1 if previous_pair else None
    return values


def variable_evidence(values: dict, inputs, wave=None) -> dict:
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
    }
    result = {}
    for key, value in values.items():
        unit = "元" if key.startswith("amount") or key in ("raw_close", "ma5", "ma10", "ma20", "bull_bear_line", "start_amount") else "分" if key.endswith("score") else "次" if key in ("limit_up_count_20d", "consecutive_limit_ups", "current_wave_index", "completed_wave_count") else "布尔" if key in ("rising_lows_3d", "first_pullback_active", "close_vs_previous_non_decreasing", "severe_event", "near_severe_event") else "标准化成交量" if key.endswith("_volume_mean") else "小数"
        formula = formulas.get(key, f"C(D)/C(D−{key[7:-1]})−1" if key.startswith("return_") else "C(D)/对应均线−1" if key.startswith("distance_") else "当日标准化事实" if value is not None else "定义待核验或必要证据缺失")
        result[key] = {"value": value, "unit": unit, "formula": formula, "window": key,
                       "price_basis": "原始价格识别" if key in ("raw_close", "limit_up_count_20d", "consecutive_limit_ups") else "同版本前复权" if unit not in ("元", "次") or key in ("ma5", "ma10", "ma20", "bull_bear_line") else "人民币元",
                       "scope": "主题材完整合格比较组（收益基准逐股剔自身）" if key == "relative_lead_score" else "个股/市场交易日", "as_of": inputs.trade_date,
                       "data_version": inputs.provenance.get("input_versions", {}).get("stock_generation"),
                       "rule_version": VERSION, "missing_reason": "必要窗口、正式价格或事件定义不完整" if value is None else None,
                       "bucket_label_at_time": None, "bucket_rule_version": None}
        if key in WAVE_VALUE_FIELDS:
            start = (wave or {}).get("second_wave_start") or {}
            result[key].update(rule_version="wave-rule-v1", known_at=inputs.cutoff)
            if key == "start_amount" or key.startswith("start_volume_"):
                result[key].update(as_of=start.get("evidence_date"), known_at=start.get("known_at"))
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
        for tier in (0, 1):
            if tiers[tier]:
                _choose_lead_metric(tiers[tier], [theme.get("as_of"), theme["id"], tiers[tier][0]["observation"]["daily_role"]])
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


def _apply_wave_evidence(values, wave):
    if not wave:
        return
    checks = wave.pop("required_checks")
    distances = [values.get(key) for key in ("distance_ma10", "distance_ma20", "distance_bull_bear")]
    near_line = any_of(*(compare(abs(value), PARAMETERS["support_distance"], "le") if value is not None else None
                         for value in distances))
    steady = all_of(compare(values.get("low_vs_previous"), 0), compare(values.get("close_vs_open"), 0))
    near_and_steady = all_of(near_line, steady)
    rising = values["rising_lows_3d"]
    checks["support"] = any_of(near_and_steady, rising)
    for key in ("current_wave_index", "completed_wave_count", "first_impulse_return", "first_wave_pullback",
                "retrace_pct", "provisional_retrace_pct", "first_pullback_active", "pullback_volume_ratio"):
        values[key] = wave.pop(key)
    for key in ("impulse_volume", "pullback_volume", "provisional_pullback_volume"):
        summary = wave.get(key)
        values[key + "_mean"] = summary["mean"] if summary is not None else None
        if summary is not None:
            wave[key] = {name: value for name, value in summary.items() if name != "mean"}
    values["pivot_density"] = wave["pivot_density"].pop("value")
    start = wave.get("second_wave_start") or {}
    values["start_amount"] = start.get("start_amount")
    for key in ("pullback", "impulse"):
        values[f"start_volume_vs_{key}_ratio"] = start.get("volume", {}).get("start_vs_" + key)
    wave["wave_quality"] = {
        "required_checks": checks,
        "analysis_features": AnalysisFeatures(
            variable_refs=["pivot_density", "atr14_pct", "close_vs_open", "close_vs_previous_non_decreasing",
                           "low_vs_previous", "low_vs_two_days_before", "previous_low_vs_two_days_before",
                           "start_amount", "start_volume_vs_pullback_ratio", "start_volume_vs_impulse_ratio"],
            start_amount_as_of=start.get("evidence_date")),
    }
    wave["support_evidence"] = {"near_line_and_steady": near_and_steady, "rising_lows": rising,
                                "variable_refs": ["distance_ma10", "distance_ma20", "distance_bull_bear", "low_vs_previous",
                                                  "close_vs_open", "rising_lows_3d"]}


def apply_observation(inputs, records: list[dict], sectors: list[dict], previous: dict, calendar: list[str], cancel,
                      waves=None, wave_starts=()) -> dict:
    """Retain legacy confirmations as evidence; replace all legacy selection gates."""
    frame = inputs.prices.with_columns(pl.col("date").cast(pl.String)).filter(pl.col("date") <= inputs.trade_date)
    halts = defaultdict(set)
    for item in inputs.provenance.get("suspensions", []):
        if item.get("confirmed") is True:
            halts[item["symbol"]].add(str(item["date"]))
    # A long suspension may put the previous traded close outside the 60-day indicator window.
    first = max(0, len(calendar) - 5)
    dates = set(calendar[-60:])
    for halted in halts.values():
        preceding = _previous_trading_day(calendar, first, halted)
        if preceding:
            dates.add(preceding)
    frame = frame.filter(pl.col("date").is_in(sorted(dates)))
    by_symbol = {key[0]: {r["date"]: r for r in group.to_dicts()} for key, group in frame.partition_by("symbol", as_dict=True).items()}
    metadata = {r["symbol"]: r for r in inputs.metadata.to_dicts()}
    themes = []
    members = inputs.memberships.filter(pl.col("source") == "concept").unique(subset=["sector_id", "symbol"])
    sector_lookup = {s["id"]: s for s in sectors}
    for (sector_id,), group in members.partition_by("sector_id", as_dict=True).items():
        cancel()
        symbols = sorted(group["symbol"].to_list())
        label = group["sector_name"][0]
        blocked = exclusion(sector_id) or exclusion(label)
        daily, missing = [], []
        for index in range(first, len(calendar)):
            day = calendar[index]
            up, strong, boards = 0, 0, []
            boards_known = True
            for symbol in symbols:
                if day in halts[symbol]:
                    continue
                rows = by_symbol.get(symbol, {})
                row = rows.get(day, {})
                if row.get("formal") is not True or row.get("signal_limit_up") is None:
                    boards_known = False
                    missing.append({"symbol": symbol, "date": day, "reason": "涨停事实缺失"})
                    continue
                if row["signal_limit_up"]:
                    up += 1
                    if finite(row.get("consecutive_limit_ups")):
                        boards.append(row["consecutive_limit_ups"])
                    else:
                        boards_known = False
                else:
                    change = _daily_change(rows, calendar, index, halts[symbol])
                    if change is None:
                        missing.append({"symbol": symbol, "date": day, "reason": "日涨幅端点缺失"})
                    else:
                        strong += change >= .07 - 1e-12
            daily.append({"date": day, "limit_up": up, "strong": strong,
                          "max_consecutive": max(boards, default=0) if boards_known else None})
        complete = len(daily) == 5 and not missing
        strength = sum(2 ** (-k / 2.5) * (row["limit_up"] + .5 * row["strong"]) for k, row in enumerate(reversed(daily))) if complete else None
        theme = {"id": "concept:" + sector_id, "name": label, "members": symbols, "member_count": len(symbols),
                 "strength": strength, "complete": complete, "daily": daily, "missing": missing,
                 "exclusion": blocked[1] if blocked else "完整成员超过120只" if len(symbols) > 120 else None,
                 "as_of": inputs.trade_date, "members_as_of": inputs.cutoff or inputs.trade_date, "selected_theme": False}
        themes.append(theme)
        if theme["id"] in sector_lookup:
            source = sector_lookup[theme["id"]]
            theme["lead_source_members"] = source["members"]
            source["observation"] = theme
    eligible_themes = [s for s in themes if s["exclusion"] is None and s["strength"] is not None and s["strength"] > 0]
    # A secondary tie-break is comparable only when every theme in that tie has the fact.
    missing_height = {(s["strength"], s["daily"][-1]["limit_up"]) for s in eligible_themes
                      if s["daily"][-1]["max_consecutive"] is None}

    def theme_order(theme):
        today = theme["daily"][-1]
        strength, limit_ups = theme["strength"], today["limit_up"]
        height = 0 if (strength, limit_ups) in missing_height else today["max_consecutive"]
        return -strength, -limit_ups, -height, theme["id"]

    ranked = sorted(eligible_themes, key=theme_order)
    for rank, theme in enumerate(ranked, 1):
        theme["rank"] = rank
    ranked = ranked[:5]
    for theme in ranked:
        theme["selected_theme"] = True
    role_state = {symbol: role for symbol, role in previous.get("observation_roles", {}).items()
                  if metadata.get(symbol, {}).get("in_scope") is not False}
    counts = Counter()
    waves = waves or {}
    for record in records:
        cancel()
        symbol = record["symbol"]
        meta = metadata.get(symbol, {})
        values = stock_values(by_symbol.get(symbol, {}), calendar, halts[symbol])
        wave = waves.get(symbol)
        if not wave:
            values.update(dict.fromkeys(sorted(WAVE_VALUE_FIELDS)))
        dip_wave = ({**wave, "required_checks": wave["required_checks"]} if wave else None)
        _apply_wave_evidence(values, wave)
        if wave:
            dip_wave["required_checks"] = wave["wave_quality"]["required_checks"]
        elif inputs.wave_factors is not None:
            reason = "suspended" if inputs.trade_date in halts[symbol] else "missing_rows"
            dip_wave = {"first_pullback_eligible": {"value": False if reason == "suspended" else None,
                                                   "reason_codes": [reason]}}
        role = classify_role(values, role_state.get(symbol))
        if role["conditions"]["true_leader"] is True:
            role["last_true_date"] = inputs.trade_date
        if role["conditions"]["true_leader"] is True or role["role_status"] == "review_pending":
            role_state[symbol] = {"role": "true_leader", "last_true_date": role["last_true_date"]}
        related = [t for t in ranked if symbol in t["members"]]
        primary = related[0] if related else None
        sector = sector_lookup.get(primary["id"], {}) if primary else {}
        comparison = sector.get("scores", {}).get(symbol, {})
        lead = {"score": comparison.get("L"),
                "components": {key: comparison.get(key) for key in (
                    "X20", "X5", "_p20", "_p5", "lead_proximity", "lead_proximity_score")},
                "source": {"sector_id": sector["id"], "sample_count": comparison["rank_n"],
                           "windows": [20, 5, 60], "benchmark": "equal_weight_excluding_self",
                           "as_of": inputs.trade_date, "data_version": inputs.provenance.get("input_versions", {}).get("stock_generation"),
                           "rule_version": VERSION} if comparison else None}
        values["relative_lead_score"] = comparison.get("L")
        today = by_symbol.get(symbol, {}).get(inputs.trade_date, {})
        raw_formal = today.get("wave_formal", today.get("formal"))
        eligibility = judgement({"研究范围": meta.get("in_scope"), "正常交易": meta.get("trading_normal"),
                                 "当日正式价格": raw_formal is True and finite(today.get("raw_close"), positive=True),
                                 "资格允许观察": not meta["qualification_pause"] if meta.get("qualification_pause") is not None else None})
        observation = {**role, "board": meta.get("board"), "values": values, "variables": variable_evidence(values, inputs, wave), "wave": wave,
                       "current_price_known": None if raw_formal is None or today.get("raw_close") is None else eligibility["inputs"]["当日正式价格"],
                       "primary_theme": {k: primary[k] for k in ("id", "name", "strength")} if primary else {},
                       "theme_lead_evidence": lead,
                       "related_themes": [{k: t[k] for k in ("id", "name", "strength")} for t in related[1:]],
                       "eligibility": eligibility, "dip": dip_labels(
                           values, role, inputs.market, meta.get("regime"), wave=dip_wave,
                           events=inputs.provenance.get("severe_event_paths", {}).get(symbol),
                           day=inputs.trade_date, cutoff=inputs.cutoff),
                       "reason": f"符合{ROLE_NAMES[role['daily_role']]}当日条件"
                       if role["daily_role"] == role["role"] and role["role"] != "other"
                       else role["status_reason"] or "尚无完整角色条件",
                       "rank": None, "rule_version": VERSION}
        if observation["higher_role_pending"]:
            observation["reason"] += "；更高角色证据待核验"
        record["observation"] = observation
        record["selected"] = False
        for path in record["paths"]:
            path.pop("selected_path", None)
        counts[role["role"]] += 1
    selected = allocate(records, ranked)
    for record in records:
        if record["selected"]:
            primary = record["observation"]["primary_theme"]["id"]
            for path in record["paths"]:
                path["selected_path"] = f"{path['source']}:{path['sector_id']}" == primary
    _position_details(inputs, records, calendar)
    from app.services.evening_report_audit import annotate_audit

    audit = annotate_audit(inputs, records, themes)
    new_starts = {event["security_id"] for event in wave_starts
                  if event["segment_index"] == 2 and event["known_at"][:10] == inputs.trade_date}
    pool = [row for row in records if row["observation"]["decision"]["comparison_eligible"]]
    evaluable = [row for row in pool if row["symbol"] in new_starts
                 or (row["observation"]["wave"] or {}).get("start_evaluable")]
    starts = sum(row["symbol"] in new_starts for row in evaluable)
    audit["start_concentration"] = {
        "segment_index": 2, "pool_count": len(pool), "start_evaluable_count": len(evaluable),
        "start_unknown_count": len(pool) - len(evaluable), "new_start_count": starts,
        "known_new_starts_per_pool": starts / len(pool) if pool else None,
        "known_new_starts_per_evaluable": starts / len(evaluable) if evaluable else None,
    }
    return {"selected": selected, "themes": ranked, "role_state": role_state, "role_counts": dict(counts),
            "audit": audit,
            "theme_missing_count": sum(not t["complete"] for t in themes),
            "zero_reasons": [] if selected else ["没有完整且有强势贡献的5日题材" if not ranked else "入选题材中没有满足角色及交易资格的可分配个股"]}


def _position_details(inputs, records, calendar):
    from app.indicators.pipeline import _BENCHMARK_PREFERENCE
    from app.indicators.price_gaps import compute_price_gaps
    from app.services.abnormal_moves import rule_for

    selected = {r["symbol"] for r in records if r["selected"] or r["observation"].get("competed_quota_type")}
    price_frame = inputs.prices.with_columns(pl.col("date").cast(pl.String)).filter(
        pl.col("symbol").is_in(selected) & (pl.col("date") <= inputs.trade_date))
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
        risks = [name for name, value in record["risk"]["judgement"]["inputs"].items() if value is True]
        if record["position"].get("overheated") is True:
            risks.append("价格位置过热")
        if record["position"].get("one_price_limit_up") is True:
            risks.append("当日一字涨停，存在成交限制")
        if finite(values.get("distance_bull_bear")) and values["distance_bull_bear"] < -.03:
            risks.append("收盘低于多空线3%")
        obs["risks"] = risks
        present = set(bars["date"].to_list()) if bars is not None else set()
        expected = {d for d in calendar if present and d >= min(present)}
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
