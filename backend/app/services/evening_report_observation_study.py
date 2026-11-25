"""用定稿候选池对照5日涨幅基线，保留同日同窗口的价格路径。"""
from __future__ import annotations

from collections import defaultdict
from itertools import pairwise
from math import prod
from statistics import mean

from app.services.evening_report_observation import _competition_tier
from app.services.evening_report_rules import finite
from app.services.evening_report_start_study import published_on_trade_date
from app.services.evening_report_tracking import (
    evaluate_performance,
    evaluation_data_version,
    evaluation_prices,
)

VERSION = "observation-close-path-v1"


def baseline_symbols(records):
    groups = defaultdict(list)
    for row in records:
        obs = row.get("observation") or {}
        decision = obs.get("decision") or {}
        if decision.get("comparison_eligible"):
            groups[(decision["final_theme"], decision["competed_quota_type"])].append(row)

    def order(row):
        obs = row["observation"]
        value = obs["variables"]["return_5d"]["value"]
        return _competition_tier(obs), value is None, -value if value is not None else 0, row["symbol"]

    return {row["symbol"] for (_, quota), group in groups.items()
            for row in sorted(group, key=order)[:(1 if quota == "emotion" else 2)]}


def _peer_return(peers, dates, prices):
    if len(peers) < 5:
        return None
    daily = []
    for before, day in pairwise(dates):
        paths = [(prices.get((peer, before), {}), prices.get((peer, day), {})) for peer in peers]
        if not all(row.get("formal") and finite(row.get("close"), positive=True)
                   for pair in paths for row in pair):
            return None
        daily.append(mean(end["close"] / start["close"] for start, end in paths))
    return prod(daily) - 1


def compare_observations(store, inputs):
    """Formal frozen records only; no missing historical feature is reconstructed from today's membership."""
    days = inputs.provenance.get("calendar", {}).get("days", inputs.calendar)
    symbols, price_days = set(), set()
    cases, skipped = [], []
    for entry in reversed(store.dates()):
        day = entry["date"]
        if day > inputs.trade_date or day not in days:
            continue
        report = store.get(day)
        if not report or not published_on_trade_date(report):
            continue
        if report.get("selection_policy_version") != "theme-observation-v2":
            skipped.append({"date": day, "version": report["version"], "reason": "当日缺少可核验的v2分配前证据"})
            continue
        baseline = baseline_symbols(report["records"])
        selected = set(report["selected"])
        records = [row for row in report["records"] if row["symbol"] in baseline | selected]
        groups = {sector["id"]: sector.get("observation", {}).get("lead_source_members", [])
                  for sector in report["sectors"]}
        symbols.update(baseline | selected)
        for group in groups.values():
            symbols.update(group)
        window = days[days.index(day):days.index(day) + 6]
        price_days.update(window)
        cases.append((report, records, selected, baseline, groups, window))

    prices = evaluation_prices(inputs.prices, symbols, price_days)
    benchmark = {str(row["date"]): row["close"] for row in inputs.benchmark.iter_rows(named=True)
                 if row.get("formal") is True and finite(row.get("close"), positive=True)}
    suspensions = {(row["symbol"], str(row["date"])) for row in inputs.provenance.get("suspensions", [])
                   if row.get("confirmed") is True}
    version = evaluation_data_version(inputs)
    comparisons = []
    phases = defaultdict(list)
    for report, records, selected, baseline, groups, window in cases:
        day = report["trade_date"]
        observations = []
        for row in records:
            symbol = row["symbol"]
            obs = row["observation"]
            evaluation = evaluate_performance(symbol, day, calendar=days, as_of=inputs.trade_date,
                                              prices=prices, suspensions=suspensions, data_version=version,
                                              evaluation_context=inputs.provenance.get("evaluation_context"))
            peers = [peer for peer in groups.get(obs["decision"]["final_theme"], []) if peer != symbol]
            for n, result in evaluation.items():
                target = result["target_date"]
                market_return = benchmark[target] / benchmark[day] - 1 if target in benchmark and day in benchmark else None
                peer_return = _peer_return(peers, window[:int(n) + 1], prices) if target and target <= inputs.trade_date else None
                stock_return = result["return"]
                result["market_relative_return"] = ((1 + stock_return) / (1 + market_return) - 1
                                                    if stock_return is not None and market_return is not None else None)
                result["theme_relative_return"] = ((1 + stock_return) / (1 + peer_return) - 1
                                                   if stock_return is not None and peer_return is not None else None)
                result["theme_peer_count"] = len(peers)
            observation = {"symbol": symbol, "selected": symbol in selected, "baseline": symbol in baseline,
                           "performance": evaluation}
            observations.append(observation)
            if symbol in selected:
                wave = obs.get("wave") or {}
                phase = wave.get("pullback_phase_at_time") or wave.get("data_status") or "未记录"
                reasons = "+".join(wave.get("window_close_reason_codes", []))
                group = f"{phase}:{reasons}" if reasons else phase
                phases[group].append({"date": day, "report_version": report["version"],
                                      "symbol": symbol, "wave_data_status": wave.get("data_status"),
                                      "evaluable_windows": [n for n, row in evaluation.items() if row["status"] == "evaluated"]})
        comparisons.append({"date": day, "report_version": report["version"], "selected": len(selected),
                            "baseline_selected": len(baseline), "overlap": len(selected & baseline),
                            "evaluable": {n: {field: sum(item[field] and item["performance"][n]["status"] == "evaluated"
                                                         for item in observations)
                                              for field in ("selected", "baseline")} for n in ("1", "3", "5")},
                            "observations": observations})
    return {"rule_version": VERSION, "as_of": inputs.trade_date, "data_version": version,
            "baseline_sort": "return_5d_desc_then_symbol_within_original_theme_and_role_tier",
            "market_benchmark": "000001.SH", "theme_benchmark": "daily_equal_weight_excluding_self_min_5",
            "comparison": comparisons, "phase_groups": dict(phases), "uncovered_reports": skipped}
