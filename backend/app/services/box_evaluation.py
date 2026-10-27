"""显式本地快照上的离线评估；不导入应用、不读取行情仓库、不接入实时状态。"""
from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from dataclasses import asdict
from datetime import date
from pathlib import Path
from statistics import median
from typing import Literal, Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.indicators.box_evaluation import evaluate_structures
from app.indicators.box_oscillation import PERIOD_CONFIG, compute_box_oscillation


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Interval(_Model):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("区间终点不得早于起点")
        return self


class Reference(Interval):
    id: str = Field(min_length=1)
    lower: float = Field(gt=0)
    upper: float = Field(gt=0)
    earliest_confirmable_date: date | None = None
    note: str = ""

    @model_validator(mode="after")
    def valid_reference(self) -> Self:
        if self.upper <= self.lower:
            raise ValueError("参考结构必须 lower < upper")
        return self


class Negative(Interval):
    id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Bar(_Model):
    date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_prices(self) -> Self:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC 价格关系不合法")
        return self


class EvaluationCase(_Model):
    id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    asset_type: Literal["stock", "etf", "index"]
    period: Literal["daily", "weekly", "monthly"]
    price_basis: Literal["qfq", "index"]
    bars_are_completed: Literal[True]
    label_source: Literal["human", "synthetic", "review_candidate"]
    annotation_status: Literal["pending", "complete"]
    annotator: str | None = None
    annotation_note: str = ""
    evaluation_interval: Interval | None = None
    bars: list[Bar] = Field(min_length=1)
    references: list[Reference] = Field(default_factory=list)
    negative_intervals: list[Negative] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        dates = [bar.date for bar in self.bars]
        if dates != sorted(set(dates)):
            raise ValueError("快照日期必须严格递增且不重复；不得自动排序掩盖错误")
        if self.price_basis != ("index" if self.asset_type == "index" else "qfq"):
            raise ValueError("个股/ETF 使用前复权价，指数使用自身指数价格基准")
        if self.annotation_status == "complete":
            if self.evaluation_interval is None:
                raise ValueError("完整标注必须声明完整复核区间")
            if self.label_source == "review_candidate":
                raise ValueError("引擎候选不能充当完整参考集")
        labels = [*self.references, *self.negative_intervals]
        if len({item.id for item in labels}) != len(labels):
            raise ValueError("同一 case 的正负样 id 必须唯一")
        for item in [*labels, *([self.evaluation_interval] if self.evaluation_interval else [])]:
            if item.start_date not in dates or item.end_date not in dates:
                raise ValueError("标注端点必须对应有效 K 线")
        if self.evaluation_interval:
            for item in labels:
                if not (self.evaluation_interval.start_date <= item.start_date
                        <= item.end_date <= self.evaluation_interval.end_date):
                    raise ValueError("正负样标签必须位于完整复核区间内")
        for negative in self.negative_intervals:
            if any(ref.start_date <= negative.end_date and negative.start_date <= ref.end_date
                   for ref in self.references):
                raise ValueError("人工负样不得与正样结构的时间范围重叠")
        return self


class Split(Interval):
    name: Literal["calibration", "validation"]


class SplitPlan(_Model):
    gap_bars: int = Field(default=5, ge=0)
    splits: list[Split] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def chronological(self) -> Self:
        if [part.name for part in self.splits] != ["calibration", "validation"]:
            raise ValueError("必须依次声明校准、验证两个时间集合")
        if any(first.end_date >= second.start_date
               for first, second in zip(self.splits, self.splits[1:], strict=False)):
            raise ValueError("切分必须严格按时间排序且不得重叠")
        return self


class EvaluationDataset(_Model):
    schema_version: Literal[1]
    dataset_id: str = Field(min_length=1)
    description: str
    split_plan: SplitPlan
    match_config: dict = Field(default_factory=dict)
    cases: list[EvaluationCase]

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("case id 必须唯一")
        # 在没有标签时也校验配置，避免错误规则到正式评估才暴露。
        evaluate_structures([], [], [], [], self.match_config)
        return self


def _frame(case: EvaluationCase) -> pl.DataFrame:
    return pl.DataFrame([bar.model_dump() for bar in case.bars])


def _overlaps(first: dict, second: dict) -> bool:
    return first["start_date"] <= second["end_date"] and second["start_date"] <= first["end_date"]


def _contains(outer: dict, inner: dict) -> bool:
    return outer["start_date"] <= inner["start_date"] <= inner["end_date"] <= outer["end_date"]


