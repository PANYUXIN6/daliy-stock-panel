"""离线箱体评估纯函数；参考标签须独立标注，不能由待测引擎生成。

所有时间距离按调用方提供的有效、已完成 K 线序列计量。匹配阈值仅为
工具的试验默认值，不是已经校准的指标参数。
"""
from __future__ import annotations

import heapq
import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class MatchConfig:
    min_time_iou: float = 0.5
    max_boundary_error: float = 0.2
    max_start_error_bars: int = 5
    max_end_error_bars: int = 5


@dataclass(frozen=True)
class _Interval:
    id: str
    start: int
    end: int
    lower: float
    upper: float
    original: dict

    @property
    def width(self) -> float:
        return self.upper - self.lower

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def _date_string(value: Any) -> str:
    return value.isoformat()[:10] if hasattr(value, "isoformat") else str(value)


def _date_indices(dates: list[str]) -> dict[str, int]:
    if dates != sorted(set(dates)):
        raise ValueError("评估日期必须严格递增且不重复")
    return {value: index for index, value in enumerate(dates)}


def _index(value: Any, indices: dict[str, int], field: str) -> int:
    try:
        return indices[_date_string(value)]
    except KeyError as exc:
        raise ValueError(f"{field} 不在有效K线日期序列中: {value}") from exc


def _positive_price(value: Any, field: str) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有限正数") from exc
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"{field} 必须是有限正数")
    return price


def _intervals(values: list[dict], indices: dict[str, int]) -> list[_Interval]:
    result = []
    seen = set()
    for value in values:
        identifier = value.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("每组结构的 id 必须为非空且唯一的字符串")
        seen.add(identifier)
        start = _index(value.get("start_date"), indices, "start_date")
        end = _index(value.get("end_date"), indices, "end_date")
        lower = _positive_price(value.get("lower"), "lower")
        upper = _positive_price(value.get("upper"), "upper")
        if end < start or upper <= lower:
            raise ValueError("结构必须满足 start_date <= end_date 且 lower < upper")
        if (value.get("confirmed_date") is not None
                and _index(value["confirmed_date"], indices, "confirmed_date") < start):
            raise ValueError("confirmed_date 不得早于结构起点")
        result.append(_Interval(identifier, start, end, lower, upper, value))
    return sorted(result, key=lambda item: item.id)


def _config(config: dict | None) -> MatchConfig:
    try:
        result = MatchConfig(**(config or {}))
    except TypeError as exc:
        raise ValueError("匹配配置包含不支持的参数") from exc
    for name in ("min_time_iou",):
        value = getattr(result, name)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} 必须在 [0, 1] 内")
    for name in ("max_boundary_error",):
        value = getattr(result, name)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} 必须为有限非负数")
    for name in ("max_start_error_bars", "max_end_error_bars"):
        value = getattr(result, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} 必须为非负整数")
    return result


def _overlap(first: _Interval, second: _Interval) -> int:
    return max(0, min(first.end, second.end) - max(first.start, second.start) + 1)


def _iou(first: _Interval, second: _Interval) -> float:
    intersection = _overlap(first, second)
    return intersection / (first.length + second.length - intersection)


def _pair(detection: _Interval, reference: _Interval) -> dict:
    iou = _iou(detection, reference)
    error = max(abs(detection.lower - reference.lower),
                abs(detection.upper - reference.upper)) / reference.width
    start_error = abs(detection.start - reference.start)
    end_error = abs(detection.end - reference.end)
    return {
        "detection_id": detection.id, "reference_id": reference.id,
        "time_iou": iou, "boundary_error": error,
        "start_error_bars": start_error, "end_error_bars": end_error,
        "cost": 1 - iou + error + (start_error + end_error) / reference.length,
    }


@dataclass
class _Edge:
    to: int
    reverse: int
    capacity: int
    cost: float


