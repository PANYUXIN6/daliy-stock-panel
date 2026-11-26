"""从盘后正式输入生成一份按交易日定稿的观察名单。"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import polars as pl

from app.services.evening_report_archive import SCHEMA_VERSION
from app.services.evening_report_dip import VERSION as DIP_VERSION
from app.services.evening_report_observation import apply_observation, variable_definitions
from app.services.evening_report_rules import RULE_VERSION, SELECTION_POLICY_VERSION
from app.services.evening_report_wave import PARAMETERS
from app.services.evening_report_wave import VERSION as WAVE_VERSION

SCOPE_VERSION = "main-chinext-nonst-v1/evening-classification-v2"


@dataclass
class EveningInputs:
    trade_date: str
    calendar: list[str]
    prices: pl.DataFrame
    metadata: pl.DataFrame
    memberships: pl.DataFrame
    next_trade_date: str | None = None
    cutoff: str = ""
    market: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    abnormal_benchmarks: pl.DataFrame = field(default_factory=pl.DataFrame)


def build_report(inputs: EveningInputs, previous: dict | None = None, cancel_check: Callable | None = None) -> dict:
    """Only the previous market day's final can supply recent true-leader dates."""
    cancel = cancel_check or (lambda: None)
    cancel()
    day = inputs.trade_date
    calendar = sorted({str(d) for d in inputs.calendar if str(d) <= day})[-250:]
    if not calendar or calendar[-1] != day:
        raise ValueError("晚报目标日必须在交易日历内")
    if len(calendar) < 5:
        raise ValueError("题材强度所需市场交易日历不足5日")
    prior_roles = previous.get("role_state", {}) if previous and previous.get("trade_date") == calendar[-2] else {}
    result = apply_observation(inputs, calendar, prior_roles, cancel)
    rows = result["records"]
    selected = result["selected"]
    candidates = [r for r in rows if r["observation"].get("competed_quota_type")]
    themes = result["themes"]
    reason = [] if selected else ["入围题材没有满足名额条件的股票" if themes else "没有强度为正的可入围题材"]
    stats = {
        "scope_count": sum(r["observation"]["eligibility"]["inputs"]["研究范围"] is True for r in rows),
        "eligible_count": sum(r["observation"]["eligibility"]["value"] is True for r in rows),
        "report_count": len(candidates), "selected_count": len(selected), "theme_count": len(themes),
        "selection_eligible_before_quota": len(candidates), "selection_quota_excluded": len(candidates) - len(selected),
        "role_counts": dict(Counter(r["observation"]["role"] for r in rows)),
        "dip_counts": dict(Counter(r["observation"]["dip"]["status"] for r in candidates if r["selected"])),
        "zero_selection_reasons": reason,
    }
    return {
        "trade_date": day, "as_of": day, "next_trade_date": inputs.next_trade_date, "cutoff": inputs.cutoff,
        "rule_version": RULE_VERSION, "selection_policy_version": SELECTION_POLICY_VERSION,
        "storage_version": SCHEMA_VERSION, "scope_version": SCOPE_VERSION, "dip_rule_version": DIP_VERSION,
        "wave_rule": {"version": WAVE_VERSION, "window_sessions": 250, "parameters": PARAMETERS},
        "summary": f"{len(themes)}个重点题材，今日入选观察{len(selected)}只。",
        "market": inputs.market, "provenance": inputs.provenance, "statistics": stats,
        "selected": selected, "records": candidates, "calculations": rows,
        "role_state": result["role_state"], "audit": result["audit"],
        "observation_themes": themes, "wave_starts": result["wave_starts"],
        "sectors": [{"id": t["id"], "name": t["name"], "observation": t} for t in themes],
        "variable_definitions": variable_definitions(result["audit"]["feature_keys"], inputs),
    }