def _split_bounds(dates: list[str], plan: SplitPlan) -> dict[str, dict | None]:
    """围绕切分位置按当前周期实际 K 根数隔离；无边界数据不虚构 K。"""
    bounds = {}
    cuts = []
    gap = plan.gap_bars
    for first, second in zip(plan.splits, plan.splits[1:], strict=False):
        left = bisect_right(dates, first.end_date.isoformat())
        right = bisect_left(dates, second.start_date.isoformat())
        if 0 < left < len(dates) or 0 < right < len(dates):
            cuts.append((max(0, left - gap), min(len(dates), right + gap)))
    for part in plan.splits:
        start = bisect_left(dates, part.start_date.isoformat())
        stop = bisect_right(dates, part.end_date.isoformat())
        while start < stop and any(left <= start < right for left, right in cuts):
            start += 1
        while start < stop and any(left <= stop - 1 < right for left, right in cuts):
            stop -= 1
        bounds[part.name] = ({"start_date": dates[start], "end_date": dates[stop - 1]}
                             if start < stop else None)
    return bounds


def _reference_groups(references: list[dict]) -> list[list[dict]]:
    """时间重叠的正样按连通分量处理，避免嵌套结构分别落入两侧。"""
    groups: list[list[dict]] = []
    for reference in sorted(references, key=lambda value: (value["start_date"], value["end_date"])):
        if groups and reference["start_date"] <= max(item["end_date"] for item in groups[-1]):
            groups[-1].append(reference)
        else:
            groups.append([reference])
    return groups


def _purged_references(dataset: EvaluationDataset) -> set[tuple[str, str]]:
    """同标的跨周期结构也按时间连通组排除，避免换个周期跨集合泄漏。"""
    by_asset: dict[tuple[str, str, str], list[dict]] = {}
    for case in dataset.cases:
        if case.annotation_status != "complete":
            continue
        dates = [bar.date.isoformat() for bar in case.bars]
        bounds = _split_bounds(dates, dataset.split_plan)
        for reference in case.references:
            item = reference.model_dump(mode="json")
            owner = next((name for name, bound in bounds.items() if bound and _contains(bound, item)), None)
            by_asset.setdefault((case.asset_type, case.symbol, case.label_source), []).append(
                {**item, "case_id": case.id, "owner": owner})
    purged = set()
    for references in by_asset.values():
        for group in _reference_groups(references):
            owners = {item["owner"] for item in group}
            if None in owners or len(owners) != 1:
                purged.update((item["case_id"], item["id"]) for item in group)
    return purged


def _aggregate(reports: list[dict]) -> dict:
    """只合并同一标签来源、同一切分，保留各分母，不平均百分率。"""
    totals: dict[str, int] = {}
    for report in reports:
        for key, value in report["structure"]["counts"].items():
            totals[key] = totals.get(key, 0) + value
    tp, fp, fn = (totals.get(key, 0) for key in ("true_positive", "false_positive", "false_negative"))

    def ratio(num: int, den: int) -> float | None:
        return num / den if den else None

    matches = [item for report in reports for item in report["structure"]["matches"]]

    def distribution(values: list[float]) -> dict:
        return {"count": len(values), "mean": sum(values) / len(values) if values else None,
                "median": median(values) if values else None}

    return {
        "case_splits": len(reports), "counts": totals,
        "rates": {"precision": ratio(tp, tp + fp), "false_discovery_rate": ratio(fp, tp + fp),
                  "recall": ratio(tp, tp + fn), "false_negative_rate": ratio(fn, tp + fn),
                  "negative_segment_false_positive_rate": ratio(
                      totals.get("negative_segments_with_detection", 0), totals.get("negative_segments", 0))},
        "match_quality": {key: distribution([item[key] for item in matches])
                          for key in ("time_iou", "boundary_error")},
    }


