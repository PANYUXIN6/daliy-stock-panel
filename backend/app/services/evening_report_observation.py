"""观察名单：题材、互斥角色与配额。低吸只产生标签，不参与名额分配。"""
from __future__ import annotations

from collections import Counter, defaultdict
from math import isclose
from statistics import mean, median

import polars as pl

from app.services.evening_report_classification import exclusion
from app.services.evening_report_rules import all_of, any_of, finite, judgement

VERSION = "theme-observation-v1"
ROLE_NAMES = {"true_leader": "真龙", "fallback_leader": "兜底龙", "emotion": "情绪股", "other": "其他观察"}


def compare(value, threshold, op="ge"):
    """Ratios from prices can land one ulp across a specified open/closed boundary."""
    if not finite(value):
        return None
    if isclose(value, threshold, rel_tol=0, abs_tol=1e-12):
        return op in ("ge", "le")
    return {"ge": value >= threshold, "gt": value > threshold,
            "le": value <= threshold, "lt": value < threshold}[op]


def classify_role(values: dict, previous: dict | None = None) -> dict:
    true = all_of(compare(values.get("return_20d"), .50), compare(values.get("identity_score"), 45),
                  compare(values.get("high_proximity"), .85))
    fallback = any_of(compare(values.get("return_20d"), .30), compare(values.get("identity_score"), 30))
    emotion = all_of(compare(values.get("return_5d"), .12), compare(values.get("acceleration_5_20"), .45),
                     compare(values.get("return_20d"), .55, "lt"))
    role = "true_leader" if true is True else "fallback_leader" if fallback is True else "emotion" if emotion is True else "other"
    prior = previous or {}
    pending = prior.get("role") == "true_leader" and true is not True
    return {"role": "true_leader" if pending else role, "daily_role": role,
            "role_by_history_tracking": prior.get("role"),
            "role_status": "review_pending" if pending else "qualified",
            "emotion_attribute": emotion,
            "higher_role_pending": (true is None and role != "true_leader") or (fallback is None and role in ("emotion", "other")),
            "conditions": {"true_leader": true, "fallback_leader": fallback, "emotion": emotion},
            "last_true_date": prior.get("last_true_date"),
            "status_reason": "历史真龙跟踪待复核，结构失效规则尚待核验" if pending else None}


def dip_labels(values: dict, role: dict, market: dict, regime: str | None) -> dict:
    classification = (market.get("sentiment") or {}).get("classification") or market.get("classification") or {}
    state = classification.get("sentiment_state")
    phase = market.get("phase") or classification.get("phase")
    ge2 = market.get("ge2_count")
    if ge2 is None:
        ge2 = classification.get("evidence", {}).get("ge2_count")
    environment = False if phase == "ebb" or state == "cold" else None if phase in (None, "unknown") or state in (None, "unknown") else True
    leader = all_of(compare(values.get("space_score"), 50), compare(values.get("identity_score"), 45), compare(values.get("high_proximity"), .85))
    emotion = all_of(role.get("emotion_attribute"), compare(values.get("identity_score"), 60), compare(values.get("return_20d"), .20))
    amount_threshold = {"10%": 500_000_000, "20%": 800_000_000}.get(regime)
    gates = judgement({"环境允许低吸": environment, "龙头或情绪型低吸资格": any_of(leader, emotion),
                       "当日成交额达标": compare(values.get("amount_day"), amount_threshold) if amount_threshold else None,
                       "存在二板及以上个股": compare(ge2, 0, "gt"), "首波首次回调": None})
    e1 = judgement({"3日涨幅≤15%": compare(values.get("return_3d"), .15, "le"),
                    "缩量比≤0.65": compare(values.get("volume_ratio_2_10"), .65, "le"),
                    "距MA10≥−2%": compare(values.get("distance_ma10"), -.02),
                    "距MA10≤6%": compare(values.get("distance_ma10"), .06, "le"),
                    "振幅比≤1.2": compare(values.get("amplitude_ratio_2_10"), 1.2, "le")})
    distance = values.get("distance_bull_bear")
    e2 = judgement({"缩量比≤0.55": compare(values.get("volume_ratio_2_10"), .55, "le"),
                    "连续三日低点抬高": values.get("rising_lows_3d"),
                    "距多空线绝对值≤5%": compare(abs(distance), .05, "le") if finite(distance) else None})
    reasons = [*gates["failed_reasons"], *gates["unknown_reasons"]]
    reasons.append("严重异动事件及即将严重异动前置待核验")
    status = "environment_only" if environment is False or ge2 == 0 else "not_met" if gates["value"] is False or (e1["value"] is False and e2["value"] is False) else "pending"
    if e1["value"] is True or e2["value"] is True:
        reasons.append("数值形态符合，前置待核验")
    return {"status": status, "reasons": reasons, "gates": gates, "e1": e1, "e2": e2,
            "leader_qualification": leader, "emotion_qualification": emotion,
            "amount_threshold": amount_threshold, "wave_status": "pending", "event_status": "pending"}


