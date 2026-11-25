"""波次研究只引用已归档的当时市场与题材快照。"""
from __future__ import annotations

from bisect import bisect_left
from functools import lru_cache

from app.market_time import cn_now
from app.services.evening_report_archive import retained_observation_rows


def _snapshot(report, snapshot_id, observations=None):
    if snapshot_id == "market":
        market = report.get("market") or {}
        sentiment = (market.get("sentiment") or {}).get("classification") or market.get("classification")
        return market if sentiment else None
    if snapshot_id.startswith("audit/themes/"):
        theme_id = snapshot_id.removeprefix("audit/themes/")
        return next((theme for theme in report.get("audit", {}).get("themes", [])
                     if theme["id"] == theme_id and theme.get("selected_theme")), None)
    if snapshot_id.startswith("observation/"):
        symbol, _, variable = snapshot_id.removeprefix("observation/").partition("/")
        observation = observations.get(symbol) if observations is not None else next(
            (row.get("observation") for row in report.get("records", []) if row["symbol"] == symbol), None)
        if variable == "primary_theme":
            theme = (observation or {}).get("primary_theme") or {}
            return theme if theme.get("id") else None
        return (observation or {}).get("variables", {}).get(variable)
    return None


def reference_status(ref, report, checked_at, *, observations=None):
    expiry = ref.get("retention_until")
    if expiry is not None and checked_at >= expiry:
        return "expired", "超过源快照保留期"
    if not report:
        return "missing", "当时无可核验的共享报告"
    if report["version"] != ref["version"] or report["trade_date"] != ref["as_of"]:
        return "missing", "源快照身份、日期或版本不一致"
    if not report.get("cutoff") or not ref.get("available_by") or report["cutoff"] > ref["available_by"]:
        return "missing", "源事实获知时间晚于引用截止"
    if _snapshot(report, ref["source_snapshot_id"], observations) is None:
        return "missing", "归档未记录该快照"
    return "valid", None


def add_context_refs(report, store):
    day = report["trade_date"]
    calendar = report["provenance"].get("calendar", {}).get("days", [])
    if not calendar or not report.get("cutoff"):
        return

    @lru_cache
    def source(target):
        return report if target == day else store.referenced_report(target)

    @lru_cache
    def observations_for(target):
        archive = source(target) or {}
        rows = (retained_observation_rows(archive) if archive is report
                else archive.get("records", []))
        return {row["symbol"]: row.get("observation") or {} for row in rows}

    def references(target, theme, *, symbol=None, available_by=None):
        archive = source(target)
        version = archive["version"] if archive else None
        available_by = available_by or (archive.get("cutoff") if archive else None)
        targets = ["market"]
        if theme:
            targets.append("audit/themes/" + theme)
        if symbol:
            field = "relative_lead_score" if theme else "primary_theme"
            targets.append(f"observation/{symbol}/{field}")
        refs = []
        for key in targets:
            ref = {"source_report_id": version, "source_snapshot_id": key, "as_of": target,
                   "version": version, "retention_until": None, "available_by": available_by,
                   "retention": "long_term"}
            state, reason = reference_status(ref, archive, report["generated_at"],
                                             observations=observations_for(target) if symbol else None)
            refs.append(ref | {"reference_status": state, "reason": reason})
        return refs

    observations = [row for row in report.get("calculations", [])
                    if row["observation"]["decision"]["comparison_eligible"] and row["observation"].get("wave")]
    for row in observations:
        obs = row["observation"]
        wave = obs["wave"]
        theme = obs.get("primary_theme", {}).get("id")
        refs = {"recommendation": references(day, theme, symbol=row["symbol"])}
        high = wave.get("first_wave_high")
        low = wave.get("first_pullback_low")
        if high:
            start = high["extreme_date"]
            end = low["extreme_date"] if low else day
            dates = calendar[bisect_left(calendar, start) + 1:bisect_left(calendar, end) + 1]
            refs["pullback"] = {"start_exclusive": start, "end_inclusive": end,
                               "anchor_status": "confirmed" if low else "provisional",
                               "references": [ref for target in dates for ref in references(target, theme)]}
        wave["context_refs"] = refs
    starts = []
    for event in report.get("wave_starts", []):
        symbol = event["security_id"]
        refs = {}
        for key, target in (("evidence", event["evidence_date"]), ("known", event["known_at"][:10])):
            observation = observations_for(target).get(symbol, {})
            theme = (observation.get("primary_theme") or {}).get("id")
            refs[key] = references(target, theme, symbol=symbol, available_by=event["known_at"])
        starts.append({**event, "context_refs": refs})
    report["wave_starts"] = starts


def _checked_reference(ref, report, checked_at):
    status, reason = reference_status(ref, report, checked_at)
    return ref | {"current_reference_status": status, "current_reason": reason, "checked_at": checked_at}


def study_context(refs, source, checked_at):
    def project(ref):
        report = source(ref["as_of"], ref["source_report_id"])
        checked = _checked_reference(ref, report, checked_at)
        if checked["current_reference_status"] != "valid":
            return checked
        key = ref["source_snapshot_id"]
        snapshot = _snapshot(report, key)
        if key == "market":
            classification = (snapshot.get("sentiment") or {}).get("classification") or snapshot.get("classification", {})
            values = {field: classification.get(field) for field in
                      ("heat_level", "quality_level", "sentiment_state", "version", "universe")}
            values["phase"] = snapshot.get("phase") or classification.get("phase")
        elif key.startswith("audit/themes/"):
            values = {field: snapshot.get(field) for field in
                      ("theme_strength_5d", "rank", "theme_total_count", "ranking_scope", "ranking_rule_version")}
            values["theme_id"] = snapshot["id"]
        else:
            values = snapshot
        return checked | {"values": values}

    return {when: [project(ref) for ref in entries] for when, entries in refs.items()}


def project_context_refs(view, store, original=None):
    """Read-time status is a projection. Archive and personal snapshot remain immutable."""
    checked_at = cn_now().isoformat()

    @lru_cache
    def source(day, version):
        if original and version == original["version"]:
            return original
        return store.referenced_report(day, version) if version else None

    def project(ref):
        report = source(ref["as_of"], ref["source_report_id"])
        return _checked_reference(ref, report, checked_at)

    for row in view.get("records", []):
        wave = (row.get("observation") or {}).get("wave")
        if not wave or not wave.get("context_refs"):
            continue
        refs = wave["context_refs"]
        updated = {**refs, "recommendation": [project(ref) for ref in refs["recommendation"]]}
        if "pullback" in refs:
            interval = refs["pullback"]
            updated["pullback"] = {**interval, "references": [project(ref) for ref in interval["references"]]}
        row["observation"] = {**row["observation"], "wave": {**wave, "context_refs": updated}}
    return view