def evaluate_dataset(dataset: EvaluationDataset) -> dict:
    reports, skipped = [], []
    purged_ids = _purged_references(dataset)
    for case in dataset.cases:
        if case.annotation_status != "complete":
            skipped.append({"case_id": case.id, "reason": "awaiting_annotations"})
            continue
        frame = _frame(case)
        dates = [bar.date.isoformat() for bar in case.bars]
        bounds = _split_bounds(dates, dataset.split_plan)
        coverage = case.evaluation_interval.model_dump(mode="json")
        references = [item.model_dump(mode="json") for item in case.references]
        negatives = [item.model_dump(mode="json") for item in case.negative_intervals]
        purged = [item for item in references if (case.id, item["id"]) in purged_ids]
        for part in dataset.split_plan.splits:
            name, bound = part.name, bounds[part.name]
            if bound is None or not _overlaps(bound, coverage):
                continue
            core = {"start_date": max(bound["start_date"], coverage["start_date"]),
                    "end_date": min(bound["end_date"], coverage["end_date"])}
            prefix = frame.head(bisect_right(dates, core["end_date"]))
            # 每个集合按其截止日独立运行；绝不把完整样本最终结构回灌过去。
            all_boxes = compute_box_oscillation(prefix, case.period)
            eligible = [box for box in all_boxes if _contains(core, box)
                        and not any(_overlaps(box, item) for item in purged)]
            excluded = [{"id": box["id"], "reason": "outside_reviewed_core_or_cross_boundary_structure"}
                        for box in all_boxes if _overlaps(box, core) and box not in eligible]
            refs = [item for item in references if item not in purged and _contains(core, item)]
            negs = [item for item in negatives if _contains(core, item)
                    and not any(_overlaps(item, excluded_ref) for excluded_ref in purged)]
            structure = evaluate_structures(eligible, refs, negs, dates, dataset.match_config)
            reports.append({
                "case_id": case.id, "symbol": case.symbol, "asset_type": case.asset_type,
                "period": case.period, "label_source": case.label_source, "split": name,
                "reviewed_core": core, "recognition_as_of": str(prefix["date"][-1]),
                "structure": structure, "excluded_detections": excluded,
                "purged_reference_ids": [item["id"] for item in purged],
                "excluded_negative_ids": [item["id"] for item in negatives if item not in negs],
            })
    summary = {}
    for source in ("human", "synthetic"):
        summary[source] = {
            part.name: {period: _aggregate([report for report in reports
                                           if report["label_source"] == source
                                           and report["split"] == part.name
                                           and report["period"] == period])
                        for period in PERIOD_CONFIG}
            for part in dataset.split_plan.splits
        }
    has_human = any(report["label_source"] == "human" for report in reports)
    return {
        "schema_version": 1, "dataset_id": dataset.dataset_id,
        "calibration_status": "human_evaluation_available" if has_human else "awaiting_human_annotations",
        "quality_conclusion": "not_established", "engine_config": {key: asdict(value) for key, value in PERIOD_CONFIG.items()},
        "split_plan": dataset.split_plan.model_dump(mode="json"),
        "summary": summary,
        "match_config": evaluate_structures([], [], [], [], dataset.match_config)["config"],
        "case_reports": reports, "skipped_cases": skipped,
        "limitations": ["合成结果与人工结果分开；少量人工样本用于发现明显结构偏差",
                        "统计结果供人工检查边界，不产生仓位建议"],
    }


def _differences(expected: object, actual: object, path: str = "$") -> list[str]:
    if type(expected) is not type(actual):
        return [path]
    if isinstance(expected, dict):
        return [path + "." + key for key in expected.keys() ^ actual.keys()] + [
            child for key in expected.keys() & actual.keys()
            for child in _differences(expected[key], actual[key], path + "." + key)]
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [path + ".length"]
        return [child for index, (first, second) in enumerate(zip(expected, actual, strict=True))
                for child in _differences(first, second, f"{path}[{index}]")]
    return [] if expected == actual else [path]


def verify_regression_baseline(path: Path) -> dict:
    suite = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(suite, dict) or suite.get("schema_version") != 1 or suite.get("source_type") != "synthetic"
            or suite.get("purpose") != "engine_regression_only"):
        raise ValueError("不是受支持的合成工程回归基线")
    if (not isinstance(suite.get("default_configs"), dict)
            or not isinstance(suite.get("cases"), list) or not suite["cases"]):
        raise ValueError("回归基线必须包含冻结默认参数和非空样本")
    for case in suite["cases"]:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or case.get("period") not in PERIOD_CONFIG
                or not isinstance(case.get("bars"), list) or not case["bars"]
                or not isinstance(case.get("expected_boxes"), list)):
            raise ValueError("回归样本必须包含 id、周期、非空 K 线和冻结输出")
    if len({case["id"] for case in suite["cases"]}) != len(suite["cases"]):
        raise ValueError("回归样本 id 必须唯一")
    config = {key: asdict(value) for key, value in PERIOD_CONFIG.items()}
    config_diff = sorted(_differences(suite["default_configs"], config))
    cases = []
    for case in suite["cases"]:
        actual = compute_box_oscillation(pl.DataFrame(case["bars"]), case["period"])
        differences = sorted(_differences(case["expected_boxes"], actual))
        cases.append({"id": case["id"], "period": case["period"], "passed": not differences,
                      "bar_count": len(case["bars"]), "expected_box_count": len(case["expected_boxes"]),
                      "actual_box_count": len(actual), "different_fields": differences})
    return {"schema_version": 1, "purpose": "engine_regression_only", "source_type": "synthetic",
            "calibration_status": "awaiting_human_annotations", "engine_config": config,
            "default_config_differences": config_diff, "cases": cases,
            "passed": not config_diff and all(case["passed"] for case in cases),
            "quality_conclusion": "not_established"}