def _known_number(value, positive=False):
    return float(value) if finite(value, positive=positive) else None


def stock_values(rows: dict[str, dict], calendar: list[str], halted: set[str]) -> dict:
    """Market-date windows; no skipping suspended/missing endpoints or incomplete windows."""
    def point(day, key, positive=False):
        row = rows.get(day, {})
        return _known_number(row.get(key), positive) if row.get("formal") is True and day not in halted else None

    def window(key, count, *, offset=0, positive=False):
        days = calendar[len(calendar) - count - offset:len(calendar) - offset] if len(calendar) >= count + offset else []
        result = [point(day, key, positive) for day in days]
        return result if len(result) == count and None not in result else None

    close = point(calendar[-1], "close", True)
    values = {"raw_close": point(calendar[-1], "raw_close", True), "amount_day": point(calendar[-1], "amount"),
              "consecutive_limit_ups": point(calendar[-1], "consecutive_limit_ups")}
    for count in (1, 3, 5, 10, 20):
        start = point(calendar[-count - 1], "close", True) if len(calendar) > count else None
        values["change_pct" if count == 1 else f"return_{count}d"] = close / start - 1 if close is not None and start else None
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
    for key in ("ma5", "ma10", "bull_bear_line"):
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
    for key in ("wave_index", "first_pullback", "first_wave_pullback", "severe_event", "near_severe_event"):
        values[key] = None
    return values