def _assignment(detections: list[_Interval], references: list[_Interval],
                pairs: dict[tuple[int, int], dict]) -> list[tuple[int, int]]:
    """单位容量最小费用流：先最大匹配数，再最小总 cost；id 排序保证可复现。"""
    source = len(detections) + len(references)
    sink = source + 1
    graph: list[list[_Edge]] = [[] for _ in range(sink + 1)]

    def edge(start: int, end: int, cost: float) -> _Edge:
        forward = _Edge(end, len(graph[end]), 1, cost)
        backward = _Edge(start, len(graph[start]), 0, -cost)
        graph[start].append(forward)
        graph[end].append(backward)
        return forward

    for index in range(len(detections)):
        edge(source, index, 0)
    for index in range(len(references)):
        edge(len(detections) + index, sink, 0)
    pair_edges = {key: edge(key[0], len(detections) + key[1], value["cost"])
                  for key, value in pairs.items()}
    potentials = [0.0] * len(graph)
    while True:
        distances = [math.inf] * len(graph)
        distances[source] = 0.0
        previous: list[tuple[int, int] | None] = [None] * len(graph)
        queue = [(0.0, source)]
        while queue:
            distance, start = heapq.heappop(queue)
            if distance > distances[start] + 1e-12:
                continue
            for position, item in enumerate(graph[start]):
                if not item.capacity:
                    continue
                next_distance = distance + item.cost + potentials[start] - potentials[item.to]
                if next_distance < distances[item.to] - 1e-12:
                    distances[item.to] = next_distance
                    previous[item.to] = (start, position)
                    heapq.heappush(queue, (next_distance, item.to))
        if previous[sink] is None:
            break
        for index, distance in enumerate(distances):
            if math.isfinite(distance):
                potentials[index] += distance
        node = sink
        while node != source:
            start, position = previous[node]  # type: ignore[misc]
            item = graph[start][position]
            item.capacity -= 1
            graph[node][item.reverse].capacity += 1
            node = start
    return [key for key, item in pair_edges.items() if not item.capacity]


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def evaluate_structures(
    detections: list[dict], references: list[dict], negative_intervals: list[dict],
    dates: list[str], match_config: dict | None = None,
) -> dict:
    """评估同一标的、资产类型及周期；只在完整标注的评估区间调用。

    结构端点必须属于 dates。预测 confirmed_date 非空即为已确认结构，
    包括历史 broken；其余单列候选。参考与预测通过双边界、有效 bar 时间
    Jaccard、起止误差四条件后作最优一对一匹配。
    负样 FPR 的分母为人工明确标注的负样区间数量；预测已确认区间与某负样
    共享至少一根有效 bar 即命中该区间，不按候选数量重复计数。
    """
    indices = _date_indices(dates)
    config = _config(match_config)
    all_detections = _intervals(detections, indices)
    confirmed = [item for item in all_detections if item.original.get("confirmed_date") is not None]
    candidates = [item.id for item in all_detections if item.original.get("confirmed_date") is None]
    refs = _intervals(references, indices)
    eligible = {}
    for det_index, detection in enumerate(confirmed):
        for ref_index, reference in enumerate(refs):
            pair = _pair(detection, reference)
            if (pair["time_iou"] >= config.min_time_iou
                    and pair["boundary_error"] <= config.max_boundary_error
                    and pair["start_error_bars"] <= config.max_start_error_bars
                    and pair["end_error_bars"] <= config.max_end_error_bars):
                eligible[det_index, ref_index] = pair
    assignment = _assignment(confirmed, refs, eligible)
    matches = [eligible[key] for key in assignment]
    matched_detections = {item[0] for item in assignment}
    matched_references = {item[1] for item in assignment}
    negatives = []
    negative_ids = set()
    for value in negative_intervals:
        identifier = value.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in negative_ids:
            raise ValueError("负样区间 id 必须为非空且唯一的字符串")
        negative_ids.add(identifier)
        start = _index(value.get("start_date"), indices, "negative.start_date")
        end = _index(value.get("end_date"), indices, "negative.end_date")
        if start > end:
            raise ValueError("负样区间起点不得晚于终点")
        negatives.append({"id": identifier, "detection_ids": [
            item.id for item in confirmed if item.start <= end and item.end >= start]})
    negatives.sort(key=lambda item: item["id"])
    tp = len(assignment)
    fp, fn = len(confirmed) - tp, len(refs) - tp
    negative_hits = sum(bool(item["detection_ids"]) for item in negatives)
    return {
        "config": asdict(config),
        "counts": {
            "detections_total": len(all_detections), "confirmed_detections": len(confirmed),
            "candidates": len(candidates), "references": len(refs),
            "true_positive": tp, "false_positive": fp, "false_negative": fn,
            "negative_segments": len(negatives), "negative_segments_with_detection": negative_hits,
        },
        "rates": {"precision": _ratio(tp, tp + fp),
                  "false_discovery_rate": _ratio(fp, tp + fp), "recall": _ratio(tp, tp + fn),
                  "false_negative_rate": _ratio(fn, tp + fn),
                  "negative_segment_false_positive_rate": _ratio(negative_hits, len(negatives))},
        "matches": sorted(matches, key=lambda item: (item["reference_id"], item["detection_id"])),
        "unmatched_detection_ids": [item.id for index, item in enumerate(confirmed)
                                    if index not in matched_detections],
        "unmatched_reference_ids": [item.id for index, item in enumerate(refs)
                                    if index not in matched_references],
        "candidate_ids": candidates, "negative_segments": negatives,
        "definitions": {
            "assignment": "maximum_cardinality_then_minimum_total_cost",
            "time_iou": "inclusive_valid_bar_intersection_over_union",
            "boundary_error": "max_absolute_boundary_error_divided_by_reference_width",
            "cost": "1-time_iou+boundary_error+(start_error_bars+end_error_bars)/reference_bars",
            "negative_fpr": "negative_segments_with_any_confirmed_interval_overlap/negative_segments",
        },
    }