def variable_evidence(values: dict, inputs) -> dict:
    formulas = {
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
        unit = "元" if key.startswith("amount") or key in ("raw_close", "ma5", "ma10", "bull_bear_line") else "分" if key.endswith("score") else "次" if key in ("limit_up_count_20d", "consecutive_limit_ups", "wave_index") else "布尔" if key in ("rising_lows_3d", "first_pullback", "first_wave_pullback", "severe_event", "near_severe_event") else "小数"
        formula = formulas.get(key, f"C(D)/C(D−{key[7:-1]})−1" if key.startswith("return_") else "C(D)/对应均线−1" if key.startswith("distance_") else "当日标准化事实" if value is not None else "定义待核验或必要证据缺失")
        result[key] = {"value": value, "unit": unit, "formula": formula, "window": key,
                       "price_basis": "原始价格识别" if key in ("raw_close", "limit_up_count_20d", "consecutive_limit_ups") else "同版本前复权" if unit not in ("元", "次") or key in ("ma5", "ma10", "bull_bear_line") else "人民币元",
                       "scope": "主题材完整同制度合格比较组（旧版领涨证据）" if key == "relative_lead_score" else "个股/市场交易日", "as_of": inputs.trade_date,
                       "data_version": inputs.provenance.get("input_versions", {}).get("stock_generation"),
                       "rule_version": VERSION, "missing_reason": "必要窗口、正式价格或事件定义不完整" if value is None else None,
                       "bucket_label_at_time": None, "bucket_rule_version": None}
    return result


def _sort_candidate(record: dict) -> tuple:
    observation = record["observation"]
    values = observation["values"]
    role = observation["role"]
    keys = ("acceleration_5_20", "return_5d", "identity_score") if role == "emotion" else ("identity_score", "return_20d", "high_proximity")
    numbers = tuple((values.get(k) is None, -values[k] if values.get(k) is not None else 0) for k in keys)
    return ({"true_leader": 0, "fallback_leader": 1, "emotion": 2, "other": 3}[role],
            observation["role_status"] == "review_pending", *numbers, observation.get("board") != "chinext", record["symbol"])


def allocate(records: list[dict], themes: list[dict]) -> list[str]:
    """Primary membership is assigned before quota; no personal state is an input."""
    selected = []
    competing = []
    for theme in themes:
        candidates = sorted([r for r in records if r["observation"].get("primary_theme", {}).get("id") == theme["id"]
                             and r["observation"]["eligibility"]["value"] is True], key=_sort_candidate)
        groups = {"leader": [], "emotion": []}
        for record in candidates:
            obs = record["observation"]
            if obs["role"] == "other":
                continue
            quota = "emotion" if obs["role"] == "emotion" else "leader"
            obs["quota_eligibility"] = quota != "emotion" or obs.get("board") == "chinext"
            if obs["quota_eligibility"]:
                groups[quota].append(record)
        for quota, group in groups.items():
            for rank, record in enumerate(group, 1):
                record["observation"]["rank_within_theme_role"] = rank
                record["observation"]["competed_quota_type"] = quota
                competing.append(record)
        for record in groups["leader"][:2] + groups["emotion"][:1]:
            selected.append(record["symbol"])
            record["selected"] = True
            record["observation"]["rank"] = len(selected)
    for quota in ("leader", "emotion"):
        group = sorted([r for r in competing if r["observation"]["competed_quota_type"] == quota], key=_sort_candidate)
        for rank, record in enumerate(group, 1):
            record["observation"]["rank_global_role"] = rank
    return selected


def apply_observation(inputs, records: list[dict], sectors: list[dict], previous: dict, calendar: list[str], cancel) -> dict:
    """Retain legacy confirmations as evidence; replace all legacy selection gates."""
    frame = inputs.prices.with_columns(pl.col("date").cast(pl.String)).filter(pl.col("date") <= inputs.trade_date)
    # Only bounded daily windows cross into Python; the full series remains in Polars.
    frame = frame.filter(pl.col("date").is_in(calendar[-60:]))
    by_symbol = {key[0]: {r["date"]: r for r in group.to_dicts()} for key, group in frame.partition_by("symbol", as_dict=True).items()}
    metadata = {r["symbol"]: r for r in inputs.metadata.to_dicts()}
    halts = defaultdict(set)
    for item in inputs.provenance.get("suspensions", []):
        if item.get("confirmed") is True:
            halts[item["symbol"]].add(str(item["date"]))
    themes = []
    members = inputs.memberships.filter(pl.col("source") == "concept").unique(subset=["sector_id", "symbol"])
    sector_lookup = {s["id"]: s for s in sectors}
    for (sector_id,), group in members.partition_by("sector_id", as_dict=True).items():
        cancel()
        symbols = sorted(group["symbol"].to_list())
        label = group["sector_name"][0]
        blocked = exclusion(sector_id) or exclusion(label)
        daily, missing = [], []
        for day in calendar[-5:]:
            index = calendar.index(day)
            up, strong, boards = 0, 0, []
            for symbol in symbols:
                if day in halts[symbol]:
                    continue
                row = by_symbol.get(symbol, {}).get(day, {})
                before = by_symbol.get(symbol, {}).get(calendar[index - 1], {}) if index else {}
                if row.get("formal") is not True or row.get("signal_limit_up") is None:
                    missing.append({"symbol": symbol, "date": day, "reason": "涨停事实缺失"})
                    continue
                if row["signal_limit_up"]:
                    up += 1
                elif before.get("formal") is True and finite(before.get("close"), positive=True) and finite(row.get("close"), positive=True):
                    strong += row["close"] / before["close"] - 1 >= .07 - 1e-12
                else:
                    missing.append({"symbol": symbol, "date": day, "reason": "日涨幅端点缺失"})
                if finite(row.get("consecutive_limit_ups")):
                    boards.append(row["consecutive_limit_ups"])
            daily.append({"date": day, "limit_up": up, "strong": strong, "max_consecutive": max(boards) if boards else None})
        complete = len(daily) == 5 and not missing
        strength = sum(2 ** (-k / 2.5) * (row["limit_up"] + .5 * row["strong"]) for k, row in enumerate(reversed(daily))) if complete else None
        theme = {"id": "concept:" + sector_id, "name": label, "members": symbols, "member_count": len(symbols),
                 "strength": strength, "complete": complete, "daily": daily, "missing": missing,
                 "exclusion": blocked[1] if blocked else "完整成员超过120只" if len(symbols) > 120 else None,
                 "members_as_of": inputs.cutoff or inputs.trade_date, "selected_theme": False}
        themes.append(theme)
        if theme["id"] in sector_lookup:
            sector_lookup[theme["id"]]["observation"] = theme
    ranked = sorted([s for s in themes if s["exclusion"] is None and s["strength"] is not None and s["strength"] > 0],
                    key=lambda s: (-s["strength"], -s["daily"][-1]["limit_up"], s["daily"][-1]["max_consecutive"] is None,
                                   -(s["daily"][-1]["max_consecutive"] or 0), s["id"]))
    for rank, theme in enumerate(ranked, 1):
        theme["rank"] = rank
    ranked = ranked[:5]
    for theme in ranked:
        theme["selected_theme"] = True
    role_state = {symbol: role for symbol, role in previous.get("observation_roles", {}).items()
                  if metadata.get(symbol, {}).get("in_scope") is not False}
    counts = Counter()
    for record in records:
        cancel()
        symbol = record["symbol"]
        meta = metadata.get(symbol, {})
        values = stock_values(by_symbol.get(symbol, {}), calendar, halts[symbol])
        role = classify_role(values, role_state.get(symbol))
        if role["conditions"]["true_leader"] is True:
            role["last_true_date"] = inputs.trade_date
        if role["role"] == "true_leader":
            role_state[symbol] = {"role": role["role"], "last_true_date": role["last_true_date"]}
        related = [t for t in ranked if symbol in t["members"]]
        primary = related[0] if related else None
        comparison = sector_lookup.get(primary["id"], {}).get("scores", {}).get(symbol, {}) if primary else {}
        values["relative_lead_score"] = comparison.get("L")
        today = by_symbol.get(symbol, {}).get(inputs.trade_date, {})
        eligibility = judgement({"研究范围": meta.get("in_scope"), "正常交易": meta.get("trading_normal"),
                                 "当日正式价格": today.get("formal") is True and finite(today.get("raw_close"), positive=True),
                                 "资格允许观察": not meta["qualification_pause"] if meta.get("qualification_pause") is not None else None})
        observation = {**role, "board": meta.get("board"), "values": values, "variables": variable_evidence(values, inputs),
                       "current_price_known": None if today.get("formal") is None or today.get("raw_close") is None else eligibility["inputs"]["当日正式价格"],
                       "primary_theme": {k: primary[k] for k in ("id", "name", "strength")} if primary else {},
                       "related_themes": [{k: t[k] for k in ("id", "name", "strength")} for t in related[1:]],
                       "eligibility": eligibility, "dip": dip_labels(values, role, inputs.market, meta.get("regime")),
                       "reason": role["status_reason"] or f"符合{ROLE_NAMES[role['daily_role']]}当日条件" if role["role"] != "other" else "尚无完整角色条件",
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
